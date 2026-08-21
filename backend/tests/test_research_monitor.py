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
