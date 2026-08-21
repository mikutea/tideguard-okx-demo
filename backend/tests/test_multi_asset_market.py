from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from okx_demo_lab.ml.multi_asset_market import (
    FIVE_MINUTES_MS,
    MultiAssetMarketError,
    MultiAssetMarketStore,
    OriginProbe,
)


NOW = datetime(2026, 8, 21, 2, 0, tzinfo=timezone.utc)
CONFIRMED_AT = NOW + timedelta(seconds=61)


def candle(
    timestamp: int,
    *,
    price: str = "100",
    volume: str = "10",
    confirm: str = "1",
) -> list[str]:
    numeric_price = float(price)
    return [
        str(timestamp),
        price,
        str(numeric_price + 2),
        str(numeric_price - 2),
        price,
        volume,
        "20",
        "20",
        confirm,
    ]


def series_rows(start: int, stop: int, *, price: str) -> list[list[str]]:
    return [
        candle(FIVE_MINUTES_MS * index, price=price)
        for index in range(start, stop)
    ]


def anchor_evidence(
    store: MultiAssetMarketStore, instrument: str
) -> tuple[int, str]:
    with sqlite3.connect(store.path) as db:
        row = db.execute(
            """
            SELECT open_ts_ms, payload_sha256 FROM ma_candles
            WHERE inst_id = ? ORDER BY open_ts_ms ASC LIMIT 1
            """,
            (instrument,),
        ).fetchone()
    assert row is not None
    return int(row[0]), str(row[1])


def confirm_origin(store: MultiAssetMarketStore, instrument: str) -> None:
    oldest = store.status(instrument)["firstOpenTsMs"]
    assert isinstance(oldest, int)
    anchor_open, anchor_sha = anchor_evidence(store, instrument)
    first = store.record_origin_probe(
        OriginProbe(
            run_id=f"{instrument}-origin-a",
            instrument=instrument,
            requested_after=oldest,
            terminal_cursor=oldest,
            page_limit=300,
            empty_probe_count=3,
            anchor_open_ts_ms=anchor_open,
            anchor_payload_sha256=anchor_sha,
            observed_at=NOW,
        )
    )
    second = store.record_origin_probe(
        OriginProbe(
            run_id=f"{instrument}-origin-b",
            instrument=instrument,
            requested_after=oldest - 1,
            terminal_cursor=oldest - 1,
            page_limit=100,
            empty_probe_count=3,
            anchor_open_ts_ms=anchor_open,
            anchor_payload_sha256=anchor_sha,
            observed_at=CONFIRMED_AT,
        )
    )
    assert first["backfillComplete"] is False
    assert second["backfillComplete"] is True


def test_one_sqlite_keeps_same_timestamp_instruments_strictly_isolated(tmp_path) -> None:
    store = MultiAssetMarketStore(tmp_path / "multi-asset.sqlite3")
    btc_rows = series_rows(10, 13, price="100")
    eth_rows = series_rows(10, 13, price="200")

    btc_result = store.ingest(btc_rows, instrument="BTC-USDT", observed_at=NOW)
    eth_result = store.ingest(eth_rows, instrument="ETH-USDT", observed_at=NOW)
    confirm_origin(store, "BTC-USDT")
    confirm_origin(store, "ETH-USDT")

    assert btc_result["inserted"] == 3
    assert eth_result["inserted"] == 3
    assert store.status("BTC-USDT")["storedRows"] == 3
    assert store.status("ETH-USDT")["storedRows"] == 3

    btc_snapshot = store.create_snapshot(
        "BTC-USDT", feature_contract_sha256="a" * 64, now=CONFIRMED_AT
    )
    eth_snapshot = store.create_snapshot(
        "ETH-USDT", feature_contract_sha256="a" * 64, now=CONFIRMED_AT
    )
    assert list(store.rows(btc_snapshot.snapshot_id)) == btc_rows
    assert list(store.rows(eth_snapshot.snapshot_id)) == eth_rows
    assert btc_snapshot.content_sha256 != eth_snapshot.content_sha256
    assert btc_snapshot.series_key.value.endswith("BTC-USDT|5m")
    assert eth_snapshot.series_key.value.endswith("ETH-USDT|5m")


def test_ingest_is_append_only_and_conflicts_do_not_cross_series(tmp_path) -> None:
    store = MultiAssetMarketStore(tmp_path / "multi-asset.sqlite3")
    timestamp = FIVE_MINUTES_MS * 20
    original = candle(timestamp, price="100", volume="10")
    conflicting = candle(timestamp, price="100", volume="999")
    store.ingest([original], instrument="BTC-USDT", observed_at=NOW)
    duplicate = store.ingest([original], instrument="BTC-USDT", observed_at=NOW)
    conflict = store.ingest([conflicting], instrument="BTC-USDT", observed_at=NOW)
    store.ingest(
        [candle(timestamp, price="200")], instrument="ETH-USDT", observed_at=NOW
    )
    confirm_origin(store, "BTC-USDT")
    confirm_origin(store, "ETH-USDT")

    assert duplicate["duplicates"] == 1
    assert conflict["conflicts"] == 1
    with sqlite3.connect(store.path) as db:
        stored_volume = db.execute(
            """
            SELECT volume_text FROM ma_candles
            WHERE inst_id = 'BTC-USDT' AND open_ts_ms = ?
            """,
            (timestamp,),
        ).fetchone()[0]
    assert stored_volume == "10"
    assert store.status("BTC-USDT")["unresolvedConflicts"] == 1
    assert store.status("ETH-USDT")["unresolvedConflicts"] == 0
    with pytest.raises(MultiAssetMarketError, match="conflicts"):
        store.create_snapshot(
            "BTC-USDT", feature_contract_sha256="b" * 64, now=CONFIRMED_AT
        )
    assert store.create_snapshot(
        "ETH-USDT", feature_contract_sha256="b" * 64, now=CONFIRMED_AT
    ).row_count == 1


def test_gap_state_and_snapshot_gate_are_per_instrument(tmp_path) -> None:
    store = MultiAssetMarketStore(tmp_path / "multi-asset.sqlite3")
    store.ingest(
        [
            candle(FIVE_MINUTES_MS * 30),
            candle(FIVE_MINUTES_MS * 32),
        ],
        instrument="BTC-USDT",
        observed_at=NOW,
    )
    eth_rows = series_rows(30, 33, price="200")
    store.ingest(eth_rows, instrument="ETH-USDT", observed_at=NOW)
    confirm_origin(store, "BTC-USDT")
    confirm_origin(store, "ETH-USDT")

    btc = store.status("BTC-USDT")
    eth = store.status("ETH-USDT")
    assert btc["missingBars"] == 1
    assert btc["gapRanges"] == [
        {
            "firstOpenTsMs": FIVE_MINUTES_MS * 31,
            "lastOpenTsMs": FIVE_MINUTES_MS * 31,
            "missingBars": 1,
        }
    ]
    assert btc["readyForSnapshot"] is False
    assert eth["missingBars"] == 0
    assert eth["readyForSnapshot"] is True
    with pytest.raises(MultiAssetMarketError, match="gaps"):
        store.create_snapshot(
            "BTC-USDT", feature_contract_sha256="c" * 64, now=CONFIRMED_AT
        )
    assert store.create_snapshot(
        "ETH-USDT", feature_contract_sha256="c" * 64, now=CONFIRMED_AT
    ).row_count == len(eth_rows)


def test_origin_needs_a_distinct_later_run_after_sixty_seconds(tmp_path) -> None:
    store = MultiAssetMarketStore(tmp_path / "multi-asset.sqlite3")
    store.ingest(
        series_rows(35, 38, price="100"),
        instrument="BTC-USDT",
        observed_at=NOW,
    )
    oldest = FIVE_MINUTES_MS * 35
    anchor_open, anchor_sha = anchor_evidence(store, "BTC-USDT")
    baseline = OriginProbe(
        run_id="origin-a",
        instrument="BTC-USDT",
        requested_after=oldest,
        terminal_cursor=oldest,
        page_limit=300,
        empty_probe_count=3,
        anchor_open_ts_ms=anchor_open,
        anchor_payload_sha256=anchor_sha,
        observed_at=NOW,
    )
    first = store.record_origin_probe(baseline)
    duplicate = store.record_origin_probe(baseline)
    assert first["role"] == "baseline"
    assert first["backfillComplete"] is False
    assert duplicate["duplicate"] is True
    assert duplicate["backfillComplete"] is False

    with pytest.raises(MultiAssetMarketError, match="current oldest"):
        store.record_origin_probe(
            OriginProbe(
                run_id="forged-limit",
                instrument="BTC-USDT",
                requested_after=oldest,
                terminal_cursor=oldest,
                page_limit=100,
                empty_probe_count=3,
                anchor_open_ts_ms=anchor_open,
                anchor_payload_sha256=anchor_sha,
                observed_at=NOW,
            )
        )
    with pytest.raises(MultiAssetMarketError, match="current oldest"):
        store.record_origin_probe(
            OriginProbe(
                run_id="forged-cursor",
                instrument="BTC-USDT",
                requested_after=oldest,
                terminal_cursor=oldest - 1,
                page_limit=300,
                empty_probe_count=3,
                anchor_open_ts_ms=anchor_open,
                anchor_payload_sha256=anchor_sha,
                observed_at=NOW,
            )
        )
    with pytest.raises(MultiAssetMarketError, match="current oldest"):
        store.record_origin_probe(
            OriginProbe(
                run_id="forged-empty-count",
                instrument="BTC-USDT",
                requested_after=oldest,
                terminal_cursor=oldest,
                page_limit=300,
                empty_probe_count=1,
                anchor_open_ts_ms=anchor_open,
                anchor_payload_sha256=anchor_sha,
                observed_at=NOW,
            )
        )
    with pytest.raises(MultiAssetMarketError, match="60 seconds"):
        store.record_origin_probe(
            OriginProbe(
                run_id="origin-too-soon",
                instrument="BTC-USDT",
                requested_after=oldest - 1,
                terminal_cursor=oldest - 1,
                page_limit=100,
                empty_probe_count=3,
                anchor_open_ts_ms=anchor_open,
                anchor_payload_sha256=anchor_sha,
                observed_at=NOW + timedelta(seconds=59),
            )
        )
    with pytest.raises(MultiAssetMarketError, match="different baseline"):
        store.record_origin_probe(
            OriginProbe(
                run_id="origin-a",
                instrument="BTC-USDT",
                requested_after=oldest - 1,
                terminal_cursor=oldest - 1,
                page_limit=100,
                empty_probe_count=3,
                anchor_open_ts_ms=anchor_open,
                anchor_payload_sha256=anchor_sha,
                observed_at=CONFIRMED_AT,
            )
        )

    confirmed = store.record_origin_probe(
        OriginProbe(
            run_id="origin-b",
            instrument="BTC-USDT",
            requested_after=oldest - 1,
            terminal_cursor=oldest - 1,
            page_limit=100,
            empty_probe_count=3,
            anchor_open_ts_ms=anchor_open,
            anchor_payload_sha256=anchor_sha,
            observed_at=CONFIRMED_AT,
        )
    )
    assert confirmed["role"] == "confirmation"
    assert confirmed["backfillComplete"] is True
    assert store.status("BTC-USDT")["originProbeCount"] == 2
    assert store.status("BTC-USDT")["syncStatus"] == "complete"

    with sqlite3.connect(store.path) as db:
        persisted = db.execute(
            """
            SELECT requested_after, terminal_cursor, page_limit,
                   empty_probe_count, observed_at, evidence_sha256
            FROM ma_origin_probes
            WHERE inst_id = 'BTC-USDT' AND run_id = 'origin-b'
            """
        ).fetchone()
    assert persisted[:4] == (oldest - 1, oldest - 1, 100, 3)
    assert persisted[4].endswith("Z")
    assert len(persisted[5]) == 64

    store.ingest(
        [candle(oldest - FIVE_MINUTES_MS)],
        instrument="BTC-USDT",
        observed_at=CONFIRMED_AT,
    )
    assert store.status("BTC-USDT")["backfillComplete"] is False
    assert store.status("BTC-USDT")["originProbeCount"] == 0


def test_snapshot_gate_rejects_a_single_origin_probe(tmp_path) -> None:
    store = MultiAssetMarketStore(tmp_path / "pending.sqlite3")
    rows = series_rows(35, 38, price="100")
    store.ingest(rows, instrument="BTC-USDT", observed_at=NOW)
    oldest = int(rows[0][0])
    anchor_open, anchor_sha = anchor_evidence(store, "BTC-USDT")
    store.record_origin_probe(
        OriginProbe(
            run_id="only-run",
            instrument="BTC-USDT",
            requested_after=oldest,
            terminal_cursor=oldest,
            page_limit=300,
            empty_probe_count=3,
            anchor_open_ts_ms=anchor_open,
            anchor_payload_sha256=anchor_sha,
            observed_at=NOW,
        )
    )
    with pytest.raises(MultiAssetMarketError, match="two runs"):
        store.create_snapshot(
            "BTC-USDT", feature_contract_sha256="3" * 64, now=NOW
        )


def test_only_closed_confirmed_well_formed_5m_candles_are_stored(tmp_path) -> None:
    store = MultiAssetMarketStore(tmp_path / "multi-asset.sqlite3")
    closed = candle(FIVE_MINUTES_MS * 40)
    unconfirmed = candle(FIVE_MINUTES_MS * 41, confirm="0")
    result = store.ingest(
        [closed, unconfirmed], instrument="BTC-USDT", observed_at=NOW
    )
    assert result["inserted"] == 1
    assert result["unconfirmed"] == 1

    malformed = candle(FIVE_MINUTES_MS * 42)
    malformed[2] = "1"
    with pytest.raises(MultiAssetMarketError, match="OHLC"):
        store.ingest([malformed], instrument="BTC-USDT", observed_at=NOW)
    with pytest.raises(MultiAssetMarketError, match="5m"):
        store.ingest([closed], instrument="BTC-USDT", bar="1m", observed_at=NOW)
    with pytest.raises(MultiAssetMarketError, match="uppercase SPOT"):
        store.ingest([closed], instrument="btc-usdt", observed_at=NOW)

    future_open = round(NOW.timestamp() * 1_000 // FIVE_MINUTES_MS) * FIVE_MINUTES_MS
    with pytest.raises(MultiAssetMarketError, match="future"):
        store.ingest(
            [candle(future_open)], instrument="ETH-USDT", observed_at=NOW
        )


def test_snapshot_is_idempotent_reiterable_and_detects_candle_tampering(tmp_path) -> None:
    store = MultiAssetMarketStore(tmp_path / "multi-asset.sqlite3")
    rows = series_rows(50, 53, price="100")
    store.ingest(rows, instrument="BTC-USDT", observed_at=NOW)
    confirm_origin(store, "BTC-USDT")
    snapshot = store.create_snapshot(
        "BTC-USDT", feature_contract_sha256="d" * 64, now=CONFIRMED_AT
    )
    repeated = store.create_snapshot(
        "BTC-USDT", feature_contract_sha256="d" * 64, now=CONFIRMED_AT
    )
    view = store.rows(snapshot.snapshot_id)

    assert repeated.snapshot_id == snapshot.snapshot_id
    assert len(view) == 3
    assert list(view) == rows
    assert list(view) == rows
    assert store.snapshot_is_current(snapshot.content_sha256) is True

    tampered = list(rows[1])
    tampered[4] = "777"
    tampered_sha256 = hashlib.sha256(
        json.dumps(
            tampered,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    with sqlite3.connect(store.path) as db:
        db.execute(
            """
            UPDATE ma_candles SET close_text = '777', payload_sha256 = ?
            WHERE inst_id = 'BTC-USDT' AND open_ts_ms = ?
            """,
            (tampered_sha256, FIVE_MINUTES_MS * 51),
        )
    with pytest.raises(MultiAssetMarketError, match="snapshot content hash"):
        list(store.rows(snapshot.snapshot_id))
    with pytest.raises(MultiAssetMarketError, match="snapshot content hash"):
        store.status("BTC-USDT")


def test_snapshot_currentness_requires_an_exact_clean_complete_series(tmp_path) -> None:
    store = MultiAssetMarketStore(tmp_path / "multi-asset.sqlite3")
    btc_rows = series_rows(60, 63, price="100")
    eth_rows = series_rows(60, 63, price="200")
    store.ingest(btc_rows, instrument="BTC-USDT", observed_at=NOW)
    store.ingest(eth_rows, instrument="ETH-USDT", observed_at=NOW)
    confirm_origin(store, "BTC-USDT")
    confirm_origin(store, "ETH-USDT")
    btc = store.create_snapshot(
        "BTC-USDT", feature_contract_sha256="e" * 64, now=CONFIRMED_AT
    )
    eth = store.create_snapshot(
        "ETH-USDT", feature_contract_sha256="e" * 64, now=CONFIRMED_AT
    )

    store.ingest(
        [candle(FIVE_MINUTES_MS * 63, price="100")],
        instrument="BTC-USDT",
        observed_at=NOW,
    )
    assert store.snapshot_is_current(btc.content_sha256) is False
    assert store.status("BTC-USDT")["latestSnapshotCurrent"] is False
    assert store.snapshot_is_current(eth.content_sha256) is True

    eth_conflict = candle(FIVE_MINUTES_MS * 61, price="200", volume="999")
    store.ingest([eth_conflict], instrument="ETH-USDT", observed_at=NOW)
    assert store.snapshot_is_current(eth.content_sha256) is False
    assert store.snapshot_is_current(btc.content_sha256) is False

    store.ingest(
        [candle(FIVE_MINUTES_MS * 59, price="100")],
        instrument="BTC-USDT",
        observed_at=NOW,
    )
    assert store.snapshot_is_current(btc.content_sha256) is False


def test_initialization_does_not_rewrite_another_instances_running_task(tmp_path) -> None:
    path = tmp_path / "shared.sqlite3"
    first = MultiAssetMarketStore(path)
    first.ingest(
        [candle(FIVE_MINUTES_MS * 70)],
        instrument="BTC-USDT",
        observed_at=NOW,
    )
    with sqlite3.connect(path) as db:
        db.execute(
            """
            CREATE TABLE coordinator_tasks (
                task_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                owner TEXT NOT NULL
            )
            """
        )
        db.execute(
            """
            INSERT INTO coordinator_tasks(task_id, status, owner)
            VALUES ('history-eth', 'running', 'other-instance')
            """
        )

    second = MultiAssetMarketStore(path)
    assert second.status("BTC-USDT")["storedRows"] == 1
    with sqlite3.connect(path) as db:
        task = db.execute(
            "SELECT status, owner FROM coordinator_tasks WHERE task_id = 'history-eth'"
        ).fetchone()
    assert task == ("running", "other-instance")


def test_shared_drive_database_uses_delete_journal_and_full_sync(tmp_path) -> None:
    store = MultiAssetMarketStore(tmp_path / "multi-asset.sqlite3")
    store.ingest(
        [candle(FIVE_MINUTES_MS * 75)],
        instrument="BTC-USDT",
        observed_at=NOW,
    )
    with store._connection() as db:
        journal_mode = str(db.execute("PRAGMA journal_mode").fetchone()[0]).lower()
        synchronous = int(db.execute("PRAGMA synchronous").fetchone()[0])
    assert journal_mode == "delete"
    assert synchronous == 2
    with sqlite3.connect(store.path) as raw_db:
        assert str(raw_db.execute("PRAGMA journal_mode").fetchone()[0]).lower() != "wal"
    assert not store.path.with_name(f"{store.path.name}-wal").exists()
    assert not store.path.with_name(f"{store.path.name}-shm").exists()


def test_empty_series_status_is_safe_and_snapshot_hash_is_validated(tmp_path) -> None:
    store = MultiAssetMarketStore(tmp_path / "multi-asset.sqlite3")
    status = store.status("SOL-USDT")
    assert status["storedRows"] == 0
    assert status["expectedRows"] == 0
    assert status["latestSnapshot"] is None
    assert status["readyForSnapshot"] is False
    with pytest.raises(MultiAssetMarketError, match="empty"):
        store.create_snapshot(
            "SOL-USDT", feature_contract_sha256="f" * 64, now=NOW
        )
    with pytest.raises(MultiAssetMarketError, match="feature contract"):
        store.create_snapshot(
            "SOL-USDT", feature_contract_sha256="not-a-hash", now=NOW
        )
    assert store.snapshot_is_current(None) is False
    assert store.snapshot_is_current("0" * 63) is False
