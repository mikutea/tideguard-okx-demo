from __future__ import annotations

import hmac
import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .config import ALLOWED_INSTRUMENTS
from .ml.strategy import canonical_json, sha256_hex


RESEARCH_MONITOR_SCHEMA = "moheng.research-monitor.v1"
MAX_JSON_BYTES = 1_000_000
MAX_REPLAY_JSON_BYTES = 32 * 1024 * 1024
V6_REPORT_SCHEMA = "moheng.historical-replay-report.v4"
V6_CHECKPOINT_VALUATION_BASIS = "current_bar_open_at_checkpoint_boundary"
FIVE_MINUTES_MS = 300_000
EXPECTED_REPLAY_BARS = 30 * 288
EXPECTED_HOLDING_BARS = 12
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


def configured_research_data_dir() -> Path | None:
    override = os.environ.get("MOHENG_RESEARCH_DATA_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    project_candidate = Path(__file__).resolve().parents[3] / ".research-data"
    return project_candidate.resolve() if project_candidate.is_dir() else None


def _iso_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _read_json(
    path: Path, *, max_bytes: int = MAX_JSON_BYTES
) -> dict[str, Any] | None:
    try:
        size = path.stat().st_size
        if size < 2 or size > max_bytes or not path.is_file():
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        return None
    return dict(value) if isinstance(value, dict) else None


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size if path.is_file() else 0
    except OSError:
        return 0


def _valid_hash(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _canonical_hash_matches(
    value: Mapping[str, Any], stored_hash: object, *, excluded: set[str]
) -> bool:
    if not _valid_hash(stored_hash):
        return False
    body = {key: item for key, item in value.items() if key not in excluded}
    try:
        computed = sha256_hex(canonical_json(body))
    except (TypeError, ValueError):
        return False
    return hmac.compare_digest(str(stored_hash), computed)


def _verified_universe(report: Mapping[str, Any]) -> bool:
    stored_report_hash = report.get("reportSha256")
    if not _canonical_hash_matches(
        report, stored_report_hash, excluded={"reportSha256"}
    ):
        return False
    snapshot = report.get("snapshot")
    if not isinstance(snapshot, dict):
        return False
    stored_snapshot_hash = snapshot.get("sha256")
    if not _valid_hash(stored_snapshot_hash):
        return False
    return _canonical_hash_matches(
        snapshot, stored_snapshot_hash, excluded={"sha256"}
    )


def _integer(value: object, default: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return default
    return value


def _number(value: object) -> float | None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        return None
    return float(value)


def _close(actual: object, expected: float) -> bool:
    value = _number(actual)
    return value is not None and math.isclose(
        value, expected, rel_tol=1e-10, abs_tol=1e-7
    )


def _timestamp_ms(value: object) -> int | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return round(parsed.astimezone(timezone.utc).timestamp() * 1_000)


def _break_even_gross_return_bps(slippage_bps_per_side: float) -> float:
    fee = EXPECTED_FEE_BPS_PER_SIDE / 10_000.0
    slippage = slippage_bps_per_side / 10_000.0
    multiplier = ((1.0 + slippage) * (1.0 + fee)) / (
        (1.0 - slippage) * (1.0 - fee)
    )
    return (multiplier - 1.0) * 10_000.0


def _v6_leakage_contract(value: object) -> bool:
    return bool(
        isinstance(value, dict)
        and set(value) == _LEAKAGE_KEYS
        and value.get("causalEpisodeBinding") is True
        and value.get("checkpointValuationBasis")
        == V6_CHECKPOINT_VALUATION_BASIS
        and type(value.get("decisionToFillBars")) is int
        and value.get("decisionToFillBars") == 0
        and value.get("executionCoordinate") == "decision_rows"
        and value.get("nextDecisionRowFill") is False
        and value.get("predictionRowsAvailableBeforeDecision") is True
        and value.get("sameDecisionRowFill") is True
    )


def _v6_ledger_contract(
    value: object,
    *,
    round_trip_cost_bps: float,
    slippage_bps_per_side: float,
) -> bool:
    if not isinstance(value, dict):
        return False
    broker = value.get("broker")
    policy = value.get("policy")
    if (
        not isinstance(broker, dict)
        or set(broker) != _BROKER_KEYS
        or not isinstance(policy, dict)
        or set(policy) != _POLICY_KEYS
        or not _v6_leakage_contract(value.get("leakageGuard"))
    ):
        return False
    return bool(
        broker.get("capacityHandling") == "clip"
        and type(broker.get("executionLabelHorizonBars")) is int
        and broker.get("executionLabelHorizonBars") == EXPECTED_HOLDING_BARS
        and type(broker.get("holdingPeriodBars")) is int
        and broker.get("holdingPeriodBars") == EXPECTED_HOLDING_BARS
        and type(broker.get("latencyBars")) is int
        and broker.get("latencyBars") == 0
        and type(broker.get("checkpointStrideBars")) is int
        and broker.get("checkpointStrideBars") == EXPECTED_CHECKPOINT_STRIDE_BARS
        and _close(broker.get("startingCash"), EXPECTED_STARTING_CASH)
        and _close(
            broker.get("allocationFraction"), EXPECTED_ALLOCATION_FRACTION
        )
        and _close(broker.get("feeBpsPerSide"), EXPECTED_FEE_BPS_PER_SIDE)
        and _close(broker.get("slippageBpsPerSide"), slippage_bps_per_side)
        and _close(
            broker.get("maxQuoteVolumeParticipation"),
            EXPECTED_MAX_QUOTE_VOLUME_PARTICIPATION,
        )
        and _close(broker.get("minimumNotional"), EXPECTED_MINIMUM_NOTIONAL)
        and _close(broker.get("roundTripCostBps"), round_trip_cost_bps)
        and _close(
            broker.get("breakEvenGrossReturnBps"),
            _break_even_gross_return_bps(slippage_bps_per_side),
        )
        and type(policy.get("minEntrySpacingBars")) is int
        and policy.get("minEntrySpacingBars")
        == EXPECTED_MIN_ENTRY_SPACING_BARS
        and _close(policy.get("edgeBufferBps"), EXPECTED_EDGE_BUFFER_BPS)
        and _close(
            policy.get("requiredGrossReturnBps"),
            round_trip_cost_bps + EXPECTED_EDGE_BUFFER_BPS,
        )
    )


def _exact_failures(value: object, expected: list[str]) -> bool:
    return bool(
        isinstance(value, list)
        and all(isinstance(item, str) and item for item in value)
        and len(value) == len(set(value))
        and value == expected
    )


def _development_failures(
    ledger: Mapping[str, Any], *, trade_count: int
) -> list[str] | None:
    profitable_rate = _number(ledger.get("profitableTradeRate"))
    gross_return = _number(ledger.get("grossPnlReturn"))
    net_return = _number(ledger.get("netReturn"))
    max_drawdown = _number(ledger.get("maxDrawdown"))
    trades_per_day = _number(ledger.get("tradesPerDay"))
    submitted = ledger.get("ordersSubmitted")
    rejected = ledger.get("ordersRejected")
    if (
        profitable_rate is None
        or gross_return is None
        or net_return is None
        or max_drawdown is None
        or trades_per_day is None
        or type(submitted) is not int
        or submitted < 0
        or type(rejected) is not int
        or rejected < 0
        or rejected > submitted
    ):
        return None
    failures: list[str] = []
    if trade_count < 20:
        failures.append("trades_insufficient")
    if profitable_rate < 0.50:
        failures.append("profitable_trade_rate_below_gate")
    if gross_return < 0.0:
        failures.append("gross_return_below_gate")
    if net_return < 0.005:
        failures.append("net_return_below_gate")
    if max_drawdown > 0.10:
        failures.append("drawdown_above_gate")
    if trades_per_day > 3.0:
        failures.append("turnover_above_gate")
    if submitted and rejected / submitted > 0.05:
        failures.append("fill_rejection_rate_above_gate")
    return failures


def _stress_failures(ledger: Mapping[str, Any]) -> list[str] | None:
    gross_return = _number(ledger.get("grossPnlReturn"))
    net_return = _number(ledger.get("netReturn"))
    max_drawdown = _number(ledger.get("maxDrawdown"))
    if gross_return is None or net_return is None or max_drawdown is None:
        return None
    failures: list[str] = []
    if gross_return < 0.0:
        failures.append("stress_gross_return_below_zero")
    if net_return < 0.0:
        failures.append("stress_net_return_below_zero")
    if max_drawdown > 0.15:
        failures.append("stress_drawdown_above_gate")
    return failures


def _slice_failures(
    ordinary: Mapping[str, Any],
    stress: Mapping[str, Any],
    *,
    trade_count: int,
) -> list[str] | None:
    ordinary_return = _number(ordinary.get("netReturn"))
    stress_return = _number(stress.get("netReturn"))
    max_drawdown = _number(ordinary.get("maxDrawdown"))
    if ordinary_return is None or stress_return is None or max_drawdown is None:
        return None
    failures: list[str] = []
    if trade_count < 20:
        failures.append("execution_slice_trades_insufficient")
    if ordinary_return <= 0.0:
        failures.append("execution_slice_net_return_not_positive")
    if stress_return <= 0.0:
        failures.append("execution_slice_stress_return_not_positive")
    if max_drawdown > 0.10:
        failures.append("execution_slice_drawdown_above_gate")
    return failures


def _v6_drawdown_contract(
    ledger: Mapping[str, Any],
    *,
    first_replay_ms: int,
    last_replay_ms: int,
    allowed_instruments: set[str],
    require_checkpoints: bool,
) -> tuple[bool, dict[str, Any]]:
    summary = {
        "exactMaxDrawdownRecomputed": False,
        "fullBarSourceReplayPerformed": False,
        "method": "embedded_peak_trough_witness_bound_to_exact_checkpoints",
        "reportedMaxDrawdown": ledger.get("maxDrawdown"),
    }
    witness = ledger.get("maxDrawdownWitness")
    if (
        not isinstance(witness, dict)
        or set(witness)
        != {
            "drawdown",
            "peakAt",
            "peakEquity",
            "peakSource",
            "troughAt",
            "troughEquity",
        }
    ):
        return False, summary
    reported = _number(ledger.get("maxDrawdown"))
    witness_drawdown = _number(witness.get("drawdown"))
    peak_equity = _number(witness.get("peakEquity"))
    trough_equity = _number(witness.get("troughEquity"))
    peak_at = _timestamp_ms(witness.get("peakAt"))
    trough_at = _timestamp_ms(witness.get("troughAt"))
    peak_source = witness.get("peakSource")
    if (
        reported is None
        or witness_drawdown is None
        or peak_equity is None
        or trough_equity is None
        or peak_at is None
        or trough_at is None
        or peak_equity <= 0
        or not 0 <= trough_equity <= peak_equity
        or peak_source not in {"pre_replay_starting_cash", "checkpoint"}
        or not first_replay_ms <= peak_at <= trough_at <= last_replay_ms
        or (peak_at - first_replay_ms) % FIVE_MINUTES_MS != 0
        or (trough_at - first_replay_ms) % FIVE_MINUTES_MS != 0
    ):
        return False, summary
    calculated = (peak_equity - trough_equity) / peak_equity
    if not _close(witness_drawdown, calculated) or not _close(reported, calculated):
        return False, summary
    broker = ledger.get("broker")
    if peak_source == "pre_replay_starting_cash" and (
        not isinstance(broker, dict)
        or not _close(peak_equity, EXPECTED_STARTING_CASH)
        or peak_at != first_replay_ms
    ):
        return False, summary
    if not require_checkpoints:
        return True, summary

    checkpoints_value = ledger.get("checkpoints")
    if not isinstance(checkpoints_value, list) or len(checkpoints_value) < 2:
        return False, summary
    checkpoint_times: list[int] = []
    checkpoint_by_time: dict[int, Mapping[str, Any]] = {}
    previous_peak = EXPECTED_STARTING_CASH
    checkpoint_drawdowns: list[float] = []
    for item in checkpoints_value:
        if (
            not isinstance(item, dict)
            or set(item)
            != {
                "at",
                "cash",
                "drawdown",
                "equity",
                "peakEquity",
                "positionInstrument",
                "positionMarketValue",
            }
        ):
            return False, summary
        at = _timestamp_ms(item.get("at"))
        cash = _number(item.get("cash"))
        equity = _number(item.get("equity"))
        item_peak = _number(item.get("peakEquity"))
        position_value = _number(item.get("positionMarketValue"))
        item_drawdown = _number(item.get("drawdown"))
        instrument = item.get("positionInstrument")
        if (
            at is None
            or cash is None
            or equity is None
            or item_peak is None
            or position_value is None
            or item_drawdown is None
            or not first_replay_ms <= at <= last_replay_ms
            or (at - first_replay_ms) % FIVE_MINUTES_MS != 0
            or cash < 0
            or equity < 0
            or position_value < 0
            or item_peak < max(previous_peak, equity)
            or not 0 <= item_drawdown <= 1
            or not _close(equity, cash + position_value)
            or not _close(item_drawdown, (item_peak - equity) / item_peak)
            or not (
                (instrument is None and position_value == 0)
                or (instrument in allowed_instruments and position_value > 0)
            )
        ):
            return False, summary
        checkpoint_times.append(at)
        checkpoint_by_time[at] = item
        checkpoint_drawdowns.append(item_drawdown)
        previous_peak = item_peak
    if (
        len(checkpoint_by_time) != len(checkpoint_times)
        or any(
            left >= right
            for left, right in zip(checkpoint_times, checkpoint_times[1:])
        )
    ):
        return False, summary
    time_rows = ledger.get("timeRows")
    expected_time_rows = (last_replay_ms - first_replay_ms) // FIVE_MINUTES_MS + 1
    if type(time_rows) is not int or time_rows != expected_time_rows:
        return False, summary
    scheduled_times = {
        first_replay_ms + index * FIVE_MINUTES_MS
        for index in range(0, time_rows, EXPECTED_CHECKPOINT_STRIDE_BARS)
    }
    scheduled_times.add(last_replay_ms)
    actual_times = set(checkpoint_times)
    if (
        not scheduled_times <= actual_times
        or not actual_times - scheduled_times <= {peak_at, trough_at}
    ):
        return False, summary
    trough = checkpoint_by_time.get(trough_at)
    if (
        trough is None
        or not _close(trough.get("equity"), trough_equity)
        or not _close(trough.get("peakEquity"), peak_equity)
        or not _close(trough.get("drawdown"), witness_drawdown)
    ):
        return False, summary
    if peak_source == "checkpoint":
        peak = checkpoint_by_time.get(peak_at)
        if (
            peak is None
            or not _close(peak.get("equity"), peak_equity)
            or not _close(peak.get("peakEquity"), peak_equity)
            or not _close(peak.get("drawdown"), 0.0)
        ):
            return False, summary
    if not _close(max(checkpoint_drawdowns), reported):
        return False, summary
    final = checkpoints_value[-1]
    final_cash = _number(ledger.get("finalCash"))
    if (
        final_cash is None
        or checkpoint_times[-1] != last_replay_ms
        or not _close(final.get("cash"), final_cash)
        or not _close(final.get("equity"), final_cash)
        or final.get("positionInstrument") is not None
        or not _close(final.get("positionMarketValue"), 0.0)
    ):
        return False, summary
    summary["exactMaxDrawdownRecomputed"] = True
    return True, summary


def _instrument_status(
    member: Mapping[str, Any],
    progress: Mapping[str, Any],
) -> dict[str, Any]:
    instrument = str(member.get("instrument", ""))
    entry_value = progress.get(instrument)
    entry = entry_value if isinstance(entry_value, dict) else {}
    first_open = entry.get("firstOpenTsMs")
    terminal_cursor = entry.get("lastTerminalCursor")
    current_oldest = terminal_cursor if isinstance(terminal_cursor, int) else first_open
    last_open = entry.get("lastOpenTsMs")
    coverage_days = 0.0
    if isinstance(current_oldest, int) and isinstance(last_open, int) and last_open >= current_oldest:
        coverage_days = (last_open - current_oldest) / 86_400_000
    return {
        "backfillComplete": entry.get("backfillComplete") is True,
        "coverageDays": coverage_days,
        "firstOpenTsMs": current_oldest if isinstance(current_oldest, int) else None,
        "instrument": instrument,
        "lastOpenTsMs": last_open if isinstance(last_open, int) else None,
        "listedAt": member.get("listedAt") if isinstance(member.get("listedAt"), str) else None,
        "missingBars": _integer(entry.get("missingBars")),
        "pagesConsumed": _integer(entry.get("pagesConsumed")),
        "rowsInsertedThisRun": _integer(entry.get("rowsInserted")),
        "stage": str(entry.get("stage", "waiting")),
        "storedRowsAtCheckpoint": _integer(entry.get("storedRows")),
        "unresolvedConflicts": _integer(entry.get("unresolvedConflicts")),
    }


def _latest_cohort(root: Path) -> dict[str, Any] | None:
    cohort_root = root / "cohorts"
    if not cohort_root.is_dir():
        return None
    candidates: list[tuple[int, Path]] = []
    for path in cohort_root.glob("cohort_*/manifest.json"):
        try:
            candidates.append((path.stat().st_mtime_ns, path))
        except OSError:
            continue
    for _, path in sorted(candidates, reverse=True)[:100]:
        value = _read_json(path)
        if value is None:
            continue
        content_hash = value.get("contentSha256")
        manifest_valid = bool(
            _canonical_hash_matches(
                value,
                content_hash,
                excluded={"cohortId", "contentSha256", "createdAt", "promotable"},
            )
            and value.get("cohortId") == f"cohort_{str(content_hash)[:24]}"
            and path.parent.name == value.get("cohortId")
            and value.get("promotable") is False
        )
        return {
            "blockers": value.get("promotionBlockers") if isinstance(value.get("promotionBlockers"), list) else [],
            "cohortId": value.get("cohortId"),
            "contentSha256": content_hash,
            "createdAt": value.get("createdAt"),
            "instruments": value.get("instruments") if isinstance(value.get("instruments"), list) else [],
            "manifestValid": manifest_valid,
            "promotable": value.get("promotable") is True,
            "rowCount": _integer(value.get("rowCount")),
        }
    return None


def _latest_benchmark(root: Path) -> dict[str, Any] | None:
    benchmark_root = root / "benchmarks"
    if not benchmark_root.is_dir():
        return None
    candidates: list[tuple[int, Path]] = []
    for path in benchmark_root.glob("multi-asset-*.json"):
        try:
            candidates.append((path.stat().st_mtime_ns, path))
        except OSError:
            continue
    for _, path in sorted(candidates, reverse=True)[:100]:
        value = _read_json(path)
        if value is None:
            continue
        report_hash = value.get("reportSha256")
        schema_version = value.get("schemaVersion")
        valid = bool(
            _canonical_hash_matches(
                value, report_hash, excluded={"reportSha256"}
            )
            and value.get("promotable") is False
            and schema_version
            in {
                "moheng.multi-asset-research.v1",
                "moheng.multi-asset-research.v2",
            }
        )
        dataset = value.get("dataset")
        dataset_value = dataset if isinstance(dataset, dict) else {}
        evaluation = value.get("evaluation")
        evaluation_value = evaluation if isinstance(evaluation, dict) else {}
        results_value = value.get("results")
        results = results_value if isinstance(results_value, list) else []
        summaries = []
        benchmark_blockers: list[str] = []
        for item in results[:20]:
            if not isinstance(item, dict):
                continue
            ordinary = item.get("ordinary")
            ordinary_value = ordinary if isinstance(ordinary, dict) else {}
            policy = item.get("chosenPolicy")
            policy_value = policy if isinstance(policy, dict) else {}
            calibration = item.get("calibration")
            calibration_value = calibration if isinstance(calibration, dict) else {}
            item_blockers = item.get("promotionBlockers")
            if isinstance(item_blockers, list):
                benchmark_blockers.extend(
                    blocker for blocker in item_blockers if isinstance(blocker, str)
                )
            summaries.append(
                {
                    "calibrationImproved": calibration_value.get("improved") is True,
                    "cashBarRate": ordinary_value.get("cashBarRate"),
                    "chosenPolicy": {
                        "edgeBufferBps": policy_value.get("edgeBufferBps"),
                        "minEntrySpacingBars": policy_value.get(
                            "minEntrySpacingBars"
                        ),
                        "requiredGrossReturnBps": policy_value.get(
                            "requiredGrossReturnBps"
                        ),
                    }
                    if policy_value
                    else None,
                    "chosenThreshold": item.get("chosenThreshold"),
                    "developmentGatePassed": item.get("developmentGatePassed") is True,
                    "exploratoryGatePassed": item.get("exploratoryGatePassed") is True,
                    "family": item.get("family"),
                    "grossReturn": ordinary_value.get("grossReturn"),
                    "maxDrawdown": ordinary_value.get("maxDrawdown"),
                    "maxInstrumentTradeShare": ordinary_value.get(
                        "maxInstrumentTradeShare"
                    ),
                    "netReturn": ordinary_value.get("netReturn"),
                    "trades": _integer(ordinary_value.get("trades")),
                    "tradesPerDay": ordinary_value.get("tradesPerDay"),
                }
            )
        return {
            "benchmarkId": value.get("benchmarkId"),
            "blockers": list(dict.fromkeys(benchmark_blockers)),
            "cohortId": dataset_value.get("cohortId"),
            "completedAt": value.get("completedAt"),
            "developmentGatePassed": any(
                item["developmentGatePassed"] for item in summaries
            ),
            "evaluationScope": evaluation_value.get("scope"),
            "exploratoryGatePassed": any(
                item["exploratoryGatePassed"] for item in summaries
            ),
            "promotable": value.get("promotable") is True,
            "reportSha256": report_hash,
            "results": summaries,
            "schemaVersion": schema_version,
            "valid": valid,
        }
    return None


def _latest_replay(root: Path) -> dict[str, Any] | None:
    replay_root = root / "replays"
    if not replay_root.is_dir():
        return None
    candidates: list[tuple[int, int, str, Path]] = []
    for priority, pattern in (
        (6, "historical-replay-v6-*.json"),
        (5, "historical-replay-v5-*.json"),
        (4, "historical-replay-v4-*.json"),
        (3, "historical-replay-v3-*.json"),
    ):
        for path in replay_root.glob(pattern):
            try:
                candidates.append(
                    (priority, path.stat().st_mtime_ns, path.name, path)
                )
            except OSError:
                continue
    for _, _, _, path in sorted(candidates, reverse=True)[:100]:
        value = _read_json(path, max_bytes=MAX_REPLAY_JSON_BYTES)
        if value is None:
            continue
        report_hash = value.get("reportSha256")
        execution = value.get("execution")
        execution_value = execution if isinstance(execution, dict) else {}
        schema_version = value.get("schemaVersion")
        leakage = value.get("leakageAudit")
        leakage_value = leakage if isinstance(leakage, dict) else {}
        model = value.get("model")
        model_value = model if isinstance(model, dict) else {}
        target = model_value.get("targetContract")
        target_value = target if isinstance(target, dict) else {}
        dataset = value.get("dataset")
        dataset_value = dataset if isinstance(dataset, dict) else {}
        protocol = value.get("protocol")
        protocol_value = protocol if isinstance(protocol, dict) else {}
        timing = value.get("timing")
        timing_value = timing if isinstance(timing, dict) else {}
        result = value.get("result")
        result_value = result if isinstance(result, dict) else {}
        ordinary = result_value.get("ordinary")
        ordinary_value = ordinary if isinstance(ordinary, dict) else {}
        ordinary_broker = ordinary_value.get("broker")
        ordinary_broker_value = (
            ordinary_broker if isinstance(ordinary_broker, dict) else {}
        )
        stress = result_value.get("stress48Bps")
        stress_value = stress if isinstance(stress, dict) else {}
        stress_broker = stress_value.get("broker")
        stress_broker_value = (
            stress_broker if isinstance(stress_broker, dict) else {}
        )
        ordinary_leakage = ordinary_value.get("leakageGuard")
        ordinary_leakage_value = (
            ordinary_leakage if isinstance(ordinary_leakage, dict) else {}
        )
        execution_slice = result_value.get("executionSlice")
        execution_slice_value = (
            execution_slice if isinstance(execution_slice, dict) else {}
        )
        execution_slice_ordinary = execution_slice_value.get("ordinary")
        execution_slice_ordinary_value = (
            execution_slice_ordinary
            if isinstance(execution_slice_ordinary, dict)
            else {}
        )
        execution_slice_stress = execution_slice_value.get("stress48Bps")
        execution_slice_stress_value = (
            execution_slice_stress
            if isinstance(execution_slice_stress, dict)
            else {}
        )
        execution_slice_ordinary_broker = execution_slice_ordinary_value.get(
            "broker"
        )
        execution_slice_ordinary_broker_value = (
            execution_slice_ordinary_broker
            if isinstance(execution_slice_ordinary_broker, dict)
            else {}
        )
        execution_slice_stress_broker = execution_slice_stress_value.get(
            "broker"
        )
        execution_slice_stress_broker_value = (
            execution_slice_stress_broker
            if isinstance(execution_slice_stress_broker, dict)
            else {}
        )
        execution_slice_ordinary_leakage = execution_slice_ordinary_value.get(
            "leakageGuard"
        )
        execution_slice_ordinary_leakage_value = (
            execution_slice_ordinary_leakage
            if isinstance(execution_slice_ordinary_leakage, dict)
            else {}
        )
        execution_slice_stress_leakage = execution_slice_stress_value.get(
            "leakageGuard"
        )
        execution_slice_stress_leakage_value = (
            execution_slice_stress_leakage
            if isinstance(execution_slice_stress_leakage, dict)
            else {}
        )
        execution_slice_failures = execution_slice_value.get("failures")
        execution_slice_failures_value = (
            [item for item in execution_slice_failures if isinstance(item, str)]
            if isinstance(execution_slice_failures, list)
            else []
        )
        policy = result_value.get("chosenPolicy")
        policy_value = policy if isinstance(policy, dict) else {}
        selection_bias = result_value.get("historicalSelectionBias")
        selection_bias_value = (
            selection_bias if isinstance(selection_bias, dict) else {}
        )
        instruments_value = dataset_value.get("instruments")
        replay_instruments = (
            [item for item in instruments_value if isinstance(item, str)]
            if isinstance(instruments_value, list)
            else []
        )
        first_replay_ms = _timestamp_ms(dataset_value.get("firstReplayAt"))
        last_replay_ms = _timestamp_ms(dataset_value.get("lastReplayAt"))
        replay_time_rows = dataset_value.get("replayTimeRows")
        episode_count = protocol_value.get("episodeCount")
        trades_value = ordinary_value.get("trades")
        ordinary_trade_count = len(trades_value) if isinstance(trades_value, list) else -1
        stress_trade_count = stress_value.get("trades")
        slice_ordinary_trade_count = execution_slice_ordinary_value.get("trades")
        slice_stress_trade_count = execution_slice_stress_value.get("trades")
        ordinary_failures = (
            _development_failures(
                ordinary_value,
                trade_count=ordinary_trade_count,
            )
            if ordinary_trade_count >= 0
            else None
        )
        stress_failures = _stress_failures(stress_value)
        slice_metric_failures = (
            _slice_failures(
                execution_slice_ordinary_value,
                execution_slice_stress_value,
                trade_count=slice_ordinary_trade_count,
            )
            if type(slice_ordinary_trade_count) is int
            and slice_ordinary_trade_count >= 0
            else None
        )
        drawdown_contract_valid = False
        max_drawdown_verification: dict[str, Any] = {
            "exactMaxDrawdownRecomputed": False,
            "fullBarSourceReplayPerformed": False,
            "method": "embedded_peak_trough_witness_bound_to_exact_checkpoints",
            "reportedMaxDrawdown": ordinary_value.get("maxDrawdown"),
        }
        if (
            first_replay_ms is not None
            and last_replay_ms is not None
            and replay_instruments
        ):
            ordinary_drawdown_valid, max_drawdown_verification = (
                _v6_drawdown_contract(
                    ordinary_value,
                    first_replay_ms=first_replay_ms,
                    last_replay_ms=last_replay_ms,
                    allowed_instruments=set(replay_instruments),
                    require_checkpoints=True,
                )
            )
            stress_drawdown_valid, _ = _v6_drawdown_contract(
                stress_value,
                first_replay_ms=first_replay_ms,
                last_replay_ms=last_replay_ms,
                allowed_instruments=set(replay_instruments),
                require_checkpoints=True,
            )
            slice_ordinary_drawdown_valid, _ = _v6_drawdown_contract(
                execution_slice_ordinary_value,
                first_replay_ms=first_replay_ms,
                last_replay_ms=last_replay_ms,
                allowed_instruments={"BTC-USDT"},
                require_checkpoints=True,
            )
            slice_stress_drawdown_valid, _ = _v6_drawdown_contract(
                execution_slice_stress_value,
                first_replay_ms=first_replay_ms,
                last_replay_ms=last_replay_ms,
                allowed_instruments={"BTC-USDT"},
                require_checkpoints=True,
            )
            drawdown_contract_valid = all(
                (
                    ordinary_drawdown_valid,
                    stress_drawdown_valid,
                    slice_ordinary_drawdown_valid,
                    slice_stress_drawdown_valid,
                )
            )
        v6_contract_valid = bool(
            schema_version == V6_REPORT_SCHEMA
            and type(execution_value.get("decisionToFillLatencyBars")) is int
            and execution_value.get("decisionToFillLatencyBars") == 0
            and execution_value.get("engineSchemaVersion")
            == "moheng.historical-replay.v3"
            and execution_value.get("checkpointValuationBasis")
            == V6_CHECKPOINT_VALUATION_BASIS
            and type(leakage_value.get("decisionToFillBars")) is int
            and leakage_value.get("decisionToFillBars") == 0
            and leakage_value.get("checkpointValuationBasis")
            == V6_CHECKPOINT_VALUATION_BASIS
            and leakage_value.get("decisionTimestampEqualsEntryTimestamp") is True
            and leakage_value.get("entryBarVolumeUsedExPost") is False
            and leakage_value.get("featureSourceCloseToEntryBars") == 0
            and leakage_value.get("instantaneousDecisionFillAssumption") is True
            and leakage_value.get("nextCandleAfterFeatureSource") is True
            and leakage_value.get("sameSourceBarFillAllowed") is False
            and leakage_value.get("sameTimestampFillAllowed") is True
            and leakage_value.get("targetExecutionAligned") is True
            and dataset_value.get("capacityVolumeSource")
            == "confirmed_feature_source_bar"
            and target_value.get("decisionAt")
            == "confirmed_bar_close_next_bar_open_boundary"
            and target_value.get("entryAt") == "next_bar_open_same_timestamp"
            and target_value.get("exitAt") == "entry_plus_12_bars_open"
            and target_value.get("labelHorizonBars") == 12
            and target_value.get("predictionUnit") == "gross_return"
            and protocol_value.get("executionLabelHorizonBars") == 12
            and protocol_value.get("developmentHistoryAlreadyObserved") is True
            and type(episode_count) is int
            and episode_count > 0
            and type(replay_time_rows) is int
            and replay_time_rows == episode_count * EXPECTED_REPLAY_BARS
            and first_replay_ms is not None
            and last_replay_ms is not None
            and last_replay_ms - first_replay_ms
            == (replay_time_rows - 1) * FIVE_MINUTES_MS
            and isinstance(instruments_value, list)
            and len(replay_instruments) == len(instruments_value)
            and len(replay_instruments) == len(set(replay_instruments))
            and all(item.endswith("-USDT") for item in replay_instruments)
            and selection_bias_value.get("resultMayBeOptimistic") is True
            and _v6_ledger_contract(
                ordinary_value,
                round_trip_cost_bps=24.0,
                slippage_bps_per_side=EXPECTED_STANDARD_SLIPPAGE_BPS_PER_SIDE,
            )
            and _v6_ledger_contract(
                stress_value,
                round_trip_cost_bps=48.0,
                slippage_bps_per_side=EXPECTED_STRESS_SLIPPAGE_BPS_PER_SIDE,
            )
            and _v6_ledger_contract(
                execution_slice_ordinary_value,
                round_trip_cost_bps=24.0,
                slippage_bps_per_side=EXPECTED_STANDARD_SLIPPAGE_BPS_PER_SIDE,
            )
            and _v6_ledger_contract(
                execution_slice_stress_value,
                round_trip_cost_bps=48.0,
                slippage_bps_per_side=EXPECTED_STRESS_SLIPPAGE_BPS_PER_SIDE,
            )
            and set(policy_value) == _POLICY_KEYS
            and policy_value == ordinary_value.get("policy")
            and policy_value == execution_slice_ordinary_value.get("policy")
            and execution_slice_value.get("instrument") == "BTC-USDT"
            and execution_slice_value.get("decision") == "research_only"
            and type(stress_trade_count) is int
            and stress_trade_count >= 0
            and type(slice_ordinary_trade_count) is int
            and slice_ordinary_trade_count >= 0
            and type(slice_stress_trade_count) is int
            and slice_stress_trade_count >= 0
            and ordinary_failures is not None
            and stress_failures is not None
            and slice_metric_failures is not None
            and _exact_failures(ordinary_value.get("failures"), ordinary_failures)
            and ordinary_value.get("developmentGatePassed")
            is (not ordinary_failures)
            and _exact_failures(stress_value.get("failures"), stress_failures)
            and stress_value.get("developmentGatePassed") is (not stress_failures)
            and result_value.get("developmentGatePassed")
            is (not ordinary_failures and not stress_failures)
            and result_value.get("decision") == "research_only"
            and result_value.get("shadowDaysCredited") == 0
            and _exact_failures(
                execution_slice_value.get("failures"), slice_metric_failures
            )
            and execution_slice_value.get("developmentGatePassed")
            is (not slice_metric_failures)
            and drawdown_contract_valid
        )
        valid = bool(
            _canonical_hash_matches(value, report_hash, excluded={"reportSha256"})
            and v6_contract_valid
            and value.get("promotable") is False
            and value.get("shadowDaysCredited") == 0
            and value.get("decision") == "research_only"
            and execution_value.get("historicalReplayOnly") is True
            and execution_value.get("orderCapability") is False
            and execution_value.get("privateApi") is False
            and execution_value.get("publicDataOnly") is True
            and execution_value.get("executionAllowlistChanged") is False
        )
        checkpoints_value = ordinary_value.get("checkpoints")
        checkpoints = (
            [item for item in checkpoints_value if isinstance(item, dict)][:2_000]
            if isinstance(checkpoints_value, list)
            else []
        )
        trades_value = ordinary_value.get("trades")
        trades = (
            [item for item in trades_value if isinstance(item, dict)][-50:]
            if isinstance(trades_value, list)
            else []
        )
        blockers_value = value.get("promotionBlockers")
        blockers = (
            [item for item in blockers_value if isinstance(item, str)]
            if isinstance(blockers_value, list)
            else []
        )
        episodes_value = value.get("episodes")
        episodes = []
        if isinstance(episodes_value, list):
            for item in episodes_value[:64]:
                if not isinstance(item, dict):
                    continue
                diagnostics = item.get("diagnostics")
                diagnostics_value = diagnostics if isinstance(diagnostics, dict) else {}
                episodes.append(
                    {
                        "assetRows": _integer(item.get("assetRows")),
                        "availableAt": item.get("availableAt"),
                        "calibrationRows": _integer(item.get("calibrationRows")),
                        "calibrationStartAt": item.get("calibrationStartAt"),
                        "calibrationStopAt": item.get("calibrationStopAt"),
                        "calibratedBrier": diagnostics_value.get("calibratedBrier"),
                        "episode": _integer(item.get("episode")),
                        "episodeId": item.get("episodeId"),
                        "fitRows": _integer(item.get("fitRows")),
                        "fitStartAt": item.get("fitStartAt"),
                        "fitStopAt": item.get("fitStopAt"),
                        "labelCompleteAt": item.get("labelCompleteAt"),
                        "rawBrier": diagnostics_value.get("rawBrier"),
                        "replayRows": _integer(item.get("replayRows")),
                        "replayStartAt": item.get("replayStartAt"),
                        "replayStopAt": item.get("replayStopAt"),
                        "trainingSeconds": item.get("trainingSeconds"),
                    }
                )
        return {
            "blockers": blockers,
            "calibrationImproved": model_value.get("calibrationImproved") is True,
            "capacityHandling": ordinary_broker_value.get("capacityHandling"),
            "checkpoints": checkpoints,
            "chosenPolicy": {
                "edgeBufferBps": policy_value.get("edgeBufferBps"),
                "minEntrySpacingBars": policy_value.get("minEntrySpacingBars"),
                "requiredGrossReturnBps": policy_value.get(
                    "requiredGrossReturnBps"
                ),
            }
            if policy_value
            else None,
            "cohortId": dataset_value.get("cohortId"),
            "completedAt": value.get("completedAt"),
            "compressionMultiple": timing_value.get("compressionMultiple"),
            "decision": value.get("decision"),
            "developmentGatePassed": result_value.get("developmentGatePassed")
            is True,
            "developmentHistoryAlreadyObserved": (
                protocol_value.get("developmentHistoryAlreadyObserved") is True
            ),
            "episodeCount": _integer(protocol_value.get("episodeCount")),
            "episodes": episodes,
            "executionSlice": {
                "developmentGatePassed": execution_slice_value.get(
                    "developmentGatePassed"
                )
                is True,
                "failures": execution_slice_failures_value,
                "instrument": execution_slice_value.get("instrument"),
                "maxDrawdown": execution_slice_ordinary_value.get("maxDrawdown"),
                "netReturn": execution_slice_ordinary_value.get("netReturn"),
                "stressNetReturn": execution_slice_stress_value.get("netReturn"),
                "trades": _integer(execution_slice_ordinary_value.get("trades")),
            }
            if execution_slice_value
            else None,
            "family": model_value.get("family"),
            "independentVerificationRequired": True,
            "monitorContractValid": valid,
            "executionSemantics": (
                "corrected_next_open_boundary"
                if schema_version == V6_REPORT_SCHEMA
                else "retired_legacy_semantics"
            ),
            "finalCash": ordinary_value.get("finalCash"),
            "firstReplayAt": dataset_value.get("firstReplayAt"),
            "lastReplayAt": dataset_value.get("lastReplayAt"),
            "maxDrawdown": ordinary_value.get("maxDrawdown"),
            "maxDrawdownVerification": max_drawdown_verification,
            "netReturn": ordinary_value.get("netReturn"),
            "ordersClipped": _integer(ordinary_value.get("ordersClipped")),
            "ordersRejected": _integer(ordinary_value.get("ordersRejected")),
            "cashBarRate": ordinary_value.get("cashBarRate"),
            "ordinaryCostBps": ordinary_broker_value.get("roundTripCostBps"),
            "promotable": value.get("promotable") is True,
            "replayId": value.get("replayId"),
            "retrainEveryDays": protocol_value.get("retrainEveryDays"),
            "reportSha256": report_hash,
            "schemaVersion": value.get("schemaVersion"),
            "shadowDaysCredited": _integer(value.get("shadowDaysCredited")),
            "selectionBiasWarning": (
                selection_bias_value.get("resultMayBeOptimistic") is True
            ),
            "retiredSemanticMismatch": (
                schema_version != V6_REPORT_SCHEMA
            ),
            "simulatedDays": ordinary_value.get("simulatedDays"),
            "startingCash": ordinary_broker_value.get("startingCash"),
            "stressNetReturn": stress_value.get("netReturn"),
            "totalEstimatedSlippageCost": ordinary_value.get(
                "totalEstimatedSlippageCost"
            ),
            "targetExecutionAligned": (
                leakage_value.get("targetExecutionAligned") is True
            ),
            "totalFees": ordinary_value.get("totalFees"),
            "totalWallSeconds": timing_value.get("totalWallSeconds"),
            "trades": trades,
            "tradeCount": len(trades_value) if isinstance(trades_value, list) else 0,
            "tradesPerDay": ordinary_value.get("tradesPerDay"),
            "turnoverMultiple": ordinary_value.get("turnoverMultiple"),
            "valid": valid,
        }
    return None


class ResearchMonitor:
    """Read project-local public-research telemetry without trading capability."""

    def __init__(self, root: Path | None = None):
        self.root = (root or configured_research_data_dir())

    def status(self) -> dict[str, Any]:
        root = self.root
        base: dict[str, Any] = {
            "available": False,
            "generatedAt": _iso_now(),
            "safety": {
                "executionAllowlist": sorted(ALLOWED_INSTRUMENTS),
                "orderCapability": False,
                "privateApi": False,
                "publicDataOnly": True,
            },
            "schemaVersion": RESEARCH_MONITOR_SCHEMA,
        }
        if root is None or not root.is_dir():
            return {
                **base,
                "blockers": ["research_data_not_configured"],
                "benchmark": None,
                "cohort": None,
                "history": None,
                "replay": None,
                "signals": {"available": False},
                "storageRoot": None,
                "universe": None,
            }

        universe_report = _read_json(root / "universes" / "universe-latest.json")
        progress = _read_json(root / "multi-asset-history-progress.json") or {}
        snapshot = (
            universe_report.get("snapshot")
            if isinstance(universe_report, dict)
            and isinstance(universe_report.get("snapshot"), dict)
            else {}
        )
        members = snapshot.get("members") if isinstance(snapshot, dict) else []
        member_rows = [item for item in members if isinstance(item, dict)] if isinstance(members, list) else []
        progress_instruments = progress.get("instruments")
        instrument_progress = progress_instruments if isinstance(progress_instruments, dict) else {}
        instruments = [
            _instrument_status(member, instrument_progress) for member in member_rows
        ]
        universe_valid = bool(universe_report and _verified_universe(universe_report))
        report_hash = (
            universe_report.get("reportSha256") if universe_report else None
        )
        snapshot_hash = snapshot.get("sha256") if isinstance(snapshot, dict) else None
        history_universe_match = bool(
            universe_valid
            and _valid_hash(report_hash)
            and _valid_hash(snapshot_hash)
            and hmac.compare_digest(
                str(progress.get("universeReportSha256")), str(report_hash)
            )
            and hmac.compare_digest(
                str(progress.get("universeSnapshotSha256")), str(snapshot_hash)
            )
        )
        database = root / "multi-asset-market.sqlite3"
        signal_database = root / "public-signals.sqlite3"
        history_state = str(progress.get("state", "idle"))
        history = {
            "active": (root / "multi-asset-history.lock").is_file()
            and history_state == "running",
            "databaseBytes": _file_size(database),
            "instruments": instruments,
            "pageBudget": _integer(progress.get("pageBudget")),
            "pagesConsumed": _integer(progress.get("pagesConsumed")),
            "runId": progress.get("runId") if isinstance(progress.get("runId"), str) else None,
            "startedAt": progress.get("startedAt") if isinstance(progress.get("startedAt"), str) else None,
            "state": history_state,
            "universeMatch": history_universe_match,
            "updatedAt": progress.get("updatedAt") if isinstance(progress.get("updatedAt"), str) else None,
        }
        blockers: list[str] = []
        if not universe_valid:
            blockers.append("universe_integrity_unverified")
        if progress and not history_universe_match:
            blockers.append("history_universe_mismatch")
        if not instruments or not all(item["backfillComplete"] for item in instruments):
            blockers.append("multi_asset_history_incomplete")
        if any(item["missingBars"] for item in instruments):
            blockers.append("unresolved_history_gaps")
        if any(item["unresolvedConflicts"] for item in instruments):
            blockers.append("immutable_data_conflicts")
        cohort = _latest_cohort(root)
        if cohort is None:
            blockers.append("aligned_cohort_not_built")
        elif not cohort["manifestValid"]:
            blockers.append("cohort_manifest_integrity_unverified")
        benchmark = _latest_benchmark(root)
        if benchmark is None:
            blockers.append("multi_asset_oos_not_run")
        elif not benchmark["valid"]:
            blockers.append("benchmark_integrity_unverified")
        elif not benchmark["exploratoryGatePassed"]:
            blockers.append("multi_asset_oos_gate_failed")
        if benchmark is not None and benchmark["valid"]:
            blockers.extend(benchmark["blockers"])
        replay = _latest_replay(root)
        if replay is not None and not replay["valid"]:
            blockers.append("historical_replay_integrity_unverified")
        if replay is not None and replay.get("retiredSemanticMismatch") is True:
            blockers.append("historical_replay_semantics_retired")
        if (
            replay is not None
            and replay["valid"]
            and cohort is not None
            and replay["cohortId"] != cohort["cohortId"]
        ):
            blockers.append("historical_replay_cohort_mismatch")
        blockers.extend(["requires_90_day_forward_public_shadow", "static_cost_only"])
        return {
            **base,
            "available": True,
            "benchmark": benchmark,
            "blockers": list(dict.fromkeys(blockers)),
            "cohort": cohort,
            "history": history,
            "replay": replay,
            "signals": {
                "available": signal_database.is_file(),
                "databaseBytes": _file_size(signal_database),
                "fullTextStored": False,
                "orderCapability": False,
                "source": "GDELT metadata + VADER baseline",
            },
            "storageRoot": str(root),
            "universe": {
                "createdAt": snapshot.get("createdAt") if isinstance(snapshot, dict) else None,
                "members": [item["instrument"] for item in instruments],
                "policySha256": snapshot.get("policySha256") if isinstance(snapshot, dict) else None,
                "reportSha256": report_hash,
                "snapshotSha256": snapshot_hash,
                "valid": universe_valid,
            },
        }


__all__ = [
    "RESEARCH_MONITOR_SCHEMA",
    "ResearchMonitor",
    "configured_research_data_dir",
]
