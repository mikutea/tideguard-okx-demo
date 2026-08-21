from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from okx_demo_lab.ml.strategy import canonical_json, sha256_hex
from okx_demo_lab.research_monitor import (
    MAX_JSON_BYTES,
    MAX_REPLAY_JSON_BYTES,
    ResearchMonitor,
)


def _write_json(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _universe() -> dict[str, object]:
    snapshot: dict[str, object] = {
        "createdAt": "2026-08-21T12:00:00.000Z",
        "instrumentRows": 2,
        "members": [
            {
                "instrument": "BTC-USDT",
                "listedAt": "2021-01-29T08:08:06.000Z",
            },
            {
                "instrument": "ETH-USDT",
                "listedAt": "2021-01-29T08:08:06.000Z",
            },
        ],
        "policySha256": "a" * 64,
        "schemaVersion": "moheng.research-universe.v1",
        "tickerRows": 2,
    }
    snapshot["sha256"] = sha256_hex(canonical_json(snapshot))
    report: dict[str, object] = {
        "executionAllowlist": ["BTC-USDT"],
        "executionAllowlistChanged": False,
        "snapshot": snapshot,
    }
    report["reportSha256"] = sha256_hex(canonical_json(report))
    return report


def test_monitor_is_fail_closed_when_research_root_is_unavailable(tmp_path) -> None:
    status = ResearchMonitor(tmp_path / "missing").status()

    assert status["available"] is False
    assert status["safety"]["executionAllowlist"] == ["BTC-USDT"]
    assert status["safety"]["privateApi"] is False
    assert status["safety"]["orderCapability"] is False
    assert status["blockers"] == ["research_data_not_configured"]


def test_monitor_reports_running_public_history_without_opening_private_surfaces(
    tmp_path,
) -> None:
    root = tmp_path / ".research-data"
    root.mkdir()
    _write_json(root / "universes" / "universe-latest.json", _universe())
    _write_json(
        root / "multi-asset-history-progress.json",
        {
            "instruments": {
                "BTC-USDT": {
                    "backfillComplete": False,
                    "firstOpenTsMs": 1_787_000_000_000,
                    "lastOpenTsMs": 1_787_314_200_000,
                    "lastTerminalCursor": 1_786_000_000_000,
                    "missingBars": 0,
                    "pagesConsumed": 80,
                    "rowsInserted": 24_000,
                    "stage": "oldestBackfill",
                    "storedRows": 6_000,
                    "unresolvedConflicts": 0,
                }
            },
            "pageBudget": 20_000,
            "pagesConsumed": 80,
            "runId": "history-test",
            "startedAt": "2026-08-21T12:00:00.000Z",
            "state": "running",
            "universeReportSha256": _universe()["reportSha256"],
            "universeSnapshotSha256": _universe()["snapshot"]["sha256"],
            "updatedAt": "2026-08-21T12:01:00.000Z",
        },
    )
    (root / "multi-asset-history.lock").write_text("{}", encoding="utf-8")
    (root / "multi-asset-market.sqlite3").write_bytes(b"public-only")

    status = ResearchMonitor(root).status()

    assert status["available"] is True
    assert status["universe"]["valid"] is True
    assert status["universe"]["members"] == ["BTC-USDT", "ETH-USDT"]
    assert status["history"]["active"] is True
    assert status["history"]["universeMatch"] is True
    assert status["history"]["pagesConsumed"] == 80
    btc = status["history"]["instruments"][0]
    assert btc["rowsInsertedThisRun"] == 24_000
    assert btc["firstOpenTsMs"] == 1_786_000_000_000
    assert status["safety"]["executionAllowlist"] == ["BTC-USDT"]
    assert "multi_asset_history_incomplete" in status["blockers"]
    assert "aligned_cohort_not_built" in status["blockers"]


def test_monitor_blocks_progress_from_a_different_frozen_universe(tmp_path) -> None:
    root = tmp_path / ".research-data"
    root.mkdir()
    _write_json(root / "universes" / "universe-latest.json", _universe())
    _write_json(
        root / "multi-asset-history-progress.json",
        {
            "instruments": {},
            "state": "partial",
            "universeReportSha256": "b" * 64,
            "universeSnapshotSha256": "c" * 64,
        },
    )

    status = ResearchMonitor(root).status()

    assert status["history"]["universeMatch"] is False
    assert "history_universe_mismatch" in status["blockers"]


def test_monitor_rejects_tampered_universe_hash(tmp_path) -> None:
    root = tmp_path / ".research-data"
    root.mkdir()
    report = _universe()
    report["snapshot"]["members"][0]["instrument"] = "ETH-USDT"
    _write_json(root / "universes" / "universe-latest.json", report)

    status = ResearchMonitor(root).status()

    assert status["universe"]["valid"] is False
    assert "universe_integrity_unverified" in status["blockers"]


def test_monitor_verifies_latest_cohort_manifest_and_oos_report(tmp_path) -> None:
    root = tmp_path / ".research-data"
    root.mkdir()
    cohort_content: dict[str, object] = {
        "instruments": ["BTC-USDT", "ETH-USDT", "SOL-USDT"],
        "promotionBlockers": [
            "fixed_current_survivor_cohort",
            "requires_90_day_forward_public_shadow",
            "static_cost_only",
        ],
        "rowCount": 100,
        "schemaVersion": "moheng.multi-asset-cohort.v1",
    }
    cohort_sha = sha256_hex(canonical_json(cohort_content))
    cohort_id = f"cohort_{cohort_sha[:24]}"
    cohort_manifest = {
        **cohort_content,
        "cohortId": cohort_id,
        "contentSha256": cohort_sha,
        "createdAt": "2026-08-21T13:00:00.000Z",
        "promotable": False,
    }
    _write_json(
        root / "cohorts" / cohort_id / "manifest.json", cohort_manifest
    )
    benchmark_body: dict[str, object] = {
        "benchmarkId": "mabench_test",
        "completedAt": "2026-08-21T14:00:00.000Z",
        "dataset": {"cohortId": cohort_id},
        "promotable": False,
        "results": [
            {
                "chosenThreshold": 0.56,
                "exploratoryGatePassed": True,
                "family": "hist_gradient_boosting",
                "ordinary": {
                    "maxDrawdown": 0.02,
                    "netReturn": 0.03,
                    "trades": 42,
                },
            }
        ],
        "schemaVersion": "moheng.multi-asset-research.v1",
    }
    benchmark = {
        **benchmark_body,
        "reportSha256": sha256_hex(canonical_json(benchmark_body)),
    }
    _write_json(root / "benchmarks" / "multi-asset-test.json", benchmark)

    status = ResearchMonitor(root).status()

    assert status["cohort"]["manifestValid"] is True
    assert status["cohort"]["blockers"] == cohort_content["promotionBlockers"]
    assert status["benchmark"]["valid"] is True
    assert status["benchmark"]["exploratoryGatePassed"] is True
    assert status["benchmark"]["results"][0]["trades"] == 42
    assert "multi_asset_oos_not_run" not in status["blockers"]


def test_monitor_surfaces_v2_cost_and_calibration_diagnostics(tmp_path) -> None:
    root = tmp_path / ".research-data"
    root.mkdir()
    benchmark_body: dict[str, object] = {
        "benchmarkId": "mabench_v2_test",
        "completedAt": "2026-08-22T00:00:00.000Z",
        "dataset": {"cohortId": "cohort_" + "a" * 24},
        "evaluation": {"scope": "retrospective-development-only"},
        "promotable": False,
        "results": [
            {
                "calibration": {"improved": True},
                "chosenPolicy": {
                    "edgeBufferBps": 24.0,
                    "minEntrySpacingBars": 96,
                    "requiredGrossReturnBps": 48.0,
                },
                "developmentGatePassed": False,
                "exploratoryGatePassed": False,
                "family": "hist_gradient_boosting",
                "ordinary": {
                    "cashBarRate": 0.95,
                    "grossReturn": 0.02,
                    "maxDrawdown": 0.01,
                    "maxInstrumentTradeShare": 0.5,
                    "netReturn": 0.01,
                    "trades": 20,
                    "tradesPerDay": 0.2,
                },
                "promotionBlockers": [
                    "prior_sealed_folds_already_observed",
                    "fresh_sealed_oos_unavailable",
                ],
            }
        ],
        "schemaVersion": "moheng.multi-asset-research.v2",
    }
    benchmark = {
        **benchmark_body,
        "reportSha256": sha256_hex(canonical_json(benchmark_body)),
    }
    _write_json(root / "benchmarks" / "multi-asset-v2-test.json", benchmark)

    status = ResearchMonitor(root).status()

    assert status["benchmark"]["valid"] is True
    assert status["benchmark"]["schemaVersion"].endswith(".v2")
    assert status["benchmark"]["evaluationScope"] == "retrospective-development-only"
    result = status["benchmark"]["results"][0]
    assert result["calibrationImproved"] is True
    assert result["cashBarRate"] == 0.95
    assert result["chosenPolicy"]["minEntrySpacingBars"] == 96
    assert "fresh_sealed_oos_unavailable" in status["blockers"]


def _replay_report(cohort_id: str) -> dict[str, object]:
    body: dict[str, object] = {
        "completedAt": "2026-08-22T01:00:00.000Z",
        "dataset": {
            "cohortId": cohort_id,
            "firstReplayAt": "2023-01-01T00:00:00.000Z",
            "lastReplayAt": "2025-04-20T00:00:00.000Z",
        },
        "decision": "research_only",
        "execution": {
            "executionAllowlistChanged": False,
            "historicalReplayOnly": True,
            "orderCapability": False,
            "privateApi": False,
            "publicDataOnly": True,
        },
        "model": {
            "calibrationImproved": True,
            "family": "hist_gradient_boosting",
        },
        "episodes": [
            {
                "assetRows": 3,
                "availableAt": "2022-12-31T23:55:00.000Z",
                "calibrationRows": 25920,
                "calibrationStartAt": "2022-12-01T00:00:00.000Z",
                "calibrationStopAt": "2022-12-31T22:50:00.000Z",
                "diagnostics": {"calibratedBrier": 0.21, "rawBrier": 0.31},
                "episode": 0,
                "episodeId": "replay_episode_test",
                "fitRows": 289000,
                "fitStartAt": "2022-01-01T00:00:00.000Z",
                "fitStopAt": "2022-11-30T22:50:00.000Z",
                "labelCompleteAt": "2022-12-31T23:50:00.000Z",
                "replayRows": 25920,
                "replayStartAt": "2023-01-01T00:00:00.000Z",
                "replayStopAt": "2023-01-30T23:55:00.000Z",
                "trainingSeconds": 3.5,
            }
        ],
        "promotable": False,
        "promotionBlockers": [
            "historical_replay_development_only",
            "requires_90_day_forward_public_shadow",
        ],
        "protocol": {
            "episodeCount": 28,
            "retrainEveryDays": 30.0,
        },
        "replayId": "hreplay_" + "a" * 24,
        "result": {
            "chosenPolicy": {
                "edgeBufferBps": 24.0,
                "minEntrySpacingBars": 96,
                "requiredGrossReturnBps": 48.0,
            },
            "developmentGatePassed": False,
            "ordinary": {
                "broker": {"roundTripCostBps": 24.0},
                "cashBarRate": 0.98,
                "checkpoints": [
                    {
                        "at": "2023-01-01T00:00:00.000Z",
                        "cash": 10_000.0,
                        "drawdown": 0.0,
                        "equity": 10_000.0,
                        "positionInstrument": None,
                        "positionMarketValue": 0.0,
                    }
                ],
                "maxDrawdown": 0.02,
                "netReturn": -0.01,
                "finalCash": 9_900.0,
                "simulatedDays": 840.0,
                "totalEstimatedSlippageCost": 12.0,
                "totalFees": 24.0,
                "trades": [
                    {
                        "instrument": "BTC-USDT",
                        "netPnl": -10.0,
                        "tradeId": "replay_trade_test",
                    }
                ],
                "tradesPerDay": 0.04,
                "turnoverMultiple": 3.2,
            },
            "stress48Bps": {"netReturn": -0.02},
        },
        "schemaVersion": "moheng.historical-replay-report.v1",
        "shadowDaysCredited": 0,
        "timing": {
            "compressionMultiple": 9_000.0,
            "totalWallSeconds": 8.0,
        },
    }
    return {**body, "reportSha256": sha256_hex(canonical_json(body))}


_CHECKPOINT_VALUATION_BASIS = (
    "current_bar_open_at_checkpoint_boundary"
)
_FIRST_REPLAY = datetime(2023, 1, 1, tzinfo=timezone.utc)
_REPLAY_ROWS = 30 * 288
_LAST_REPLAY = _FIRST_REPLAY + timedelta(minutes=5 * (_REPLAY_ROWS - 1))


def _iso_z(value: datetime) -> str:
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _break_even_gross_return_bps(slippage_bps_per_side: float) -> float:
    fee = 8.0 / 10_000.0
    slippage = slippage_bps_per_side / 10_000.0
    multiplier = ((1.0 + slippage) * (1.0 + fee)) / (
        (1.0 - slippage) * (1.0 - fee)
    )
    return (multiplier - 1.0) * 10_000.0


def _v6_policy(round_trip_cost_bps: float) -> dict[str, object]:
    return {
        "edgeBufferBps": 72.0,
        "minEntrySpacingBars": 12,
        "requiredGrossReturnBps": round_trip_cost_bps + 72.0,
    }


def _v6_broker(
    *, round_trip_cost_bps: float, slippage_bps_per_side: float
) -> dict[str, object]:
    return {
        "allocationFraction": 0.25,
        "breakEvenGrossReturnBps": _break_even_gross_return_bps(
            slippage_bps_per_side
        ),
        "capacityHandling": "clip",
        "checkpointStrideBars": 288,
        "executionLabelHorizonBars": 12,
        "feeBpsPerSide": 8.0,
        "holdingPeriodBars": 12,
        "latencyBars": 0,
        "maxQuoteVolumeParticipation": 0.005,
        "minimumNotional": 10.0,
        "roundTripCostBps": round_trip_cost_bps,
        "slippageBpsPerSide": slippage_bps_per_side,
        "startingCash": 10_000.0,
    }


def _v6_leakage_guard() -> dict[str, object]:
    return {
        "causalEpisodeBinding": True,
        "checkpointValuationBasis": _CHECKPOINT_VALUATION_BASIS,
        "decisionToFillBars": 0,
        "executionCoordinate": "decision_rows",
        "nextDecisionRowFill": False,
        "predictionRowsAvailableBeforeDecision": True,
        "sameDecisionRowFill": True,
    }


def _v6_checkpoints() -> list[dict[str, object]]:
    indices = list(range(0, _REPLAY_ROWS, 288))
    if indices[-1] != _REPLAY_ROWS - 1:
        indices.append(_REPLAY_ROWS - 1)
    return [
        {
            "at": _iso_z(_FIRST_REPLAY + timedelta(minutes=5 * index)),
            "cash": 10_000.0,
            "drawdown": 0.0,
            "equity": 10_000.0,
            "peakEquity": 10_000.0,
            "positionInstrument": None,
            "positionMarketValue": 0.0,
        }
        for index in indices
    ]


def _v6_drawdown_witness() -> dict[str, object]:
    first_at = _iso_z(_FIRST_REPLAY)
    return {
        "drawdown": 0.0,
        "peakAt": first_at,
        "peakEquity": 10_000.0,
        "peakSource": "pre_replay_starting_cash",
        "troughAt": first_at,
        "troughEquity": 10_000.0,
    }


def _v6_ledger(
    *,
    round_trip_cost_bps: float,
    slippage_bps_per_side: float,
    trade_value: object,
    failures: list[str] | None = None,
    development_gate_passed: bool | None = None,
) -> dict[str, object]:
    ledger: dict[str, object] = {
        "broker": _v6_broker(
            round_trip_cost_bps=round_trip_cost_bps,
            slippage_bps_per_side=slippage_bps_per_side,
        ),
        "cashBarRate": 1.0,
        "checkpoints": _v6_checkpoints(),
        "finalCash": 10_000.0,
        "grossPnlReturn": 0.0,
        "leakageGuard": _v6_leakage_guard(),
        "maxDrawdown": 0.0,
        "maxDrawdownWitness": _v6_drawdown_witness(),
        "netReturn": 0.0,
        "ordersClipped": 0,
        "ordersRejected": 0,
        "ordersSubmitted": 0,
        "policy": _v6_policy(round_trip_cost_bps),
        "profitableTradeRate": 0.0,
        "simulatedDays": 30.0,
        "timeRows": _REPLAY_ROWS,
        "totalEstimatedSlippageCost": 0.0,
        "totalFees": 0.0,
        "trades": trade_value,
        "tradesPerDay": 0.0,
        "turnoverMultiple": 0.0,
    }
    if failures is not None:
        ledger["failures"] = failures
    if development_gate_passed is not None:
        ledger["developmentGatePassed"] = development_gate_passed
    return ledger


def _reseal(report: dict[str, object]) -> dict[str, object]:
    report.pop("reportSha256", None)
    report["reportSha256"] = sha256_hex(canonical_json(report))
    return report


def _v6_replay_report(cohort_id: str) -> dict[str, object]:
    ordinary_failures = [
        "trades_insufficient",
        "profitable_trade_rate_below_gate",
        "net_return_below_gate",
    ]
    slice_failures = [
        "execution_slice_trades_insufficient",
        "execution_slice_net_return_not_positive",
        "execution_slice_stress_return_not_positive",
    ]
    ordinary = _v6_ledger(
        round_trip_cost_bps=24.0,
        slippage_bps_per_side=4.0,
        trade_value=[],
        failures=ordinary_failures,
        development_gate_passed=False,
    )
    stress = _v6_ledger(
        round_trip_cost_bps=48.0,
        slippage_bps_per_side=16.0,
        trade_value=0,
        failures=[],
        development_gate_passed=True,
    )
    btc_ordinary = _v6_ledger(
        round_trip_cost_bps=24.0,
        slippage_bps_per_side=4.0,
        trade_value=0,
    )
    btc_stress = _v6_ledger(
        round_trip_cost_bps=48.0,
        slippage_bps_per_side=16.0,
        trade_value=0,
    )
    report: dict[str, object] = {
        "completedAt": "2026-08-22T01:00:00.000Z",
        "dataset": {
            "capacityVolumeSource": "confirmed_feature_source_bar",
            "cohortId": cohort_id,
            "firstReplayAt": _iso_z(_FIRST_REPLAY),
            "instruments": ["BTC-USDT", "ETH-USDT"],
            "lastReplayAt": _iso_z(_LAST_REPLAY),
            "replayTimeRows": _REPLAY_ROWS,
        },
        "decision": "research_only",
        "execution": {
            "checkpointValuationBasis": _CHECKPOINT_VALUATION_BASIS,
            "decisionToFillLatencyBars": 0,
            "engineSchemaVersion": "moheng.historical-replay.v3",
            "executionAllowlistChanged": False,
            "historicalReplayOnly": True,
            "orderCapability": False,
            "privateApi": False,
            "publicDataOnly": True,
        },
        "leakageAudit": {
            "checkpointValuationBasis": _CHECKPOINT_VALUATION_BASIS,
            "decisionTimestampEqualsEntryTimestamp": True,
            "decisionToFillBars": 0,
            "entryBarVolumeUsedExPost": False,
            "featureSourceCloseToEntryBars": 0,
            "instantaneousDecisionFillAssumption": True,
            "nextCandleAfterFeatureSource": True,
            "sameSourceBarFillAllowed": False,
            "sameTimestampFillAllowed": True,
            "targetExecutionAligned": True,
        },
        "model": {
            "calibrationImproved": True,
            "family": "hist_gradient_boosting",
            "targetContract": {
                "decisionAt": "confirmed_bar_close_next_bar_open_boundary",
                "entryAt": "next_bar_open_same_timestamp",
                "exitAt": "entry_plus_12_bars_open",
                "labelHorizonBars": 12,
                "predictionUnit": "gross_return",
            },
        },
        "episodes": [
            {
                "assetRows": 2,
                "availableAt": "2022-12-31T23:55:00.000Z",
                "calibrationRows": 25_920,
                "calibrationStartAt": "2022-12-01T00:00:00.000Z",
                "calibrationStopAt": "2022-12-31T22:50:00.000Z",
                "diagnostics": {
                    "calibratedBrier": 0.21,
                    "rawBrier": 0.31,
                },
                "episode": 0,
                "episodeId": "replay_episode_test",
                "fitRows": 289_000,
                "fitStartAt": "2022-01-01T00:00:00.000Z",
                "fitStopAt": "2022-11-30T22:50:00.000Z",
                "labelCompleteAt": "2022-12-31T23:50:00.000Z",
                "replayRows": _REPLAY_ROWS,
                "replayStartAt": _iso_z(_FIRST_REPLAY),
                "replayStopAt": _iso_z(_LAST_REPLAY),
                "trainingSeconds": 3.5,
            }
        ],
        "promotable": False,
        "promotionBlockers": [
            "historical_replay_development_only",
            "requires_90_day_forward_public_shadow",
        ],
        "protocol": {
            "developmentHistoryAlreadyObserved": True,
            "episodeCount": 1,
            "executionLabelHorizonBars": 12,
            "retrainEveryDays": 30.0,
        },
        "replayId": "hreplay_" + "a" * 24,
        "result": {
            "chosenPolicy": _v6_policy(24.0),
            "decision": "research_only",
            "developmentGatePassed": False,
            "executionSlice": {
                "decision": "research_only",
                "developmentGatePassed": False,
                "failures": slice_failures,
                "instrument": "BTC-USDT",
                "ordinary": btc_ordinary,
                "stress48Bps": btc_stress,
            },
            "historicalSelectionBias": {"resultMayBeOptimistic": True},
            "ordinary": ordinary,
            "shadowDaysCredited": 0,
            "stress48Bps": stress,
        },
        "schemaVersion": "moheng.historical-replay-report.v4",
        "shadowDaysCredited": 0,
        "timing": {
            "compressionMultiple": 9_000.0,
            "totalWallSeconds": 8.0,
        },
    }
    return _reseal(report)


def test_monitor_retires_v1_historical_replay_even_with_a_valid_hash(
    tmp_path,
) -> None:
    root = tmp_path / ".research-data"
    root.mkdir()
    cohort_id = "cohort_" + "a" * 24
    _write_json(
        root / "replays" / "historical-replay-v3-test.json",
        _replay_report(cohort_id),
    )

    status = ResearchMonitor(root).status()

    replay = status["replay"]
    assert replay["valid"] is False
    assert replay["monitorContractValid"] is False
    assert replay["retiredSemanticMismatch"] is True
    assert replay["executionSemantics"] == "retired_legacy_semantics"
    assert replay["decision"] == "research_only"
    assert replay["shadowDaysCredited"] == 0
    assert replay["episodeCount"] == 28
    assert replay["episodes"][0]["fitRows"] == 289000
    assert replay["episodes"][0]["calibratedBrier"] == 0.21
    assert replay["ordinaryCostBps"] == 24.0
    assert replay["cashBarRate"] == 0.98
    assert replay["finalCash"] == 9_900.0
    assert replay["tradeCount"] == 1
    assert replay["checkpoints"][0]["equity"] == 10_000.0
    assert "historical_replay_integrity_unverified" in status["blockers"]
    assert "historical_replay_semantics_retired" in status["blockers"]
    assert status["safety"]["orderCapability"] is False
    assert status["safety"]["privateApi"] is False


def test_monitor_rejects_unsealed_v6_hash_tampering(tmp_path) -> None:
    root = tmp_path / ".research-data"
    root.mkdir()
    report = _v6_replay_report("cohort_" + "a" * 24)
    report["timing"]["totalWallSeconds"] = 9.0
    _write_json(root / "replays" / "historical-replay-v6-test.json", report)

    status = ResearchMonitor(root).status()

    assert status["replay"]["valid"] is False
    assert "historical_replay_integrity_unverified" in status["blockers"]


def test_monitor_retires_v2_execution_aligned_replay(tmp_path) -> None:
    root = tmp_path / ".research-data"
    root.mkdir()
    path = root / "replays" / "historical-replay-v4-test.json"
    report = _replay_report("cohort_" + "b" * 24)
    report.pop("reportSha256")
    report["schemaVersion"] = "moheng.historical-replay-report.v2"
    report["execution"]["engineSchemaVersion"] = "moheng.historical-replay.v2"
    report["leakageAudit"] = {"targetExecutionAligned": True}
    report["model"]["targetContract"] = {
        "decisionAt": "confirmed_bar_close",
        "entryAt": "next_bar_open",
        "exitAt": "entry_plus_12_bars_open",
        "labelHorizonBars": 13,
        "predictionUnit": "gross_return",
    }
    report["protocol"].update(
        {
            "developmentHistoryAlreadyObserved": True,
            "executionLabelHorizonBars": 13,
        }
    )
    report["result"]["historicalSelectionBias"] = {
        "resultMayBeOptimistic": True
    }
    report["result"]["ordinary"]["broker"].update(
        {"capacityHandling": "clip", "executionLabelHorizonBars": 13}
    )
    report["result"]["ordinary"].update(
        {"ordersClipped": 4, "ordersRejected": 0}
    )
    report["reportSha256"] = sha256_hex(canonical_json(report))
    _write_json(path, report)

    replay = ResearchMonitor(root).status()["replay"]

    assert replay["valid"] is False
    assert replay["monitorContractValid"] is False
    assert replay["retiredSemanticMismatch"] is True
    assert replay["targetExecutionAligned"] is True
    assert replay["capacityHandling"] == "clip"
    assert replay["ordersClipped"] == 4
    assert replay["selectionBiasWarning"] is True

    report.pop("reportSha256")
    report["model"]["targetContract"].pop("decisionAt")
    report["reportSha256"] = sha256_hex(canonical_json(report))
    _write_json(path, report)

    assert ResearchMonitor(root).status()["replay"]["valid"] is False


def test_monitor_retires_v3_btc_execution_slice(tmp_path) -> None:
    root = tmp_path / ".research-data"
    root.mkdir()
    path = root / "replays" / "historical-replay-v5-test.json"
    report = _replay_report("cohort_" + "c" * 24)
    report.pop("reportSha256")
    report["schemaVersion"] = "moheng.historical-replay-report.v3"
    report["execution"]["engineSchemaVersion"] = "moheng.historical-replay.v2"
    report["leakageAudit"] = {"targetExecutionAligned": True}
    report["model"]["targetContract"] = {
        "decisionAt": "confirmed_bar_close",
        "entryAt": "next_bar_open",
        "exitAt": "entry_plus_12_bars_open",
        "labelHorizonBars": 13,
        "predictionUnit": "gross_return",
    }
    report["protocol"].update(
        {
            "developmentHistoryAlreadyObserved": True,
            "executionLabelHorizonBars": 13,
        }
    )
    report["result"]["historicalSelectionBias"] = {"resultMayBeOptimistic": True}
    report["result"]["ordinary"]["broker"].update(
        {"capacityHandling": "clip", "executionLabelHorizonBars": 13}
    )
    report["result"]["executionSlice"] = {
        "decision": "research_only",
        "developmentGatePassed": False,
        "failures": ["execution_slice_trades_insufficient"],
        "instrument": "BTC-USDT",
        "ordinary": {"maxDrawdown": 0.01, "netReturn": 0.006, "trades": 4},
        "stress48Bps": {"netReturn": 0.001, "trades": 4},
    }
    report["reportSha256"] = sha256_hex(canonical_json(report))
    _write_json(path, report)

    replay = ResearchMonitor(root).status()["replay"]

    assert replay["valid"] is False
    assert replay["monitorContractValid"] is False
    assert replay["retiredSemanticMismatch"] is True
    assert replay["executionSlice"] == {
        "developmentGatePassed": False,
        "failures": ["execution_slice_trades_insufficient"],
        "instrument": "BTC-USDT",
        "maxDrawdown": 0.01,
        "netReturn": 0.006,
        "stressNetReturn": 0.001,
        "trades": 4,
    }


def test_monitor_validates_complete_v6_corrected_execution_contract(
    tmp_path,
) -> None:
    root = tmp_path / ".research-data"
    root.mkdir()
    path = root / "replays" / "historical-replay-v6-test.json"
    report = _v6_replay_report("cohort_" + "d" * 24)
    _write_json(path, report)

    status = ResearchMonitor(root).status()
    replay = status["replay"]

    assert replay["valid"] is True
    assert replay["monitorContractValid"] is True
    assert replay["independentVerificationRequired"] is True
    assert replay["executionSemantics"] == "corrected_next_open_boundary"
    assert replay["retiredSemanticMismatch"] is False
    assert replay["episodeCount"] == 1
    assert replay["ordinaryCostBps"] == 24.0
    assert replay["tradeCount"] == 0
    assert len(replay["checkpoints"]) == 31
    assert replay["maxDrawdownVerification"] == {
        "exactMaxDrawdownRecomputed": True,
        "fullBarSourceReplayPerformed": False,
        "method": "embedded_peak_trough_witness_bound_to_exact_checkpoints",
        "reportedMaxDrawdown": 0.0,
    }
    assert "historical_replay_integrity_unverified" not in status["blockers"]
    assert "historical_replay_semantics_retired" not in status["blockers"]

    ledgers = [
        report["result"]["ordinary"],
        report["result"]["stress48Bps"],
        report["result"]["executionSlice"]["ordinary"],
        report["result"]["executionSlice"]["stress48Bps"],
    ]
    assert all(len(ledger["checkpoints"]) == 31 for ledger in ledgers)
    assert all("maxDrawdownWitness" in ledger for ledger in ledgers)


def test_monitor_prefers_v6_over_a_newer_legacy_file(tmp_path) -> None:
    root = tmp_path / ".research-data"
    root.mkdir()
    _write_json(
        root / "replays" / "historical-replay-v6-valid.json",
        _v6_replay_report("cohort_" + "d" * 24),
    )
    _write_json(
        root / "replays" / "historical-replay-v5-newer.json",
        _replay_report("cohort_" + "e" * 24),
    )

    replay = ResearchMonitor(root).status()["replay"]

    assert replay["schemaVersion"] == "moheng.historical-replay-report.v4"
    assert replay["valid"] is True


def test_monitor_rejects_resealed_main_stress_leakage_tamper(tmp_path) -> None:
    root = tmp_path / ".research-data"
    root.mkdir()
    report = _v6_replay_report("cohort_" + "f" * 24)
    report["result"]["stress48Bps"]["leakageGuard"][
        "causalEpisodeBinding"
    ] = False
    _write_json(
        root / "replays" / "historical-replay-v6-tampered.json",
        _reseal(report),
    )

    replay = ResearchMonitor(root).status()["replay"]

    assert replay["valid"] is False
    assert replay["monitorContractValid"] is False


@pytest.mark.parametrize("gate", ["ordinary", "stress", "execution_slice"])
def test_monitor_recomputes_resealed_v6_gates(tmp_path, gate: str) -> None:
    root = tmp_path / ".research-data"
    root.mkdir()
    report = _v6_replay_report("cohort_" + "1" * 24)
    if gate == "ordinary":
        report["result"]["ordinary"]["failures"] = []
        report["result"]["ordinary"]["developmentGatePassed"] = True
    elif gate == "stress":
        report["result"]["stress48Bps"]["failures"] = [
            "stress_net_return_below_zero"
        ]
        report["result"]["stress48Bps"]["developmentGatePassed"] = False
    else:
        report["result"]["executionSlice"]["failures"] = []
        report["result"]["executionSlice"]["developmentGatePassed"] = True
    _write_json(
        root / "replays" / "historical-replay-v6-gate.json",
        _reseal(report),
    )

    replay = ResearchMonitor(root).status()["replay"]

    assert replay["valid"] is False
    assert replay["monitorContractValid"] is False


@pytest.mark.parametrize(
    ("component", "field", "value"),
    [
        ("broker", "latencyBars", False),
        ("broker", "allocationFraction", 0.5),
        ("broker", "breakEvenGrossReturnBps", 24.0),
        ("broker", "checkpointStrideBars", True),
        ("policy", "edgeBufferBps", 71.0),
        ("policy", "minEntrySpacingBars", False),
    ],
)
def test_monitor_rejects_resealed_v6_policy_and_broker_tampering(
    tmp_path,
    component: str,
    field: str,
    value: object,
) -> None:
    root = tmp_path / ".research-data"
    root.mkdir()
    report = _v6_replay_report("cohort_" + "2" * 24)
    report["result"]["ordinary"][component][field] = value
    _write_json(
        root / "replays" / "historical-replay-v6-contract.json",
        _reseal(report),
    )

    replay = ResearchMonitor(root).status()["replay"]

    assert replay["valid"] is False
    assert replay["monitorContractValid"] is False


def test_monitor_rejects_zeroed_resealed_max_drawdown(tmp_path) -> None:
    root = tmp_path / ".research-data"
    root.mkdir()
    path = root / "replays" / "historical-replay-v6-drawdown.json"
    report = _v6_replay_report("cohort_" + "3" * 24)
    ordinary = report["result"]["ordinary"]
    trough = ordinary["checkpoints"][1]
    trough.update(
        {
            "cash": 9_900.0,
            "drawdown": 0.01,
            "equity": 9_900.0,
        }
    )
    ordinary["maxDrawdown"] = 0.01
    ordinary["maxDrawdownWitness"].update(
        {
            "drawdown": 0.01,
            "troughAt": trough["at"],
            "troughEquity": 9_900.0,
        }
    )
    _write_json(path, _reseal(report))
    assert ResearchMonitor(root).status()["replay"]["valid"] is True

    ordinary["maxDrawdown"] = 0.0
    _write_json(path, _reseal(report))

    replay = ResearchMonitor(root).status()["replay"]
    assert replay["valid"] is False
    assert replay["maxDrawdownVerification"][
        "exactMaxDrawdownRecomputed"
    ] is False


@pytest.mark.parametrize("tamper", ["checkpoint", "witness"])
def test_monitor_rejects_resealed_drawdown_binding_tamper(
    tmp_path, tamper: str
) -> None:
    root = tmp_path / ".research-data"
    root.mkdir()
    report = _v6_replay_report("cohort_" + "4" * 24)
    ordinary = report["result"]["ordinary"]
    checkpoint = ordinary["checkpoints"][1]
    if tamper == "checkpoint":
        checkpoint.update(
            {
                "cash": 9_999.0,
                "drawdown": 0.0001,
                "equity": 9_999.0,
            }
        )
    else:
        ordinary["maxDrawdown"] = 0.0001
        ordinary["maxDrawdownWitness"].update(
            {
                "drawdown": 0.0001,
                "troughAt": checkpoint["at"],
                "troughEquity": 9_999.0,
            }
        )
    _write_json(
        root / "replays" / "historical-replay-v6-drawdown-tamper.json",
        _reseal(report),
    )

    replay = ResearchMonitor(root).status()["replay"]

    assert replay["valid"] is False
    assert replay["monitorContractValid"] is False


def test_replay_size_limit_accepts_six_mib_and_rejects_over_32_mib(
    tmp_path,
) -> None:
    assert MAX_JSON_BYTES == 1_000_000
    assert MAX_REPLAY_JSON_BYTES == 32 * 1024 * 1024

    root = tmp_path / ".research-data"
    root.mkdir()
    path = root / "replays" / "historical-replay-v6-size.json"
    report = _v6_replay_report("cohort_" + "5" * 24)
    report["padding"] = "x" * (6 * 1024 * 1024)
    _write_json(path, _reseal(report))

    assert 5 * 1024 * 1024 < path.stat().st_size <= MAX_REPLAY_JSON_BYTES
    assert ResearchMonitor(root).status()["replay"]["valid"] is True

    oversized = _v6_replay_report("cohort_" + "6" * 24)
    oversized["padding"] = "x" * MAX_REPLAY_JSON_BYTES
    _write_json(path, _reseal(oversized))

    assert path.stat().st_size > MAX_REPLAY_JSON_BYTES
    assert ResearchMonitor(root).status()["replay"] is None
