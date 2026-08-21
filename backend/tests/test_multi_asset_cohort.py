from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest

from okx_demo_lab.config import ALLOWED_INSTRUMENTS
from okx_demo_lab.ml.multi_asset_cohort import (
    RAW_CANDLE_FEATURE_CONTRACT_SHA256,
    MultiAssetCohortError,
    build_aligned_cohort,
)
from okx_demo_lab.ml.multi_asset_market import (
    FIVE_MINUTES_MS,
    MultiAssetMarketStore,
    OriginProbe,
)
from okx_demo_lab.ml.strategy import canonical_json, sha256_hex


NOW = datetime(2026, 8, 21, 4, 0, tzinfo=timezone.utc)


def _candle(index: int, price: float) -> list[str]:
    return [
        str(index * FIVE_MINUTES_MS),
        str(price),
        str(price + 2),
        str(price - 2),
        str(price + (index % 3) * 0.1),
        "10",
        "20",
        "30",
        "1",
    ]


def _complete_snapshot(
    store: MultiAssetMarketStore,
    instrument: str,
    start: int,
    stop: int,
    price: float,
) -> None:
    rows = [_candle(index, price + index * 0.3) for index in range(start, stop)]
    store.ingest(rows, instrument=instrument, observed_at=NOW)
    oldest = start * FIVE_MINUTES_MS
    with sqlite3.connect(store.path) as db:
        anchor = db.execute(
            """
            SELECT payload_sha256 FROM ma_candles
            WHERE inst_id = ? AND open_ts_ms = ?
            """,
            (instrument, oldest),
        ).fetchone()
    assert anchor is not None
    anchor_sha = str(anchor[0])
    store.record_origin_probe(
        OriginProbe(
            run_id=f"{instrument}-baseline",
            instrument=instrument,
            requested_after=oldest,
            terminal_cursor=oldest,
            page_limit=300,
            empty_probe_count=3,
            anchor_open_ts_ms=oldest,
            anchor_payload_sha256=anchor_sha,
            observed_at=NOW,
        )
    )
    store.record_origin_probe(
        OriginProbe(
            run_id=f"{instrument}-confirmation",
            instrument=instrument,
            requested_after=oldest - 1,
            terminal_cursor=oldest - 1,
            page_limit=100,
            empty_probe_count=3,
            anchor_open_ts_ms=oldest,
            anchor_payload_sha256=anchor_sha,
            observed_at=NOW + timedelta(seconds=61),
        )
    )
    store.create_snapshot(
        instrument,
        feature_contract_sha256=RAW_CANDLE_FEATURE_CONTRACT_SHA256,
        now=NOW + timedelta(seconds=62),
    )


def _universe(path: Path, members: list[str]) -> Path:
    snapshot_body = {
        "createdAt": "2026-08-21T04:00:00.000Z",
        "instrumentRows": len(members),
        "members": [{"instrument": item} for item in members],
        "policySha256": "a" * 64,
        "schemaVersion": "moheng.research-universe.v1",
        "tickerRows": len(members),
    }
    snapshot = {**snapshot_body, "sha256": sha256_hex(canonical_json(snapshot_body))}
    report_body = {
        "executionAllowlist": sorted(ALLOWED_INSTRUMENTS),
        "executionAllowlistChanged": False,
        "nextGate": "complete_history_and_aligned_portfolio_oos",
        "snapshot": snapshot,
    }
    report = {**report_body, "reportSha256": sha256_hex(canonical_json(report_body))}
    path.write_text(json.dumps(report), encoding="utf-8")
    return path


def test_builds_strict_intersection_arrays_and_blocks_promotion(tmp_path: Path) -> None:
    store = MultiAssetMarketStore(tmp_path / "market.sqlite3")
    members = ["BTC-USDT", "ETH-USDT", "SOL-USDT"]
    _complete_snapshot(store, members[0], 10, 17, 100.0)
    _complete_snapshot(store, members[1], 11, 18, 200.0)
    _complete_snapshot(store, members[2], 12, 19, 300.0)
    result = build_aligned_cohort(
        store=store,
        universe_path=_universe(tmp_path / "universe.json", members),
        output_root=tmp_path / "cohorts",
        now=NOW,
    )

    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    root = result.manifest_path.parent
    timestamps = np.load(root / "timestamps.npy", allow_pickle=False)
    candles = np.load(root / "candles.npy", allow_pickle=False)
    correlation = np.load(root / "correlation.npy", allow_pickle=False)
    assert result.row_count == 5
    assert timestamps.tolist() == [index * FIVE_MINUTES_MS for index in range(12, 17)]
    assert candles.shape == (5, 3, 7)
    assert correlation.shape == (3, 3)
    assert manifest["promotable"] is False
    assert manifest["survivorshipMode"] == "fixed-current-survivor-cohort"
    assert "requires_90_day_forward_public_shadow" in manifest["promotionBlockers"]
    assert manifest["signalSnapshotSha256"] is None
    assert result.manifest_path.resolve().is_relative_to(tmp_path.resolve())


def test_universe_hash_or_execution_allowlist_tampering_fails_closed(tmp_path: Path) -> None:
    path = _universe(
        tmp_path / "universe.json", ["BTC-USDT", "ETH-USDT", "SOL-USDT"]
    )
    value = json.loads(path.read_text(encoding="utf-8"))
    value["executionAllowlist"] = ["BTC-USDT", "ETH-USDT"]
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(MultiAssetCohortError, match="hash mismatch"):
        build_aligned_cohort(
            store=MultiAssetMarketStore(tmp_path / "market.sqlite3"),
            universe_path=path,
            output_root=tmp_path / "cohorts",
            now=NOW,
        )


def test_missing_current_snapshot_fails_instead_of_forward_filling(tmp_path: Path) -> None:
    store = MultiAssetMarketStore(tmp_path / "market.sqlite3")
    members = ["BTC-USDT", "ETH-USDT", "SOL-USDT"]
    _complete_snapshot(store, members[0], 10, 17, 100.0)
    _complete_snapshot(store, members[1], 10, 17, 200.0)
    store.ingest([_candle(10, 300.0)], instrument=members[2], observed_at=NOW)
    with pytest.raises(MultiAssetCohortError, match="not ready"):
        build_aligned_cohort(
            store=store,
            universe_path=_universe(tmp_path / "universe.json", members),
            output_root=tmp_path / "cohorts",
            now=NOW,
        )


def test_existing_cohort_array_tampering_is_never_silently_reused(tmp_path: Path) -> None:
    store = MultiAssetMarketStore(tmp_path / "market.sqlite3")
    members = ["BTC-USDT", "ETH-USDT", "SOL-USDT"]
    for index, instrument in enumerate(members):
        _complete_snapshot(store, instrument, 10, 17, 100.0 + index * 100)
    universe = _universe(tmp_path / "universe.json", members)
    first = build_aligned_cohort(
        store=store,
        universe_path=universe,
        output_root=tmp_path / "cohorts",
        now=NOW,
    )
    candles_path = first.manifest_path.parent / "candles.npy"
    with candles_path.open("ab") as handle:
        handle.write(b"tampered")
    with pytest.raises(MultiAssetCohortError, match="array hash mismatch"):
        build_aligned_cohort(
            store=store,
            universe_path=universe,
            output_root=tmp_path / "cohorts",
            now=NOW + timedelta(seconds=1),
        )


def test_existing_survivor_cohort_cannot_be_relabelled_promotable(tmp_path: Path) -> None:
    store = MultiAssetMarketStore(tmp_path / "market.sqlite3")
    members = ["BTC-USDT", "ETH-USDT", "SOL-USDT"]
    for index, instrument in enumerate(members):
        _complete_snapshot(store, instrument, 10, 17, 100.0 + index * 100)
    universe = _universe(tmp_path / "universe.json", members)
    first = build_aligned_cohort(
        store=store,
        universe_path=universe,
        output_root=tmp_path / "cohorts",
        now=NOW,
    )
    manifest = json.loads(first.manifest_path.read_text(encoding="utf-8"))
    manifest["promotable"] = True
    first.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(MultiAssetCohortError, match="manifest hash mismatch"):
        build_aligned_cohort(
            store=store,
            universe_path=universe,
            output_root=tmp_path / "cohorts",
            now=NOW + timedelta(seconds=1),
        )
