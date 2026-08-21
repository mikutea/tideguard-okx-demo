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
