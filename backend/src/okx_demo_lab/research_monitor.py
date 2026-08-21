from __future__ import annotations

import hmac
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .config import ALLOWED_INSTRUMENTS
from .ml.strategy import canonical_json, sha256_hex


RESEARCH_MONITOR_SCHEMA = "moheng.research-monitor.v1"
MAX_JSON_BYTES = 1_000_000


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


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        size = path.stat().st_size
        if size < 2 or size > MAX_JSON_BYTES or not path.is_file():
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
        blockers.extend(["requires_90_day_forward_public_shadow", "static_cost_only"])
        return {
            **base,
            "available": True,
            "benchmark": benchmark,
            "blockers": list(dict.fromkeys(blockers)),
            "cohort": cohort,
            "history": history,
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
