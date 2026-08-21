from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from okx_demo_lab.ml.strategy import canonical_json, sha256_hex


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPLAY_ROOT = PROJECT_ROOT / ".research-data" / "replays"
FIVE_MINUTES_MS = 300_000
EXPECTED_HOLDING_BARS = 12
EXPECTED_REPLAY_BARS = 30 * 288
V6_REPORT_SCHEMA = "moheng.historical-replay-report.v4"
RETIRED_REPORT_SCHEMAS = frozenset(
    {
        "moheng.historical-replay-report.v1",
        "moheng.historical-replay-report.v2",
        "moheng.historical-replay-report.v3",
    }
)
MAX_REPLAY_JSON_BYTES = 32 * 1024 * 1024
V6_CHECKPOINT_VALUATION_BASIS = "current_bar_open_at_checkpoint_boundary"
EXPECTED_STARTING_CASH = 10_000.0
EXPECTED_ALLOCATION_FRACTION = 0.25
EXPECTED_FEE_BPS_PER_SIDE = 8.0
EXPECTED_STANDARD_SLIPPAGE_BPS_PER_SIDE = 4.0
EXPECTED_STRESS_SLIPPAGE_BPS_PER_SIDE = 16.0
EXPECTED_MAX_QUOTE_VOLUME_PARTICIPATION = 0.005
EXPECTED_MINIMUM_NOTIONAL = 10.0
EXPECTED_CHECKPOINT_STRIDE_BARS = 288
EXPECTED_EDGE_BUFFER_BPS = 72.0
EXPECTED_MIN_ENTRY_SPACING_BARS = 12
_BROKER_KEYS = frozenset(
    {
        "allocationFraction",
        "breakEvenGrossReturnBps",
        "capacityHandling",
        "checkpointStrideBars",
        "executionLabelHorizonBars",
        "feeBpsPerSide",
        "holdingPeriodBars",
        "latencyBars",
        "maxQuoteVolumeParticipation",
        "minimumNotional",
        "roundTripCostBps",
        "slippageBpsPerSide",
        "startingCash",
    }
)
_POLICY_KEYS = frozenset(
    {"edgeBufferBps", "minEntrySpacingBars", "requiredGrossReturnBps"}
)
_LEAKAGE_KEYS = frozenset(
    {
        "causalEpisodeBinding",
        "checkpointValuationBasis",
        "decisionToFillBars",
        "executionCoordinate",
        "nextDecisionRowFill",
        "predictionRowsAvailableBeforeDecision",
        "sameDecisionRowFill",
    }
)
CORE_DIGEST_PROJECTION_VERSION = "moheng.historical-replay-core.v1"
_CORE_RUNTIME_KEYS = frozenset(
    {"startedAt", "completedAt", "timing", "replayId", "reportSha256"}
)


class ReplayVerificationError(RuntimeError):
    pass


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ReplayVerificationError(message)


def _timestamp_ms(value: object, name: str) -> int:
    _require(isinstance(value, str), f"{name} must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReplayVerificationError(f"{name} must be an ISO timestamp") from exc
    _require(parsed.tzinfo is not None, f"{name} must include a timezone")
    return round(parsed.astimezone(timezone.utc).timestamp() * 1_000)


def _number(value: object, name: str) -> float:
    _require(
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value)),
        f"{name} must be finite",
    )
    return float(value)


def _nonnegative_integer(value: object, name: str) -> int:
    _require(
        isinstance(value, int) and not isinstance(value, bool) and value >= 0,
        f"{name} must be a nonnegative integer",
    )
    return value


def _require_close(actual: float, expected: float, message: str) -> None:
    _require(
        math.isclose(actual, expected, rel_tol=1e-10, abs_tol=1e-7),
        message,
    )


def _break_even_gross_return_bps(
    *, fee_bps_per_side: float, slippage_bps_per_side: float
) -> float:
    fee = fee_bps_per_side / 10_000.0
    slippage = slippage_bps_per_side / 10_000.0
    multiplier = ((1.0 + slippage) * (1.0 + fee)) / (
        (1.0 - slippage) * (1.0 - fee)
    )
    return (multiplier - 1.0) * 10_000.0


def _failure_list(value: object, name: str) -> list[str]:
    _require(
        isinstance(value, list)
        and all(isinstance(item, str) and item for item in value)
        and len(value) == len(set(value)),
        f"{name} must be a duplicate-free string list",
    )
    return list(value)


def _development_failures(ledger: dict[str, Any], *, trade_count: int) -> list[str]:
    failures: list[str] = []
    if trade_count < 20:
        failures.append("trades_insufficient")
    if _number(ledger.get("profitableTradeRate"), "ordinary.profitableTradeRate") < 0.50:
        failures.append("profitable_trade_rate_below_gate")
    if _number(ledger.get("grossPnlReturn"), "ordinary.grossPnlReturn") < 0.0:
        failures.append("gross_return_below_gate")
    if _number(ledger.get("netReturn"), "ordinary.netReturn") < 0.005:
        failures.append("net_return_below_gate")
    if _number(ledger.get("maxDrawdown"), "ordinary.maxDrawdown") > 0.10:
        failures.append("drawdown_above_gate")
    if _number(ledger.get("tradesPerDay"), "ordinary.tradesPerDay") > 3.0:
        failures.append("turnover_above_gate")
    submitted = _nonnegative_integer(
        ledger.get("ordersSubmitted"), "ordinary.ordersSubmitted"
    )
    rejected = _nonnegative_integer(
        ledger.get("ordersRejected"), "ordinary.ordersRejected"
    )
    if submitted and rejected / submitted > 0.05:
        failures.append("fill_rejection_rate_above_gate")
    return failures


def _stress_failures(ledger: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    if _number(ledger.get("grossPnlReturn"), "stress48Bps.grossPnlReturn") < 0.0:
        failures.append("stress_gross_return_below_zero")
    if _number(ledger.get("netReturn"), "stress48Bps.netReturn") < 0.0:
        failures.append("stress_net_return_below_zero")
    if _number(ledger.get("maxDrawdown"), "stress48Bps.maxDrawdown") > 0.15:
        failures.append("stress_drawdown_above_gate")
    return failures


def _execution_slice_failures(
    ordinary: dict[str, Any], stress: dict[str, Any], *, trade_count: int
) -> list[str]:
    failures: list[str] = []
    if trade_count < 20:
        failures.append("execution_slice_trades_insufficient")
    if _number(ordinary.get("netReturn"), "BTC ordinary.netReturn") <= 0.0:
        failures.append("execution_slice_net_return_not_positive")
    if _number(stress.get("netReturn"), "BTC stress48Bps.netReturn") <= 0.0:
        failures.append("execution_slice_stress_return_not_positive")
    if _number(ordinary.get("maxDrawdown"), "BTC ordinary.maxDrawdown") > 0.10:
        failures.append("execution_slice_drawdown_above_gate")
    return failures


def deterministic_core_projection(report: dict[str, Any]) -> dict[str, Any]:
    """Return the stable, semantic replay projection used for cross-run hashes.

    Only runtime-dependent top-level fields (``startedAt``, ``completedAt``,
    ``timing``, ``replayId``, and ``reportSha256``) and per-episode
    ``trainingSeconds`` are omitted.  Dataset timestamps, trade timestamps, and
    every other field remain covered because they are part of the evidence.
    """

    projected = {
        key: value for key, value in report.items() if key not in _CORE_RUNTIME_KEYS
    }
    episodes = report.get("episodes")
    if isinstance(episodes, list):
        projected["episodes"] = [
            (
                {key: value for key, value in episode.items() if key != "trainingSeconds"}
                if isinstance(episode, dict)
                else episode
            )
            for episode in episodes
        ]
    return projected


def deterministic_core_digest(report: dict[str, Any]) -> str:
    """Hash :func:`deterministic_core_projection` as canonical JSON."""

    return sha256_hex(canonical_json(deterministic_core_projection(report)))


def _verify_v6_ledger_contract(
    ledger: object,
    *,
    name: str,
    round_trip_cost_bps: float,
    slippage_bps_per_side: float,
) -> dict[str, Any]:
    _require(isinstance(ledger, dict), f"{name} ledger is missing")
    broker = ledger.get("broker")
    policy = ledger.get("policy")
    leakage = ledger.get("leakageGuard")
    _require(
        isinstance(broker, dict) and set(broker) == _BROKER_KEYS,
        f"{name} V6 broker fields are incomplete",
    )
    _require(
        isinstance(policy, dict) and set(policy) == _POLICY_KEYS,
        f"{name} V6 policy fields are incomplete",
    )
    _require(
        isinstance(leakage, dict) and set(leakage) == _LEAKAGE_KEYS,
        f"{name} V6 leakage fields are incomplete",
    )
    _require(
        broker.get("capacityHandling") == "clip"
        and type(broker.get("executionLabelHorizonBars")) is int
        and broker.get("executionLabelHorizonBars") == EXPECTED_HOLDING_BARS
        and type(broker.get("holdingPeriodBars")) is int
        and broker.get("holdingPeriodBars") == EXPECTED_HOLDING_BARS
        and type(broker.get("latencyBars")) is int
        and broker.get("latencyBars") == 0
        and type(broker.get("checkpointStrideBars")) is int
        and broker.get("checkpointStrideBars") == EXPECTED_CHECKPOINT_STRIDE_BARS,
        f"{name} V6 capacity, latency, horizon, or checkpoint contract failed",
    )
    _require_close(
        _number(broker.get("startingCash"), f"{name}.startingCash"),
        EXPECTED_STARTING_CASH,
        f"{name} starting cash contract failed",
    )
    _require_close(
        _number(broker.get("allocationFraction"), f"{name}.allocationFraction"),
        EXPECTED_ALLOCATION_FRACTION,
        f"{name} allocation contract failed",
    )
    _require_close(
        _number(broker.get("feeBpsPerSide"), f"{name}.feeBpsPerSide"),
        EXPECTED_FEE_BPS_PER_SIDE,
        f"{name} fee contract failed",
    )
    _require_close(
        _number(broker.get("slippageBpsPerSide"), f"{name}.slippageBpsPerSide"),
        slippage_bps_per_side,
        f"{name} slippage contract failed",
    )
    _require_close(
        _number(
            broker.get("maxQuoteVolumeParticipation"),
            f"{name}.maxQuoteVolumeParticipation",
        ),
        EXPECTED_MAX_QUOTE_VOLUME_PARTICIPATION,
        f"{name} quote-volume participation contract failed",
    )
    _require_close(
        _number(broker.get("minimumNotional"), f"{name}.minimumNotional"),
        EXPECTED_MINIMUM_NOTIONAL,
        f"{name} minimum notional contract failed",
    )
    expected_round_trip = 2.0 * (
        EXPECTED_FEE_BPS_PER_SIDE + slippage_bps_per_side
    )
    _require_close(
        _number(broker.get("roundTripCostBps"), f"{name}.roundTripCostBps"),
        expected_round_trip,
        f"{name} round-trip cost contract failed",
    )
    _require_close(
        expected_round_trip,
        round_trip_cost_bps,
        f"{name} named cost scenario is inconsistent",
    )
    _require_close(
        _number(
            broker.get("breakEvenGrossReturnBps"),
            f"{name}.breakEvenGrossReturnBps",
        ),
        _break_even_gross_return_bps(
            fee_bps_per_side=EXPECTED_FEE_BPS_PER_SIDE,
            slippage_bps_per_side=slippage_bps_per_side,
        ),
        f"{name} break-even return contract failed",
    )
    _require(
        type(policy.get("minEntrySpacingBars")) is int
        and policy.get("minEntrySpacingBars") == EXPECTED_MIN_ENTRY_SPACING_BARS,
        f"{name} entry-spacing contract failed",
    )
    _require_close(
        _number(policy.get("edgeBufferBps"), f"{name}.edgeBufferBps"),
        EXPECTED_EDGE_BUFFER_BPS,
        f"{name} edge-buffer contract failed",
    )
    _require_close(
        _number(
            policy.get("requiredGrossReturnBps"),
            f"{name}.requiredGrossReturnBps",
        ),
        round_trip_cost_bps + EXPECTED_EDGE_BUFFER_BPS,
        f"{name} required gross return contract failed",
    )
    _require(
        leakage.get("causalEpisodeBinding") is True
        and leakage.get("checkpointValuationBasis")
        == V6_CHECKPOINT_VALUATION_BASIS
        and type(leakage.get("decisionToFillBars")) is int
        and leakage.get("decisionToFillBars") == 0
        and leakage.get("executionCoordinate") == "decision_rows"
        and leakage.get("nextDecisionRowFill") is False
        and leakage.get("predictionRowsAvailableBeforeDecision") is True
        and leakage.get("sameDecisionRowFill") is True,
        f"{name} V6 leakage contract failed",
    )
    return broker


def _verify_aggregate_ledger(
    ledger: dict[str, Any],
    *,
    name: str,
    trade_count: int,
    allowed_instruments: set[str],
) -> tuple[float, float]:
    broker = ledger.get("broker")
    _require(isinstance(broker, dict), f"{name}.broker is missing")
    starting_cash = _number(broker.get("startingCash"), f"{name}.startingCash")
    final_cash = _number(ledger.get("finalCash"), f"{name}.finalCash")
    _require(starting_cash > 0 and final_cash >= 0, f"{name} cash ledger is invalid")

    net_return = _number(ledger.get("netReturn"), f"{name}.netReturn")
    _require_close(
        net_return,
        final_cash / starting_cash - 1.0,
        f"{name} net return does not reconcile to cash",
    )
    max_drawdown = _number(ledger.get("maxDrawdown"), f"{name}.maxDrawdown")
    _require(0 <= max_drawdown <= 1, f"{name} max drawdown is outside [0, 1]")

    total_fees = _number(ledger.get("totalFees"), f"{name}.totalFees")
    total_slippage = _number(
        ledger.get("totalEstimatedSlippageCost"),
        f"{name}.totalEstimatedSlippageCost",
    )
    total_clip_notional = _number(
        ledger.get("totalCapacityClipNotional"),
        f"{name}.totalCapacityClipNotional",
    )
    turnover_multiple = _number(
        ledger.get("turnoverMultiple"), f"{name}.turnoverMultiple"
    )
    gross_pnl_return = _number(
        ledger.get("grossPnlReturn"), f"{name}.grossPnlReturn"
    )
    _require(
        total_fees >= 0
        and total_slippage >= 0
        and total_clip_notional >= 0
        and turnover_multiple >= 0,
        f"{name} cost ledger is invalid",
    )

    fee_rate = _number(broker.get("feeBpsPerSide"), f"{name}.feeBpsPerSide") / 10_000.0
    slippage_rate = (
        _number(broker.get("slippageBpsPerSide"), f"{name}.slippageBpsPerSide")
        / 10_000.0
    )
    _require(
        0 <= fee_rate < 1 and 0 <= slippage_rate < 1,
        f"{name} broker rates are invalid",
    )
    _require_close(
        total_fees,
        turnover_multiple * starting_cash * fee_rate,
        f"{name} fees do not reconcile to turnover",
    )
    net_pnl = final_cash - starting_cash
    _require_close(
        gross_pnl_return * starting_cash,
        net_pnl + total_fees + total_slippage,
        f"{name} gross PnL, fees, slippage, and net PnL do not reconcile",
    )

    orders_submitted = _nonnegative_integer(
        ledger.get("ordersSubmitted"), f"{name}.ordersSubmitted"
    )
    orders_rejected = _nonnegative_integer(
        ledger.get("ordersRejected"), f"{name}.ordersRejected"
    )
    orders_clipped = _nonnegative_integer(
        ledger.get("ordersClipped"), f"{name}.ordersClipped"
    )
    evaluated_decisions = _nonnegative_integer(
        ledger.get("evaluatedDecisions"), f"{name}.evaluatedDecisions"
    )
    exposure_bars = _nonnegative_integer(
        ledger.get("exposureBars"), f"{name}.exposureBars"
    )
    qualifying_signals = _nonnegative_integer(
        ledger.get("qualifyingSignals"), f"{name}.qualifyingSignals"
    )
    time_rows = _nonnegative_integer(ledger.get("timeRows"), f"{name}.timeRows")
    _require(
        orders_submitted == trade_count + orders_rejected
        and orders_clipped <= trade_count,
        f"{name} order counters do not reconcile",
    )
    _require(
        time_rows > 0
        and exposure_bars <= time_rows
        and qualifying_signals == orders_submitted
        and qualifying_signals <= evaluated_decisions
        and evaluated_decisions <= time_rows,
        f"{name} replay counters do not reconcile",
    )
    _require(
        (orders_clipped == 0 and total_clip_notional == 0)
        or (orders_clipped > 0 and total_clip_notional > 0),
        f"{name} capacity clipping ledger does not reconcile",
    )

    rejection_reasons = ledger.get("rejectionReasons")
    _require(isinstance(rejection_reasons, dict), f"{name}.rejectionReasons is invalid")
    rejection_total = 0
    for reason, count in rejection_reasons.items():
        _require(isinstance(reason, str) and reason, f"{name} rejection reason is invalid")
        rejection_total += _nonnegative_integer(count, f"{name}.rejectionReasons[{reason}]")
    _require(
        rejection_total == orders_rejected,
        f"{name} rejection reasons do not reconcile",
    )

    trades_by_instrument = ledger.get("tradesByInstrument")
    _require(isinstance(trades_by_instrument, dict), f"{name}.tradesByInstrument is invalid")
    _require(
        set(trades_by_instrument) == allowed_instruments,
        f"{name} trade instrument coverage is invalid",
    )
    counted_trades = 0
    for instrument, count in trades_by_instrument.items():
        _require(
            instrument in allowed_instruments,
            f"{name} contains an unexpected trade instrument",
        )
        counted_trades += _nonnegative_integer(
            count, f"{name}.tradesByInstrument[{instrument}]"
        )
    _require(counted_trades == trade_count, f"{name} trade counts do not reconcile")

    pnl_by_instrument = ledger.get("netPnlByInstrument")
    _require(isinstance(pnl_by_instrument, dict), f"{name}.netPnlByInstrument is invalid")
    _require(
        set(pnl_by_instrument) == allowed_instruments,
        f"{name} PnL instrument coverage is invalid",
    )
    reported_pnl = 0.0
    for instrument, value in pnl_by_instrument.items():
        _require(
            instrument in allowed_instruments,
            f"{name} contains an unexpected PnL instrument",
        )
        reported_pnl += _number(value, f"{name}.netPnlByInstrument[{instrument}]")
    _require_close(
        reported_pnl,
        net_pnl,
        f"{name} instrument PnL does not reconcile to final cash",
    )

    profitable_rate = _number(
        ledger.get("profitableTradeRate"), f"{name}.profitableTradeRate"
    )
    _require(0 <= profitable_rate <= 1, f"{name} profitable trade rate is invalid")
    simulated_days = _number(ledger.get("simulatedDays"), f"{name}.simulatedDays")
    trades_per_day = _number(ledger.get("tradesPerDay"), f"{name}.tradesPerDay")
    cash_bar_rate = _number(ledger.get("cashBarRate"), f"{name}.cashBarRate")
    _require(simulated_days > 0, f"{name} simulated days is invalid")
    _require_close(
        simulated_days,
        time_rows / 288.0,
        f"{name} simulated days do not reconcile to time rows",
    )
    _require_close(
        cash_bar_rate,
        1.0 - exposure_bars / time_rows,
        f"{name} cash-bar rate does not reconcile to exposure",
    )
    _require_close(
        trades_per_day,
        trade_count / simulated_days,
        f"{name} trades per day do not reconcile",
    )
    return starting_cash, final_cash


def _verify_drawdown_witness(
    ledger: dict[str, Any],
    *,
    name: str,
    first_replay_ms: int,
    last_replay_ms: int,
    checkpoints: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    witness = ledger.get("maxDrawdownWitness")
    _require(
        isinstance(witness, dict)
        and set(witness)
        == {
            "drawdown",
            "peakAt",
            "peakEquity",
            "peakSource",
            "troughAt",
            "troughEquity",
        },
        f"{name} maximum-drawdown witness is incomplete",
    )
    reported = _number(ledger.get("maxDrawdown"), f"{name}.maxDrawdown")
    witness_drawdown = _number(witness.get("drawdown"), f"{name}.witness.drawdown")
    peak_equity = _number(witness.get("peakEquity"), f"{name}.witness.peakEquity")
    trough_equity = _number(
        witness.get("troughEquity"), f"{name}.witness.troughEquity"
    )
    peak_at = _timestamp_ms(witness.get("peakAt"), f"{name}.witness.peakAt")
    trough_at = _timestamp_ms(
        witness.get("troughAt"), f"{name}.witness.troughAt"
    )
    peak_source = witness.get("peakSource")
    _require(
        peak_equity > 0
        and 0 <= trough_equity <= peak_equity
        and peak_source in {"pre_replay_starting_cash", "checkpoint"}
        and first_replay_ms <= peak_at <= trough_at <= last_replay_ms
        and (peak_at - first_replay_ms) % FIVE_MINUTES_MS == 0
        and (trough_at - first_replay_ms) % FIVE_MINUTES_MS == 0,
        f"{name} maximum-drawdown witness domain is invalid",
    )
    calculated = (peak_equity - trough_equity) / peak_equity
    _require_close(
        witness_drawdown,
        calculated,
        f"{name} maximum-drawdown witness arithmetic failed",
    )
    _require_close(
        reported,
        calculated,
        f"{name} maximum drawdown does not match its witness",
    )
    if peak_source == "pre_replay_starting_cash":
        broker = ledger.get("broker")
        _require(isinstance(broker, dict), f"{name}.broker is missing")
        _require_close(
            peak_equity,
            _number(broker.get("startingCash"), f"{name}.startingCash"),
            f"{name} pre-replay peak is not starting cash",
        )
        _require(
            peak_at == first_replay_ms,
            f"{name} pre-replay peak timestamp is invalid",
        )

    if checkpoints is not None:
        by_time = {
            _timestamp_ms(item.get("at"), f"{name}.checkpoint.at"): item
            for item in checkpoints
        }
        trough = by_time.get(trough_at)
        _require(trough is not None, f"{name} witness trough is not an exact checkpoint")
        _require_close(
            _number(trough.get("equity"), f"{name}.trough.equity"),
            trough_equity,
            f"{name} witness trough equity does not match its checkpoint",
        )
        _require_close(
            _number(trough.get("peakEquity"), f"{name}.trough.peakEquity"),
            peak_equity,
            f"{name} witness peak equity does not match its trough checkpoint",
        )
        _require_close(
            _number(trough.get("drawdown"), f"{name}.trough.drawdown"),
            witness_drawdown,
            f"{name} witness drawdown does not match its trough checkpoint",
        )
        if peak_source == "checkpoint":
            peak = by_time.get(peak_at)
            _require(peak is not None, f"{name} witness peak is not an exact checkpoint")
            _require_close(
                _number(peak.get("equity"), f"{name}.peak.equity"),
                peak_equity,
                f"{name} witness peak equity does not match its checkpoint",
            )
            _require_close(
                _number(peak.get("peakEquity"), f"{name}.peak.peakEquity"),
                peak_equity,
                f"{name} witness peak checkpoint does not establish the peak",
            )
            _require_close(
                _number(peak.get("drawdown"), f"{name}.peak.drawdown"),
                0.0,
                f"{name} witness peak checkpoint must have zero drawdown",
            )
        checkpoint_max = max(
            _number(item.get("drawdown"), f"{name}.checkpoint.drawdown")
            for item in checkpoints
        )
        _require_close(
            checkpoint_max,
            reported,
            f"{name} checkpoint series does not contain the maximum drawdown",
        )
    return {
        "exactMaxDrawdownRecomputed": checkpoints is not None,
        "fullBarSourceReplayPerformed": False,
        "method": "embedded_peak_trough_witness_bound_to_exact_checkpoints",
        "reportedMaxDrawdown": reported,
    }


def _verify_checkpoint_series(
    ledger: dict[str, Any],
    *,
    name: str,
    first_replay_ms: int,
    last_replay_ms: int,
    replay_time_rows: int,
    allowed_instruments: set[str],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    checkpoints = ledger.get("checkpoints")
    _require(
        isinstance(checkpoints, list) and len(checkpoints) >= 2,
        f"{name} checkpoint series is missing",
    )
    _require(
        _nonnegative_integer(ledger.get("timeRows"), f"{name}.timeRows")
        == replay_time_rows,
        f"{name} checkpoint clock does not cover the replay dataset",
    )
    checkpoint_times: list[int] = []
    previous_peak_equity = EXPECTED_STARTING_CASH
    for checkpoint in checkpoints:
        _require(
            isinstance(checkpoint, dict)
            and set(checkpoint)
            == {
                "at",
                "cash",
                "drawdown",
                "equity",
                "peakEquity",
                "positionInstrument",
                "positionMarketValue",
            },
            f"{name} checkpoint row is invalid",
        )
        checkpoint_at = _timestamp_ms(checkpoint.get("at"), f"{name}.checkpoint.at")
        cash = _number(checkpoint.get("cash"), f"{name}.checkpoint.cash")
        equity = _number(checkpoint.get("equity"), f"{name}.checkpoint.equity")
        peak_equity = _number(
            checkpoint.get("peakEquity"), f"{name}.checkpoint.peakEquity"
        )
        position_value = _number(
            checkpoint.get("positionMarketValue"),
            f"{name}.checkpoint.positionMarketValue",
        )
        position_instrument = checkpoint.get("positionInstrument")
        drawdown = _number(
            checkpoint.get("drawdown"), f"{name}.checkpoint.drawdown"
        )
        _require(
            first_replay_ms <= checkpoint_at <= last_replay_ms
            and (checkpoint_at - first_replay_ms) % FIVE_MINUTES_MS == 0
            and cash >= 0
            and equity >= 0
            and position_value >= 0
            and peak_equity >= max(previous_peak_equity, equity)
            and 0 <= drawdown <= 1
            and (
                (position_instrument is None and position_value == 0)
                or (
                    position_instrument in allowed_instruments
                    and position_value > 0
                )
            ),
            f"{name} checkpoint ledger is invalid",
        )
        _require_close(
            equity,
            cash + position_value,
            f"{name} checkpoint equity does not reconcile to cash and position value",
        )
        _require_close(
            drawdown,
            (peak_equity - equity) / peak_equity,
            f"{name} checkpoint drawdown does not reconcile to peak and equity",
        )
        checkpoint_times.append(checkpoint_at)
        previous_peak_equity = peak_equity

    _require(
        all(
            left < right
            for left, right in zip(checkpoint_times, checkpoint_times[1:])
        ),
        f"{name} checkpoint clock is not strictly increasing",
    )
    witness = ledger.get("maxDrawdownWitness")
    _require(
        isinstance(witness, dict),
        f"{name} maximum-drawdown witness is missing",
    )
    witness_times = {
        _timestamp_ms(witness.get("peakAt"), f"{name}.witness.peakAt"),
        _timestamp_ms(witness.get("troughAt"), f"{name}.witness.troughAt"),
    }
    scheduled_times = {
        first_replay_ms + index * FIVE_MINUTES_MS
        for index in range(0, replay_time_rows, EXPECTED_CHECKPOINT_STRIDE_BARS)
    }
    scheduled_times.add(last_replay_ms)
    actual_checkpoint_times = set(checkpoint_times)
    _require(
        len(actual_checkpoint_times) == len(checkpoint_times)
        and scheduled_times <= actual_checkpoint_times
        and actual_checkpoint_times - scheduled_times <= witness_times,
        f"{name} checkpoint schedule contains missing, duplicate, or unexplained rows",
    )
    final_checkpoint = checkpoints[-1]
    final_cash = _number(ledger.get("finalCash"), f"{name}.finalCash")
    _require_close(
        _number(final_checkpoint.get("cash"), f"{name}.final checkpoint.cash"),
        final_cash,
        f"{name} final checkpoint cash does not match final cash",
    )
    _require_close(
        _number(final_checkpoint.get("equity"), f"{name}.final checkpoint.equity"),
        final_cash,
        f"{name} final checkpoint equity does not match final cash",
    )
    _require(
        checkpoint_times[-1] == last_replay_ms
        and final_checkpoint.get("positionInstrument") is None
        and _number(
            final_checkpoint.get("positionMarketValue"),
            f"{name}.final checkpoint.positionMarketValue",
        )
        == 0,
        f"{name} final checkpoint is not settled at the replay boundary",
    )
    verification = _verify_drawdown_witness(
        ledger,
        name=name,
        first_replay_ms=first_replay_ms,
        last_replay_ms=last_replay_ms,
        checkpoints=checkpoints,
    )
    return checkpoints, verification


def verify_report(report: dict[str, Any]) -> dict[str, Any]:
    stored_hash = report.get("reportSha256")
    _require(
        isinstance(stored_hash, str)
        and len(stored_hash) == 64
        and stored_hash
        == sha256_hex(
            canonical_json(
                {key: value for key, value in report.items() if key != "reportSha256"}
            )
        ),
        "canonical report hash does not match",
    )
    schema_version = report.get("schemaVersion")
    if schema_version in RETIRED_REPORT_SCHEMAS:
        raise ReplayVerificationError(
            "legacy historical replay schemas are retired and are not V6-verifiable"
        )
    _require(
        schema_version == V6_REPORT_SCHEMA,
        "only the V6 historical replay report schema is verifiable",
    )
    execution = report.get("execution")
    _require(isinstance(execution, dict), "execution contract is missing")
    _require(
        report.get("decision") == "research_only"
        and report.get("promotable") is False
        and report.get("shadowDaysCredited") == 0
        and execution.get("historicalReplayOnly") is True
        and execution.get("publicDataOnly") is True
        and execution.get("privateApi") is False
        and execution.get("orderCapability") is False
        and execution.get("executionAllowlistChanged") is False,
        "research-only safety contract failed",
    )

    dataset = report.get("dataset")
    protocol = report.get("protocol")
    episodes = report.get("episodes")
    _require(
        isinstance(dataset, dict)
        and isinstance(protocol, dict)
        and isinstance(episodes, list),
        "dataset, protocol, or episodes are missing",
    )
    instruments = dataset.get("instruments")
    asset_rows = dataset.get("assetRows")
    _require(
        isinstance(asset_rows, int)
        and not isinstance(asset_rows, bool)
        and 3 <= asset_rows <= 8
        and isinstance(instruments, list)
        and len(instruments) == asset_rows
        and len(set(instruments)) == asset_rows
        and all(isinstance(item, str) and item.endswith("-USDT") for item in instruments),
        "replay instrument universe is invalid",
    )
    episode_count = protocol.get("episodeCount")
    _require(
        isinstance(episode_count, int)
        and episode_count == len(episodes)
        and episode_count > 0
        and protocol.get("trainBars") == 365 * 288
        and protocol.get("retrainEveryBars") == EXPECTED_REPLAY_BARS
        and protocol.get("holdingBars") == EXPECTED_HOLDING_BARS
        and protocol.get("scope") == "retrospective-development-only",
        "rolling replay protocol is invalid",
    )
    replay_time_rows = _nonnegative_integer(
        dataset.get("replayTimeRows"), "dataset.replayTimeRows"
    )
    first_replay_ms = _timestamp_ms(
        dataset.get("firstReplayAt"), "dataset.firstReplayAt"
    )
    last_replay_ms = _timestamp_ms(
        dataset.get("lastReplayAt"), "dataset.lastReplayAt"
    )
    _require(
        replay_time_rows == episode_count * EXPECTED_REPLAY_BARS
        and last_replay_ms - first_replay_ms
        == (replay_time_rows - 1) * FIVE_MINUTES_MS,
        "dataset replay clock or row count is invalid",
    )
    model = report.get("model")
    _require(isinstance(model, dict), "model contract is invalid")
    target = model.get("targetContract")
    leakage = report.get("leakageAudit")
    _require(
        isinstance(target, dict)
        and target.get("decisionAt")
        == "confirmed_bar_close_next_bar_open_boundary"
        and target.get("entryAt") == "next_bar_open_same_timestamp"
        and target.get("exitAt") == "entry_plus_12_bars_open"
        and target.get("labelHorizonBars") == 12
        and target.get("predictionUnit") == "gross_return"
        and isinstance(leakage, dict)
        and leakage.get("checkpointValuationBasis")
        == V6_CHECKPOINT_VALUATION_BASIS
        and type(leakage.get("decisionToFillBars")) is int
        and leakage.get("decisionToFillBars") == 0
        and leakage.get("decisionTimestampEqualsEntryTimestamp") is True
        and leakage.get("entryBarVolumeUsedExPost") is False
        and leakage.get("featureSourceCloseToEntryBars") == 0
        and leakage.get("instantaneousDecisionFillAssumption") is True
        and leakage.get("nextCandleAfterFeatureSource") is True
        and leakage.get("sameSourceBarFillAllowed") is False
        and leakage.get("sameTimestampFillAllowed") is True
        and leakage.get("targetExecutionAligned") is True
        and dataset.get("capacityVolumeSource")
        == "confirmed_feature_source_bar"
        and protocol.get("executionLabelHorizonBars") == 12
        and protocol.get("developmentHistoryAlreadyObserved") is True,
        "V6 corrected execution target or selection-bias contract failed",
    )
    _require(
        execution.get("checkpointValuationBasis")
        == V6_CHECKPOINT_VALUATION_BASIS,
        "V6 checkpoint valuation marker is missing from execution contract",
    )

    previous_stop: int | None = None
    episode_ids: set[str] = set()
    episode_replay_windows: dict[str, tuple[int, int]] = {}
    for expected_index, episode in enumerate(episodes):
        _require(isinstance(episode, dict), "episode row is invalid")
        episode_id = episode.get("episodeId")
        _require(
            episode.get("episode") == expected_index
            and isinstance(episode_id, str)
            and episode_id.startswith("replay_episode_")
            and episode_id not in episode_ids,
            "episode identity is invalid",
        )
        episode_ids.add(episode_id)
        timeline = [
            _timestamp_ms(episode.get("fitStartAt"), "fitStartAt"),
            _timestamp_ms(episode.get("fitStopAt"), "fitStopAt"),
            _timestamp_ms(episode.get("calibrationStartAt"), "calibrationStartAt"),
            _timestamp_ms(episode.get("calibrationStopAt"), "calibrationStopAt"),
            _timestamp_ms(episode.get("labelCompleteAt"), "labelCompleteAt"),
            _timestamp_ms(episode.get("availableAt"), "availableAt"),
            _timestamp_ms(episode.get("replayStartAt"), "replayStartAt"),
            _timestamp_ms(episode.get("replayStopAt"), "replayStopAt"),
        ]
        _require(
            all(left < right for left, right in zip(timeline, timeline[1:])),
            "episode timeline is not strictly causal",
        )
        _require(
            timeline[6] - timeline[5] == FIVE_MINUTES_MS
            and timeline[7] - timeline[6]
            == (EXPECTED_REPLAY_BARS - 1) * FIVE_MINUTES_MS,
            "model availability or fixed replay window is invalid",
        )
        if previous_stop is not None:
            _require(
                timeline[6] - previous_stop == FIVE_MINUTES_MS,
                "episode replay windows are not contiguous",
            )
        previous_stop = timeline[7]
        episode_replay_windows[episode_id] = (timeline[6], timeline[7])
        _require(
            episode.get("assetRows") == asset_rows
            and episode.get("replayRows") == EXPECTED_REPLAY_BARS * asset_rows
            and isinstance(episode.get("fitRows"), int)
            and episode.get("fitRows") > 0
            and isinstance(episode.get("calibrationRows"), int)
            and episode.get("calibrationRows") > 0,
            "episode row counts are invalid",
        )
    _require(
        bool(episodes)
        and _timestamp_ms(episodes[0].get("replayStartAt"), "replayStartAt")
        == first_replay_ms
        and _timestamp_ms(episodes[-1].get("replayStopAt"), "replayStopAt")
        == last_replay_ms,
        "dataset replay bounds do not match episode windows",
    )

    result = report.get("result")
    _require(isinstance(result, dict), "replay result is missing")
    ordinary = result.get("ordinary")
    stress = result.get("stress48Bps")
    _require(
        isinstance(ordinary, dict) and isinstance(stress, dict),
        "ordinary or stress ledger is missing",
    )
    _require(
        type(execution.get("decisionToFillLatencyBars")) is int
        and execution.get("decisionToFillLatencyBars") == 0
        and execution.get("engineSchemaVersion") == "moheng.historical-replay.v3",
        "V6 capacity, latency, or engine contract failed",
    )
    ordinary_broker = _verify_v6_ledger_contract(
        ordinary,
        name="ordinary",
        round_trip_cost_bps=24.0,
        slippage_bps_per_side=EXPECTED_STANDARD_SLIPPAGE_BPS_PER_SIDE,
    )
    _verify_v6_ledger_contract(
        stress,
        name="stress48Bps",
        round_trip_cost_bps=48.0,
        slippage_bps_per_side=EXPECTED_STRESS_SLIPPAGE_BPS_PER_SIDE,
    )

    execution_slice = result.get("executionSlice")
    _require(isinstance(execution_slice, dict), "BTC execution slice is missing")
    slice_ordinary = execution_slice.get("ordinary")
    slice_stress = execution_slice.get("stress48Bps")
    _require(
        execution_slice.get("instrument") == "BTC-USDT"
        and execution_slice.get("decision") == "research_only"
        and isinstance(slice_ordinary, dict)
        and isinstance(slice_stress, dict),
        "BTC execution slice contract failed",
    )
    slice_ordinary_trades = _nonnegative_integer(
        slice_ordinary.get("trades"), "BTC ordinary.trades"
    )
    slice_stress_trades = _nonnegative_integer(
        slice_stress.get("trades"), "BTC stress48Bps.trades"
    )
    _verify_v6_ledger_contract(
        slice_ordinary,
        name="BTC ordinary",
        round_trip_cost_bps=24.0,
        slippage_bps_per_side=EXPECTED_STANDARD_SLIPPAGE_BPS_PER_SIDE,
    )
    _verify_v6_ledger_contract(
        slice_stress,
        name="BTC stress48Bps",
        round_trip_cost_bps=48.0,
        slippage_bps_per_side=EXPECTED_STRESS_SLIPPAGE_BPS_PER_SIDE,
    )
    chosen_policy = result.get("chosenPolicy")
    _require(
        isinstance(chosen_policy, dict)
        and set(chosen_policy) == _POLICY_KEYS
        and chosen_policy == ordinary.get("policy")
        and chosen_policy == slice_ordinary.get("policy"),
        "V6 chosen policy does not match the ordinary execution ledgers",
    )
    _verify_aggregate_ledger(
        slice_ordinary,
        name="BTC ordinary",
        trade_count=slice_ordinary_trades,
        allowed_instruments={"BTC-USDT"},
    )
    _verify_aggregate_ledger(
        slice_stress,
        name="BTC stress48Bps",
        trade_count=slice_stress_trades,
        allowed_instruments={"BTC-USDT"},
    )
    expected_slice_failures = _execution_slice_failures(
        slice_ordinary,
        slice_stress,
        trade_count=slice_ordinary_trades,
    )
    _require(
        _failure_list(execution_slice.get("failures"), "executionSlice.failures")
        == expected_slice_failures
        and execution_slice.get("developmentGatePassed")
        is (not expected_slice_failures),
        "BTC execution slice gate does not reconcile to its metrics",
    )
    starting_cash = _number(ordinary_broker.get("startingCash"), "startingCash")
    final_cash = _number(ordinary.get("finalCash"), "finalCash")
    max_drawdown = _number(ordinary.get("maxDrawdown"), "maxDrawdown")
    _require(starting_cash > 0 and final_cash >= 0, "cash ledger is invalid")
    _require(0 <= max_drawdown <= 1, "max drawdown is outside [0, 1]")
    _require(
        _number(ordinary.get("totalFees"), "totalFees") >= 0
        and _number(
            ordinary.get("totalEstimatedSlippageCost"),
            "totalEstimatedSlippageCost",
        )
        >= 0,
        "cost ledger is invalid",
    )

    trades = ordinary.get("trades")
    _require(isinstance(trades, list), "ordinary replay trades are missing")
    checkpoints, drawdown_verification = _verify_checkpoint_series(
        ordinary,
        name="ordinary",
        first_replay_ms=first_replay_ms,
        last_replay_ms=last_replay_ms,
        replay_time_rows=replay_time_rows,
        allowed_instruments=set(instruments),
    )
    _verify_checkpoint_series(
        stress,
        name="stress48Bps",
        first_replay_ms=first_replay_ms,
        last_replay_ms=last_replay_ms,
        replay_time_rows=replay_time_rows,
        allowed_instruments=set(instruments),
    )
    _verify_checkpoint_series(
        slice_ordinary,
        name="BTC ordinary",
        first_replay_ms=first_replay_ms,
        last_replay_ms=last_replay_ms,
        replay_time_rows=replay_time_rows,
        allowed_instruments={"BTC-USDT"},
    )
    _verify_checkpoint_series(
        slice_stress,
        name="BTC stress48Bps",
        first_replay_ms=first_replay_ms,
        last_replay_ms=last_replay_ms,
        replay_time_rows=replay_time_rows,
        allowed_instruments={"BTC-USDT"},
    )

    trade_ids: set[str] = set()
    net_pnl = 0.0
    derived_fees = 0.0
    derived_slippage = 0.0
    derived_turnover = 0.0
    derived_gross_pnl = 0.0
    derived_profitable_trades = 0
    derived_trade_counts = {instrument: 0 for instrument in instruments}
    derived_instrument_pnl = {instrument: 0.0 for instrument in instruments}
    fee_rate = _number(ordinary_broker.get("feeBpsPerSide"), "feeBpsPerSide") / 10_000.0
    slippage_rate = (
        _number(ordinary_broker.get("slippageBpsPerSide"), "slippageBpsPerSide")
        / 10_000.0
    )
    allocation_fraction = _number(
        ordinary_broker.get("allocationFraction"), "allocationFraction"
    )
    minimum_notional = _number(
        ordinary_broker.get("minimumNotional"), "minimumNotional"
    )
    required_gross_return_bps = _number(
        ordinary.get("policy", {}).get("requiredGrossReturnBps"),
        "ordinary.requiredGrossReturnBps",
    )
    _require(
        0 <= fee_rate < 1 and 0 <= slippage_rate < 1,
        "ordinary broker rates are invalid",
    )
    previous_trade_exit: int | None = None
    cash_before_trade = starting_cash
    for trade in trades:
        _require(isinstance(trade, dict), "trade row is invalid")
        trade_id = trade.get("tradeId")
        _require(
            isinstance(trade_id, str)
            and trade_id not in trade_ids
            and trade.get("episodeId") in episode_ids
            and trade.get("instrument") in instruments,
            "trade identity is invalid",
        )
        trade_ids.add(trade_id)
        signal_at = _timestamp_ms(trade.get("signalAt"), "trade.signalAt")
        entered_at = _timestamp_ms(trade.get("enteredAt"), "trade.enteredAt")
        exited_at = _timestamp_ms(trade.get("exitedAt"), "trade.exitedAt")
        episode_start, episode_stop = episode_replay_windows[trade["episodeId"]]
        _require(
            entered_at - signal_at == 0
            and exited_at - entered_at == EXPECTED_HOLDING_BARS * FIVE_MINUTES_MS,
            "trade timing violates latency or holding period",
        )
        _require(
            first_replay_ms <= signal_at <= episode_stop <= last_replay_ms
            and episode_start <= signal_at <= episode_stop
            and first_replay_ms <= entered_at < exited_at <= last_replay_ms
            and (signal_at - first_replay_ms) % FIVE_MINUTES_MS == 0,
            "trade timestamps are outside their replay or episode window",
        )
        _require(
            previous_trade_exit is None or entered_at >= previous_trade_exit,
            "trade intervals overlap or are out of order",
        )
        previous_trade_exit = exited_at
        quantity = _number(trade.get("quantity"), "trade.quantity")
        entry_fill_price = _number(
            trade.get("entryFillPrice"), "trade.entryFillPrice"
        )
        exit_fill_price = _number(trade.get("exitFillPrice"), "trade.exitFillPrice")
        _require(
            quantity > 0 and entry_fill_price > 0 and exit_fill_price > 0,
            "trade quantity and fill prices must be positive",
        )
        raw_entry_price = entry_fill_price / (1.0 + slippage_rate)
        raw_exit_price = exit_fill_price / (1.0 - slippage_rate)
        entry_notional = quantity * entry_fill_price
        exit_notional = quantity * exit_fill_price
        entry_fee = entry_notional * fee_rate
        exit_fee = exit_notional * fee_rate
        fees = entry_fee + exit_fee
        committed_cash = entry_notional + entry_fee
        calculated_gross_return = raw_exit_price / raw_entry_price - 1.0
        calculated_net_pnl = exit_notional - exit_fee - committed_cash
        calculated_net_return = calculated_net_pnl / committed_cash
        calculated_slippage = quantity * (
            entry_fill_price - raw_entry_price + raw_exit_price - exit_fill_price
        )
        calculated_gross_pnl = quantity * (raw_exit_price - raw_entry_price)
        expected_gross_return_bps = _number(
            trade.get("expectedGrossReturnBps"), "trade.expectedGrossReturnBps"
        )
        _require(
            entry_notional >= minimum_notional
            and committed_cash
            <= cash_before_trade * allocation_fraction + 1e-7
            and expected_gross_return_bps > required_gross_return_bps,
            "trade violates the fixed allocation, notional, or policy threshold",
        )

        _require_close(
            _number(trade.get("fees"), "trade.fees"),
            fees,
            "trade fees do not reconcile to fills",
        )
        _require_close(
            _number(trade.get("grossReturn"), "trade.grossReturn"),
            calculated_gross_return,
            "trade gross return does not reconcile to raw prices",
        )
        reported_net_pnl = _number(trade.get("netPnl"), "trade.netPnl")
        _require_close(
            reported_net_pnl,
            calculated_net_pnl,
            "trade net PnL does not reconcile to fills and fees",
        )
        _require_close(
            _number(
                trade.get("netReturnOnCommittedCash"),
                "trade.netReturnOnCommittedCash",
            ),
            calculated_net_return,
            "trade return on committed cash does not reconcile",
        )
        instrument = trade["instrument"]
        net_pnl += reported_net_pnl
        cash_before_trade += reported_net_pnl
        _require(cash_before_trade >= 0, "trade sequence produces negative cash")
        derived_fees += fees
        derived_slippage += calculated_slippage
        derived_turnover += entry_notional + exit_notional
        derived_gross_pnl += calculated_gross_pnl
        derived_profitable_trades += int(reported_net_pnl > 0)
        derived_trade_counts[instrument] += 1
        derived_instrument_pnl[instrument] += reported_net_pnl
    _require(
        math.isclose(cash_before_trade, final_cash, rel_tol=0, abs_tol=1e-7)
        and math.isclose(
            starting_cash + net_pnl,
            final_cash,
            rel_tol=0,
            abs_tol=1e-7,
        ),
        "cash ledger does not reconcile to trade net PnL",
    )

    aggregate_starting_cash, aggregate_final_cash = _verify_aggregate_ledger(
        ordinary,
        name="ordinary",
        trade_count=len(trades),
        allowed_instruments=set(instruments),
    )
    _require_close(
        aggregate_starting_cash,
        starting_cash,
        "ordinary starting cash is inconsistent",
    )
    _require_close(
        aggregate_final_cash,
        final_cash,
        "ordinary final cash is inconsistent",
    )
    _require_close(
        _number(ordinary.get("totalFees"), "totalFees"),
        derived_fees,
        "ordinary total fees do not reconcile to trades",
    )
    _require_close(
        _number(
            ordinary.get("totalEstimatedSlippageCost"),
            "totalEstimatedSlippageCost",
        ),
        derived_slippage,
        "ordinary total slippage does not reconcile to trades",
    )
    _require_close(
        _number(ordinary.get("turnoverMultiple"), "turnoverMultiple"),
        derived_turnover / starting_cash,
        "ordinary turnover does not reconcile to trades",
    )
    _require_close(
        _number(ordinary.get("grossPnlReturn"), "grossPnlReturn"),
        derived_gross_pnl / starting_cash,
        "ordinary gross PnL return does not reconcile to trades",
    )
    _require_close(
        _number(ordinary.get("profitableTradeRate"), "profitableTradeRate"),
        derived_profitable_trades / len(trades) if trades else 0.0,
        "ordinary profitable trade rate does not reconcile to trades",
    )
    _require(
        ordinary.get("tradesByInstrument") == derived_trade_counts,
        "ordinary per-instrument trade counts do not reconcile to trades",
    )
    reported_instrument_pnl = ordinary.get("netPnlByInstrument")
    _require(
        isinstance(reported_instrument_pnl, dict),
        "ordinary per-instrument PnL is invalid",
    )
    for instrument, derived_value in derived_instrument_pnl.items():
        _require_close(
            _number(
                reported_instrument_pnl.get(instrument),
                f"netPnlByInstrument[{instrument}]",
            ),
            derived_value,
            "ordinary per-instrument PnL does not reconcile to trades",
        )

    stress_trade_count = _nonnegative_integer(
        stress.get("trades"), "stress48Bps.trades"
    )
    _verify_aggregate_ledger(
        stress,
        name="stress48Bps",
        trade_count=stress_trade_count,
        allowed_instruments=set(instruments),
    )
    expected_ordinary_failures = _development_failures(
        ordinary, trade_count=len(trades)
    )
    expected_stress_failures = _stress_failures(stress)
    _require(
        _failure_list(ordinary.get("failures"), "ordinary.failures")
        == expected_ordinary_failures
        and ordinary.get("developmentGatePassed")
        is (not expected_ordinary_failures),
        "ordinary development gate does not reconcile to its metrics",
    )
    _require(
        _failure_list(stress.get("failures"), "stress48Bps.failures")
        == expected_stress_failures
        and stress.get("developmentGatePassed") is (not expected_stress_failures),
        "stress development gate does not reconcile to its metrics",
    )
    _require(
        result.get("decision") == "research_only"
        and result.get("shadowDaysCredited") == 0
        and result.get("developmentGatePassed")
        is (not expected_ordinary_failures and not expected_stress_failures),
        "combined development gate does not reconcile to ordinary and stress gates",
    )

    return {
        "assetRows": asset_rows,
        "checkpoints": len(checkpoints),
        "episodes": episode_count,
        "finalCash": final_cash,
        "maxDrawdown": max_drawdown,
        "maxDrawdownVerification": drawdown_verification,
        "netReturn": _number(ordinary.get("netReturn"), "netReturn"),
        "reportSha256": stored_hash,
        "coreDigestProjection": CORE_DIGEST_PROJECTION_VERSION,
        "coreDigestSha256": deterministic_core_digest(report),
        "executionSemantics": "corrected_next_open_boundary",
        "sourceReplayVerified": False,
        "structuralLedgerVerified": True,
        "shadowDaysCredited": 0,
        "trades": len(trades),
        "verified": True,
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify one V6 historical replay report; legacy schemas are retired."
    )
    parser.add_argument("report", type=Path)
    return parser.parse_args()


def main() -> int:
    path = _parse_args().report.expanduser().resolve()
    try:
        path.relative_to(REPLAY_ROOT.resolve())
    except ValueError as exc:
        raise ReplayVerificationError(
            "replay report must stay under project .research-data/replays"
        ) from exc
    _require(
        path.is_file() and 2 <= path.stat().st_size <= MAX_REPLAY_JSON_BYTES,
        "report file is invalid",
    )
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReplayVerificationError("report is not valid UTF-8 JSON") from exc
    _require(isinstance(value, dict), "report root must be an object")
    print(json.dumps(verify_report(value), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ReplayVerificationError as exc:
        print(json.dumps({"error": str(exc), "verified": False}, ensure_ascii=False))
        raise SystemExit(1) from exc
