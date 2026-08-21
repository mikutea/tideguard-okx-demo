from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .sqlite_runtime import configure_sqlite_connection


SENSITIVE_FRAGMENTS = ("secret", "passphrase", "api_key", "apikey", "signature", "authorization")
ENVIRONMENT_SWITCH_BLOCKING_INTENT_STATES = frozenset(
    {
        "dispatching",
        "uncertain",
        "manual_review",
        "accepted",
        "reconciled",
        "transport_error",
    }
)


class IntentIdentityConflict(RuntimeError):
    pass


class PersistentStateError(RuntimeError):
    pass


def _sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        clean: dict[str, Any] = {}
        for key, item in value.items():
            if any(fragment in key.lower() for fragment in SENSITIVE_FRAGMENTS):
                clean[key] = "[REDACTED]"
            else:
                clean[key] = _sanitize(item)
        return clean
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    if isinstance(value, tuple):
        return [_sanitize(item) for item in value]
    return value


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class AuditStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        configure_sqlite_connection(connection, self.path)
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _flag_value(db: sqlite3.Connection, name: str, default: str) -> str:
        row = db.execute("SELECT value FROM flags WHERE name = ?", (name,)).fetchone()
        return str(row["value"]) if row else default

    @staticmethod
    def _parse_kill_active(value: str) -> bool:
        if value not in {"true", "false"}:
            raise PersistentStateError("kill_active 持久状态损坏")
        return value == "true"

    @staticmethod
    def _parse_kill_generation(value: str) -> int:
        if not value.isdecimal():
            raise PersistentStateError("kill_generation 持久状态损坏")
        return int(value)

    @staticmethod
    def _upsert_flag(db: sqlite3.Connection, name: str, value: str, timestamp: str) -> None:
        db.execute(
            """
            INSERT INTO flags (name, value, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """,
            (name, value, timestamp),
        )

    def _engage_kill_in_transaction(
        self,
        db: sqlite3.Connection,
        credential_fingerprint: str | None = None,
        account_fingerprint: str | None = None,
    ) -> int:
        generation = self._parse_kill_generation(
            self._flag_value(db, "kill_generation", "0")
        )
        next_generation = generation + 1
        timestamp = utc_now()
        self._upsert_flag(db, "kill_active", "true", timestamp)
        self._upsert_flag(db, "kill_generation", str(next_generation), timestamp)
        if credential_fingerprint and account_fingerprint:
            self._upsert_flag(
                db, "kill_credential_fingerprint", credential_fingerprint, timestamp
            )
            self._upsert_flag(db, "kill_account_fingerprint", account_fingerprint, timestamp)
        return next_generation

    def _initialize(self) -> None:
        with self._connection() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    utc_time TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    correlation_id TEXT,
                    payload_json TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    event_hash TEXT NOT NULL UNIQUE
                );
                CREATE TABLE IF NOT EXISTS flags (
                    name TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS intents (
                    intent_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    decision_json TEXT NOT NULL,
                    digest TEXT NOT NULL,
                    cl_ord_id TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL,
                    commit_key TEXT UNIQUE,
                    okx_ord_id TEXT,
                    credential_fingerprint TEXT,
                    account_fingerprint TEXT,
                    authorization_kind TEXT NOT NULL DEFAULT 'manual',
                    supervisor_decision_id TEXT,
                    supervisor_purpose TEXT
                );
                """
            )
            columns = {
                str(row["name"])
                for row in db.execute("PRAGMA table_info(intents)").fetchall()
            }
            if "credential_fingerprint" not in columns:
                db.execute("ALTER TABLE intents ADD COLUMN credential_fingerprint TEXT")
            if "account_fingerprint" not in columns:
                db.execute("ALTER TABLE intents ADD COLUMN account_fingerprint TEXT")
            if "authorization_kind" not in columns:
                db.execute(
                    "ALTER TABLE intents ADD COLUMN authorization_kind TEXT NOT NULL DEFAULT 'manual'"
                )
            if "supervisor_decision_id" not in columns:
                db.execute("ALTER TABLE intents ADD COLUMN supervisor_decision_id TEXT")
            if "supervisor_purpose" not in columns:
                db.execute("ALTER TABLE intents ADD COLUMN supervisor_purpose TEXT")
            timestamp = utc_now()
            db.execute(
                "INSERT OR IGNORE INTO flags (name, value, updated_at) VALUES (?, ?, ?)",
                ("kill_active", "false", timestamp),
            )
            db.execute(
                "INSERT OR IGNORE INTO flags (name, value, updated_at) VALUES (?, ?, ?)",
                ("kill_generation", "0", timestamp),
            )

    def append(
        self,
        event_type: str,
        payload: dict[str, Any] | None = None,
        *,
        actor: str = "system",
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        timestamp = utc_now()
        sanitized = _sanitize(payload or {})
        payload_json = json.dumps(sanitized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with self._lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT event_hash FROM audit_events ORDER BY id DESC LIMIT 1").fetchone()
            previous_hash = row["event_hash"] if row else "0" * 64
            material = "|".join(
                [previous_hash, timestamp, actor, event_type, correlation_id or "", payload_json]
            )
            event_hash = hashlib.sha256(material.encode("utf-8")).hexdigest()
            cursor = db.execute(
                """
                INSERT INTO audit_events
                (utc_time, actor, event_type, correlation_id, payload_json, previous_hash, event_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (timestamp, actor, event_type, correlation_id, payload_json, previous_hash, event_hash),
            )
            return {
                "id": cursor.lastrowid,
                "utcTime": timestamp,
                "actor": actor,
                "eventType": event_type,
                "correlationId": correlation_id,
                "payload": sanitized,
                "eventHash": event_hash,
            }

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        safe_limit = max(1, min(limit, 200))
        with self._connection() as db:
            rows = db.execute(
                "SELECT * FROM audit_events ORDER BY id DESC LIMIT ?", (safe_limit,)
            ).fetchall()
        return [
            {
                "id": row["id"],
                "utcTime": row["utc_time"],
                "actor": row["actor"],
                "eventType": row["event_type"],
                "correlationId": row["correlation_id"],
                "payload": json.loads(row["payload_json"]),
                "eventHash": row["event_hash"],
            }
            for row in rows
        ]

    def verify_chain(self) -> bool:
        with self._connection() as db:
            rows = db.execute("SELECT * FROM audit_events ORDER BY id ASC").fetchall()
        previous_hash = "0" * 64
        for row in rows:
            if row["previous_hash"] != previous_hash:
                return False
            material = "|".join(
                [
                    previous_hash,
                    row["utc_time"],
                    row["actor"],
                    row["event_type"],
                    row["correlation_id"] or "",
                    row["payload_json"],
                ]
            )
            expected = hashlib.sha256(material.encode("utf-8")).hexdigest()
            if expected != row["event_hash"]:
                return False
            previous_hash = expected
        return True

    def get_flag(self, name: str, default: str = "false") -> str:
        with self._connection() as db:
            row = db.execute("SELECT value FROM flags WHERE name = ?", (name,)).fetchone()
        value = str(row["value"]) if row else default
        if name == "kill_active":
            self._parse_kill_active(value)
        elif name == "kill_generation":
            self._parse_kill_generation(value)
        return value

    def set_flag(self, name: str, value: str) -> None:
        with self._connection() as db:
            db.execute(
                """
                INSERT INTO flags (name, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                """,
                (name, value, utc_now()),
            )

    def get_kill_generation(self) -> int:
        with self._connection() as db:
            return self._parse_kill_generation(
                self._flag_value(db, "kill_generation", "0")
            )

    def get_kill_state(self) -> tuple[bool, int]:
        with self._connection() as db:
            active = self._parse_kill_active(
                self._flag_value(db, "kill_active", "false")
            )
            generation = self._parse_kill_generation(
                self._flag_value(db, "kill_generation", "0")
            )
        return active, generation

    def engage_kill_latch(
        self,
        credential_fingerprint: str | None = None,
        account_fingerprint: str | None = None,
    ) -> int:
        with self._lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            return self._engage_kill_in_transaction(
                db, credential_fingerprint, account_fingerprint
            )

    def get_kill_identity(self) -> tuple[str, str] | None:
        with self._connection() as db:
            credential = self._flag_value(db, "kill_credential_fingerprint", "")
            account = self._flag_value(db, "kill_account_fingerprint", "")
        if not credential or not account:
            return None
        return credential, account

    def save_intent(self, record: dict[str, str]) -> None:
        with self._connection() as db:
            db.execute(
                """
                INSERT INTO intents
                (intent_id, created_at, expires_at, payload_json, decision_json, digest,
                 cl_ord_id, status, credential_fingerprint, account_fingerprint,
                 authorization_kind, supervisor_decision_id, supervisor_purpose)
                VALUES (:intent_id, :created_at, :expires_at, :payload_json, :decision_json,
                        :digest, :cl_ord_id, :status, :credential_fingerprint,
                        :account_fingerprint, :authorization_kind,
                        :supervisor_decision_id, :supervisor_purpose)
                """,
                {
                    **record,
                    "credential_fingerprint": record.get("credential_fingerprint"),
                    "account_fingerprint": record.get("account_fingerprint"),
                    "authorization_kind": record.get("authorization_kind", "manual"),
                    "supervisor_decision_id": record.get("supervisor_decision_id"),
                    "supervisor_purpose": record.get("supervisor_purpose"),
                },
            )

    def get_intent(self, intent_id: str) -> dict[str, Any] | None:
        with self._connection() as db:
            row = db.execute("SELECT * FROM intents WHERE intent_id = ?", (intent_id,)).fetchone()
        return dict(row) if row else None

    def claim_intent(self, intent_id: str, commit_key: str) -> bool:
        """Atomically claim one preview while enforcing a single potential-order identity."""
        with self._lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            target = db.execute(
                """
                SELECT credential_fingerprint, account_fingerprint
                FROM intents WHERE intent_id = ?
                """,
                (intent_id,),
            ).fetchone()
            if (
                target is None
                or not target["credential_fingerprint"]
                or not target["account_fingerprint"]
            ):
                raise IntentIdentityConflict("订单预检缺少账户身份绑定")
            expected = (
                target["credential_fingerprint"],
                target["account_fingerprint"],
            )
            bindings = db.execute(
                """
                SELECT DISTINCT credential_fingerprint, account_fingerprint
                FROM intents
                WHERE status IN (
                    'dispatching', 'uncertain', 'manual_review', 'accepted', 'reconciled'
                )
                """
            ).fetchall()
            if any(
                not row["credential_fingerprint"]
                or not row["account_fingerprint"]
                or (
                    row["credential_fingerprint"], row["account_fingerprint"]
                ) != expected
                for row in bindings
            ):
                raise IntentIdentityConflict("存在其他模拟账户身份的潜在订单")
            cursor = db.execute(
                """
                UPDATE intents
                SET status = 'dispatching', commit_key = ?
                WHERE intent_id = ? AND status = 'previewed' AND commit_key IS NULL
                """,
                (commit_key, intent_id),
            )
            return cursor.rowcount == 1

    def recover_unresolved_intents(self) -> list[dict[str, Any]]:
        """Fail closed after a crash; unresolved dispatches require manual review."""
        with self._lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                """
                SELECT intent_id, cl_ord_id, status,
                       credential_fingerprint, account_fingerprint
                FROM intents
                WHERE status IN ('dispatching', 'uncertain', 'manual_review')
                """
            ).fetchall()
            if rows:
                identities = {
                    (row["credential_fingerprint"], row["account_fingerprint"])
                    for row in rows
                    if row["credential_fingerprint"] and row["account_fingerprint"]
                }
                binding = next(iter(identities)) if len(identities) == 1 else (None, None)
                self._engage_kill_in_transaction(db, binding[0], binding[1])
                db.execute(
                    "UPDATE intents SET status = 'manual_review' WHERE status IN ('dispatching', 'uncertain')"
                )
            return [
                {
                    "intent_id": row["intent_id"],
                    "cl_ord_id": row["cl_ord_id"],
                    "status": row["status"],
                }
                for row in rows
            ]

    def mark_manual_review_and_kill(self, intent_id: str) -> None:
        """Atomically persist the order hold and kill latch."""
        with self._lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            identity = db.execute(
                """
                SELECT credential_fingerprint, account_fingerprint
                FROM intents WHERE intent_id = ?
                """,
                (intent_id,),
            ).fetchone()
            db.execute(
                "UPDATE intents SET status = 'manual_review' WHERE intent_id = ?",
                (intent_id,),
            )
            self._engage_kill_in_transaction(
                db,
                identity["credential_fingerprint"] if identity else None,
                identity["account_fingerprint"] if identity else None,
            )

    def close_manual_reviews_and_clear_kill(self, expected_generation: int) -> bool:
        """Atomically close reviewed intents when the user explicitly resets kill."""
        with self._lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            current_generation = self._parse_kill_generation(
                self._flag_value(db, "kill_generation", "0")
            )
            if current_generation != expected_generation:
                return False
            db.execute("UPDATE intents SET status = 'review_closed' WHERE status = 'manual_review'")
            timestamp = utc_now()
            self._upsert_flag(db, "kill_active", "false", timestamp)
            self._upsert_flag(db, "kill_credential_fingerprint", "", timestamp)
            self._upsert_flag(db, "kill_account_fingerprint", "", timestamp)
            return True

    def has_manual_reviews(self) -> bool:
        with self._connection() as db:
            row = db.execute(
                "SELECT 1 FROM intents WHERE status = 'manual_review' LIMIT 1"
            ).fetchone()
        return row is not None

    def manual_review_intents(self) -> list[dict[str, Any]]:
        with self._connection() as db:
            rows = db.execute(
                """
                SELECT intent_id, cl_ord_id, credential_fingerprint, account_fingerprint
                FROM intents
                WHERE status = 'manual_review'
                ORDER BY created_at ASC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def has_dispatched_intents(self) -> bool:
        with self._connection() as db:
            row = db.execute(
                "SELECT 1 FROM intents WHERE commit_key IS NOT NULL LIMIT 1"
            ).fetchone()
        return row is not None

    def dispatched_identity_bindings(self) -> list[tuple[str | None, str | None]]:
        """Return identities attached to every intent that crossed the dispatch claim."""
        with self._connection() as db:
            rows = db.execute(
                """
                SELECT DISTINCT credential_fingerprint, account_fingerprint
                FROM intents
                WHERE commit_key IS NOT NULL
                """
            ).fetchall()
        return [
            (row["credential_fingerprint"], row["account_fingerprint"])
            for row in rows
        ]

    def potential_order_identity_bindings(self) -> list[tuple[str | None, str | None]]:
        """Return identities for orders that may still exist at the exchange."""
        with self._connection() as db:
            rows = db.execute(
                """
                SELECT DISTINCT credential_fingerprint, account_fingerprint
                FROM intents
                WHERE status IN (
                    'dispatching', 'uncertain', 'manual_review', 'accepted', 'reconciled'
                )
                """
            ).fetchall()
        return [
            (row["credential_fingerprint"], row["account_fingerprint"])
            for row in rows
        ]

    def potential_order_intents(self) -> list[dict[str, Any]]:
        with self._connection() as db:
            rows = db.execute(
                """
                SELECT intent_id, cl_ord_id, status,
                       credential_fingerprint, account_fingerprint, okx_ord_id
                FROM intents
                WHERE status IN (
                    'dispatching', 'uncertain', 'manual_review', 'accepted', 'reconciled'
                )
                ORDER BY created_at ASC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def revoke_uncommitted_previews_for_environment_switch(self) -> int:
        """Invalidate requests proven not to have crossed the dispatch claim."""

        with self._lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            cursor = db.execute(
                """
                UPDATE intents SET status = 'environment_revoked'
                WHERE status = 'previewed' AND commit_key IS NULL
                """
            )
            return int(cursor.rowcount)

    def environment_switch_blocking_intents(self) -> list[dict[str, Any]]:
        placeholders = ",".join("?" for _ in ENVIRONMENT_SWITCH_BLOCKING_INTENT_STATES)
        with self._connection() as db:
            rows = db.execute(
                f"""
                SELECT intent_id, status, cl_ord_id, credential_fingerprint,
                       account_fingerprint, okx_ord_id
                FROM intents
                WHERE status IN ({placeholders})
                ORDER BY created_at ASC
                """,
                tuple(sorted(ENVIRONMENT_SWITCH_BLOCKING_INTENT_STATES)),
            ).fetchall()
        return [dict(row) for row in rows]

    def update_intent(self, intent_id: str, **fields: str | None) -> None:
        allowed = {"status", "commit_key", "okx_ord_id"}
        if not fields or not set(fields).issubset(allowed):
            raise ValueError("Invalid intent update")
        assignments = ", ".join(f"{key} = ?" for key in fields)
        values = list(fields.values()) + [intent_id]
        with self._connection() as db:
            db.execute(f"UPDATE intents SET {assignments} WHERE intent_id = ?", values)
