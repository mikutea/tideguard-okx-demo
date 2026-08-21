from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from ..sqlite_runtime import configure_sqlite_connection
from .alternative_data import (
    ALTERNATIVE_DATA_SCHEMA_VERSION,
    PublicTextEvent,
    SentimentScore,
    SourcePolicy,
)
from .strategy import canonical_json, sha256_hex


SIGNAL_STORE_SCHEMA_VERSION = 1
SIGNAL_STORE_VERSION = "moheng.signal-store.v1"
SIGNAL_SCORE_SCHEMA_VERSION = "moheng.sentiment-score.v1"
SIGNAL_SNAPSHOT_SCHEMA_VERSION = "moheng.signal-snapshot.v1"
CLOCK_SKEW_TOLERANCE = timedelta(seconds=5)


class SignalStoreError(RuntimeError):
    pass


def _utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise SignalStoreError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return _utc(value, "timestamp").isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _parse_iso(value: Any, name: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise SignalStoreError(f"{name} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SignalStoreError(f"{name} is invalid") from exc
    return _utc(parsed, name)


def _valid_hash(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )


def _event_content_dict(event: PublicTextEvent) -> dict[str, Any]:
    """Return immutable publisher content, excluding collector clock fields."""

    return {
        "asset": event.asset,
        "headline": event.headline,
        "language": event.language,
        "publishedAt": _iso(event.published_at),
        "revision": event.revision,
        "schemaVersion": ALTERNATIVE_DATA_SCHEMA_VERSION,
        "sourceEventId": event.source_event_id,
        "sourceId": event.source_id,
        "url": event.url,
    }


def _event_content_sha256(event: PublicTextEvent) -> str:
    return sha256_hex(canonical_json(_event_content_dict(event)))


def _event_record_sha256(
    event: PublicTextEvent,
    *,
    policy_sha256: str,
    content_sha256: str,
    accepted_at: datetime,
) -> str:
    return sha256_hex(
        canonical_json(
            {
                "acceptedAt": _iso(accepted_at),
                "contentSha256": content_sha256,
                "event": event.to_dict(),
                "eventSha256": event.sha256,
                "policySha256": policy_sha256,
                "schemaVersion": SIGNAL_STORE_VERSION,
            }
        )
    )


def _score_dict(score: SentimentScore) -> dict[str, Any]:
    return {
        "assetRelevance": score.asset_relevance,
        "eventSha256": score.event_sha256,
        "modelSha256": score.model_sha256,
        "negative": score.negative,
        "neutral": score.neutral,
        "positive": score.positive,
        "schemaVersion": SIGNAL_SCORE_SCHEMA_VERSION,
        "scoredAt": _iso(score.scored_at),
    }


def _score_content_dict(score: SentimentScore) -> dict[str, Any]:
    value = _score_dict(score)
    value.pop("scoredAt")
    return value


def _score_sha256(score: SentimentScore) -> str:
    return sha256_hex(canonical_json(_score_dict(score)))


def _score_content_sha256(score: SentimentScore) -> str:
    return sha256_hex(canonical_json(_score_content_dict(score)))


def _score_record_sha256(
    score: SentimentScore, *, content_sha256: str, accepted_at: datetime
) -> str:
    return sha256_hex(
        canonical_json(
            {
                "acceptedAt": _iso(accepted_at),
                "contentSha256": content_sha256,
                "schemaVersion": SIGNAL_STORE_VERSION,
                "score": _score_dict(score),
                "scoreSha256": _score_sha256(score),
            }
        )
    )


def _policy_from_json(value: str) -> SourcePolicy:
    try:
        item = json.loads(value)
        return SourcePolicy(
            source_id=item["sourceId"],
            license_id=item["licenseId"],
            headline_storage_allowed=item["headlineStorageAllowed"],
            full_text_storage_allowed=item["fullTextStorageAllowed"],
            redistribution_allowed=item["redistributionAllowed"],
            reliability_weight=item["reliabilityWeight"],
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise SignalStoreError("stored source policy is invalid") from exc


@dataclass(frozen=True)
class SignalRecord:
    source_policy: SourcePolicy
    event: PublicTextEvent
    score: SentimentScore
    policy_sha256: str
    content_sha256: str
    score_sha256: str
    event_accepted_at: datetime
    score_accepted_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "contentSha256": self.content_sha256,
            "event": self.event.to_dict(),
            "eventAcceptedAt": _iso(self.event_accepted_at),
            "eventSha256": self.event.sha256,
            "policySha256": self.policy_sha256,
            "score": _score_dict(self.score),
            "scoreAcceptedAt": _iso(self.score_accepted_at),
            "scoreSha256": self.score_sha256,
            "sourcePolicy": self.source_policy.to_dict(),
        }


@dataclass(frozen=True)
class SignalSnapshot:
    snapshot_id: str
    asset: str
    as_of: datetime
    lookback_seconds: int
    model_sha256: str
    prospective_only: bool
    row_count: int
    content_sha256: str
    created_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "asOf": _iso(self.as_of),
            "asset": self.asset,
            "contentSha256": self.content_sha256,
            "createdAt": _iso(self.created_at),
            "lookbackSeconds": self.lookback_seconds,
            "modelSha256": self.model_sha256,
            "prospectiveOnly": self.prospective_only,
            "rowCount": self.row_count,
            "schemaVersion": SIGNAL_SNAPSHOT_SCHEMA_VERSION,
            "snapshotId": self.snapshot_id,
        }


class SignalStore:
    """Append-only warehouse for public, point-in-time alternative-data signals.

    This store deliberately has no network client, secret access or execution API.
    Accepted facts are never updated. Re-observations are appended to audit tables;
    identity conflicts are quarantined and block new integrity snapshots.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
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
        with self._connection() as db:
            version = int(db.execute("PRAGMA user_version").fetchone()[0])
            if version not in {0, SIGNAL_STORE_SCHEMA_VERSION}:
                raise SignalStoreError("signal store schema is newer than this application")
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS source_policies (
                    policy_sha256 TEXT PRIMARY KEY,
                    source_id TEXT NOT NULL,
                    license_id TEXT NOT NULL,
                    policy_json TEXT NOT NULL,
                    registered_at TEXT NOT NULL
                ) WITHOUT ROWID;

                CREATE TABLE IF NOT EXISTS signal_events (
                    event_sha256 TEXT PRIMARY KEY,
                    source_id TEXT NOT NULL,
                    source_event_id TEXT NOT NULL,
                    revision INTEGER NOT NULL CHECK(revision >= 0),
                    asset TEXT NOT NULL,
                    published_at TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    fetched_at TEXT NOT NULL,
                    available_at TEXT NOT NULL,
                    historical_backfill INTEGER NOT NULL
                        CHECK(historical_backfill IN (0, 1)),
                    policy_sha256 TEXT NOT NULL,
                    content_sha256 TEXT NOT NULL,
                    event_json TEXT NOT NULL,
                    record_sha256 TEXT NOT NULL,
                    accepted_at TEXT NOT NULL,
                    UNIQUE(source_id, source_event_id, revision),
                    FOREIGN KEY(policy_sha256) REFERENCES source_policies(policy_sha256)
                );
                CREATE INDEX IF NOT EXISTS idx_signal_events_point_in_time
                    ON signal_events(asset, available_at, fetched_at, accepted_at);

                CREATE TABLE IF NOT EXISTS event_observations (
                    observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    observed_event_sha256 TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    source_event_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    policy_sha256 TEXT NOT NULL,
                    content_sha256 TEXT NOT NULL,
                    event_json TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    disposition TEXT NOT NULL
                        CHECK(disposition IN ('accepted', 'duplicate', 'conflict'))
                );

                CREATE TABLE IF NOT EXISTS event_conflicts (
                    observation_id INTEGER PRIMARY KEY,
                    canonical_event_sha256 TEXT,
                    reason TEXT NOT NULL,
                    detected_at TEXT NOT NULL,
                    FOREIGN KEY(observation_id) REFERENCES event_observations(observation_id)
                );

                CREATE TABLE IF NOT EXISTS sentiment_scores (
                    score_sha256 TEXT PRIMARY KEY,
                    event_sha256 TEXT NOT NULL,
                    model_sha256 TEXT NOT NULL,
                    scored_at TEXT NOT NULL,
                    positive REAL NOT NULL,
                    neutral REAL NOT NULL,
                    negative REAL NOT NULL,
                    asset_relevance REAL NOT NULL,
                    content_sha256 TEXT NOT NULL,
                    score_json TEXT NOT NULL,
                    record_sha256 TEXT NOT NULL,
                    accepted_at TEXT NOT NULL,
                    UNIQUE(event_sha256, model_sha256),
                    FOREIGN KEY(event_sha256) REFERENCES signal_events(event_sha256)
                );
                CREATE INDEX IF NOT EXISTS idx_sentiment_scores_point_in_time
                    ON sentiment_scores(model_sha256, scored_at, accepted_at);

                CREATE TABLE IF NOT EXISTS score_observations (
                    observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    observed_score_sha256 TEXT NOT NULL,
                    event_sha256 TEXT NOT NULL,
                    model_sha256 TEXT NOT NULL,
                    content_sha256 TEXT NOT NULL,
                    score_json TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    disposition TEXT NOT NULL
                        CHECK(disposition IN ('accepted', 'duplicate', 'conflict'))
                );

                CREATE TABLE IF NOT EXISTS score_conflicts (
                    observation_id INTEGER PRIMARY KEY,
                    canonical_score_sha256 TEXT,
                    reason TEXT NOT NULL,
                    detected_at TEXT NOT NULL,
                    FOREIGN KEY(observation_id) REFERENCES score_observations(observation_id)
                );

                CREATE TABLE IF NOT EXISTS signal_snapshots (
                    snapshot_id TEXT PRIMARY KEY,
                    asset TEXT NOT NULL,
                    as_of TEXT NOT NULL,
                    lookback_seconds INTEGER NOT NULL CHECK(lookback_seconds > 0),
                    model_sha256 TEXT NOT NULL,
                    prospective_only INTEGER NOT NULL CHECK(prospective_only IN (0, 1)),
                    row_count INTEGER NOT NULL CHECK(row_count >= 0),
                    content_sha256 TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                ) WITHOUT ROWID;
                """
            )
            for table in (
                "source_policies",
                "signal_events",
                "event_observations",
                "event_conflicts",
                "sentiment_scores",
                "score_observations",
                "score_conflicts",
                "signal_snapshots",
            ):
                db.executescript(
                    f"""
                    CREATE TRIGGER IF NOT EXISTS no_update_{table}
                    BEFORE UPDATE ON {table}
                    BEGIN SELECT RAISE(ABORT, 'append-only table'); END;
                    CREATE TRIGGER IF NOT EXISTS no_delete_{table}
                    BEFORE DELETE ON {table}
                    BEGIN SELECT RAISE(ABORT, 'append-only table'); END;
                    """
                )
            db.execute(f"PRAGMA user_version={SIGNAL_STORE_SCHEMA_VERSION}")

    @staticmethod
    def _validate_query(
        *, asset: str, as_of: datetime, lookback: timedelta, model_sha256: str
    ) -> tuple[datetime, int]:
        closed = _utc(as_of, "as_of")
        if not asset or asset.upper() != asset or len(asset) > 32:
            raise SignalStoreError("signal asset is invalid")
        if lookback <= timedelta(0) or lookback > timedelta(days=30):
            raise SignalStoreError("signal lookback is invalid")
        if not _valid_hash(model_sha256):
            raise SignalStoreError("sentiment model hash is invalid")
        return closed, round(lookback.total_seconds())

    @staticmethod
    def _register_policy(
        db: sqlite3.Connection,
        policy: SourcePolicy,
        *,
        registered_at: datetime,
    ) -> None:
        payload = canonical_json(policy.to_dict())
        existing = db.execute(
            "SELECT * FROM source_policies WHERE policy_sha256 = ?",
            (policy.sha256,),
        ).fetchone()
        if existing is not None:
            if not hmac.compare_digest(str(existing["policy_json"]), payload):
                raise SignalStoreError("stored source policy hash mismatch")
            return
        db.execute(
            """
            INSERT INTO source_policies
            (policy_sha256, source_id, license_id, policy_json, registered_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                policy.sha256,
                policy.source_id,
                policy.license_id,
                payload,
                _iso(registered_at),
            ),
        )

    def register_source_policy(
        self, policy: SourcePolicy, *, registered_at: datetime
    ) -> str:
        timestamp = _utc(registered_at, "registered_at")
        with self._lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._register_policy(db, policy, registered_at=timestamp)
        return policy.sha256

    def append_event(
        self,
        event: PublicTextEvent,
        source_policy: SourcePolicy,
        *,
        observed_at: datetime,
    ) -> dict[str, str]:
        observed = _utc(observed_at, "observed_at")
        if event.source_id != source_policy.source_id:
            raise SignalStoreError("event source does not match source policy")
        if not source_policy.headline_storage_allowed:
            raise SignalStoreError("source policy does not allow headline storage")
        if _utc(event.fetched_at, "fetched_at") > observed + CLOCK_SKEW_TOLERANCE:
            raise SignalStoreError("event fetched_at is after observed_at")

        event_json = canonical_json(event.to_dict())
        event_sha = event.sha256
        content_sha = _event_content_sha256(event)
        record_sha = _event_record_sha256(
            event,
            policy_sha256=source_policy.sha256,
            content_sha256=content_sha,
            accepted_at=observed,
        )
        disposition = "accepted"
        reason: str | None = None
        canonical_sha: str | None = None

        with self._lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._register_policy(db, source_policy, registered_at=observed)
            existing = db.execute(
                """
                SELECT * FROM signal_events
                WHERE source_id = ? AND source_event_id = ? AND revision = ?
                """,
                (event.source_id, event.source_event_id, event.revision),
            ).fetchone()
            if existing is not None:
                canonical_sha = str(existing["event_sha256"])
                conflicts: list[str] = []
                if not hmac.compare_digest(str(existing["content_sha256"]), content_sha):
                    conflicts.append("content mismatch")
                if str(existing["policy_sha256"]) != source_policy.sha256:
                    conflicts.append("source policy mismatch")
                if bool(existing["historical_backfill"]) != event.historical_backfill:
                    conflicts.append("historical provenance mismatch")
                if _utc(event.first_seen_at, "first_seen_at") < _parse_iso(
                    existing["first_seen_at"], "stored first_seen_at"
                ):
                    conflicts.append("first_seen regression")
                if conflicts:
                    disposition = "conflict"
                    reason = "; ".join(conflicts)
                else:
                    disposition = "duplicate"
            else:
                identity_first = db.execute(
                    """
                    SELECT event_sha256, first_seen_at FROM signal_events
                    WHERE source_id = ? AND source_event_id = ?
                    ORDER BY first_seen_at ASC LIMIT 1
                    """,
                    (event.source_id, event.source_event_id),
                ).fetchone()
                if identity_first is not None and _utc(
                    event.first_seen_at, "first_seen_at"
                ) < _parse_iso(identity_first["first_seen_at"], "stored first_seen_at"):
                    disposition = "conflict"
                    reason = "first_seen regression across revisions"
                    canonical_sha = str(identity_first["event_sha256"])

            observation = db.execute(
                """
                INSERT INTO event_observations
                (observed_event_sha256, source_id, source_event_id, revision,
                 policy_sha256, content_sha256, event_json, observed_at, disposition)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_sha,
                    event.source_id,
                    event.source_event_id,
                    event.revision,
                    source_policy.sha256,
                    content_sha,
                    event_json,
                    _iso(observed),
                    disposition,
                ),
            )
            observation_id = int(observation.lastrowid)
            if disposition == "accepted":
                db.execute(
                    """
                    INSERT INTO signal_events
                    (event_sha256, source_id, source_event_id, revision, asset,
                     published_at, first_seen_at, fetched_at, available_at,
                     historical_backfill, policy_sha256, content_sha256,
                     event_json, record_sha256, accepted_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_sha,
                        event.source_id,
                        event.source_event_id,
                        event.revision,
                        event.asset,
                        _iso(event.published_at),
                        _iso(event.first_seen_at),
                        _iso(event.fetched_at),
                        _iso(event.available_at),
                        int(event.historical_backfill),
                        source_policy.sha256,
                        content_sha,
                        event_json,
                        record_sha,
                        _iso(observed),
                    ),
                )
            elif disposition == "conflict":
                db.execute(
                    """
                    INSERT INTO event_conflicts
                    (observation_id, canonical_event_sha256, reason, detected_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (observation_id, canonical_sha, reason, _iso(observed)),
                )
        return {
            "contentSha256": content_sha,
            "eventSha256": event_sha,
            "status": disposition,
        }

    def append_score(
        self, score: SentimentScore, *, observed_at: datetime
    ) -> dict[str, str]:
        observed = _utc(observed_at, "observed_at")
        if _utc(score.scored_at, "scored_at") > observed + CLOCK_SKEW_TOLERANCE:
            raise SignalStoreError("score scored_at is after observed_at")
        score_json = canonical_json(_score_dict(score))
        score_sha = _score_sha256(score)
        content_sha = _score_content_sha256(score)
        record_sha = _score_record_sha256(
            score, content_sha256=content_sha, accepted_at=observed
        )
        disposition = "accepted"
        reason: str | None = None
        canonical_sha: str | None = None

        with self._lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            event = db.execute(
                "SELECT event_sha256 FROM signal_events WHERE event_sha256 = ?",
                (score.event_sha256,),
            ).fetchone()
            if event is None:
                raise SignalStoreError("sentiment score references an unknown event")
            existing = db.execute(
                """
                SELECT * FROM sentiment_scores
                WHERE event_sha256 = ? AND model_sha256 = ?
                """,
                (score.event_sha256, score.model_sha256),
            ).fetchone()
            if existing is not None:
                canonical_sha = str(existing["score_sha256"])
                conflicts: list[str] = []
                if not hmac.compare_digest(str(existing["content_sha256"]), content_sha):
                    conflicts.append("score content mismatch")
                if _utc(score.scored_at, "scored_at") < _parse_iso(
                    existing["scored_at"], "stored scored_at"
                ):
                    conflicts.append("scored_at regression")
                if conflicts:
                    disposition = "conflict"
                    reason = "; ".join(conflicts)
                else:
                    disposition = "duplicate"
            observation = db.execute(
                """
                INSERT INTO score_observations
                (observed_score_sha256, event_sha256, model_sha256,
                 content_sha256, score_json, observed_at, disposition)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    score_sha,
                    score.event_sha256,
                    score.model_sha256,
                    content_sha,
                    score_json,
                    _iso(observed),
                    disposition,
                ),
            )
            observation_id = int(observation.lastrowid)
            if disposition == "accepted":
                db.execute(
                    """
                    INSERT INTO sentiment_scores
                    (score_sha256, event_sha256, model_sha256, scored_at,
                     positive, neutral, negative, asset_relevance,
                     content_sha256, score_json, record_sha256, accepted_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        score_sha,
                        score.event_sha256,
                        score.model_sha256,
                        _iso(score.scored_at),
                        score.positive,
                        score.neutral,
                        score.negative,
                        score.asset_relevance,
                        content_sha,
                        score_json,
                        record_sha,
                        _iso(observed),
                    ),
                )
            elif disposition == "conflict":
                db.execute(
                    """
                    INSERT INTO score_conflicts
                    (observation_id, canonical_score_sha256, reason, detected_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (observation_id, canonical_sha, reason, _iso(observed)),
                )
        return {
            "contentSha256": content_sha,
            "scoreSha256": score_sha,
            "status": disposition,
        }

    @staticmethod
    def _read_policy(policy_row: sqlite3.Row) -> SourcePolicy:
        policy = _policy_from_json(str(policy_row["policy_json"]))
        try:
            _parse_iso(policy_row["registered_at"], "policy registered_at")
        except (IndexError, KeyError) as exc:
            raise SignalStoreError("stored source policy is incomplete") from exc
        if not hmac.compare_digest(policy.sha256, str(policy_row["policy_sha256"])):
            raise SignalStoreError("stored source policy hash mismatch")
        if (
            policy.source_id != policy_row["source_id"]
            or policy.license_id != policy_row["license_id"]
            or not hmac.compare_digest(
                canonical_json(policy.to_dict()), str(policy_row["policy_json"])
            )
        ):
            raise SignalStoreError("stored source policy identity mismatch")
        return policy

    @classmethod
    def _read_event(
        cls, db: sqlite3.Connection, row: sqlite3.Row
    ) -> tuple[PublicTextEvent, SourcePolicy]:
        policy_row = db.execute(
            "SELECT * FROM source_policies WHERE policy_sha256 = ?",
            (row["policy_sha256"],),
        ).fetchone()
        if policy_row is None:
            raise SignalStoreError("stored event source policy is missing")
        policy = cls._read_policy(policy_row)
        try:
            item = json.loads(str(row["event_json"]))
            event = PublicTextEvent(
                source_id=item["sourceId"],
                source_event_id=item["sourceEventId"],
                asset=item["asset"],
                published_at=_parse_iso(item["publishedAt"], "published_at"),
                first_seen_at=_parse_iso(item["firstSeenAt"], "first_seen_at"),
                fetched_at=_parse_iso(item["fetchedAt"], "fetched_at"),
                headline=item["headline"],
                url=item["url"],
                language=item["language"],
                revision=item["revision"],
                historical_backfill=item["historicalBackfill"],
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise SignalStoreError("stored event payload is invalid") from exc
        accepted_at = _parse_iso(row["accepted_at"], "event accepted_at")
        checks = (
            hmac.compare_digest(event.sha256, str(row["event_sha256"])),
            hmac.compare_digest(_event_content_sha256(event), str(row["content_sha256"])),
            hmac.compare_digest(canonical_json(event.to_dict()), str(row["event_json"])),
            hmac.compare_digest(
                _event_record_sha256(
                    event,
                    policy_sha256=str(row["policy_sha256"]),
                    content_sha256=str(row["content_sha256"]),
                    accepted_at=accepted_at,
                ),
                str(row["record_sha256"]),
            ),
            event.source_id == row["source_id"],
            event.source_event_id == row["source_event_id"],
            event.revision == int(row["revision"]),
            event.asset == row["asset"],
            _iso(event.published_at) == row["published_at"],
            _iso(event.first_seen_at) == row["first_seen_at"],
            _iso(event.fetched_at) == row["fetched_at"],
            _iso(event.available_at) == row["available_at"],
            event.historical_backfill == bool(row["historical_backfill"]),
            policy.source_id == event.source_id,
            _utc(event.fetched_at, "fetched_at")
            <= accepted_at + CLOCK_SKEW_TOLERANCE,
        )
        if not all(checks):
            raise SignalStoreError("stored event integrity check failed")
        return event, policy

    @staticmethod
    def _read_score(row: sqlite3.Row) -> SentimentScore:
        try:
            item = json.loads(str(row["score_json"]))
            score = SentimentScore(
                event_sha256=item["eventSha256"],
                model_sha256=item["modelSha256"],
                scored_at=_parse_iso(item["scoredAt"], "scored_at"),
                positive=item["positive"],
                neutral=item["neutral"],
                negative=item["negative"],
                asset_relevance=item["assetRelevance"],
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise SignalStoreError("stored score payload is invalid") from exc
        accepted_at = _parse_iso(row["accepted_at"], "score accepted_at")
        checks = (
            hmac.compare_digest(_score_sha256(score), str(row["score_sha256"])),
            hmac.compare_digest(_score_content_sha256(score), str(row["content_sha256"])),
            hmac.compare_digest(canonical_json(_score_dict(score)), str(row["score_json"])),
            hmac.compare_digest(
                _score_record_sha256(
                    score,
                    content_sha256=str(row["content_sha256"]),
                    accepted_at=accepted_at,
                ),
                str(row["record_sha256"]),
            ),
            score.event_sha256 == row["event_sha256"],
            score.model_sha256 == row["model_sha256"],
            _iso(score.scored_at) == row["scored_at"],
            score.positive == float(row["positive"]),
            score.neutral == float(row["neutral"]),
            score.negative == float(row["negative"]),
            score.asset_relevance == float(row["asset_relevance"]),
            _utc(score.scored_at, "scored_at")
            <= accepted_at + CLOCK_SKEW_TOLERANCE,
        )
        if not all(checks):
            raise SignalStoreError("stored score integrity check failed")
        return score

    def point_in_time(
        self,
        *,
        asset: str,
        as_of: datetime,
        lookback: timedelta,
        model_sha256: str,
        prospective_only: bool = True,
    ) -> tuple[SignalRecord, ...]:
        closed, lookback_seconds = self._validate_query(
            asset=asset, as_of=as_of, lookback=lookback, model_sha256=model_sha256
        )
        if not isinstance(prospective_only, bool):
            raise SignalStoreError("prospective_only must be boolean")
        cutoff = closed - timedelta(seconds=lookback_seconds)
        with self._connection() as db:
            rows = db.execute(
                """
                SELECT * FROM signal_events
                WHERE asset = ?
                  AND available_at > ? AND available_at <= ?
                  AND fetched_at <= ? AND accepted_at <= ?
                  AND (? = 0 OR historical_backfill = 0)
                ORDER BY source_id, source_event_id, revision DESC
                """,
                (
                    asset,
                    _iso(cutoff),
                    _iso(closed),
                    _iso(closed),
                    _iso(closed),
                    int(prospective_only),
                ),
            ).fetchall()
            latest: dict[tuple[str, str], sqlite3.Row] = {}
            for row in rows:
                key = (str(row["source_id"]), str(row["source_event_id"]))
                if key not in latest:
                    latest[key] = row

            records: list[SignalRecord] = []
            for row in latest.values():
                event, policy = self._read_event(db, row)
                score_row = db.execute(
                    """
                    SELECT * FROM sentiment_scores
                    WHERE event_sha256 = ? AND model_sha256 = ?
                      AND scored_at <= ? AND accepted_at <= ?
                    """,
                    (event.sha256, model_sha256, _iso(closed), _iso(closed)),
                ).fetchone()
                # Never fall back to a stale revision when its latest visible
                # revision has not yet been scored.
                if score_row is None:
                    continue
                score = self._read_score(score_row)
                records.append(
                    SignalRecord(
                        source_policy=policy,
                        event=event,
                        score=score,
                        policy_sha256=str(row["policy_sha256"]),
                        content_sha256=str(row["content_sha256"]),
                        score_sha256=str(score_row["score_sha256"]),
                        event_accepted_at=_parse_iso(row["accepted_at"], "event accepted_at"),
                        score_accepted_at=_parse_iso(
                            score_row["accepted_at"], "score accepted_at"
                        ),
                    )
                )
        return tuple(
            sorted(
                records,
                key=lambda item: (
                    item.event.available_at,
                    item.event.source_id,
                    item.event.source_event_id,
                    item.event.revision,
                ),
            )
        )

    @staticmethod
    def _snapshot_metadata(
        *,
        asset: str,
        as_of: datetime,
        lookback_seconds: int,
        model_sha256: str,
        prospective_only: bool,
        row_count: int,
    ) -> dict[str, Any]:
        return {
            "as_of": _iso(as_of),
            "asset": asset,
            "lookback_seconds": lookback_seconds,
            "model_sha256": model_sha256,
            "prospective_only": prospective_only,
            "row_count": row_count,
            "schema_version": SIGNAL_SNAPSHOT_SCHEMA_VERSION,
        }

    @classmethod
    def _records_sha256(
        cls,
        records: tuple[SignalRecord, ...],
        *,
        asset: str,
        as_of: datetime,
        lookback_seconds: int,
        model_sha256: str,
        prospective_only: bool,
    ) -> str:
        digest = hashlib.sha256()
        digest.update(
            canonical_json(
                cls._snapshot_metadata(
                    asset=asset,
                    as_of=as_of,
                    lookback_seconds=lookback_seconds,
                    model_sha256=model_sha256,
                    prospective_only=prospective_only,
                    row_count=len(records),
                )
            ).encode("utf-8")
        )
        digest.update(b"\n")
        for record in records:
            digest.update(canonical_json(record.to_dict()).encode("utf-8"))
            digest.update(b"\n")
        return digest.hexdigest()

    def create_snapshot(
        self,
        *,
        asset: str,
        as_of: datetime,
        lookback: timedelta,
        model_sha256: str,
        prospective_only: bool = True,
        now: datetime,
    ) -> SignalSnapshot:
        closed, lookback_seconds = self._validate_query(
            asset=asset, as_of=as_of, lookback=lookback, model_sha256=model_sha256
        )
        created = _utc(now, "now")
        if closed > created + CLOCK_SKEW_TOLERANCE:
            raise SignalStoreError("snapshot as_of is in the future")
        quality = self.status()
        if not quality["healthy"]:
            raise SignalStoreError("signal store has conflicts or integrity errors")
        records = self.point_in_time(
            asset=asset,
            as_of=closed,
            lookback=lookback,
            model_sha256=model_sha256,
            prospective_only=prospective_only,
        )
        content_sha = self._records_sha256(
            records,
            asset=asset,
            as_of=closed,
            lookback_seconds=lookback_seconds,
            model_sha256=model_sha256,
            prospective_only=prospective_only,
        )
        snapshot = SignalSnapshot(
            snapshot_id=f"sig_{content_sha[:24]}",
            asset=asset,
            as_of=closed,
            lookback_seconds=lookback_seconds,
            model_sha256=model_sha256,
            prospective_only=prospective_only,
            row_count=len(records),
            content_sha256=content_sha,
            created_at=created,
        )
        with self._lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT * FROM signal_snapshots WHERE snapshot_id = ?",
                (snapshot.snapshot_id,),
            ).fetchone()
            if existing is not None:
                if not hmac.compare_digest(str(existing["content_sha256"]), content_sha):
                    raise SignalStoreError("signal snapshot ID collision")
                snapshot = self._snapshot_from_row(existing)
            else:
                db.execute(
                    """
                    INSERT INTO signal_snapshots
                    (snapshot_id, asset, as_of, lookback_seconds, model_sha256,
                     prospective_only, row_count, content_sha256, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        snapshot.snapshot_id,
                        snapshot.asset,
                        _iso(snapshot.as_of),
                        snapshot.lookback_seconds,
                        snapshot.model_sha256,
                        int(snapshot.prospective_only),
                        snapshot.row_count,
                        snapshot.content_sha256,
                        _iso(snapshot.created_at),
                    ),
                )
        return snapshot

    @staticmethod
    def _snapshot_from_row(row: sqlite3.Row) -> SignalSnapshot:
        return SignalSnapshot(
            snapshot_id=str(row["snapshot_id"]),
            asset=str(row["asset"]),
            as_of=_parse_iso(row["as_of"], "snapshot as_of"),
            lookback_seconds=int(row["lookback_seconds"]),
            model_sha256=str(row["model_sha256"]),
            prospective_only=bool(row["prospective_only"]),
            row_count=int(row["row_count"]),
            content_sha256=str(row["content_sha256"]),
            created_at=_parse_iso(row["created_at"], "snapshot created_at"),
        )

    def get_snapshot(self, snapshot_id: str) -> SignalSnapshot | None:
        with self._connection() as db:
            row = db.execute(
                "SELECT * FROM signal_snapshots WHERE snapshot_id = ?", (snapshot_id,)
            ).fetchone()
        return self._snapshot_from_row(row) if row is not None else None

    def snapshot_records(self, snapshot_id: str) -> tuple[SignalRecord, ...]:
        snapshot = self.get_snapshot(snapshot_id)
        if snapshot is None:
            raise SignalStoreError("signal snapshot does not exist")
        records = self.point_in_time(
            asset=snapshot.asset,
            as_of=snapshot.as_of,
            lookback=timedelta(seconds=snapshot.lookback_seconds),
            model_sha256=snapshot.model_sha256,
            prospective_only=snapshot.prospective_only,
        )
        if len(records) != snapshot.row_count:
            raise SignalStoreError("signal snapshot row count changed")
        actual = self._records_sha256(
            records,
            asset=snapshot.asset,
            as_of=snapshot.as_of,
            lookback_seconds=snapshot.lookback_seconds,
            model_sha256=snapshot.model_sha256,
            prospective_only=snapshot.prospective_only,
        )
        if not hmac.compare_digest(actual, snapshot.content_sha256):
            raise SignalStoreError("signal snapshot content hash changed")
        return records

    def status(self) -> dict[str, Any]:
        integrity_errors = 0
        latest_available: str | None = None
        latest_scored: str | None = None
        snapshot_ids: list[str] = []
        with self._connection() as db:
            counts = {
                "sourcePolicies": db.execute("SELECT COUNT(*) FROM source_policies").fetchone()[0],
                "storedEvents": db.execute("SELECT COUNT(*) FROM signal_events").fetchone()[0],
                "storedScores": db.execute("SELECT COUNT(*) FROM sentiment_scores").fetchone()[0],
                "eventObservations": db.execute("SELECT COUNT(*) FROM event_observations").fetchone()[0],
                "scoreObservations": db.execute("SELECT COUNT(*) FROM score_observations").fetchone()[0],
                "eventConflicts": db.execute("SELECT COUNT(*) FROM event_conflicts").fetchone()[0],
                "scoreConflicts": db.execute("SELECT COUNT(*) FROM score_conflicts").fetchone()[0],
                "snapshots": db.execute("SELECT COUNT(*) FROM signal_snapshots").fetchone()[0],
                "historicalBackfillEvents": db.execute(
                    "SELECT COUNT(*) FROM signal_events WHERE historical_backfill = 1"
                ).fetchone()[0],
                "duplicateObservations": db.execute(
                    """
                    SELECT
                      (SELECT COUNT(*) FROM event_observations WHERE disposition = 'duplicate') +
                      (SELECT COUNT(*) FROM score_observations WHERE disposition = 'duplicate')
                    """
                ).fetchone()[0],
            }
            latest_available = db.execute(
                "SELECT MAX(available_at) FROM signal_events"
            ).fetchone()[0]
            latest_scored = db.execute(
                "SELECT MAX(scored_at) FROM sentiment_scores"
            ).fetchone()[0]
            if str(db.execute("PRAGMA quick_check").fetchone()[0]).lower() != "ok":
                integrity_errors += 1
            integrity_errors += len(db.execute("PRAGMA foreign_key_check").fetchall())
            for row in db.execute("SELECT * FROM source_policies"):
                try:
                    self._read_policy(row)
                except SignalStoreError:
                    integrity_errors += 1
            for row in db.execute("SELECT * FROM signal_events"):
                try:
                    self._read_event(db, row)
                except SignalStoreError:
                    integrity_errors += 1
            for row in db.execute("SELECT * FROM sentiment_scores"):
                try:
                    self._read_score(row)
                except SignalStoreError:
                    integrity_errors += 1
            snapshot_ids = [
                str(row["snapshot_id"])
                for row in db.execute("SELECT snapshot_id FROM signal_snapshots")
            ]

        for snapshot_id in snapshot_ids:
            try:
                self.snapshot_records(snapshot_id)
            except SignalStoreError:
                integrity_errors += 1

        unresolved = int(counts["eventConflicts"]) + int(counts["scoreConflicts"])
        disk_bytes = sum(
            candidate.stat().st_size
            for candidate in (self.path, Path(f"{self.path}-wal"), Path(f"{self.path}-shm"))
            if candidate.exists()
        )
        return {
            **{key: int(value) for key, value in counts.items()},
            "diskBytes": disk_bytes,
            "healthy": unresolved == 0 and integrity_errors == 0,
            "integrityErrors": integrity_errors,
            "latestAvailableAt": latest_available,
            "latestScoredAt": latest_scored,
            "prospectiveEvents": int(counts["storedEvents"])
            - int(counts["historicalBackfillEvents"]),
            "schemaVersion": SIGNAL_STORE_VERSION,
            "unresolvedConflicts": unresolved,
        }

    def quality_report(self) -> dict[str, Any]:
        return self.status()


__all__ = [
    "CLOCK_SKEW_TOLERANCE",
    "SIGNAL_SCORE_SCHEMA_VERSION",
    "SIGNAL_SNAPSHOT_SCHEMA_VERSION",
    "SIGNAL_STORE_SCHEMA_VERSION",
    "SIGNAL_STORE_VERSION",
    "SignalRecord",
    "SignalSnapshot",
    "SignalStore",
    "SignalStoreError",
]
