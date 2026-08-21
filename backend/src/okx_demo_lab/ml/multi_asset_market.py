from __future__ import annotations

import hashlib
import hmac
import json
import re
import sqlite3
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


MULTI_ASSET_MARKET_SCHEMA_VERSION = 1
MULTI_ASSET_MARKET_SCHEMA = "moheng.multi-asset-market.v1"
MULTI_ASSET_SNAPSHOT_SCHEMA = "moheng.multi-asset-snapshot.v1"
OKX_PUBLIC_SOURCE = "okx-public-v5"
DEFAULT_BAR = "5m"
FIVE_MINUTES_MS = 300_000
ORIGIN_CONFIRMATION_DELAY = timedelta(seconds=60)
_INSTRUMENT_PATTERN = re.compile(r"^[A-Z0-9]{1,20}-[A-Z0-9]{1,20}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


class MultiAssetMarketError(RuntimeError):
    pass


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise MultiAssetMarketError("value is not canonical JSON") from exc


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise MultiAssetMarketError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return _utc(value, "timestamp").isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _parse_iso(value: str, name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise MultiAssetMarketError(f"{name} is invalid") from exc
    return _utc(parsed, name)


def _instrument(value: str) -> str:
    if not isinstance(value, str):
        raise MultiAssetMarketError("instrument is invalid")
    normalized = value.strip()
    if normalized != value or not _INSTRUMENT_PATTERN.fullmatch(normalized):
        raise MultiAssetMarketError("instrument must be an uppercase SPOT pair")
    return normalized


def _bar(value: str) -> str:
    if value != DEFAULT_BAR:
        raise MultiAssetMarketError("multi-asset research is fixed to confirmed 5m candles")
    return value


def _feature_hash(value: str) -> str:
    if not isinstance(value, str) or not _SHA256_PATTERN.fullmatch(value):
        raise MultiAssetMarketError("feature contract hash is invalid")
    return value


def _decimal(value: str, name: str, *, allow_zero: bool) -> Decimal:
    try:
        parsed = Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise MultiAssetMarketError(f"{name} is invalid") from exc
    if not parsed.is_finite() or parsed < 0 or (parsed == 0 and not allow_zero):
        raise MultiAssetMarketError(f"{name} is invalid")
    return parsed


def _normalize_candle(row: Sequence[Any]) -> tuple[str, ...] | None:
    if not isinstance(row, (list, tuple)) or len(row) != 9:
        raise MultiAssetMarketError("OKX candle does not match the 9-field schema")
    if any(
        isinstance(value, bool) or not isinstance(value, (str, int, float))
        for value in row
    ):
        raise MultiAssetMarketError("OKX candle fields must be scalar values")
    normalized = tuple(str(value).strip() for value in row)
    timestamp_text = normalized[0]
    if not timestamp_text.isdigit():
        raise MultiAssetMarketError("OKX candle timestamp is invalid")
    timestamp = int(timestamp_text)
    if timestamp <= 0 or timestamp % FIVE_MINUTES_MS != 0:
        raise MultiAssetMarketError("OKX candle timestamp is not aligned to the 5m grid")
    if normalized[8] == "0":
        return None
    if normalized[8] != "1":
        raise MultiAssetMarketError("OKX candle confirm flag is invalid")

    open_price = _decimal(normalized[1], "open", allow_zero=False)
    high = _decimal(normalized[2], "high", allow_zero=False)
    low = _decimal(normalized[3], "low", allow_zero=False)
    close = _decimal(normalized[4], "close", allow_zero=False)
    for index, name in ((5, "volume"), (6, "volume currency"), (7, "quote volume")):
        _decimal(normalized[index], name, allow_zero=True)
    if low > high or high < max(open_price, close) or low > min(open_price, close):
        raise MultiAssetMarketError("OKX candle OHLC values are inconsistent")
    return normalized


def _payload_sha256(row: Sequence[str]) -> str:
    return _sha256(_canonical_json(list(row)))


@dataclass(frozen=True, order=True)
class MarketSeriesKey:
    instrument: str
    bar: str = DEFAULT_BAR
    source: str = OKX_PUBLIC_SOURCE

    def __post_init__(self) -> None:
        _instrument(self.instrument)
        _bar(self.bar)
        if self.source != OKX_PUBLIC_SOURCE:
            raise MultiAssetMarketError("only the credential-free OKX public source is allowed")

    @property
    def value(self) -> str:
        return f"{self.source}|{self.instrument}|{self.bar}"


@dataclass(frozen=True)
class OriginProbe:
    """Auditable evidence for one terminal public-history empty-page probe."""

    run_id: str
    instrument: str
    requested_after: int
    terminal_cursor: int
    page_limit: int
    empty_probe_count: int
    anchor_open_ts_ms: int
    anchor_payload_sha256: str
    observed_at: datetime
    bar: str = DEFAULT_BAR
    source: str = OKX_PUBLIC_SOURCE

    def __post_init__(self) -> None:
        MarketSeriesKey(
            source=self.source,
            instrument=self.instrument,
            bar=self.bar,
        )
        if not isinstance(self.run_id, str) or not _RUN_ID_PATTERN.fullmatch(
            self.run_id
        ):
            raise MultiAssetMarketError("origin probe run_id is invalid")
        for value, name in (
            (self.requested_after, "requested_after"),
            (self.terminal_cursor, "terminal_cursor"),
            (self.page_limit, "page_limit"),
            (self.empty_probe_count, "empty_probe_count"),
            (self.anchor_open_ts_ms, "anchor_open_ts_ms"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise MultiAssetMarketError(f"origin probe {name} is invalid")
        if self.page_limit > 300:
            raise MultiAssetMarketError("origin probe page_limit is invalid")
        if not _SHA256_PATTERN.fullmatch(self.anchor_payload_sha256):
            raise MultiAssetMarketError("origin probe anchor hash is invalid")
        _utc(self.observed_at, "origin probe observed_at")

    @property
    def series_key(self) -> MarketSeriesKey:
        return MarketSeriesKey(
            source=self.source,
            instrument=self.instrument,
            bar=self.bar,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "bar": self.bar,
            "anchorOpenTsMs": self.anchor_open_ts_ms,
            "anchorPayloadSha256": self.anchor_payload_sha256,
            "emptyProbeCount": self.empty_probe_count,
            "instrument": self.instrument,
            "observedAt": _iso(self.observed_at),
            "pageLimit": self.page_limit,
            "requestedAfter": self.requested_after,
            "runId": self.run_id,
            "seriesKey": self.series_key.value,
            "source": self.source,
            "terminalCursor": self.terminal_cursor,
        }

    @property
    def sha256(self) -> str:
        return _sha256(_canonical_json(self.to_dict()))


def _origin_probe_evidence_sha256(probe: OriginProbe, origin_open_ts_ms: int) -> str:
    return _sha256(
        _canonical_json(
            {
                **probe.to_dict(),
                "originOpenTsMs": origin_open_ts_ms,
                "schemaVersion": MULTI_ASSET_MARKET_SCHEMA,
            }
        )
    )


@dataclass(frozen=True)
class MultiAssetMarketSnapshot:
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

    @property
    def series_key(self) -> MarketSeriesKey:
        return MarketSeriesKey(
            source=self.source,
            instrument=self.instrument,
            bar=self.bar,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "bar": self.bar,
            "contentSha256": self.content_sha256,
            "createdAt": _iso(self.created_at),
            "featureContractSha256": self.feature_contract_sha256,
            "firstOpenAt": _iso(
                datetime.fromtimestamp(self.first_open_ts_ms / 1_000, tz=timezone.utc)
            ),
            "firstOpenTsMs": self.first_open_ts_ms,
            "instrument": self.instrument,
            "lastOpenAt": _iso(
                datetime.fromtimestamp(self.last_open_ts_ms / 1_000, tz=timezone.utc)
            ),
            "lastOpenTsMs": self.last_open_ts_ms,
            "rowCount": self.row_count,
            "schemaVersion": MULTI_ASSET_SNAPSHOT_SCHEMA,
            "seriesKey": self.series_key.value,
            "snapshotId": self.snapshot_id,
            "source": self.source,
        }


def _snapshot_metadata(
    *,
    series: MarketSeriesKey,
    feature_contract_sha256: str,
    first_open_ts_ms: int,
    last_open_ts_ms: int,
    row_count: int,
) -> dict[str, Any]:
    return {
        "bar": series.bar,
        "feature_contract_sha256": feature_contract_sha256,
        "first_open_ts_ms": first_open_ts_ms,
        "instrument": series.instrument,
        "last_open_ts_ms": last_open_ts_ms,
        "row_count": row_count,
        "schema_version": MULTI_ASSET_SNAPSHOT_SCHEMA,
        "source": series.source,
    }


def _row_from_stored(stored: sqlite3.Row) -> list[str]:
    return [
        str(stored["open_ts_ms"]),
        str(stored["open_text"]),
        str(stored["high_text"]),
        str(stored["low_text"]),
        str(stored["close_text"]),
        str(stored["volume_text"]),
        str(stored["volume_ccy_text"]),
        str(stored["volume_quote_text"]),
        str(stored["confirm"]),
    ]


class MultiAssetSnapshotRows:
    """Re-iterable, integrity-checked rows for one immutable series snapshot."""

    def __init__(self, path: Path, snapshot: MultiAssetMarketSnapshot):
        self.path = path
        self.snapshot = snapshot

    def __len__(self) -> int:
        return self.snapshot.row_count

    def __iter__(self) -> Iterator[list[str]]:
        if self.snapshot.snapshot_id != f"maset_{self.snapshot.content_sha256[:24]}":
            raise MultiAssetMarketError("snapshot ID does not match its content hash")
        metadata = _snapshot_metadata(
            series=self.snapshot.series_key,
            feature_contract_sha256=self.snapshot.feature_contract_sha256,
            first_open_ts_ms=self.snapshot.first_open_ts_ms,
            last_open_ts_ms=self.snapshot.last_open_ts_ms,
            row_count=self.snapshot.row_count,
        )
        digest = hashlib.sha256()
        digest.update(_canonical_json(metadata).encode("utf-8"))
        digest.update(b"\n")
        seen = 0
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("PRAGMA busy_timeout=10000")
        try:
            cursor = db.execute(
                """
                SELECT open_ts_ms, open_text, high_text, low_text, close_text,
                       volume_text, volume_ccy_text, volume_quote_text,
                       confirm, payload_sha256
                FROM ma_candles
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
                row = _row_from_stored(stored)
                if not hmac.compare_digest(
                    _payload_sha256(row), str(stored["payload_sha256"])
                ):
                    raise MultiAssetMarketError("stored candle payload hash mismatch")
                digest.update(_canonical_json(row).encode("utf-8"))
                digest.update(b"\n")
                seen += 1
                yield row
        finally:
            db.close()
        if seen != self.snapshot.row_count:
            raise MultiAssetMarketError("snapshot row count changed after creation")
        if not hmac.compare_digest(digest.hexdigest(), self.snapshot.content_sha256):
            raise MultiAssetMarketError("snapshot content hash changed after creation")


class MultiAssetMarketStore:
    """Credential-free, append-only warehouse for confirmed public 5m candles.

    All instruments share one SQLite database, while every candle, conflict,
    status query and snapshot is bound to the complete source/instrument/bar
    series key.  Initialization performs schema DDL only; it never rewrites
    another process's running-task state.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=10000")
        journal_mode = str(db.execute("PRAGMA journal_mode=DELETE").fetchone()[0])
        if journal_mode.lower() != "delete":
            db.close()
            raise MultiAssetMarketError("SQLite DELETE journal mode is required")
        db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA foreign_keys=ON")
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
        with self._connection() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS ma_schema_metadata (
                    component TEXT PRIMARY KEY,
                    schema_version INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                ) WITHOUT ROWID;
                CREATE TABLE IF NOT EXISTS ma_series (
                    source TEXT NOT NULL,
                    inst_id TEXT NOT NULL,
                    bar TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(source, inst_id, bar)
                ) WITHOUT ROWID;
                CREATE TABLE IF NOT EXISTS ma_candles (
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
                    observed_at TEXT NOT NULL,
                    PRIMARY KEY(source, inst_id, bar, open_ts_ms),
                    FOREIGN KEY(source, inst_id, bar)
                        REFERENCES ma_series(source, inst_id, bar)
                ) WITHOUT ROWID;
                CREATE TABLE IF NOT EXISTS ma_conflicts (
                    source TEXT NOT NULL,
                    inst_id TEXT NOT NULL,
                    bar TEXT NOT NULL,
                    open_ts_ms INTEGER NOT NULL,
                    stored_sha256 TEXT NOT NULL,
                    observed_sha256 TEXT NOT NULL,
                    observed_payload_json TEXT NOT NULL,
                    detected_at TEXT NOT NULL,
                    PRIMARY KEY(source, inst_id, bar, open_ts_ms, observed_sha256),
                    FOREIGN KEY(source, inst_id, bar)
                        REFERENCES ma_series(source, inst_id, bar)
                ) WITHOUT ROWID;
                CREATE TABLE IF NOT EXISTS ma_origin_probes (
                    source TEXT NOT NULL,
                    inst_id TEXT NOT NULL,
                    bar TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    origin_open_ts_ms INTEGER NOT NULL CHECK(origin_open_ts_ms > 0),
                    requested_after INTEGER NOT NULL CHECK(requested_after > 0),
                    terminal_cursor INTEGER NOT NULL CHECK(terminal_cursor > 0),
                    page_limit INTEGER NOT NULL CHECK(page_limit IN (100, 300)),
                    empty_probe_count INTEGER NOT NULL CHECK(empty_probe_count = 3),
                    anchor_open_ts_ms INTEGER NOT NULL CHECK(anchor_open_ts_ms > 0),
                    anchor_payload_sha256 TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    evidence_sha256 TEXT NOT NULL,
                    PRIMARY KEY(source, inst_id, bar, run_id, origin_open_ts_ms),
                    FOREIGN KEY(source, inst_id, bar)
                        REFERENCES ma_series(source, inst_id, bar)
                ) WITHOUT ROWID;
                CREATE TABLE IF NOT EXISTS ma_snapshots (
                    snapshot_id TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    inst_id TEXT NOT NULL,
                    bar TEXT NOT NULL,
                    first_open_ts_ms INTEGER NOT NULL,
                    last_open_ts_ms INTEGER NOT NULL,
                    row_count INTEGER NOT NULL CHECK(row_count > 0),
                    content_sha256 TEXT NOT NULL UNIQUE,
                    feature_contract_sha256 TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(source, inst_id, bar)
                        REFERENCES ma_series(source, inst_id, bar)
                ) WITHOUT ROWID;
                CREATE INDEX IF NOT EXISTS ma_snapshots_series_created
                    ON ma_snapshots(source, inst_id, bar, created_at DESC);
                """
            )
            schema_row = db.execute(
                """
                SELECT schema_version FROM ma_schema_metadata
                WHERE component = 'multi_asset_market'
                """
            ).fetchone()
            if schema_row is None:
                db.execute(
                    """
                    INSERT INTO ma_schema_metadata(component, schema_version, created_at)
                    VALUES ('multi_asset_market', ?, ?)
                    """,
                    (
                        MULTI_ASSET_MARKET_SCHEMA_VERSION,
                        _iso(datetime.now(timezone.utc)),
                    ),
                )
            elif int(schema_row["schema_version"]) != MULTI_ASSET_MARKET_SCHEMA_VERSION:
                raise MultiAssetMarketError(
                    "multi-asset market schema is incompatible with this application"
                )

    @staticmethod
    def _series(instrument: str, bar: str) -> MarketSeriesKey:
        return MarketSeriesKey(instrument=_instrument(instrument), bar=_bar(bar))

    @staticmethod
    def _insert_series(
        db: sqlite3.Connection,
        series: MarketSeriesKey,
        *,
        created_at: str,
    ) -> None:
        db.execute(
            """
            INSERT OR IGNORE INTO ma_series(source, inst_id, bar, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (series.source, series.instrument, series.bar, created_at),
        )

    def ingest(
        self,
        rows: Sequence[Sequence[Any]],
        *,
        instrument: str,
        observed_at: datetime,
        bar: str = DEFAULT_BAR,
    ) -> dict[str, Any]:
        """Append one public OKX page without overwriting confirmed history."""

        series = self._series(instrument, bar)
        observed = _utc(observed_at, "observed_at")
        observed_at_text = _iso(observed)
        observed_at_ms = round(observed.timestamp() * 1_000)
        normalized_rows: list[tuple[str, ...]] = []
        unconfirmed = 0
        for row in rows:
            normalized = _normalize_candle(row)
            if normalized is None:
                unconfirmed += 1
                continue
            if int(normalized[0]) + FIVE_MINUTES_MS > observed_at_ms + 2_000:
                raise MultiAssetMarketError("confirmed candle closes in the future")
            normalized_rows.append(normalized)

        inserted = 0
        duplicates = 0
        conflicts = 0
        backfill_complete = False
        with self._lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._insert_series(db, series, created_at=observed_at_text)
            for row in normalized_rows:
                open_ts_ms = int(row[0])
                payload_sha256 = _payload_sha256(row)
                existing = db.execute(
                    """
                    SELECT payload_sha256 FROM ma_candles
                    WHERE source = ? AND inst_id = ? AND bar = ? AND open_ts_ms = ?
                    """,
                    (*self._series_values(series), open_ts_ms),
                ).fetchone()
                if existing is None:
                    db.execute(
                        """
                        INSERT INTO ma_candles
                        (source, inst_id, bar, open_ts_ms, open_text, high_text,
                         low_text, close_text, volume_text, volume_ccy_text,
                         volume_quote_text, confirm, payload_sha256, observed_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                        """,
                        (
                            *self._series_values(series),
                            open_ts_ms,
                            *row[1:8],
                            payload_sha256,
                            observed_at_text,
                        ),
                    )
                    inserted += 1
                elif hmac.compare_digest(
                    str(existing["payload_sha256"]), payload_sha256
                ):
                    duplicates += 1
                else:
                    result = db.execute(
                        """
                        INSERT OR IGNORE INTO ma_conflicts
                        (source, inst_id, bar, open_ts_ms, stored_sha256,
                         observed_sha256, observed_payload_json, detected_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            *self._series_values(series),
                            open_ts_ms,
                            str(existing["payload_sha256"]),
                            payload_sha256,
                            _canonical_json(list(row)),
                            observed_at_text,
                        ),
                    )
                    conflicts += max(0, result.rowcount)
            origin_row = db.execute(
                """
                SELECT MIN(open_ts_ms) AS first_ts FROM ma_candles
                WHERE source = ? AND inst_id = ? AND bar = ?
                """,
                self._series_values(series),
            ).fetchone()
            first_ts = origin_row["first_ts"]
            backfill_complete, _observation_count, _confirmed_at = (
                self._origin_status(db, series, first_ts)
            )
        return {
            "backfillComplete": backfill_complete,
            "conflicts": conflicts,
            "duplicates": duplicates,
            "inserted": inserted,
            "instrument": series.instrument,
            "seriesKey": series.value,
            "unconfirmed": unconfirmed,
        }

    @staticmethod
    def _series_values(series: MarketSeriesKey) -> tuple[str, str, str]:
        return series.source, series.instrument, series.bar

    @staticmethod
    def _coverage(
        db: sqlite3.Connection, series: MarketSeriesKey
    ) -> tuple[int, int | None, int | None]:
        row = db.execute(
            """
            SELECT COUNT(*) AS stored_rows, MIN(open_ts_ms) AS first_ts,
                   MAX(open_ts_ms) AS last_ts
            FROM ma_candles
            WHERE source = ? AND inst_id = ? AND bar = ?
            """,
            MultiAssetMarketStore._series_values(series),
        ).fetchone()
        return int(row["stored_rows"]), row["first_ts"], row["last_ts"]

    @staticmethod
    def _gaps(
        db: sqlite3.Connection,
        series: MarketSeriesKey,
        *,
        range_limit: int = 100,
    ) -> tuple[int, list[dict[str, int]], bool]:
        previous: int | None = None
        missing = 0
        ranges: list[dict[str, int]] = []
        truncated = False
        for row in db.execute(
            """
            SELECT open_ts_ms FROM ma_candles
            WHERE source = ? AND inst_id = ? AND bar = ?
            ORDER BY open_ts_ms ASC
            """,
            MultiAssetMarketStore._series_values(series),
        ):
            current = int(row["open_ts_ms"])
            if previous is not None:
                delta = current - previous
                if delta <= 0 or delta % FIVE_MINUTES_MS != 0:
                    raise MultiAssetMarketError(
                        "stored candle timestamps violate the 5m grid"
                    )
                if delta > FIVE_MINUTES_MS:
                    count = delta // FIVE_MINUTES_MS - 1
                    missing += count
                    if len(ranges) < range_limit:
                        ranges.append(
                            {
                                "firstOpenTsMs": previous + FIVE_MINUTES_MS,
                                "lastOpenTsMs": current - FIVE_MINUTES_MS,
                                "missingBars": count,
                            }
                        )
                    else:
                        truncated = True
            previous = current
        return missing, ranges, truncated

    @staticmethod
    def _conflict_count(db: sqlite3.Connection, series: MarketSeriesKey) -> int:
        row = db.execute(
            """
            SELECT COUNT(*) AS conflict_count FROM ma_conflicts
            WHERE source = ? AND inst_id = ? AND bar = ?
            """,
            MultiAssetMarketStore._series_values(series),
        ).fetchone()
        return int(row["conflict_count"])

    @staticmethod
    def _probe_role(probe: OriginProbe, origin_open_ts_ms: int) -> str:
        if (
            probe.requested_after == origin_open_ts_ms
            and probe.terminal_cursor == origin_open_ts_ms
            and probe.page_limit == 300
            and probe.empty_probe_count == 3
        ):
            return "baseline"
        if (
            probe.requested_after == origin_open_ts_ms - 1
            and probe.terminal_cursor == origin_open_ts_ms - 1
            and probe.page_limit == 100
            and probe.empty_probe_count == 3
        ):
            return "confirmation"
        raise MultiAssetMarketError(
            "origin probe does not match the current oldest candle protocol"
        )

    @staticmethod
    def _probe_from_row(row: sqlite3.Row) -> tuple[OriginProbe, int]:
        origin_open_ts_ms = int(row["origin_open_ts_ms"])
        probe = OriginProbe(
            run_id=str(row["run_id"]),
            instrument=str(row["inst_id"]),
            requested_after=int(row["requested_after"]),
            terminal_cursor=int(row["terminal_cursor"]),
            page_limit=int(row["page_limit"]),
            empty_probe_count=int(row["empty_probe_count"]),
            anchor_open_ts_ms=int(row["anchor_open_ts_ms"]),
            anchor_payload_sha256=str(row["anchor_payload_sha256"]),
            observed_at=_parse_iso(str(row["observed_at"]), "probe observed_at"),
            bar=str(row["bar"]),
            source=str(row["source"]),
        )
        expected_sha256 = _origin_probe_evidence_sha256(probe, origin_open_ts_ms)
        if not hmac.compare_digest(expected_sha256, str(row["evidence_sha256"])):
            raise MultiAssetMarketError("origin probe evidence hash mismatch")
        MultiAssetMarketStore._probe_role(probe, origin_open_ts_ms)
        return probe, origin_open_ts_ms

    @staticmethod
    def _origin_probes(
        db: sqlite3.Connection,
        series: MarketSeriesKey,
        origin_open_ts_ms: int,
    ) -> list[OriginProbe]:
        probes: list[OriginProbe] = []
        for row in db.execute(
            """
            SELECT * FROM ma_origin_probes
            WHERE source = ? AND inst_id = ? AND bar = ?
              AND origin_open_ts_ms = ?
            ORDER BY observed_at ASC, run_id ASC
            """,
            (*MultiAssetMarketStore._series_values(series), origin_open_ts_ms),
        ):
            probe, stored_origin = MultiAssetMarketStore._probe_from_row(row)
            if stored_origin != origin_open_ts_ms or probe.series_key != series:
                raise MultiAssetMarketError("origin probe series key mismatch")
            probes.append(probe)
        return probes

    @staticmethod
    def _origin_status(
        db: sqlite3.Connection,
        series: MarketSeriesKey,
        first_open_ts_ms: int | None,
    ) -> tuple[bool, int, str | None]:
        if first_open_ts_ms is None:
            return False, 0, None
        probes = MultiAssetMarketStore._origin_probes(
            db, series, int(first_open_ts_ms)
        )
        baselines = [
            probe
            for probe in probes
            if MultiAssetMarketStore._probe_role(probe, int(first_open_ts_ms))
            == "baseline"
        ]
        confirmations = [
            probe
            for probe in probes
            if MultiAssetMarketStore._probe_role(probe, int(first_open_ts_ms))
            == "confirmation"
        ]
        confirmed_at: datetime | None = None
        for confirmation in confirmations:
            for baseline in baselines:
                if (
                    baseline.run_id != confirmation.run_id
                    and confirmation.observed_at - baseline.observed_at
                    >= ORIGIN_CONFIRMATION_DELAY
                ):
                    confirmed_at = confirmation.observed_at
                    break
            if confirmed_at is not None:
                break
        return (
            confirmed_at is not None,
            len(probes),
            _iso(confirmed_at) if confirmed_at is not None else None,
        )

    def record_origin_probe(self, probe: OriginProbe) -> dict[str, Any]:
        """Persist a terminal empty-page proof for one public-history run.

        A baseline probe must request ``after=current_oldest`` with limit 300
        and observe three terminal empty responses.  Confirmation must be a
        different run at least 60 seconds later, request
        ``after=current_oldest-1`` with limit 100 and again observe three empty
        responses.  All evidence is bound to the current oldest stored candle.
        """

        if not isinstance(probe, OriginProbe):
            raise MultiAssetMarketError("origin probe is invalid")
        series = probe.series_key
        duplicate = False
        with self._lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            origin_row = db.execute(
                """
                SELECT MIN(open_ts_ms) AS first_ts FROM ma_candles
                WHERE source = ? AND inst_id = ? AND bar = ?
                """,
                self._series_values(series),
            ).fetchone()
            first_ts = origin_row["first_ts"]
            if first_ts is None:
                raise MultiAssetMarketError("cannot probe the origin of an empty series")
            origin_open_ts_ms = int(first_ts)
            if probe.anchor_open_ts_ms != origin_open_ts_ms:
                raise MultiAssetMarketError(
                    "origin probe anchor does not match the current oldest candle"
                )
            anchor_row = db.execute(
                """
                SELECT payload_sha256 FROM ma_candles
                WHERE source = ? AND inst_id = ? AND bar = ? AND open_ts_ms = ?
                """,
                (*self._series_values(series), origin_open_ts_ms),
            ).fetchone()
            if anchor_row is None or not hmac.compare_digest(
                str(anchor_row["payload_sha256"]), probe.anchor_payload_sha256
            ):
                raise MultiAssetMarketError(
                    "origin probe anchor payload does not match stored history"
                )
            role = self._probe_role(probe, origin_open_ts_ms)
            prior_probes = self._origin_probes(db, series, origin_open_ts_ms)
            if role == "confirmation" and not any(
                self._probe_role(prior, origin_open_ts_ms) == "baseline"
                and prior.run_id != probe.run_id
                and probe.observed_at - prior.observed_at
                >= ORIGIN_CONFIRMATION_DELAY
                for prior in prior_probes
            ):
                raise MultiAssetMarketError(
                    "origin confirmation requires a different baseline run "
                    "at least 60 seconds earlier"
                )
            evidence_sha256 = _origin_probe_evidence_sha256(
                probe, origin_open_ts_ms
            )
            existing = db.execute(
                """
                SELECT * FROM ma_origin_probes
                WHERE source = ? AND inst_id = ? AND bar = ?
                  AND run_id = ? AND origin_open_ts_ms = ?
                """,
                (*self._series_values(series), probe.run_id, origin_open_ts_ms),
            ).fetchone()
            if existing is not None:
                _existing_probe, _existing_origin = self._probe_from_row(existing)
                if not hmac.compare_digest(
                    str(existing["evidence_sha256"]), evidence_sha256
                ):
                    raise MultiAssetMarketError(
                        "origin run_id already has different evidence"
                    )
                duplicate = True
            else:
                db.execute(
                    """
                    INSERT INTO ma_origin_probes
                    (source, inst_id, bar, run_id, origin_open_ts_ms,
                     requested_after, terminal_cursor, page_limit,
                     empty_probe_count, anchor_open_ts_ms,
                     anchor_payload_sha256, observed_at, evidence_sha256)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        *self._series_values(series),
                        probe.run_id,
                        origin_open_ts_ms,
                        probe.requested_after,
                        probe.terminal_cursor,
                        probe.page_limit,
                        probe.empty_probe_count,
                        probe.anchor_open_ts_ms,
                        probe.anchor_payload_sha256,
                        _iso(probe.observed_at),
                        evidence_sha256,
                    ),
                )
            backfill_complete, probe_count, confirmed_at = self._origin_status(
                db, series, origin_open_ts_ms
            )
        return {
            "backfillComplete": backfill_complete,
            "duplicate": duplicate,
            "instrument": series.instrument,
            "originConfirmedAt": confirmed_at,
            "originOpenTsMs": origin_open_ts_ms,
            "originProbeCount": probe_count,
            "probeSha256": evidence_sha256,
            "role": role,
            "runId": probe.run_id,
            "seriesKey": series.value,
        }

    def _snapshot_from_row(self, row: sqlite3.Row) -> MultiAssetMarketSnapshot:
        return MultiAssetMarketSnapshot(
            snapshot_id=str(row["snapshot_id"]),
            source=str(row["source"]),
            instrument=str(row["inst_id"]),
            bar=str(row["bar"]),
            first_open_ts_ms=int(row["first_open_ts_ms"]),
            last_open_ts_ms=int(row["last_open_ts_ms"]),
            row_count=int(row["row_count"]),
            content_sha256=str(row["content_sha256"]),
            feature_contract_sha256=str(row["feature_contract_sha256"]),
            created_at=_parse_iso(str(row["created_at"]), "snapshot created_at"),
        )

    def status(self, instrument: str, *, bar: str = DEFAULT_BAR) -> dict[str, Any]:
        series = self._series(instrument, bar)
        with self._connection() as db:
            stored_rows, first_ts, last_ts = self._coverage(db, series)
            missing_bars, gap_ranges, gap_ranges_truncated = self._gaps(db, series)
            conflicts = self._conflict_count(db, series)
            backfill_complete, origin_probe_count, origin_confirmed_at = (
                self._origin_status(db, series, first_ts)
            )
            latest_row = db.execute(
                """
                SELECT * FROM ma_snapshots
                WHERE source = ? AND inst_id = ? AND bar = ?
                ORDER BY created_at DESC, snapshot_id DESC
                LIMIT 1
                """,
                self._series_values(series),
            ).fetchone()
        expected_rows = (
            (int(last_ts) - int(first_ts)) // FIVE_MINUTES_MS + 1
            if first_ts is not None and last_ts is not None
            else 0
        )
        latest = self._snapshot_from_row(latest_row) if latest_row else None
        latest_current = bool(
            latest
            and backfill_complete
            and conflicts == 0
            and missing_bars == 0
            and first_ts is not None
            and last_ts is not None
            and latest.first_open_ts_ms == int(first_ts)
            and latest.last_open_ts_ms == int(last_ts)
            and latest.row_count == stored_rows
        )
        if latest_current and latest is not None:
            # Bounds/count equality is insufficient if a local database row
            # and its per-row hash were both altered. Consume the immutable
            # snapshot view so status only reports current after content-level
            # SHA-256 verification as well.
            for _row in self.rows(latest.snapshot_id):
                pass
        disk_bytes = sum(
            candidate.stat().st_size
            for candidate in (
                self.path,
                Path(f"{self.path}-journal"),
            )
            if candidate.exists()
        )
        return {
            "backfillComplete": backfill_complete,
            "bar": series.bar,
            "completeGrid": stored_rows > 0 and missing_bars == 0,
            "coverageDays": (
                (int(last_ts) - int(first_ts)) / 86_400_000
                if first_ts is not None and last_ts is not None
                else 0.0
            ),
            "diskBytes": disk_bytes,
            "expectedRows": expected_rows,
            "firstOpenTsMs": first_ts,
            "gapRanges": gap_ranges,
            "gapRangesTruncated": gap_ranges_truncated,
            "instrument": series.instrument,
            "lastOpenTsMs": last_ts,
            "latestSnapshot": latest.to_dict() if latest else None,
            "latestSnapshotCurrent": latest_current,
            "missingBars": missing_bars,
            "originConfirmedAt": origin_confirmed_at,
            "originProbeCount": origin_probe_count,
            "readyForSnapshot": (
                backfill_complete
                and stored_rows > 0
                and missing_bars == 0
                and conflicts == 0
            ),
            "schemaVersion": MULTI_ASSET_MARKET_SCHEMA,
            "seriesKey": series.value,
            "source": series.source,
            "storedRows": stored_rows,
            "syncStatus": (
                "empty"
                if stored_rows == 0
                else "complete" if backfill_complete else "originPending"
            ),
            "unresolvedConflicts": conflicts,
        }

    def create_snapshot(
        self,
        instrument: str,
        *,
        feature_contract_sha256: str,
        now: datetime,
        bar: str = DEFAULT_BAR,
    ) -> MultiAssetMarketSnapshot:
        series = self._series(instrument, bar)
        feature_hash = _feature_hash(feature_contract_sha256)
        created_at = _utc(now, "now")
        with self._lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            stored_rows, first_ts, last_ts = self._coverage(db, series)
            missing_bars, _ranges, _truncated = self._gaps(db, series)
            conflicts = self._conflict_count(db, series)
            if stored_rows < 1 or first_ts is None or last_ts is None:
                raise MultiAssetMarketError("series history is empty")
            backfill_complete, _observations, _confirmed_at = self._origin_status(
                db, series, first_ts
            )
            if not backfill_complete:
                raise MultiAssetMarketError(
                    "series origin requires two runs at least 60 seconds apart"
                )
            if missing_bars:
                raise MultiAssetMarketError("series history has gaps")
            if conflicts:
                raise MultiAssetMarketError("series history has unresolved conflicts")

            metadata = _snapshot_metadata(
                series=series,
                feature_contract_sha256=feature_hash,
                first_open_ts_ms=int(first_ts),
                last_open_ts_ms=int(last_ts),
                row_count=stored_rows,
            )
            digest = hashlib.sha256()
            digest.update(_canonical_json(metadata).encode("utf-8"))
            digest.update(b"\n")
            seen = 0
            for stored in db.execute(
                """
                SELECT open_ts_ms, open_text, high_text, low_text, close_text,
                       volume_text, volume_ccy_text, volume_quote_text,
                       confirm, payload_sha256
                FROM ma_candles
                WHERE source = ? AND inst_id = ? AND bar = ?
                ORDER BY open_ts_ms ASC
                """,
                self._series_values(series),
            ):
                row = _row_from_stored(stored)
                if not hmac.compare_digest(
                    _payload_sha256(row), str(stored["payload_sha256"])
                ):
                    raise MultiAssetMarketError("stored candle payload hash mismatch")
                digest.update(_canonical_json(row).encode("utf-8"))
                digest.update(b"\n")
                seen += 1
            if seen != stored_rows:
                raise MultiAssetMarketError("series coverage changed during snapshot creation")

            content_sha256 = digest.hexdigest()
            snapshot_id = f"maset_{content_sha256[:24]}"
            existing = db.execute(
                "SELECT * FROM ma_snapshots WHERE snapshot_id = ?", (snapshot_id,)
            ).fetchone()
            if existing:
                if not hmac.compare_digest(
                    str(existing["content_sha256"]), content_sha256
                ):
                    raise MultiAssetMarketError("snapshot ID collision")
                return self._snapshot_from_row(existing)
            db.execute(
                """
                INSERT INTO ma_snapshots
                (snapshot_id, source, inst_id, bar, first_open_ts_ms,
                 last_open_ts_ms, row_count, content_sha256,
                 feature_contract_sha256, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot_id,
                    series.source,
                    series.instrument,
                    series.bar,
                    first_ts,
                    last_ts,
                    stored_rows,
                    content_sha256,
                    feature_hash,
                    _iso(created_at),
                ),
            )
            stored_snapshot = db.execute(
                "SELECT * FROM ma_snapshots WHERE snapshot_id = ?", (snapshot_id,)
            ).fetchone()
            if stored_snapshot is None:
                raise MultiAssetMarketError("snapshot could not be persisted")
            return self._snapshot_from_row(stored_snapshot)

    def get_snapshot(self, snapshot_id: str) -> MultiAssetMarketSnapshot | None:
        if not isinstance(snapshot_id, str) or not snapshot_id.startswith("maset_"):
            return None
        with self._connection() as db:
            row = db.execute(
                "SELECT * FROM ma_snapshots WHERE snapshot_id = ?", (snapshot_id,)
            ).fetchone()
        return self._snapshot_from_row(row) if row else None

    def rows(self, snapshot_id: str) -> MultiAssetSnapshotRows:
        snapshot = self.get_snapshot(snapshot_id)
        if snapshot is None:
            raise MultiAssetMarketError("multi-asset snapshot does not exist")
        return MultiAssetSnapshotRows(self.path, snapshot)

    def snapshot_rows(self, snapshot_id: str) -> MultiAssetSnapshotRows:
        """Compatibility alias for callers shared with the single-asset store."""

        return self.rows(snapshot_id)

    def snapshot_is_current(self, content_sha256: str | None) -> bool:
        if not isinstance(content_sha256, str) or not _SHA256_PATTERN.fullmatch(
            content_sha256
        ):
            return False
        with self._connection() as db:
            row = db.execute(
                """
                SELECT * FROM ma_snapshots WHERE content_sha256 = ?
                LIMIT 1
                """,
                (content_sha256,),
            ).fetchone()
        if row is None:
            return False
        snapshot = self._snapshot_from_row(row)
        state = self.status(snapshot.instrument, bar=snapshot.bar)
        return bool(
            state["readyForSnapshot"]
            and state["firstOpenTsMs"] == snapshot.first_open_ts_ms
            and state["lastOpenTsMs"] == snapshot.last_open_ts_ms
            and state["storedRows"] == snapshot.row_count
        )


__all__ = [
    "DEFAULT_BAR",
    "FIVE_MINUTES_MS",
    "MarketSeriesKey",
    "MultiAssetMarketError",
    "MultiAssetMarketSnapshot",
    "MultiAssetMarketStore",
    "MultiAssetSnapshotRows",
    "OKX_PUBLIC_SOURCE",
    "ORIGIN_CONFIRMATION_DELAY",
    "OriginProbe",
]
