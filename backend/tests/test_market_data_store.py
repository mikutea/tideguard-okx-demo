from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from okx_demo_lab.ml.market_data import (
    ORIGIN_CONFIRMATION_DELAY,
    MarketDataError,
    MarketDataStore,
)
from okx_demo_lab.ml.pipeline import BAR_MILLISECONDS
from okx_demo_lab.okx_client import OkxClient, OkxClientError


NOW = datetime(2026, 8, 21, 0, 0, tzinfo=timezone.utc)


def candle(timestamp: int, *, close: str | None = None, confirm: str = "1") -> list[str]:
    value = close or str(timestamp // BAR_MILLISECONDS + 100)
    price = float(value)
    return [
        str(timestamp),
        value,
        str(price + 2),
        str(price - 2),
        value,
        "10",
        "20",
        "20",
        confirm,
    ]


def history_transport(
    rows: list[list[str]],
    *,
    server_cap: int = 2,
    failures: list[str] | None = None,
) -> tuple[httpx.MockTransport, list[tuple[str | None, str]]]:
    requests: list[tuple[str | None, str]] = []
    pending_failures = list(failures or [])

    async def handler(request: httpx.Request) -> httpx.Response:
        after = request.url.params.get("after")
        requests.append((after, request.url.params["limit"]))
        if pending_failures:
            code = pending_failures.pop(0)
            if code == "http429":
                return httpx.Response(429, json={"code": "50011", "msg": "slow"})
            return httpx.Response(200, json={"code": code, "msg": "slow", "data": []})
        upper = int(after) if after else 2**63 - 1
        eligible = [row for row in rows if int(row[0]) < upper]
        page = sorted(eligible, key=lambda row: int(row[0]), reverse=True)[:server_cap]
        return httpx.Response(200, json={"code": "0", "msg": "", "data": page})

    return httpx.MockTransport(handler), requests


@pytest.mark.asyncio
async def test_full_history_sync_accepts_short_pages_and_stops_only_on_empty(tmp_path) -> None:
    rows = [candle(BAR_MILLISECONDS * index) for index in range(10, 16)]
    transport, requests = history_transport(rows, server_cap=2)
    client = OkxClient(transport=transport, history_page_delay_seconds=0)
    store = MarketDataStore(tmp_path / "market-data.sqlite3")
    try:
        unconfirmed = await store.sync_all(client, now=NOW)
        result = await store.sync_all(
            client,
            now=NOW + ORIGIN_CONFIRMATION_DELAY + timedelta(seconds=1),
        )
    finally:
        await client.close()

    assert unconfirmed["backfillComplete"] is False
    assert unconfirmed["syncStatus"] == "partial"
    assert unconfirmed["lastErrorType"] == "HistoryOriginUnconfirmed"
    assert result["backfillComplete"] is True
    assert result["storedRows"] == 6
    assert result["missingBars"] == 0
    assert result["unresolvedConflicts"] == 0
    assert all(limit in {"100", "300"} for _after, limit in requests)
    assert requests[-1][1] == "100"
    assert requests[-1][0] == str(BAR_MILLISECONDS * 10 - 1)
    assert len(requests) >= 4

    snapshot = store.create_snapshot(
        feature_contract_sha256="a" * 64,
        now=NOW,
    )
    assert snapshot.row_count == 6
    assert list(store.snapshot_rows(snapshot.snapshot_id)) == rows
    assert store.create_snapshot(
        feature_contract_sha256="a" * 64,
        now=NOW,
    ).snapshot_id == snapshot.snapshot_id


@pytest.mark.asyncio
async def test_history_sync_resumes_from_durable_oldest_row_after_partial_run(tmp_path) -> None:
    rows = [candle(BAR_MILLISECONDS * index) for index in range(20, 28)]
    transport, _requests = history_transport(rows, server_cap=2)
    client = OkxClient(transport=transport, history_page_delay_seconds=0)
    store = MarketDataStore(tmp_path / "market-data.sqlite3")
    try:
        partial = await store.sync_all(client, now=NOW, max_pages=2)
        unconfirmed = await store.sync_all(client, now=NOW + timedelta(seconds=1))
        completed = await store.sync_all(
            client,
            now=NOW + ORIGIN_CONFIRMATION_DELAY + timedelta(seconds=2),
        )
    finally:
        await client.close()

    assert partial["backfillComplete"] is False
    assert 0 < partial["storedRows"] < len(rows)
    assert unconfirmed["backfillComplete"] is False
    assert completed["backfillComplete"] is True
    assert completed["storedRows"] == len(rows)


@pytest.mark.asyncio
async def test_new_sync_clears_a_previously_complete_latch_until_origin_is_reproved(
    tmp_path,
) -> None:
    initial_rows = [candle(BAR_MILLISECONDS * index) for index in range(100, 104)]
    initial_transport, _requests = history_transport(initial_rows, server_cap=2)
    initial_client = OkxClient(
        transport=initial_transport,
        history_page_delay_seconds=0,
    )
    store = MarketDataStore(tmp_path / "market-data.sqlite3")
    try:
        unconfirmed = await store.sync_all(initial_client, now=NOW)
        completed = await store.sync_all(
            initial_client,
            now=NOW + ORIGIN_CONFIRMATION_DELAY + timedelta(seconds=1),
        )
    finally:
        await initial_client.close()
    assert unconfirmed["backfillComplete"] is False
    assert completed["backfillComplete"] is True

    extended_rows = [candle(BAR_MILLISECONDS * index) for index in range(98, 104)]
    extended_transport, _requests = history_transport(extended_rows, server_cap=2)
    extended_client = OkxClient(
        transport=extended_transport,
        history_page_delay_seconds=0,
    )
    try:
        partial = await store.sync_all(
            extended_client,
            now=NOW + ORIGIN_CONFIRMATION_DELAY + timedelta(seconds=2),
            max_pages=1,
        )
        extended = await store.sync_all(
            extended_client,
            now=NOW + ORIGIN_CONFIRMATION_DELAY + timedelta(seconds=3),
        )
        reproved = await store.sync_all(
            extended_client,
            now=NOW + ORIGIN_CONFIRMATION_DELAY * 2 + timedelta(seconds=4),
        )
    finally:
        await extended_client.close()

    assert partial["backfillComplete"] is False
    assert partial["syncStatus"] == "partial"
    assert extended["backfillComplete"] is False
    assert reproved["backfillComplete"] is True
    assert reproved["storedRows"] == len(extended_rows)


@pytest.mark.asyncio
async def test_history_client_retries_rate_limit_and_requires_decreasing_cursor() -> None:
    rows = [candle(BAR_MILLISECONDS * index) for index in range(30, 34)]
    transport, requests = history_transport(
        rows,
        server_cap=2,
        failures=["50011", "http429"],
    )
    client = OkxClient(transport=transport, history_page_delay_seconds=0)
    try:
        pages = []
        async for page, cursor in client.iter_history_candle_pages(page_limit=300):
            pages.append((page, cursor))
            if len(pages) == 1:
                break
    finally:
        await client.close()
    assert len(requests) == 3
    assert len(pages[0][0]) == 2

    async def stalled_handler(request: httpx.Request) -> httpx.Response:
        after = request.url.params.get("after")
        timestamp = int(after) if after else BAR_MILLISECONDS * 40
        return httpx.Response(
            200,
            json={"code": "0", "msg": "", "data": [candle(timestamp)]},
        )

    stalled = OkxClient(
        transport=httpx.MockTransport(stalled_handler),
        history_page_delay_seconds=0,
    )
    try:
        with pytest.raises(OkxClientError, match="严格递减"):
            async for _page, _cursor in stalled.iter_history_candle_pages(
                after=BAR_MILLISECONDS * 40
            ):
                pass
    finally:
        await stalled.close()


@pytest.mark.asyncio
async def test_history_client_does_not_treat_one_successful_empty_page_as_origin() -> None:
    rows = [candle(BAR_MILLISECONDS * index) for index in range(70, 74)]
    requests: list[str | None] = []
    returned_transient_empty = False

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal returned_transient_empty
        after = request.url.params.get("after")
        requests.append(after)
        upper = int(after) if after else 2**63 - 1
        if after is not None and not returned_transient_empty:
            returned_transient_empty = True
            return httpx.Response(200, json={"code": "0", "msg": "", "data": []})
        eligible = [row for row in rows if int(row[0]) < upper]
        page = sorted(eligible, key=lambda row: int(row[0]), reverse=True)[:2]
        return httpx.Response(200, json={"code": "0", "msg": "", "data": page})

    client = OkxClient(
        transport=httpx.MockTransport(handler),
        history_page_delay_seconds=0,
    )
    try:
        collected: list[list[str]] = []
        async for page, _cursor in client.iter_history_candle_pages(page_limit=300):
            collected.extend(page)
    finally:
        await client.close()

    assert {int(row[0]) for row in collected} == {
        BAR_MILLISECONDS * index for index in range(70, 74)
    }
    assert requests.count(str(BAR_MILLISECONDS * 72)) == 2


@pytest.mark.asyncio
async def test_conflict_and_gap_block_snapshot_without_overwriting_confirmed_data(tmp_path) -> None:
    rows = [
        candle(BAR_MILLISECONDS * 50),
        candle(BAR_MILLISECONDS * 52),
    ]
    transport, _requests = history_transport(rows, server_cap=2)
    client = OkxClient(transport=transport, history_page_delay_seconds=0)
    store = MarketDataStore(tmp_path / "market-data.sqlite3")
    try:
        await store.sync_all(client, now=NOW)
        result = await store.sync_all(
            client,
            now=NOW + ORIGIN_CONFIRMATION_DELAY + timedelta(seconds=1),
        )
    finally:
        await client.close()
    assert result["missingBars"] == 1
    with pytest.raises(MarketDataError, match="gaps"):
        store.create_snapshot(feature_contract_sha256="b" * 64, now=NOW)

    conflict = candle(BAR_MILLISECONDS * 50)
    conflict[5] = "999"
    ingest = store.ingest_page([conflict], observed_at=NOW)
    assert ingest["conflicts"] == 1
    with sqlite3.connect(store.path) as db:
        stored_volume = db.execute(
            "SELECT volume_text FROM market_candles WHERE open_ts_ms = ?",
            (BAR_MILLISECONDS * 50,),
        ).fetchone()[0]
    assert stored_volume != "999"
    with pytest.raises(MarketDataError, match="conflicts"):
        store.create_snapshot(feature_contract_sha256="b" * 64, now=NOW)


@pytest.mark.asyncio
async def test_later_sync_repairs_a_deep_internal_gap_when_source_recovers(tmp_path) -> None:
    missing_rows = [
        candle(BAR_MILLISECONDS * index)
        for index in range(10, 21)
        if index != 15
    ]
    transport, _requests = history_transport(missing_rows, server_cap=3)
    client = OkxClient(transport=transport, history_page_delay_seconds=0)
    store = MarketDataStore(tmp_path / "market-data.sqlite3")
    try:
        await store.sync_all(client, now=NOW)
        incomplete = await store.sync_all(
            client,
            now=NOW + ORIGIN_CONFIRMATION_DELAY + timedelta(seconds=1),
        )
    finally:
        await client.close()
    assert incomplete["backfillComplete"] is True
    assert incomplete["missingBars"] == 1

    complete_rows = [candle(BAR_MILLISECONDS * index) for index in range(10, 21)]
    repaired_transport, _requests = history_transport(complete_rows, server_cap=3)
    repaired_client = OkxClient(
        transport=repaired_transport,
        history_page_delay_seconds=0,
    )
    try:
        repaired = await store.sync_all(
            repaired_client,
            now=NOW + ORIGIN_CONFIRMATION_DELAY + timedelta(seconds=2),
        )
    finally:
        await repaired_client.close()

    assert repaired["backfillComplete"] is True
    assert repaired["storedRows"] == len(complete_rows)
    assert repaired["missingBars"] == 0


def test_unconfirmed_and_malformed_candles_are_rejected_or_excluded(tmp_path) -> None:
    store = MarketDataStore(tmp_path / "market-data.sqlite3")
    result = store.ingest_page(
        [candle(BAR_MILLISECONDS * 60, confirm="0")], observed_at=NOW
    )
    assert result == {
        "conflicts": 0,
        "duplicates": 0,
        "inserted": 0,
        "unconfirmed": 1,
    }
    malformed = candle(BAR_MILLISECONDS * 61)
    malformed[2] = "1"
    with pytest.raises(MarketDataError, match="OHLC"):
        store.ingest_page([malformed], observed_at=NOW)


def test_snapshot_reader_detects_local_candle_tampering(tmp_path) -> None:
    store = MarketDataStore(tmp_path / "market-data.sqlite3")
    rows = [candle(BAR_MILLISECONDS * index) for index in range(70, 73)]
    store.ingest_page(rows, observed_at=NOW)
    with sqlite3.connect(store.path) as db:
        db.execute(
            """
            UPDATE market_sync_state
            SET backfill_complete = 1, stored_rows = 3,
                first_open_ts_ms = ?, last_open_ts_ms = ?
            """,
            (BAR_MILLISECONDS * 70, BAR_MILLISECONDS * 72),
        )
    snapshot = store.create_snapshot(feature_contract_sha256="c" * 64, now=NOW)
    with sqlite3.connect(store.path) as db:
        db.execute(
            "UPDATE market_candles SET close_text = '777' WHERE open_ts_ms = ?",
            (BAR_MILLISECONDS * 71,),
        )
    with pytest.raises(MarketDataError, match="payload hash"):
        list(store.snapshot_rows(snapshot.snapshot_id))


def test_snapshot_currentness_tracks_the_warehouse_origin_not_the_head(tmp_path) -> None:
    store = MarketDataStore(tmp_path / "market-data.sqlite3")
    rows = [candle(BAR_MILLISECONDS * index) for index in range(80, 83)]
    store.ingest_page(rows, observed_at=NOW)
    with sqlite3.connect(store.path) as db:
        db.execute(
            """
            UPDATE market_sync_state
            SET backfill_complete = 1, stored_rows = 3,
                first_open_ts_ms = ?, last_open_ts_ms = ?
            """,
            (BAR_MILLISECONDS * 80, BAR_MILLISECONDS * 82),
        )
    snapshot = store.create_snapshot(feature_contract_sha256="d" * 64, now=NOW)
    assert store.snapshot_is_current(snapshot.content_sha256) is True

    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE market_sync_state SET missing_bars = 1")
    assert store.snapshot_is_current(snapshot.content_sha256) is False
    with sqlite3.connect(store.path) as db:
        db.execute(
            "UPDATE market_sync_state SET missing_bars = 0, unresolved_conflicts = 1"
        )
    assert store.snapshot_is_current(snapshot.content_sha256) is False
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE market_sync_state SET unresolved_conflicts = 0")
    assert store.snapshot_is_current(snapshot.content_sha256) is True

    newer = candle(BAR_MILLISECONDS * 83)
    store.ingest_page([newer], observed_at=NOW)
    with sqlite3.connect(store.path) as db:
        db.execute(
            """
            UPDATE market_sync_state
            SET stored_rows = 4, last_open_ts_ms = ?
            """,
            (BAR_MILLISECONDS * 83,),
        )
    assert store.snapshot_is_current(snapshot.content_sha256) is True

    older = candle(BAR_MILLISECONDS * 79)
    store.ingest_page([older], observed_at=NOW)
    with sqlite3.connect(store.path) as db:
        db.execute(
            """
            UPDATE market_sync_state
            SET stored_rows = 5, first_open_ts_ms = ?
            """,
            (BAR_MILLISECONDS * 79,),
        )
    assert store.snapshot_is_current(snapshot.content_sha256) is False
