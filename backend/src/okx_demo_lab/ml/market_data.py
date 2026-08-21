from __future__ import annotations

import hashlib
import hmac
import sqlite3
import threading
import uuid
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from ..okx_client import OkxClient
from ..sqlite_runtime import configure_sqlite_connection
from .pipeline import BAR_MILLISECONDS
from .strategy import canonical_json, sha256_hex


MARKET_DATA_SCHEMA_VERSION = 1
SNAPSHOT_SCHEMA_VERSION = "tideguard.market-snapshot.v1"
OKX_PUBLIC_SOURCE = "okx-public-v5"
DEFAULT_INSTRUMENT = "BTC-USDT"
DEFAULT_BAR = "5m"
ORIGIN_CONFIRMATION_DELAY = timedelta(seconds=60)


class MarketDataError(RuntimeError):
    pass


def _utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise MarketDataError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return _utc(value, "timestamp").isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _parse_iso(value: str, name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise MarketDataError(f"{name} is invalid") from exc
    return _utc(parsed, name)


def _decimal(value: str, name: str, *, allow_zero: bool) -> Decimal:
    try:
        parsed = Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise MarketDataError(f"{name} is invalid") from exc
    if not parsed.is_finite() or parsed < 0 or (parsed == 0 and not allow_zero):
        raise MarketDataError(f"{name} is invalid")
    return parsed


def _normalize_candle(row: Sequence[Any]) -> tuple[str, ...] | None:
    if not isinstance(row, (list, tuple)) or len(row) != 9:
        raise MarketDataError("OKX candle does not match the 9-field schema")
    if any(
        isinstance(value, bool) or not isinstance(value, (str, int, float))
        for value in row
    ):
        raise MarketDataError("OKX candle fields must be scalar values")
    normalized = tuple(str(value).strip() for value in row)
    timestamp_text = normalized[0]
    if not timestamp_text.isdigit():
        raise MarketDataError("OKX candle timestamp is invalid")
    timestamp = int(timestamp_text)
    if timestamp <= 0 or timestamp % BAR_MILLISECONDS != 0:
        raise MarketDataError("OKX candle timestamp is not aligned to the 5m grid")
    if normalized[8] == "0":
        return None
    if normalized[8] != "1":
        raise MarketDataError("OKX candle confirm flag is invalid")

    open_price = _decimal(normalized[1], "open", allow_zero=False)
    high = _decimal(normalized[2], "high", allow_zero=False)
    low = _decimal(normalized[3], "low", allow_zero=False)
    close = _decimal(normalized[4], "close", allow_zero=False)
    for index, name in ((5, "volume"), (6, "volume currency"), (7, "quote volume")):
        _decimal(normalized[index], name, allow_zero=True)
    if low > high or high < max(open_price, close) or low > min(open_price, close):
        raise MarketDataError("OKX candle OHLC values are inconsistent")
    return normalized


def _payload_sha256(row: Sequence[str]) -> str:
    return sha256_hex(canonical_json(list(row)))


@dataclass(frozen=True)
class MarketSnapshot:
    snapshot_id: str
    source: str
    instrument: str
    bar: str
    first_open_ts_ms: int
    last_open_ts_ms: int
    row_count: int
    content_sha256: str
    feature_contract_sha256: str
    created_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "bar": self.bar,
            "contentSha256": self.content_sha256,
            "createdAt": _iso(self.created_at),
            "featureContractSha256": self.feature_contract_sha256,
            "firstOpenAt": _iso(
                datetime.fromtimestamp(self.first_open_ts_ms / 1_000, tz=timezone.utc)
            ),
            "instrument": self.instrument,
            "lastOpenAt": _iso(
                datetime.fromtimestamp(self.last_open_ts_ms / 1_000, tz=timezone.utc)
            ),
            "rowCount": self.row_count,
            "schemaVersion": SNAPSHOT_SCHEMA_VERSION,
            "snapshotId": self.snapshot_id,
            "source": self.source,
        }


class CandleSnapshotRows:
    """A re-iterable, integrity-checked view over one immutable snapshot range."""

    def __init__(self, path: Path, snapshot: MarketSnapshot):
        self.path = path
        self.snapshot = snapshot

    def __len__(self) -> int:
        return self.snapshot.row_count

    def __iter__(self) -> Iterator[list[str]]:
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        digest = hashlib.sha256()
        metadata = {
            "bar": self.snapshot.bar,
            "feature_contract_sha256": self.snapshot.feature_contract_sha256,
            "first_open_ts_ms": self.snapshot.first_open_ts_ms,
            "instrument": self.snapshot.instrument,
            "last_open_ts_ms": self.snapshot.last_open_ts_ms,
            "row_count": self.snapshot.row_count,
            "schema_version": SNAPSHOT_SCHEMA_VERSION,
            "source": self.snapshot.source,
        }
        digest.update(canonical_json(metadata).encode("utf-8"))
        digest.update(b"\n")
        seen = 0
        try:
            cursor = db.execute(
                """
                SELECT open_ts_ms, open_text, high_text, low_text, close_text,
                       volume_text, volume_ccy_text, volume_quote_text,
                       confirm, payload_sha256
                FROM market_candles
                WHERE source = ? AND inst_id = ? AND bar = ?
                  AND open_ts_ms BETWEEN ? AND ?
                ORDER BY open_ts_ms ASC
                """,
                (
                    self.snapshot.source,
                    self.snapshot.instrument,
                    self.snapshot.bar,
                    self.snapshot.first_open_ts_ms,
                    self.snapshot.last_open_ts_ms,
                ),
            )
            for stored in cursor:
                row = [
                    str(stored["open_ts_ms"]),
                    stored["open_text"],
                    stored["high_text"],
                    stored["low_text"],
                    stored["close_text"],
                    stored["volume_text"],
                    stored["volume_ccy_text"],
                    stored["volume_quote_text"],
                    str(stored["confirm"]),
                ]
                payload_sha = _payload_sha256(row)
                if not hmac.compare_digest(payload_sha, str(stored["payload_sha256"])):
                    raise MarketDataError("stored candle payload hash mismatch")
                digest.update(canonical_json(row).encode("utf-8"))
                digest.update(b"\n")
                seen += 1
                yield row
        finally:
            db.close()
        if seen != self.snapshot.row_count:
            raise MarketDataError("snapshot row count changed after creation")
        if not hmac.compare_digest(digest.hexdigest(), self.snapshot.content_sha256):
            raise MarketDataError("snapshot content hash changed after creation")


class MarketDataStore:
    """Append-oriented cache for confirmed OKX public candles and snapshots."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        configure_sqlite_connection(db, self.path)
        return db

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        db = self._connect()
        try:
            with db:
                yield db
        finally:
            db.close()

    def _initialize(self) -> None:
        now = _iso(datetime.now(timezone.utc))
        with self._connection() as db:
            version = int(db.execute("PRAGMA user_version").fetchone()[0])
            if version not in {0, MARKET_DATA_SCHEMA_VERSION}:
                raise MarketDataError("market data schema is newer than this application")
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS market_candles (
                    source TEXT NOT NULL,
                    inst_id TEXT NOT NULL,
                    bar TEXT NOT NULL,
                    open_ts_ms INTEGER NOT NULL CHECK(open_ts_ms > 0),
                    open_text TEXT NOT NULL,
                    high_text TEXT NOT NULL,
                    low_text TEXT NOT NULL,
                    close_text TEXT NOT NULL,
                    volume_text TEXT NOT NULL,
                    volume_ccy_text TEXT NOT NULL,
                    volume_quote_text TEXT NOT NULL,
                    confirm INTEGER NOT NULL CHECK(confirm = 1),
                    payload_sha256 TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    PRIMARY KEY(source, inst_id, bar, open_ts_ms)
                ) WITHOUT ROWID;
                CREATE TABLE IF NOT EXISTS candle_conflicts (
                    source TEXT NOT NULL,
                    inst_id TEXT NOT NULL,
                    bar TEXT NOT NULL,
                    open_ts_ms INTEGER NOT NULL,
                    stored_sha256 TEXT NOT NULL,
                    observed_sha256 TEXT NOT NULL,
                    observed_payload_json TEXT NOT NULL,
                    detected_at TEXT NOT NULL,
                    resolved_at TEXT,
                    PRIMARY KEY(source, inst_id, bar, open_ts_ms, observed_sha256)
                ) WITHOUT ROWID;
                CREATE TABLE IF NOT EXISTS market_sync_state (
                    source TEXT NOT NULL,
                    inst_id TEXT NOT NULL,
                    bar TEXT NOT NULL,
                    sync_status TEXT NOT NULL,
                    backfill_complete INTEGER NOT NULL CHECK(backfill_complete IN (0,1)),
                    cursor_ts_ms INTEGER,
                    stored_rows INTEGER NOT NULL,
                    first_open_ts_ms INTEGER,
                    last_open_ts_ms INTEGER,
                    missing_bars INTEGER NOT NULL,
                    unresolved_conflicts INTEGER NOT NULL,
                    pages_fetched INTEGER NOT NULL,
                    rows_inserted INTEGER NOT NULL,
                    last_sync_at TEXT,
                    last_error_type TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(source, inst_id, bar)
                ) WITHOUT ROWID;
                CREATE TABLE IF NOT EXISTS market_sync_runs (
                    run_id TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    inst_id TEXT NOT NULL,
                    bar TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    pages_fetched INTEGER NOT NULL,
                    rows_inserted INTEGER NOT NULL,
                    cursor_ts_ms INTEGER,
                    error_type TEXT
                );
                CREATE TABLE IF NOT EXISTS dataset_snapshots (
                    snapshot_id TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    inst_id TEXT NOT NULL,
                    bar TEXT NOT NULL,
                    first_open_ts_ms INTEGER NOT NULL,
                    last_open_ts_ms INTEGER NOT NULL,
                    row_count INTEGER NOT NULL,
                    content_sha256 TEXT NOT NULL UNIQUE,
                    feature_contract_sha256 TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            db.execute(f"PRAGMA user_version={MARKET_DATA_SCHEMA_VERSION}")
            db.execute(
                """
                INSERT OR IGNORE INTO market_sync_state
                (source, inst_id, bar, sync_status, backfill_complete,
                 cursor_ts_ms, stored_rows, first_open_ts_ms, last_open_ts_ms,
                 missing_bars, unresolved_conflicts, pages_fetched,
                 rows_inserted, last_sync_at, last_error_type, updated_at)
                VALUES (?, ?, ?, 'idle', 0, NULL, 0, NULL, NULL,
                        0, 0, 0, 0, NULL, NULL, ?)
                """,
                (OKX_PUBLIC_SOURCE, DEFAULT_INSTRUMENT, DEFAULT_BAR, now),
            )
            db.execute(
                """
                UPDATE market_sync_runs
                SET status = 'failed', completed_at = ?, error_type = 'ProcessRestart'
                WHERE status = 'running'
                """,
                (now,),
            )

    @staticmethod
    def _validate_market(inst_id: str, bar: str) -> None:
        if inst_id != DEFAULT_INSTRUMENT or bar != DEFAULT_BAR:
            raise MarketDataError("market data store is fixed to BTC-USDT 5m")

    def ingest_page(
        self,
        rows: Sequence[Sequence[Any]],
        *,
        observed_at: datetime,
        inst_id: str = DEFAULT_INSTRUMENT,
        bar: str = DEFAULT_BAR,
    ) -> dict[str, int]:
        self._validate_market(inst_id, bar)
        timestamp = _iso(observed_at)
        observed_at_ms = round(_utc(observed_at, "observed_at").timestamp() * 1_000)
        normalized_rows: list[tuple[str, ...]] = []
        unconfirmed = 0
        for row in rows:
            normalized = _normalize_candle(row)
            if normalized is None:
                unconfirmed += 1
            else:
                if int(normalized[0]) + BAR_MILLISECONDS > observed_at_ms + 2_000:
                    raise MarketDataError("confirmed candle closes in the future")
                normalized_rows.append(normalized)
        inserted = 0
        duplicates = 0
        conflicts = 0
        with self._lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            for row in normalized_rows:
                open_ts_ms = int(row[0])
                payload_sha = _payload_sha256(row)
                existing = db.execute(
                    """
                    SELECT payload_sha256 FROM market_candles
                    WHERE source = ? AND inst_id = ? AND bar = ? AND open_ts_ms = ?
                    """,
                    (OKX_PUBLIC_SOURCE, inst_id, bar, open_ts_ms),
                ).fetchone()
                if existing is None:
                    db.execute(
                        """
                        INSERT INTO market_candles
                        (source, inst_id, bar, open_ts_ms, open_text, high_text,
                         low_text, close_text, volume_text, volume_ccy_text,
                         volume_quote_text, confirm, payload_sha256,
                         first_seen_at, last_seen_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
                        """,
                        (
                            OKX_PUBLIC_SOURCE,
                            inst_id,
                            bar,
                            open_ts_ms,
                            *row[1:8],
                            payload_sha,
                            timestamp,
                            timestamp,
                        ),
                    )
                    inserted += 1
                elif hmac.compare_digest(str(existing["payload_sha256"]), payload_sha):
                    db.execute(
                        """
                        UPDATE market_candles SET last_seen_at = ?
                        WHERE source = ? AND inst_id = ? AND bar = ? AND open_ts_ms = ?
                        """,
                        (timestamp, OKX_PUBLIC_SOURCE, inst_id, bar, open_ts_ms),
                    )
                    duplicates += 1
                else:
                    conflict_insert = db.execute(
                        """
                        INSERT OR IGNORE INTO candle_conflicts
                        (source, inst_id, bar, open_ts_ms, stored_sha256,
                         observed_sha256, observed_payload_json, detected_at, resolved_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)
                        """,
                        (
                            OKX_PUBLIC_SOURCE,
                            inst_id,
                            bar,
                            open_ts_ms,
                            existing["payload_sha256"],
                            payload_sha,
                            canonical_json(list(row)),
                            timestamp,
                        ),
                    )
                    conflicts += max(0, conflict_insert.rowcount)
            if normalized_rows:
                page_first = min(int(row[0]) for row in normalized_rows)
                page_last = max(int(row[0]) for row in normalized_rows)
                db.execute(
                    """
                    UPDATE market_sync_state
                    SET stored_rows = stored_rows + ?,
                        first_open_ts_ms = CASE
                            WHEN first_open_ts_ms IS NULL THEN ?
                            ELSE MIN(first_open_ts_ms, ?)
                        END,
                        last_open_ts_ms = CASE
                            WHEN last_open_ts_ms IS NULL THEN ?
                            ELSE MAX(last_open_ts_ms, ?)
                        END,
                        unresolved_conflicts = (
                            SELECT COUNT(*) FROM candle_conflicts
                            WHERE source = ? AND inst_id = ? AND bar = ?
                              AND resolved_at IS NULL
                        ),
                        updated_at = ?
                    WHERE source = ? AND inst_id = ? AND bar = ?
                    """,
                    (
                        inserted,
                        page_first,
                        page_first,
                        page_last,
                        page_last,
                        OKX_PUBLIC_SOURCE,
                        inst_id,
                        bar,
                        timestamp,
                        OKX_PUBLIC_SOURCE,
                        inst_id,
                        bar,
                    ),
                )
        return {
            "conflicts": conflicts,
            "duplicates": duplicates,
            "inserted": inserted,
            "unconfirmed": unconfirmed,
        }

    def _coverage(self, db: sqlite3.Connection) -> tuple[int, int | None, int | None]:
        row = db.execute(
            """
            SELECT COUNT(*) AS stored_rows, MIN(open_ts_ms) AS first_ts,
                   MAX(open_ts_ms) AS last_ts
            FROM market_candles
            WHERE source = ? AND inst_id = ? AND bar = ?
            """,
            (OKX_PUBLIC_SOURCE, DEFAULT_INSTRUMENT, DEFAULT_BAR),
        ).fetchone()
        return int(row["stored_rows"]), row["first_ts"], row["last_ts"]

    def _quality(self, db: sqlite3.Connection) -> tuple[int, int]:
        missing = sum(
            (stop - start) // BAR_MILLISECONDS + 1
            for start, stop in self._gap_ranges(db)
        )
        conflict_row = db.execute(
            """
            SELECT COUNT(*) AS count FROM candle_conflicts
            WHERE source = ? AND inst_id = ? AND bar = ? AND resolved_at IS NULL
            """,
            (OKX_PUBLIC_SOURCE, DEFAULT_INSTRUMENT, DEFAULT_BAR),
        ).fetchone()
        return missing, int(conflict_row["count"])

    def _gap_ranges(self, db: sqlite3.Connection) -> list[tuple[int, int]]:
        gaps: list[tuple[int, int]] = []
        previous: int | None = None
        for row in db.execute(
            """
            SELECT open_ts_ms FROM market_candles
            WHERE source = ? AND inst_id = ? AND bar = ? ORDER BY open_ts_ms ASC
            """,
            (OKX_PUBLIC_SOURCE, DEFAULT_INSTRUMENT, DEFAULT_BAR),
        ):
            current = int(row["open_ts_ms"])
            if previous is not None:
                delta = current - previous
                if delta <= 0 or delta % BAR_MILLISECONDS != 0:
                    raise MarketDataError("stored candle timestamps violate the 5m grid")
                if delta > BAR_MILLISECONDS:
                    gaps.append(
                        (previous + BAR_MILLISECONDS, current - BAR_MILLISECONDS)
                    )
                    if len(gaps) > 10_000:
                        raise MarketDataError("market history contains too many gap ranges")
            previous = current
        return gaps

    def _update_state(
        self,
        db: sqlite3.Connection,
        *,
        sync_status: str,
        backfill_complete: bool,
        cursor_ts_ms: int | None,
        pages_fetched: int,
        rows_inserted: int,
        now: datetime,
        last_error_type: str | None,
        audit_quality: bool,
    ) -> None:
        stored_rows, first_ts, last_ts = self._coverage(db)
        if audit_quality:
            missing, conflicts = self._quality(db)
        else:
            current = db.execute(
                """
                SELECT missing_bars, unresolved_conflicts FROM market_sync_state
                WHERE source = ? AND inst_id = ? AND bar = ?
                """,
                (OKX_PUBLIC_SOURCE, DEFAULT_INSTRUMENT, DEFAULT_BAR),
            ).fetchone()
            missing = int(current["missing_bars"])
            conflicts = int(current["unresolved_conflicts"])
        db.execute(
            """
            UPDATE market_sync_state
            SET sync_status = ?, backfill_complete = ?, cursor_ts_ms = ?,
                stored_rows = ?, first_open_ts_ms = ?, last_open_ts_ms = ?,
                missing_bars = ?, unresolved_conflicts = ?,
                pages_fetched = pages_fetched + ?,
                rows_inserted = rows_inserted + ?, last_sync_at = ?,
                last_error_type = ?, updated_at = ?
            WHERE source = ? AND inst_id = ? AND bar = ?
            """,
            (
                sync_status,
                int(backfill_complete),
                cursor_ts_ms,
                stored_rows,
                first_ts,
                last_ts,
                missing,
                conflicts,
                pages_fetched,
                rows_inserted,
                _iso(now),
                last_error_type,
                _iso(now),
                OKX_PUBLIC_SOURCE,
                DEFAULT_INSTRUMENT,
                DEFAULT_BAR,
            ),
        )

    async def sync_all(
        self,
        client: OkxClient,
        *,
        now: datetime,
        max_pages: int | None = None,
        progress: Callable[[int, int], None] | None = None,
    ) -> dict[str, Any]:
        """Refresh the head and resume backward pagination until an empty page."""

        current_now = _utc(now, "now")
        if max_pages is not None and max_pages < 1:
            raise MarketDataError("max_pages must be positive")
        state_before = self.status()
        previous_latest_ms = state_before["lastOpenTsMs"]
        run_id = f"sync_{uuid.uuid4().hex[:24]}"
        with self._lock, self._connection() as db:
            prior_origin_candidate = db.execute(
                """
                SELECT cursor_ts_ms, completed_at
                FROM market_sync_runs
                WHERE source = ? AND inst_id = ? AND bar = ?
                  AND status = 'partial'
                  AND error_type = 'HistoryOriginUnconfirmed'
                  AND cursor_ts_ms IS NOT NULL
                  AND completed_at IS NOT NULL
                ORDER BY completed_at DESC
                LIMIT 1
                """,
                (OKX_PUBLIC_SOURCE, DEFAULT_INSTRUMENT, DEFAULT_BAR),
            ).fetchone()
            db.execute(
                """
                INSERT INTO market_sync_runs
                (run_id, source, inst_id, bar, status, started_at, completed_at,
                 pages_fetched, rows_inserted, cursor_ts_ms, error_type)
                VALUES (?, ?, ?, ?, 'running', ?, NULL, 0, 0, NULL, NULL)
                """,
                (
                    run_id,
                    OKX_PUBLIC_SOURCE,
                    DEFAULT_INSTRUMENT,
                    DEFAULT_BAR,
                    _iso(current_now),
                ),
            )
            self._update_state(
                db,
                sync_status="syncing",
                # Every refresh must re-prove the current official origin.  A
                # previously latched completion must not survive an extended or
                # interrupted backfill.
                backfill_complete=False,
                cursor_ts_ms=state_before["cursorTsMs"],
                pages_fetched=0,
                rows_inserted=0,
                now=current_now,
                last_error_type=None,
                audit_quality=False,
            )

        pages = 0
        inserted = 0
        cursor: int | None = None
        reached_head_overlap = previous_latest_ms is None
        reached_origin = False
        origin_unconfirmed = False
        try:
            async for page, next_cursor in client.iter_history_candle_pages(
                inst_id=DEFAULT_INSTRUMENT,
                bar=DEFAULT_BAR,
                page_limit=300,
            ):
                if max_pages is not None and pages >= max_pages:
                    break
                if not page:
                    break
                result = self.ingest_page(page, observed_at=current_now)
                pages += 1
                inserted += result["inserted"]
                cursor = next_cursor
                if progress:
                    progress(pages, inserted)
                if previous_latest_ms is None or next_cursor <= previous_latest_ms:
                    reached_head_overlap = True
                    break

            if reached_head_overlap and (max_pages is None or pages < max_pages):
                with self._connection() as db:
                    _count, oldest, _latest = self._coverage(db)
                if oldest is not None:
                    prior_matches = bool(
                        prior_origin_candidate
                        and int(prior_origin_candidate["cursor_ts_ms"]) == int(oldest)
                        and current_now
                        - _parse_iso(
                            str(prior_origin_candidate["completed_at"]),
                            "origin confirmation timestamp",
                        )
                        >= ORIGIN_CONFIRMATION_DELAY
                    )
                    confirming_known_boundary = bool(
                        state_before["backfillComplete"] or prior_matches
                    )
                    async for page, next_cursor in client.iter_history_candle_pages(
                        inst_id=DEFAULT_INSTRUMENT,
                        bar=DEFAULT_BAR,
                        after=oldest - 1 if confirming_known_boundary else oldest,
                        page_limit=100 if confirming_known_boundary else 300,
                    ):
                        if max_pages is not None and pages >= max_pages:
                            break
                        if not page:
                            with self._connection() as db:
                                _count, current_oldest, _latest = self._coverage(db)
                            cursor = current_oldest
                            if (
                                confirming_known_boundary
                                and current_oldest is not None
                                and int(current_oldest) == int(oldest)
                            ):
                                reached_origin = True
                            else:
                                origin_unconfirmed = True
                            break
                        result = self.ingest_page(page, observed_at=current_now)
                        pages += 1
                        inserted += result["inserted"]
                        cursor = next_cursor
                        if progress:
                            progress(pages, inserted)

            if reached_origin and (max_pages is None or pages < max_pages):
                with self._connection() as db:
                    gap_ranges = self._gap_ranges(db)
                for gap_start, gap_stop in gap_ranges:
                    async for page, next_cursor in client.iter_history_candle_pages(
                        inst_id=DEFAULT_INSTRUMENT,
                        bar=DEFAULT_BAR,
                        after=gap_stop + BAR_MILLISECONDS,
                        page_limit=300,
                    ):
                        if max_pages is not None and pages >= max_pages:
                            reached_origin = False
                            break
                        if not page:
                            break
                        result = self.ingest_page(page, observed_at=current_now)
                        pages += 1
                        inserted += result["inserted"]
                        if progress:
                            progress(pages, inserted)
                        if next_cursor is not None and next_cursor <= gap_start:
                            break
                    if not reached_origin:
                        break

            complete = reached_origin
            partial = not complete
            # ``now`` is the run's injected, timezone-aware clock.  Persisting
            # wall time here made origin confirmation non-deterministic and
            # could even move backwards relative to the next run when a caller
            # supplies a controlled clock.  The confirmation delay is between
            # run starts, so use the same captured instant throughout the run.
            completed_at = current_now
            with self._lock, self._connection() as db:
                db.execute("BEGIN IMMEDIATE")
                _count, actual_oldest, _latest = self._coverage(db)
                cursor = actual_oldest
                origin_error = (
                    "HistoryOriginUnconfirmed" if origin_unconfirmed else None
                )
                db.execute(
                    """
                    UPDATE market_sync_runs
                    SET status = ?, completed_at = ?, pages_fetched = ?,
                        rows_inserted = ?, cursor_ts_ms = ?, error_type = ?
                    WHERE run_id = ? AND status = 'running'
                    """,
                    (
                        "partial" if partial else "completed",
                        _iso(completed_at),
                        pages,
                        inserted,
                        cursor,
                        origin_error,
                        run_id,
                    ),
                )
                self._update_state(
                    db,
                    sync_status="partial" if partial else "idle",
                    backfill_complete=complete,
                    cursor_ts_ms=cursor,
                    pages_fetched=pages,
                    rows_inserted=inserted,
                    now=completed_at,
                    last_error_type=origin_error,
                    audit_quality=True,
                )
            return {**self.status(), "runId": run_id}
        except BaseException as exc:
            failed_at = current_now
            try:
                with self._lock, self._connection() as db:
                    db.execute("BEGIN IMMEDIATE")
                    db.execute(
                        """
                        UPDATE market_sync_runs
                        SET status = 'failed', completed_at = ?, pages_fetched = ?,
                            rows_inserted = ?, cursor_ts_ms = ?, error_type = ?
                        WHERE run_id = ? AND status = 'running'
                        """,
                        (
                            _iso(failed_at),
                            pages,
                            inserted,
                            cursor,
                            type(exc).__name__,
                            run_id,
                        ),
                    )
                    self._update_state(
                        db,
                        sync_status="error",
                        backfill_complete=False,
                        cursor_ts_ms=cursor,
                        pages_fetched=pages,
                        rows_inserted=inserted,
                        now=failed_at,
                        last_error_type=type(exc).__name__,
                        audit_quality=False,
                    )
            except Exception:
                pass
            raise

    def create_snapshot(
        self,
        *,
        feature_contract_sha256: str,
        now: datetime,
    ) -> MarketSnapshot:
        if len(feature_contract_sha256) != 64 or any(
            character not in "0123456789abcdef"
            for character in feature_contract_sha256
        ):
            raise MarketDataError("feature contract hash is invalid")
        state = self.status()
        if not state["backfillComplete"]:
            raise MarketDataError("official history backfill is not complete")
        with self._connection() as quality_db:
            row_count, first_ts, last_ts = self._coverage(quality_db)
            missing_bars, unresolved_conflicts = self._quality(quality_db)
        if missing_bars or unresolved_conflicts:
            raise MarketDataError("official history has gaps or unresolved conflicts")
        if row_count < 1 or first_ts is None or last_ts is None:
            raise MarketDataError("official history is empty")

        metadata = {
            "bar": DEFAULT_BAR,
            "feature_contract_sha256": feature_contract_sha256,
            "first_open_ts_ms": first_ts,
            "instrument": DEFAULT_INSTRUMENT,
            "last_open_ts_ms": last_ts,
            "row_count": row_count,
            "schema_version": SNAPSHOT_SCHEMA_VERSION,
            "source": OKX_PUBLIC_SOURCE,
        }
        digest = hashlib.sha256()
        digest.update(canonical_json(metadata).encode("utf-8"))
        digest.update(b"\n")
        seen = 0
        with self._connection() as db:
            for row in db.execute(
                """
                SELECT open_ts_ms, open_text, high_text, low_text, close_text,
                       volume_text, volume_ccy_text, volume_quote_text,
                       confirm, payload_sha256
                FROM market_candles
                WHERE source = ? AND inst_id = ? AND bar = ?
                ORDER BY open_ts_ms ASC
                """,
                (OKX_PUBLIC_SOURCE, DEFAULT_INSTRUMENT, DEFAULT_BAR),
            ):
                candle = [
                    str(row["open_ts_ms"]),
                    row["open_text"],
                    row["high_text"],
                    row["low_text"],
                    row["close_text"],
                    row["volume_text"],
                    row["volume_ccy_text"],
                    row["volume_quote_text"],
                    str(row["confirm"]),
                ]
                if not hmac.compare_digest(
                    _payload_sha256(candle), str(row["payload_sha256"])
                ):
                    raise MarketDataError("stored candle payload hash mismatch")
                digest.update(canonical_json(candle).encode("utf-8"))
                digest.update(b"\n")
                seen += 1
        if seen != row_count:
            raise MarketDataError("market coverage changed during snapshot creation")
        content_sha = digest.hexdigest()
        snapshot_id = f"dset_{content_sha[:24]}"
        created = _utc(now, "now")
        snapshot = MarketSnapshot(
            snapshot_id=snapshot_id,
            source=OKX_PUBLIC_SOURCE,
            instrument=DEFAULT_INSTRUMENT,
            bar=DEFAULT_BAR,
            first_open_ts_ms=int(first_ts),
            last_open_ts_ms=int(last_ts),
            row_count=row_count,
            content_sha256=content_sha,
            feature_contract_sha256=feature_contract_sha256,
            created_at=created,
        )
        with self._lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT * FROM dataset_snapshots WHERE snapshot_id = ?", (snapshot_id,)
            ).fetchone()
            if existing:
                if not hmac.compare_digest(str(existing["content_sha256"]), content_sha):
                    raise MarketDataError("snapshot ID collision")
                snapshot = MarketSnapshot(
                    snapshot_id=snapshot.snapshot_id,
                    source=snapshot.source,
                    instrument=snapshot.instrument,
                    bar=snapshot.bar,
                    first_open_ts_ms=snapshot.first_open_ts_ms,
                    last_open_ts_ms=snapshot.last_open_ts_ms,
                    row_count=snapshot.row_count,
                    content_sha256=snapshot.content_sha256,
                    feature_contract_sha256=snapshot.feature_contract_sha256,
                    created_at=_parse_iso(
                        str(existing["created_at"]), "snapshot created_at"
                    ),
                )
            else:
                db.execute(
                    """
                    INSERT INTO dataset_snapshots
                    (snapshot_id, source, inst_id, bar, first_open_ts_ms,
                     last_open_ts_ms, row_count, content_sha256,
                     feature_contract_sha256, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        snapshot.snapshot_id,
                        snapshot.source,
                        snapshot.instrument,
                        snapshot.bar,
                        snapshot.first_open_ts_ms,
                        snapshot.last_open_ts_ms,
                        snapshot.row_count,
                        snapshot.content_sha256,
                        snapshot.feature_contract_sha256,
                        _iso(snapshot.created_at),
                    ),
                )
        return snapshot

    def get_snapshot(self, snapshot_id: str) -> MarketSnapshot | None:
        with self._connection() as db:
            row = db.execute(
                "SELECT * FROM dataset_snapshots WHERE snapshot_id = ?", (snapshot_id,)
            ).fetchone()
        if row is None:
            return None
        return MarketSnapshot(
            snapshot_id=row["snapshot_id"],
            source=row["source"],
            instrument=row["inst_id"],
            bar=row["bar"],
            first_open_ts_ms=int(row["first_open_ts_ms"]),
            last_open_ts_ms=int(row["last_open_ts_ms"]),
            row_count=int(row["row_count"]),
            content_sha256=row["content_sha256"],
            feature_contract_sha256=row["feature_contract_sha256"],
            created_at=_parse_iso(row["created_at"], "snapshot created_at"),
        )

    def snapshot_rows(self, snapshot_id: str) -> CandleSnapshotRows:
        snapshot = self.get_snapshot(snapshot_id)
        if snapshot is None:
            raise MarketDataError("market snapshot does not exist")
        return CandleSnapshotRows(self.path, snapshot)

    def snapshot_is_current(self, content_sha256: str | None) -> bool:
        """Return whether a model snapshot still covers the warehouse origin.

        Appending newer candles must not invalidate a champion, but discovering
        older history must.  A sync that has not re-confirmed the official
        origin also fails closed.
        """

        if (
            not isinstance(content_sha256, str)
            or len(content_sha256) != 64
            or any(character not in "0123456789abcdef" for character in content_sha256)
        ):
            return False
        state = self.status()
        if (
            not state["backfillComplete"]
            or state["syncStatus"] != "idle"
            or state["missingBars"] != 0
            or state["unresolvedConflicts"] != 0
            or state["firstOpenTsMs"] is None
        ):
            return False
        with self._connection() as db:
            snapshot = db.execute(
                """
                SELECT first_open_ts_ms, last_open_ts_ms
                FROM dataset_snapshots
                WHERE content_sha256 = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (content_sha256,),
            ).fetchone()
        return bool(
            snapshot
            and int(snapshot["first_open_ts_ms"]) == int(state["firstOpenTsMs"])
            and int(snapshot["last_open_ts_ms"]) <= int(state["lastOpenTsMs"])
        )

    def status(self) -> dict[str, Any]:
        with self._connection() as db:
            row = db.execute(
                """
                SELECT * FROM market_sync_state
                WHERE source = ? AND inst_id = ? AND bar = ?
                """,
                (OKX_PUBLIC_SOURCE, DEFAULT_INSTRUMENT, DEFAULT_BAR),
            ).fetchone()
            latest_snapshot = db.execute(
                "SELECT * FROM dataset_snapshots ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        if row is None:
            raise MarketDataError("market sync state is missing")
        first_ts = row["first_open_ts_ms"]
        last_ts = row["last_open_ts_ms"]
        coverage_days = (
            (int(last_ts) - int(first_ts)) / 86_400_000
            if first_ts is not None and last_ts is not None
            else 0.0
        )
        expected_rows = (
            (int(last_ts) - int(first_ts)) // BAR_MILLISECONDS + 1
            if first_ts is not None and last_ts is not None
            else 0
        )
        first_open_at = (
            _iso(datetime.fromtimestamp(int(first_ts) / 1_000, tz=timezone.utc))
            if first_ts is not None
            else None
        )
        last_open_at = (
            _iso(datetime.fromtimestamp(int(last_ts) / 1_000, tz=timezone.utc))
            if last_ts is not None
            else None
        )
        last_closed_at = (
            _iso(
                datetime.fromtimestamp(
                    (int(last_ts) + BAR_MILLISECONDS) / 1_000,
                    tz=timezone.utc,
                )
            )
            if last_ts is not None
            else None
        )
        disk_bytes = sum(
            candidate.stat().st_size
            for candidate in (
                self.path,
                Path(f"{self.path}-wal"),
                Path(f"{self.path}-shm"),
            )
            if candidate.exists()
        )
        return {
            "backfillComplete": bool(row["backfill_complete"]),
            "bar": row["bar"],
            "coverageDays": coverage_days,
            "cursorTsMs": row["cursor_ts_ms"],
            "diskBytes": disk_bytes,
            "expectedRows": expected_rows,
            "firstOpenAt": first_open_at,
            "firstOpenTsMs": first_ts,
            "instrument": row["inst_id"],
            "lastErrorType": row["last_error_type"],
            "lastClosedAt": last_closed_at,
            "lastOpenAt": last_open_at,
            "lastOpenTsMs": last_ts,
            "lastSyncAt": row["last_sync_at"],
            "latestSnapshot": (
                self.get_snapshot(str(latest_snapshot["snapshot_id"])).to_dict()
                if latest_snapshot
                else None
            ),
            "missingBars": int(row["missing_bars"]),
            "pagesFetched": int(row["pages_fetched"]),
            "rowsInserted": int(row["rows_inserted"]),
            "schemaVersion": f"tideguard.market-data.v{MARKET_DATA_SCHEMA_VERSION}",
            "source": row["source"],
            "storedRows": int(row["stored_rows"]),
            "syncStatus": row["sync_status"],
            "unresolvedConflicts": int(row["unresolved_conflicts"]),
        }


__all__ = [
    "CandleSnapshotRows",
    "MarketDataError",
    "MarketDataStore",
    "MarketSnapshot",
]
