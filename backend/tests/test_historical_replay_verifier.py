from __future__ import annotations

import copy
import json
import sys
from datetime import datetime, timedelta, timezone

import pytest

from okx_demo_lab.ml.strategy import canonical_json, sha256_hex
from research import verify_historical_replay as verifier_module
from research.verify_historical_replay import (
    CORE_DIGEST_PROJECTION_VERSION,
    MAX_REPLAY_JSON_BYTES,
    ReplayVerificationError,
    deterministic_core_digest,
    verify_report,
)


def _sealed(body: dict[str, object]) -> dict[str, object]:
    return {**body, "reportSha256": sha256_hex(canonical_json(body))}


def _resealed(report: dict[str, object]) -> dict[str, object]:
    body = copy.deepcopy(report)
    body.pop("reportSha256", None)
    return _sealed(body)


FIRST_REPLAY = "2023-08-01T01:10:00.000Z"
LAST_REPLAY = "2023-08-31T01:05:00.000Z"
CHECKPOINT_VALUATION_BASIS = "current_bar_open_at_checkpoint_boundary"


def _break_even(slippage_bps: float) -> float:
    fee = 8.0 / 10_000.0
    slippage = slippage_bps / 10_000.0
    return (
        ((1.0 + slippage) * (1.0 + fee))
        / ((1.0 - slippage) * (1.0 - fee))
        - 1.0
    ) * 10_000.0


def _scheduled_checkpoints() -> list[dict[str, object]]:
    start = datetime.fromisoformat(FIRST_REPLAY.replace("Z", "+00:00"))
    indices = list(range(0, 30 * 288, 288))
    indices.append(30 * 288 - 1)
    return [
        {
            "at": (start + timedelta(minutes=5 * index))
            .astimezone(timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "cash": 10_000.0,
            "drawdown": 0.0,
            "equity": 10_000.0,
            "peakEquity": 10_000.0,
            "positionInstrument": None,
            "positionMarketValue": 0.0,
        }
        for index in indices
    ]


def _zero_ledger(
    round_trip_cost_bps: float,
    instruments: list[str],
    *,
    detailed_trades: bool = False,
) -> dict[str, object]:
    slippage_bps = 4.0 if round_trip_cost_bps == 24.0 else 16.0
    return {
        "broker": {
            "allocationFraction": 0.25,
            "breakEvenGrossReturnBps": _break_even(slippage_bps),
            "capacityHandling": "clip",
            "checkpointStrideBars": 288,
            "executionLabelHorizonBars": 12,
            "feeBpsPerSide": 8.0,
            "holdingPeriodBars": 12,
            "latencyBars": 0,
            "maxQuoteVolumeParticipation": 0.005,
            "minimumNotional": 10.0,
            "roundTripCostBps": round_trip_cost_bps,
            "slippageBpsPerSide": slippage_bps,
            "startingCash": 10_000.0,
        },
        "cashBarRate": 1.0,
        "checkpoints": _scheduled_checkpoints(),
        "evaluatedDecisions": 0,
        "exposureBars": 0,
        "finalCash": 10_000.0,
        "grossPnlReturn": 0.0,
        "leakageGuard": {
            "causalEpisodeBinding": True,
            "checkpointValuationBasis": CHECKPOINT_VALUATION_BASIS,
            "decisionToFillBars": 0,
            "executionCoordinate": "decision_rows",
            "nextDecisionRowFill": False,
            "predictionRowsAvailableBeforeDecision": True,
            "sameDecisionRowFill": True,
        },
        "maxDrawdown": 0.0,
        "maxDrawdownWitness": {
            "drawdown": 0.0,
            "peakAt": FIRST_REPLAY,
            "peakEquity": 10_000.0,
            "peakSource": "pre_replay_starting_cash",
            "troughAt": FIRST_REPLAY,
            "troughEquity": 10_000.0,
        },
        "netPnlByInstrument": {instrument: 0.0 for instrument in instruments},
        "netReturn": 0.0,
        "ordersClipped": 0,
        "ordersRejected": 0,
        "ordersSubmitted": 0,
        "policy": {
            "edgeBufferBps": 72.0,
            "minEntrySpacingBars": 12,
            "requiredGrossReturnBps": round_trip_cost_bps + 72.0,
        },
        "profitableTradeRate": 0.0,
        "qualifyingSignals": 0,
        "rejectionReasons": {},
        "simulatedDays": 30.0,
        "timeRows": 30 * 288,
        "totalCapacityClipNotional": 0.0,
        "totalEstimatedSlippageCost": 0.0,
        "totalFees": 0.0,
        "trades": [] if detailed_trades else 0,
        "tradesByInstrument": {instrument: 0 for instrument in instruments},
        "tradesPerDay": 0.0,
        "turnoverMultiple": 0.0,
    }


def _v6_report() -> dict[str, object]:
    instruments = ["BTC-USDT", "ETH-USDT", "SOL-USDT"]
    ordinary = _zero_ledger(24.0, instruments, detailed_trades=True)
    ordinary["developmentGatePassed"] = False
    ordinary["failures"] = [
        "trades_insufficient",
        "profitable_trade_rate_below_gate",
        "net_return_below_gate",
    ]
    stress = _zero_ledger(48.0, instruments)
    stress["developmentGatePassed"] = True
    stress["failures"] = []
    body: dict[str, object] = {
        "decision": "research_only",
        "dataset": {
            "assetRows": 3,
            "capacityVolumeSource": "confirmed_feature_source_bar",
            "firstReplayAt": FIRST_REPLAY,
            "instruments": instruments,
            "lastReplayAt": LAST_REPLAY,
            "replayTimeRows": 30 * 288,
        },
        "episodes": [
            {
                "assetRows": 3,
                "availableAt": "2023-08-01T01:05:00.000Z",
                "calibrationRows": 3,
                "calibrationStartAt": "2023-07-01T00:00:00.000Z",
                "calibrationStopAt": "2023-08-01T00:00:00.000Z",
                "episode": 0,
                "episodeId": "replay_episode_v6_test",
                "fitRows": 3,
                "fitStartAt": "2023-01-01T00:00:00.000Z",
                "fitStopAt": "2023-06-30T23:55:00.000Z",
                "labelCompleteAt": "2023-08-01T01:00:00.000Z",
                "replayRows": 30 * 288 * 3,
                "replayStartAt": "2023-08-01T01:10:00.000Z",
                "replayStopAt": "2023-08-31T01:05:00.000Z",
            }
        ],
        "execution": {
            "checkpointValuationBasis": CHECKPOINT_VALUATION_BASIS,
            "decisionToFillLatencyBars": 0,
            "engineSchemaVersion": "moheng.historical-replay.v3",
            "executionAllowlistChanged": False,
            "historicalReplayOnly": True,
            "orderCapability": False,
            "privateApi": False,
            "publicDataOnly": True,
        },
        "leakageAudit": {
            "checkpointValuationBasis": CHECKPOINT_VALUATION_BASIS,
            "decisionToFillBars": 0,
            "decisionTimestampEqualsEntryTimestamp": True,
            "entryBarVolumeUsedExPost": False,
            "featureSourceCloseToEntryBars": 0,
            "instantaneousDecisionFillAssumption": True,
            "nextCandleAfterFeatureSource": True,
            "sameSourceBarFillAllowed": False,
            "sameTimestampFillAllowed": True,
            "targetExecutionAligned": True,
        },
        "model": {
            "targetContract": {
                "decisionAt": "confirmed_bar_close_next_bar_open_boundary",
                "entryAt": "next_bar_open_same_timestamp",
                "exitAt": "entry_plus_12_bars_open",
                "labelHorizonBars": 12,
                "predictionUnit": "gross_return",
            }
        },
        "promotable": False,
        "protocol": {
            "developmentHistoryAlreadyObserved": True,
            "episodeCount": 1,
            "executionLabelHorizonBars": 12,
            "holdingBars": 12,
            "retrainEveryBars": 30 * 288,
            "scope": "retrospective-development-only",
            "trainBars": 365 * 288,
        },
        "result": {
            "chosenPolicy": {
                "edgeBufferBps": 72.0,
                "minEntrySpacingBars": 12,
                "requiredGrossReturnBps": 96.0,
            },
            "decision": "research_only",
            "developmentGatePassed": False,
            "executionSlice": {
                "decision": "research_only",
                "developmentGatePassed": False,
                "failures": [
                    "execution_slice_trades_insufficient",
                    "execution_slice_net_return_not_positive",
                    "execution_slice_stress_return_not_positive",
                ],
                "instrument": "BTC-USDT",
                "ordinary": _zero_ledger(24.0, ["BTC-USDT"]),
                "stress48Bps": _zero_ledger(48.0, ["BTC-USDT"]),
            },
            "ordinary": ordinary,
            "shadowDaysCredited": 0,
            "stress48Bps": stress,
        },
        "schemaVersion": "moheng.historical-replay-report.v4",
        "shadowDaysCredited": 0,
    }
    return _sealed(body)


def _v6_report_with_one_trade() -> dict[str, object]:
    report = _v6_report()
    report.pop("reportSha256")
    ordinary = report["result"]["ordinary"]  # type: ignore[index]
    quantity = 1.0
    raw_entry = 100.0
    raw_exit = 102.0
    fee_rate = 8.0 / 10_000.0
    slippage_rate = 4.0 / 10_000.0
    entry_fill = raw_entry * (1.0 + slippage_rate)
    exit_fill = raw_exit * (1.0 - slippage_rate)
    entry_notional = quantity * entry_fill
    exit_notional = quantity * exit_fill
    fees = (entry_notional + exit_notional) * fee_rate
    committed_cash = entry_notional * (1.0 + fee_rate)
    net_pnl = exit_notional * (1.0 - fee_rate) - committed_cash
    slippage = quantity * (entry_fill - raw_entry + raw_exit - exit_fill)
    final_cash = 10_000.0 + net_pnl
    ordinary.update(  # type: ignore[union-attr]
        {
            "cashBarRate": 1.0 - 12.0 / (30 * 288),
            "evaluatedDecisions": 1,
            "exposureBars": 12,
            "failures": ["trades_insufficient", "net_return_below_gate"],
            "finalCash": final_cash,
            "grossPnlReturn": quantity * (raw_exit - raw_entry) / 10_000.0,
            "netPnlByInstrument": {
                "BTC-USDT": net_pnl,
                "ETH-USDT": 0.0,
                "SOL-USDT": 0.0,
            },
            "netReturn": final_cash / 10_000.0 - 1.0,
            "ordersSubmitted": 1,
            "profitableTradeRate": 1.0,
            "qualifyingSignals": 1,
            "totalEstimatedSlippageCost": slippage,
            "totalFees": fees,
            "trades": [
                {
                    "enteredAt": "2023-08-02T02:00:00.000Z",
                    "entryFillPrice": entry_fill,
                    "episodeId": "replay_episode_v6_test",
                    "exitFillPrice": exit_fill,
                    "exitedAt": "2023-08-02T03:00:00.000Z",
                    "expectedGrossReturnBps": 100.0,
                    "fees": fees,
                    "grossReturn": raw_exit / raw_entry - 1.0,
                    "instrument": "BTC-USDT",
                    "netPnl": net_pnl,
                    "netReturnOnCommittedCash": net_pnl / committed_cash,
                    "quantity": quantity,
                    "signalAt": "2023-08-02T02:00:00.000Z",
                    "tradeId": "replay_trade_v6_test",
                }
            ],
            "tradesByInstrument": {
                "BTC-USDT": 1,
                "ETH-USDT": 0,
                "SOL-USDT": 0,
            },
            "tradesPerDay": 1.0 / 30.0,
            "turnoverMultiple": (entry_notional + exit_notional) / 10_000.0,
        }
    )
    ordinary["checkpoints"][-1].update(  # type: ignore[index]
        {"cash": final_cash, "equity": final_cash, "peakEquity": final_cash}
    )
    return _sealed(report)


def test_verifier_rejects_hash_tampering_before_reading_metrics() -> None:
    body = {"decision": "research_only"}
    report = {**body, "reportSha256": sha256_hex(canonical_json(body))}
    report["decision"] = "trade"

    with pytest.raises(ReplayVerificationError, match="canonical report hash"):
        verify_report(report)


@pytest.mark.parametrize(
    "schema_version",
    [
        "moheng.historical-replay-report.v1",
        "moheng.historical-replay-report.v2",
        "moheng.historical-replay-report.v3",
    ],
)
def test_verifier_explicitly_retires_legacy_report_schemas(
    schema_version: str,
) -> None:
    body = {
        "decision": "research_only",
        "promotable": False,
        "schemaVersion": schema_version,
        "shadowDaysCredited": 0,
    }
    report = {**body, "reportSha256": sha256_hex(canonical_json(body))}

    with pytest.raises(ReplayVerificationError, match="schemas are retired"):
        verify_report(report)


def test_verifier_accepts_only_complete_v6_canonical_contract() -> None:
    summary = verify_report(_v6_report())

    assert summary["verified"] is True
    assert summary["structuralLedgerVerified"] is True
    assert summary["sourceReplayVerified"] is False
    assert summary["coreDigestProjection"] == CORE_DIGEST_PROJECTION_VERSION
    assert len(summary["coreDigestSha256"]) == 64
    assert summary["executionSemantics"] == "corrected_next_open_boundary"
    assert summary["maxDrawdownVerification"] == {
        "exactMaxDrawdownRecomputed": True,
        "fullBarSourceReplayPerformed": False,
        "method": "embedded_peak_trough_witness_bound_to_exact_checkpoints",
        "reportedMaxDrawdown": 0.0,
    }
    assert MAX_REPLAY_JSON_BYTES == 32 * 1024 * 1024


def test_verifier_recomputes_a_nonzero_main_trade_ledger() -> None:
    summary = verify_report(_v6_report_with_one_trade())

    assert summary["trades"] == 1
    assert summary["structuralLedgerVerified"] is True


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("quantity", 0.0, "quantity and fill prices must be positive"),
        ("fees", 999.0, "trade fees do not reconcile"),
        ("grossReturn", 0.5, "trade gross return does not reconcile"),
        ("netPnl", 999.0, "trade net PnL does not reconcile"),
        (
            "netReturnOnCommittedCash",
            0.5,
            "trade return on committed cash does not reconcile",
        ),
    ],
)
def test_verifier_rejects_resealed_main_trade_ledger_tampering(
    field: str,
    value: float,
    message: str,
) -> None:
    report = _v6_report_with_one_trade()
    report["result"]["ordinary"]["trades"][0][field] = value  # type: ignore[index]

    with pytest.raises(ReplayVerificationError, match=message):
        verify_report(_resealed(report))


def test_verifier_rejects_missing_v6_engine_leakage_as_controlled_error() -> None:
    report = _v6_report()
    report.pop("reportSha256")
    report["result"]["ordinary"]["leakageGuard"] = None  # type: ignore[index]
    report = _sealed(report)

    with pytest.raises(ReplayVerificationError, match="leakage fields"):
        verify_report(report)  # type: ignore[arg-type]


def test_verifier_rejects_v6_stress_latency_tampering() -> None:
    report = _v6_report()
    report.pop("reportSha256")
    report["result"]["stress48Bps"]["broker"]["latencyBars"] = 1  # type: ignore[index]
    report = _sealed(report)

    with pytest.raises(ReplayVerificationError, match="V6 capacity"):
        verify_report(report)  # type: ignore[arg-type]


def test_verifier_rejects_resealed_btc_latency_tampering() -> None:
    report = _v6_report()
    report["result"]["executionSlice"]["ordinary"]["broker"]["latencyBars"] = 1  # type: ignore[index]

    with pytest.raises(ReplayVerificationError, match="BTC ordinary V6 capacity"):
        verify_report(_resealed(report))


def test_verifier_rejects_resealed_btc_net_return_tampering() -> None:
    report = _v6_report()
    report["result"]["executionSlice"]["ordinary"]["netReturn"] = 0.01  # type: ignore[index]

    with pytest.raises(ReplayVerificationError, match="net return does not reconcile"):
        verify_report(_resealed(report))


def test_verifier_rejects_resealed_btc_fee_tampering() -> None:
    report = _v6_report()
    report["result"]["executionSlice"]["ordinary"]["totalFees"] = 1.0  # type: ignore[index]

    with pytest.raises(ReplayVerificationError, match="fees do not reconcile"):
        verify_report(_resealed(report))


def test_verifier_rejects_resealed_btc_slippage_tampering() -> None:
    report = _v6_report()
    report["result"]["executionSlice"]["ordinary"][  # type: ignore[index]
        "totalEstimatedSlippageCost"
    ] = 1.0

    with pytest.raises(ReplayVerificationError, match="gross PnL, fees, slippage"):
        verify_report(_resealed(report))


def test_verifier_rejects_resealed_btc_capacity_tampering() -> None:
    report = _v6_report()
    report["result"]["executionSlice"]["ordinary"]["broker"][  # type: ignore[index]
        "capacityHandling"
    ] = "reject"

    with pytest.raises(ReplayVerificationError, match="BTC ordinary V6 capacity"):
        verify_report(_resealed(report))


@pytest.mark.parametrize(
    ("path", "field", "value", "message"),
    [
        (("result", "ordinary", "broker"), "feeBpsPerSide", 7.0, "fee contract"),
        (
            ("result", "stress48Bps", "broker"),
            "slippageBpsPerSide",
            15.0,
            "slippage contract",
        ),
        (
            ("result", "executionSlice", "ordinary", "broker"),
            "breakEvenGrossReturnBps",
            24.0,
            "break-even return contract",
        ),
        (
            ("result", "executionSlice", "stress48Bps", "broker"),
            "allocationFraction",
            0.5,
            "allocation contract",
        ),
        (
            ("result", "ordinary", "broker"),
            "maxQuoteVolumeParticipation",
            0.01,
            "quote-volume participation contract",
        ),
        (
            ("result", "stress48Bps", "broker"),
            "checkpointStrideBars",
            144,
            "V6 capacity",
        ),
        (
            ("result", "executionSlice", "ordinary", "broker"),
            "latencyBars",
            False,
            "V6 capacity",
        ),
        (
            ("result", "executionSlice", "stress48Bps", "policy"),
            "requiredGrossReturnBps",
            119.0,
            "required gross return contract",
        ),
    ],
)
def test_verifier_rejects_resealed_fixed_v6_contract_tampering(
    path: tuple[str, ...],
    field: str,
    value: object,
    message: str,
) -> None:
    report = _v6_report()
    target: object = report
    for key in path:
        assert isinstance(target, dict)
        target = target[key]
    assert isinstance(target, dict)
    target[field] = value

    with pytest.raises(ReplayVerificationError, match=message):
        verify_report(_resealed(report))


def test_verifier_rejects_resealed_missing_v6_policy() -> None:
    report = _v6_report()
    report["result"]["ordinary"].pop("policy")  # type: ignore[index]

    with pytest.raises(ReplayVerificationError, match="policy fields"):
        verify_report(_resealed(report))


def test_verifier_rejects_resealed_result_policy_mismatch() -> None:
    report = _v6_report()
    report["result"]["chosenPolicy"]["edgeBufferBps"] = 48.0  # type: ignore[index]

    with pytest.raises(ReplayVerificationError, match="chosen policy"):
        verify_report(_resealed(report))


@pytest.mark.parametrize(
    "path",
    [
        ("execution",),
        ("leakageAudit",),
        ("result", "ordinary", "leakageGuard"),
        ("result", "stress48Bps", "leakageGuard"),
        ("result", "executionSlice", "ordinary", "leakageGuard"),
        ("result", "executionSlice", "stress48Bps", "leakageGuard"),
    ],
)
def test_verifier_rejects_resealed_missing_checkpoint_valuation_marker(
    path: tuple[str, ...],
) -> None:
    report = _v6_report()
    target: object = report
    for key in path:
        assert isinstance(target, dict)
        target = target[key]
    assert isinstance(target, dict)
    target["checkpointValuationBasis"] = "close_after_checkpoint_boundary"

    with pytest.raises(
        ReplayVerificationError,
        match="checkpoint|leakage|corrected execution target",
    ):
        verify_report(_resealed(report))


def test_verifier_recomputes_resealed_btc_slice_gate() -> None:
    report = _v6_report()
    execution_slice = report["result"]["executionSlice"]  # type: ignore[index]
    execution_slice["failures"] = []  # type: ignore[index]
    execution_slice["developmentGatePassed"] = True  # type: ignore[index]

    with pytest.raises(ReplayVerificationError, match="execution slice gate"):
        verify_report(_resealed(report))


@pytest.mark.parametrize(
    ("target", "message"),
    [
        ("ordinary", "ordinary development gate"),
        ("stress", "stress development gate"),
        ("combined", "combined development gate"),
    ],
)
def test_verifier_recomputes_resealed_main_gates(
    target: str,
    message: str,
) -> None:
    report = _v6_report()
    result = report["result"]  # type: ignore[index]
    if target == "ordinary":
        result["ordinary"]["failures"] = []  # type: ignore[index]
        result["ordinary"]["developmentGatePassed"] = True  # type: ignore[index]
    elif target == "stress":
        result["stress48Bps"]["failures"] = [  # type: ignore[index]
            "stress_net_return_below_zero"
        ]
        result["stress48Bps"]["developmentGatePassed"] = False  # type: ignore[index]
    else:
        result["developmentGatePassed"] = True  # type: ignore[index]

    with pytest.raises(ReplayVerificationError, match=message):
        verify_report(_resealed(report))


@pytest.mark.parametrize(
    "ledger_path",
    [
        ("result", "ordinary"),
        ("result", "stress48Bps"),
        ("result", "executionSlice", "ordinary"),
        ("result", "executionSlice", "stress48Bps"),
    ],
)
def test_verifier_rejects_resealed_drawdown_not_present_in_checkpoint_series(
    ledger_path: tuple[str, ...],
) -> None:
    report = _v6_report()
    ledger: object = report
    for key in ledger_path:
        assert isinstance(ledger, dict)
        ledger = ledger[key]
    assert isinstance(ledger, dict)
    checkpoint = ledger["checkpoints"][1]
    ledger["maxDrawdown"] = 0.10
    ledger["maxDrawdownWitness"] = {
        "drawdown": 0.10,
        "peakAt": FIRST_REPLAY,
        "peakEquity": 10_000.0,
        "peakSource": "pre_replay_starting_cash",
        "troughAt": checkpoint["at"],
        "troughEquity": 9_000.0,
    }

    with pytest.raises(ReplayVerificationError, match="witness trough equity"):
        verify_report(_resealed(report))


@pytest.mark.parametrize(
    "ledger_path",
    [
        ("result", "stress48Bps"),
        ("result", "executionSlice", "ordinary"),
        ("result", "executionSlice", "stress48Bps"),
    ],
)
def test_verifier_requires_checkpoint_series_for_every_executable_ledger(
    ledger_path: tuple[str, ...],
) -> None:
    report = _v6_report()
    ledger: object = report
    for key in ledger_path:
        assert isinstance(ledger, dict)
        ledger = ledger[key]
    assert isinstance(ledger, dict)
    ledger.pop("checkpoints")

    with pytest.raises(ReplayVerificationError, match="checkpoint series is missing"):
        verify_report(_resealed(report))


def test_verifier_rejects_resealed_checkpoint_peak_arithmetic_tampering() -> None:
    report = _v6_report()
    checkpoint = report["result"]["stress48Bps"]["checkpoints"][0]  # type: ignore[index]
    checkpoint["peakEquity"] = 10_001.0

    with pytest.raises(ReplayVerificationError, match="checkpoint drawdown"):
        verify_report(_resealed(report))


def test_verifier_rejects_trade_outside_bound_episode_signal_window() -> None:
    report = _v6_report_with_one_trade()
    trade = report["result"]["ordinary"]["trades"][0]  # type: ignore[index]
    trade.update(
        {
            "signalAt": "2023-07-31T23:00:00.000Z",
            "enteredAt": "2023-07-31T23:00:00.000Z",
            "exitedAt": "2023-08-01T00:00:00.000Z",
        }
    )

    with pytest.raises(ReplayVerificationError, match="outside their replay or episode"):
        verify_report(_resealed(report))


def test_verifier_rejects_trade_timestamp_off_the_global_five_minute_grid() -> None:
    report = _v6_report_with_one_trade()
    trade = report["result"]["ordinary"]["trades"][0]  # type: ignore[index]
    trade.update(
        {
            "signalAt": "2023-08-02T02:01:00.000Z",
            "enteredAt": "2023-08-02T02:01:00.000Z",
            "exitedAt": "2023-08-02T03:01:00.000Z",
        }
    )

    with pytest.raises(ReplayVerificationError, match="outside their replay or episode"):
        verify_report(_resealed(report))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("expectedGrossReturnBps", 96.0),
        ("quantity", 30.0),
    ],
)
def test_verifier_rejects_trade_outside_fixed_policy_or_allocation(
    field: str,
    value: float,
) -> None:
    report = _v6_report_with_one_trade()
    report["result"]["ordinary"]["trades"][0][field] = value  # type: ignore[index]

    with pytest.raises(
        ReplayVerificationError,
        match="fixed allocation, notional, or policy threshold",
    ):
        verify_report(_resealed(report))


def test_verifier_rejects_unique_but_overlapping_trade_intervals() -> None:
    report = _v6_report_with_one_trade()
    trades = report["result"]["ordinary"]["trades"]  # type: ignore[index]
    overlapping = copy.deepcopy(trades[0])
    overlapping["tradeId"] = "replay_trade_v6_overlap"
    trades.append(overlapping)

    with pytest.raises(ReplayVerificationError, match="trade intervals overlap"):
        verify_report(_resealed(report))


def test_verifier_cli_accepts_valid_replay_above_legacy_five_megabyte_limit(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    replay_root = tmp_path / "replays"
    replay_root.mkdir()
    report = _v6_report()
    report["padding"] = "x" * (6 * 1024 * 1024)
    report = _resealed(report)
    path = replay_root / "historical-replay-v6-large.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    assert 5 * 1024 * 1024 < path.stat().st_size < MAX_REPLAY_JSON_BYTES

    monkeypatch.setattr(verifier_module, "REPLAY_ROOT", replay_root.resolve())
    monkeypatch.setattr(sys, "argv", ["verify-historical-replay", str(path)])

    assert verifier_module.main() == 0
    assert json.loads(capsys.readouterr().out)["verified"] is True


def test_verifier_cli_rejects_replay_above_32_mib_limit(
    tmp_path,
    monkeypatch,
) -> None:
    replay_root = tmp_path / "replays"
    replay_root.mkdir()
    path = replay_root / "historical-replay-v6-too-large.json"
    with path.open("wb") as output:
        output.seek(MAX_REPLAY_JSON_BYTES)
        output.write(b"{}")
    assert path.stat().st_size > MAX_REPLAY_JSON_BYTES

    monkeypatch.setattr(verifier_module, "REPLAY_ROOT", replay_root.resolve())
    monkeypatch.setattr(sys, "argv", ["verify-historical-replay", str(path)])

    with pytest.raises(ReplayVerificationError, match="report file is invalid"):
        verifier_module.main()


def test_core_digest_excludes_only_named_runtime_fields() -> None:
    report = _v6_report()
    baseline = deterministic_core_digest(report)
    runtime_changed = copy.deepcopy(report)
    runtime_changed.update(
        {
            "startedAt": "2099-01-01T00:00:00Z",
            "completedAt": "2099-01-01T01:00:00Z",
            "timing": {"elapsedSeconds": 999.0},
            "replayId": "runtime-only-id",
            "reportSha256": "f" * 64,
        }
    )
    runtime_changed["episodes"][0]["trainingSeconds"] = 999.0  # type: ignore[index]

    assert deterministic_core_digest(runtime_changed) == baseline

    semantic_changed = copy.deepcopy(runtime_changed)
    semantic_changed["result"]["ordinary"]["netReturn"] = 0.01  # type: ignore[index]
    assert deterministic_core_digest(semantic_changed) != baseline
