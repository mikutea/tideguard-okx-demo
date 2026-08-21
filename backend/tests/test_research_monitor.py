from __future__ import annotations

import json
from pathlib import Path

from okx_demo_lab.ml.strategy import canonical_json, sha256_hex
from okx_demo_lab.research_monitor import ResearchMonitor


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


def test_monitor_surfaces_verified_historical_replay_without_trading_capability(
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
    assert replay["valid"] is True
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
    assert status["safety"]["orderCapability"] is False
    assert status["safety"]["privateApi"] is False


def test_monitor_rejects_tampered_historical_replay(tmp_path) -> None:
    root = tmp_path / ".research-data"
    root.mkdir()
    report = _replay_report("cohort_" + "a" * 24)
    report["shadowDaysCredited"] = 90
    _write_json(root / "replays" / "historical-replay-v3-test.json", report)

    status = ResearchMonitor(root).status()

    assert status["replay"]["valid"] is False
    assert "historical_replay_integrity_unverified" in status["blockers"]


def test_monitor_surfaces_execution_aligned_v4_replay(tmp_path) -> None:
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

    assert replay["valid"] is True
    assert replay["targetExecutionAligned"] is True
    assert replay["capacityHandling"] == "clip"
    assert replay["ordersClipped"] == 4
    assert replay["selectionBiasWarning"] is True

    report.pop("reportSha256")
    report["model"]["targetContract"].pop("decisionAt")
    report["reportSha256"] = sha256_hex(canonical_json(report))
    _write_json(path, report)

    assert ResearchMonitor(root).status()["replay"]["valid"] is False
