from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

from ..config import ALLOWED_INSTRUMENTS
from .multi_asset_market import (
    FIVE_MINUTES_MS,
    MultiAssetMarketError,
    MultiAssetMarketSnapshot,
    MultiAssetMarketStore,
)
from .strategy import canonical_json, sha256_hex


COHORT_SCHEMA_VERSION = "moheng.multi-asset-cohort.v1"
RAW_CANDLE_MATRIX_SCHEMA_VERSION = "moheng.aligned-candle-matrix.v1"
RAW_CANDLE_FEATURE_CONTRACT = {
    "bar": "5m",
    "columns": [
        "open",
        "high",
        "low",
        "close",
        "base_volume",
        "currency_volume",
        "quote_volume",
    ],
    "dtype": "float64",
    "join": "strict-intersection-no-fill",
    "schemaVersion": RAW_CANDLE_MATRIX_SCHEMA_VERSION,
    "source": "okx-public-v5-confirmed-only",
}
RAW_CANDLE_FEATURE_CONTRACT_SHA256 = sha256_hex(
    canonical_json(RAW_CANDLE_FEATURE_CONTRACT)
)


class MultiAssetCohortError(RuntimeError):
    pass


def _utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise MultiAssetCohortError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return _utc(value, "timestamp").isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _write_json_atomic(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _verify_universe_report(value: Mapping[str, Any]) -> tuple[str, tuple[str, ...]]:
    report_hash = value.get("reportSha256")
    if not isinstance(report_hash, str) or len(report_hash) != 64:
        raise MultiAssetCohortError("research universe report hash is missing")
    report_body = dict(value)
    report_body.pop("reportSha256", None)
    if sha256_hex(canonical_json(report_body)) != report_hash:
        raise MultiAssetCohortError("research universe report hash mismatch")
    if value.get("executionAllowlistChanged") is not False or value.get(
        "executionAllowlist"
    ) != sorted(ALLOWED_INSTRUMENTS):
        raise MultiAssetCohortError("research universe changed the execution allowlist")
    snapshot = value.get("snapshot")
    if not isinstance(snapshot, Mapping):
        raise MultiAssetCohortError("research universe snapshot is missing")
    snapshot_hash = snapshot.get("sha256")
    snapshot_body = dict(snapshot)
    snapshot_body.pop("sha256", None)
    if (
        not isinstance(snapshot_hash, str)
        or sha256_hex(canonical_json(snapshot_body)) != snapshot_hash
    ):
        raise MultiAssetCohortError("research universe snapshot hash mismatch")
    rows = snapshot.get("members")
    if not isinstance(rows, list) or not 3 <= len(rows) <= 8:
        raise MultiAssetCohortError("research universe member count is invalid")
    members: list[str] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise MultiAssetCohortError("research universe member is invalid")
        instrument = row.get("instrument")
        if not isinstance(instrument, str) or not instrument.endswith("-USDT"):
            raise MultiAssetCohortError("research universe instrument is invalid")
        members.append(instrument)
    if len(members) != len(set(members)):
        raise MultiAssetCohortError("research universe members are duplicated")
    return snapshot_hash, tuple(members)


def load_frozen_universe(path: Path) -> tuple[str, tuple[str, ...], dict[str, Any]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise MultiAssetCohortError("research universe file is unreadable") from exc
    if not isinstance(value, dict):
        raise MultiAssetCohortError("research universe file is invalid")
    universe_hash, members = _verify_universe_report(value)
    return universe_hash, members, value


@dataclass(frozen=True)
class CohortBuildResult:
    cohort_id: str
    manifest_path: Path
    instruments: tuple[str, ...]
    first_open_ts_ms: int
    last_open_ts_ms: int
    row_count: int
    content_sha256: str
    promotable: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "cohortId": self.cohort_id,
            "contentSha256": self.content_sha256,
            "firstOpenTsMs": self.first_open_ts_ms,
            "instruments": list(self.instruments),
            "lastOpenTsMs": self.last_open_ts_ms,
            "manifestPath": str(self.manifest_path),
            "promotable": self.promotable,
            "rowCount": self.row_count,
        }


@dataclass(frozen=True)
class ValidatedCohort:
    manifest_path: Path
    manifest: Mapping[str, Any]
    timestamps: np.ndarray
    candles: np.ndarray
    correlation: np.ndarray


def _latest_snapshot(store: MultiAssetMarketStore, instrument: str) -> MultiAssetMarketSnapshot:
    status = store.status(instrument)
    if not status["readyForSnapshot"]:
        raise MultiAssetCohortError(f"{instrument} is not ready for a clean snapshot")
    latest = status.get("latestSnapshot")
    if not isinstance(latest, Mapping) or not status.get("latestSnapshotCurrent"):
        raise MultiAssetCohortError(f"{instrument} has no current immutable snapshot")
    snapshot_id = latest.get("snapshotId")
    if not isinstance(snapshot_id, str):
        raise MultiAssetCohortError(f"{instrument} snapshot identity is invalid")
    snapshot = store.get_snapshot(snapshot_id)
    if snapshot is None or snapshot.instrument != instrument:
        raise MultiAssetCohortError(f"{instrument} snapshot could not be resolved")
    if snapshot.feature_contract_sha256 != RAW_CANDLE_FEATURE_CONTRACT_SHA256:
        raise MultiAssetCohortError(f"{instrument} snapshot feature contract mismatch")
    if not store.snapshot_is_current(snapshot.content_sha256):
        raise MultiAssetCohortError(f"{instrument} snapshot is stale")
    return snapshot


def _array_spec(path: Path, value: np.ndarray) -> dict[str, Any]:
    return {
        "dtype": str(value.dtype),
        "file": path.name,
        "sha256": _hash_file(path),
        "shape": list(value.shape),
    }


def _validated_array(
    root: Path,
    arrays: Mapping[str, Any],
    name: str,
    *,
    dtype: str,
    shape: tuple[int, ...],
) -> np.ndarray:
    spec = arrays.get(name)
    if not isinstance(spec, Mapping):
        raise MultiAssetCohortError(f"{name} array spec is invalid")
    file_name = spec.get("file")
    if (
        not isinstance(file_name, str)
        or Path(file_name).name != file_name
        or Path(file_name).suffix != ".npy"
    ):
        raise MultiAssetCohortError(f"{name} array path is invalid")
    path = root / file_name
    if not path.is_file() or _hash_file(path) != spec.get("sha256"):
        raise MultiAssetCohortError(f"{name} array hash mismatch")
    try:
        value = np.load(path, allow_pickle=False, mmap_mode="r")
    except (OSError, ValueError) as exc:
        raise MultiAssetCohortError(f"{name} array is unreadable") from exc
    if (
        str(value.dtype) != dtype
        or tuple(value.shape) != shape
        or spec.get("dtype") != dtype
        or spec.get("shape") != list(shape)
    ):
        raise MultiAssetCohortError(f"{name} array contract mismatch")
    return value


def load_validated_cohort(manifest_path: Path) -> ValidatedCohort:
    """Revalidate a persisted cohort before any model can consume it."""

    try:
        manifest_value = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise MultiAssetCohortError("cohort manifest is unreadable") from exc
    if not isinstance(manifest_value, dict):
        raise MultiAssetCohortError("cohort manifest is invalid")
    manifest = dict(manifest_value)
    content_sha256 = manifest.get("contentSha256")
    cohort_id = manifest.get("cohortId")
    if (
        not _is_sha256(content_sha256)
        or cohort_id != f"cohort_{content_sha256[:24]}"
    ):
        raise MultiAssetCohortError("cohort identity is invalid")
    if manifest_path.name != "manifest.json" or manifest_path.parent.name != cohort_id:
        raise MultiAssetCohortError("cohort directory identity is invalid")
    content = dict(manifest)
    for key in ("cohortId", "contentSha256", "createdAt", "promotable"):
        content.pop(key, None)
    try:
        content_hash_matches = (
            sha256_hex(canonical_json(content)) == content_sha256
        )
    except (TypeError, ValueError) as exc:
        raise MultiAssetCohortError("cohort manifest is not canonical JSON") from exc
    if not content_hash_matches:
        raise MultiAssetCohortError("cohort manifest hash mismatch")
    if manifest.get("promotable") is not False:
        raise MultiAssetCohortError("survivor cohort cannot be promotable")
    if manifest.get("schemaVersion") != COHORT_SCHEMA_VERSION:
        raise MultiAssetCohortError("cohort schema version is unsupported")
    try:
        created_at = datetime.fromisoformat(
            str(manifest.get("createdAt", "")).replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise MultiAssetCohortError("cohort creation timestamp is invalid") from exc
    if created_at.tzinfo is None or created_at.utcoffset() is None:
        raise MultiAssetCohortError("cohort creation timestamp is invalid")
    if (
        manifest.get("matrixContract") != RAW_CANDLE_FEATURE_CONTRACT
        or manifest.get("matrixContractSha256")
        != RAW_CANDLE_FEATURE_CONTRACT_SHA256
    ):
        raise MultiAssetCohortError("cohort matrix contract mismatch")
    blockers = manifest.get("promotionBlockers")
    required_blockers = {
        "fixed_current_survivor_cohort",
        "requires_90_day_forward_public_shadow",
        "static_cost_only",
    }
    if (
        not isinstance(blockers, list)
        or any(not isinstance(item, str) for item in blockers)
        or not required_blockers.issubset(set(blockers))
    ):
        raise MultiAssetCohortError("cohort promotion blockers are incomplete")
    if (
        manifest.get("pointInTimeUniverse") is not False
        or manifest.get("survivorshipMode") != "fixed-current-survivor-cohort"
        or manifest.get("costEvidence")
        != {
            "baseRoundTripBps": 24,
            "mode": "static-conservative-no-historical-spread",
            "stressRoundTripBps": 48,
        }
    ):
        raise MultiAssetCohortError("cohort research limitations are invalid")

    instruments = manifest.get("instruments")
    row_count = manifest.get("rowCount")
    first_open = manifest.get("firstOpenTsMs")
    last_open = manifest.get("lastOpenTsMs")
    if (
        not isinstance(instruments, list)
        or not 3 <= len(instruments) <= 8
        or any(
            not isinstance(item, str) or not item.endswith("-USDT")
            for item in instruments
        )
        or len(instruments) != len(set(instruments))
    ):
        raise MultiAssetCohortError("cohort instruments are invalid")
    if (
        isinstance(row_count, bool)
        or not isinstance(row_count, int)
        or row_count < 3
        or isinstance(first_open, bool)
        or not isinstance(first_open, int)
        or isinstance(last_open, bool)
        or not isinstance(last_open, int)
        or last_open - first_open != (row_count - 1) * FIVE_MINUTES_MS
    ):
        raise MultiAssetCohortError("cohort time range is invalid")
    snapshots = manifest.get("marketSnapshots")
    if not isinstance(snapshots, list) or len(snapshots) != len(instruments):
        raise MultiAssetCohortError("cohort market snapshot evidence is invalid")
    for instrument, snapshot in zip(instruments, snapshots, strict=True):
        if (
            not isinstance(snapshot, Mapping)
            or snapshot.get("instrument") != instrument
            or not isinstance(snapshot.get("rowCount"), int)
            or snapshot.get("rowCount", 0) < row_count
            or not isinstance(snapshot.get("snapshotId"), str)
            or not str(snapshot.get("snapshotId")).startswith("maset_")
            or not _is_sha256(snapshot.get("contentSha256"))
        ):
            raise MultiAssetCohortError("cohort market snapshot evidence is invalid")
    universe_sha256 = manifest.get("universeSha256")
    signal_sha256 = manifest.get("signalSnapshotSha256")
    if (
        not _is_sha256(universe_sha256)
        or (
            signal_sha256 is not None
            and not _is_sha256(signal_sha256)
        )
    ):
        raise MultiAssetCohortError("cohort source hashes are invalid")

    arrays = manifest.get("arrays")
    if not isinstance(arrays, Mapping) or set(arrays) != {
        "timestamps",
        "candles",
        "correlation",
    }:
        raise MultiAssetCohortError("cohort array manifest is invalid")
    root = manifest_path.parent
    timestamps = _validated_array(
        root, arrays, "timestamps", dtype="int64", shape=(row_count,)
    )
    candles = _validated_array(
        root,
        arrays,
        "candles",
        dtype="float64",
        shape=(row_count, len(instruments), 7),
    )
    correlation = _validated_array(
        root,
        arrays,
        "correlation",
        dtype="float64",
        shape=(len(instruments), len(instruments)),
    )
    if (
        int(timestamps[0]) != first_open
        or int(timestamps[-1]) != last_open
        or not np.all(np.diff(timestamps) == FIVE_MINUTES_MS)
    ):
        raise MultiAssetCohortError("cohort timestamps are not a strict 5m grid")
    if not np.all(np.isfinite(candles)):
        raise MultiAssetCohortError("cohort candles contain non-finite values")
    opens = candles[:, :, 0]
    highs = candles[:, :, 1]
    lows = candles[:, :, 2]
    closes = candles[:, :, 3]
    volumes = candles[:, :, 4:7]
    if (
        np.any(opens <= 0)
        or np.any(highs <= 0)
        or np.any(lows <= 0)
        or np.any(closes <= 0)
        or np.any(highs < np.maximum(np.maximum(opens, closes), lows))
        or np.any(lows > np.minimum(np.minimum(opens, closes), highs))
        or np.any(volumes < 0)
    ):
        raise MultiAssetCohortError("cohort candle domain rules failed")
    returns = np.diff(np.log(closes), axis=0)
    recomputed = np.corrcoef(returns, rowvar=False)
    if (
        not np.all(np.isfinite(correlation))
        or np.any(correlation < -1.000000000001)
        or np.any(correlation > 1.000000000001)
        or not np.allclose(correlation, correlation.T, rtol=1e-12, atol=1e-12)
        or not np.allclose(np.diag(correlation), 1.0, rtol=1e-12, atol=1e-12)
        or not np.allclose(correlation, recomputed, rtol=1e-10, atol=1e-12)
    ):
        raise MultiAssetCohortError("cohort correlation evidence is invalid")
    return ValidatedCohort(
        manifest_path=manifest_path,
        manifest=manifest,
        timestamps=timestamps,
        candles=candles,
        correlation=correlation,
    )


def _verify_existing_cohort(
    destination: Path,
    *,
    cohort_id: str,
    content: Mapping[str, Any],
    content_sha256: str,
) -> Path:
    manifest_path = destination / "manifest.json"
    existing = dict(load_validated_cohort(manifest_path).manifest)
    existing_content = dict(existing)
    for key in ("cohortId", "contentSha256", "createdAt", "promotable"):
        existing_content.pop(key, None)
    if (
        existing.get("cohortId") != cohort_id
        or existing.get("contentSha256") != content_sha256
        or existing.get("promotable") is not False
        or existing_content != dict(content)
        or sha256_hex(canonical_json(existing_content)) != content_sha256
    ):
        raise MultiAssetCohortError("existing cohort manifest hash mismatch")
    return manifest_path


def build_aligned_cohort(
    *,
    store: MultiAssetMarketStore,
    universe_path: Path,
    output_root: Path,
    now: datetime,
) -> CohortBuildResult:
    """Build a strict, no-fill matrix from current immutable series snapshots.

    The first fixed-current cohort is deliberately non-promotable because its
    membership was selected with today's survivor/liquidity information.
    """

    created_at = _utc(now, "now")
    universe_sha256, instruments, _universe = load_frozen_universe(universe_path)
    snapshots = tuple(_latest_snapshot(store, item) for item in instruments)
    first_open = max(item.first_open_ts_ms for item in snapshots)
    last_open = min(item.last_open_ts_ms for item in snapshots)
    if first_open > last_open or (last_open - first_open) % FIVE_MINUTES_MS:
        raise MultiAssetCohortError("series snapshots have no aligned 5m intersection")
    row_count = (last_open - first_open) // FIVE_MINUTES_MS + 1
    if row_count < 2:
        raise MultiAssetCohortError("aligned cohort is too short")

    timestamps = np.arange(
        first_open,
        last_open + FIVE_MINUTES_MS,
        FIVE_MINUTES_MS,
        dtype=np.int64,
    )
    candles = np.empty((row_count, len(instruments), 7), dtype=np.float64)
    for asset_index, snapshot in enumerate(snapshots):
        aligned_index = 0
        try:
            rows = store.rows(snapshot.snapshot_id)
            for row in rows:
                timestamp = int(row[0])
                if timestamp < first_open:
                    continue
                if timestamp > last_open:
                    break
                if aligned_index >= row_count or timestamp != int(timestamps[aligned_index]):
                    raise MultiAssetCohortError(
                        f"{snapshot.instrument} is missing an aligned candle"
                    )
                values = np.asarray([float(item) for item in row[1:8]], dtype=np.float64)
                if not np.all(np.isfinite(values)):
                    raise MultiAssetCohortError(
                        f"{snapshot.instrument} contains non-finite candle values"
                    )
                candles[aligned_index, asset_index] = values
                aligned_index += 1
        except (MultiAssetMarketError, ValueError) as exc:
            if isinstance(exc, MultiAssetCohortError):
                raise
            raise MultiAssetCohortError(
                f"{snapshot.instrument} snapshot failed integrity validation"
            ) from exc
        if aligned_index != row_count:
            raise MultiAssetCohortError(
                f"{snapshot.instrument} does not cover the full intersection"
            )

    close_values = candles[:, :, 3]
    if np.any(close_values <= 0):
        raise MultiAssetCohortError("aligned cohort contains invalid close prices")
    returns = np.diff(np.log(close_values), axis=0)
    if not np.all(np.isfinite(returns)):
        raise MultiAssetCohortError("aligned cohort returns are non-finite")
    if returns.shape[0] < 2 or np.any(np.std(returns, axis=0) <= 0):
        raise MultiAssetCohortError("aligned cohort has insufficient return variation")
    correlation = np.corrcoef(returns, rowvar=False)
    if correlation.shape != (len(instruments), len(instruments)) or not np.all(
        np.isfinite(correlation)
    ):
        raise MultiAssetCohortError("aligned cohort correlation is invalid")

    output_root.mkdir(parents=True, exist_ok=True)
    temporary = output_root / f".cohort-{uuid.uuid4().hex}.tmp"
    temporary.mkdir(parents=False, exist_ok=False)
    try:
        timestamps_path = temporary / "timestamps.npy"
        candles_path = temporary / "candles.npy"
        correlation_path = temporary / "correlation.npy"
        np.save(timestamps_path, timestamps, allow_pickle=False)
        np.save(candles_path, candles, allow_pickle=False)
        np.save(correlation_path, correlation, allow_pickle=False)
        array_specs = {
            "candles": _array_spec(candles_path, candles),
            "correlation": _array_spec(correlation_path, correlation),
            "timestamps": _array_spec(timestamps_path, timestamps),
        }
        content = {
            "arrays": array_specs,
            "bar": "5m",
            "costEvidence": {
                "baseRoundTripBps": 24,
                "mode": "static-conservative-no-historical-spread",
                "stressRoundTripBps": 48,
            },
            "firstOpenTsMs": first_open,
            "instruments": list(instruments),
            "lastOpenTsMs": last_open,
            "marketSnapshots": [
                {
                    "contentSha256": item.content_sha256,
                    "instrument": item.instrument,
                    "rowCount": item.row_count,
                    "snapshotId": item.snapshot_id,
                }
                for item in snapshots
            ],
            "matrixContract": RAW_CANDLE_FEATURE_CONTRACT,
            "matrixContractSha256": RAW_CANDLE_FEATURE_CONTRACT_SHA256,
            "pointInTimeUniverse": False,
            "promotionBlockers": [
                "fixed_current_survivor_cohort",
                "requires_90_day_forward_public_shadow",
                "static_cost_only",
            ],
            "rowCount": row_count,
            "schemaVersion": COHORT_SCHEMA_VERSION,
            "signalSnapshotSha256": None,
            "survivorshipMode": "fixed-current-survivor-cohort",
            "universeSha256": universe_sha256,
        }
        content_sha256 = sha256_hex(canonical_json(content))
        cohort_id = f"cohort_{content_sha256[:24]}"
        manifest = {
            **content,
            "cohortId": cohort_id,
            "contentSha256": content_sha256,
            "createdAt": _iso(created_at),
            "promotable": False,
        }
        _write_json_atomic(temporary / "manifest.json", manifest)
        destination = output_root / cohort_id
        if destination.exists():
            existing_path = _verify_existing_cohort(
                destination,
                cohort_id=cohort_id,
                content=content,
                content_sha256=content_sha256,
            )
            shutil.rmtree(temporary)
            manifest_path = existing_path
        else:
            os.replace(temporary, destination)
            manifest_path = destination / "manifest.json"
        load_validated_cohort(manifest_path)
        return CohortBuildResult(
            cohort_id=cohort_id,
            manifest_path=manifest_path,
            instruments=instruments,
            first_open_ts_ms=first_open,
            last_open_ts_ms=last_open,
            row_count=row_count,
            content_sha256=content_sha256,
        )
    except BaseException:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise


__all__ = [
    "COHORT_SCHEMA_VERSION",
    "RAW_CANDLE_FEATURE_CONTRACT",
    "RAW_CANDLE_FEATURE_CONTRACT_SHA256",
    "CohortBuildResult",
    "MultiAssetCohortError",
    "ValidatedCohort",
    "build_aligned_cohort",
    "load_validated_cohort",
    "load_frozen_universe",
]
