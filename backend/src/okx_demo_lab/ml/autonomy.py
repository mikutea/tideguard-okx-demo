from __future__ import annotations

import hashlib
import hmac
import json
import math
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterator, Literal

from .strategy import canonical_json, sha256_hex


AUTONOMY_ENABLE_CONFIRMATION = "ENABLE LONG-RUN OKX DEMO"
LEGACY_AUTONOMY_SCHEMA_VERSION = "tideguard.autonomy.v1"
AUTONOMY_SCHEMA_VERSION = "tideguard.autonomy.v2"
SUPERVISOR_ACTOR = "codex-supervisor"
ACTIVE_POSITION_STATES = frozenset(
    {"entry_submitted", "long", "exit_submitted", "manual_review"}
)
TERMINAL_POSITION_STATES = frozenset({"entry_unfilled", "closed", "closed_dust"})


class AutonomyError(RuntimeError):
    pass


class SupervisorDenied(AutonomyError):
    pass


class PositionStateError(AutonomyError):
    pass


def _utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise AutonomyError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return _utc(value, "timestamp").isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _parse_iso(value: Any, name: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise AutonomyError(f"{name} is missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AutonomyError(f"{name} is invalid") from exc
    return _utc(parsed, name)


def _decimal(value: Any, name: str, *, allow_zero: bool = True) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise AutonomyError(f"{name} is invalid") from exc
    if not parsed.is_finite() or parsed < 0 or (not allow_zero and parsed == 0):
        raise AutonomyError(f"{name} is invalid")
    return parsed


def _signed_decimal(value: Any, name: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise AutonomyError(f"{name} is invalid") from exc
    if not parsed.is_finite():
        raise AutonomyError(f"{name} is invalid")
    return parsed


def _require_sha256(value: str, name: str) -> str:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise AutonomyError(f"{name} must be lowercase sha256")
    return value


@dataclass(frozen=True)
class AutonomyPolicy:
    instrument: str = "BTC-USDT"
    timeframe: str = "5m"
    fixed_notional_usdt: Decimal = Decimal("10")
    max_daily_entries: int = 3
    hold_bars: int = 12
    stop_loss_fraction: Decimal = Decimal("0.015")
    take_profit_fraction: Decimal = Decimal("0.025")
    max_holding_bars: int = 24
    ioc_slippage_fraction: Decimal = Decimal("0.002")
    round_trip_cost_bps: Decimal = Decimal("24")
    max_exit_attempts: int = 5
    train_interval_hours: int = 24
    training_retry_hours: int = 1
    supervisor_lease_hours: int = 24
    shadow_min_settled: int = 20
    shadow_min_days: int = 7
    max_demo_drawdown: Decimal = Decimal("0.03")
    min_challenger_oos_improvement: Decimal = Decimal("0.002")
    max_challenger_drawdown_regression: Decimal = Decimal("0.01")

    def __post_init__(self) -> None:
        if self.instrument != "BTC-USDT" or self.timeframe != "5m":
            raise ValueError("autonomy is fixed to BTC-USDT 5m")
        if not Decimal("0") < self.fixed_notional_usdt <= Decimal("25"):
            raise ValueError("fixed notional is outside the hard limit")
        if not 1 <= self.max_daily_entries <= 12:
            raise ValueError("daily entry limit is invalid")
        if not 1 <= self.hold_bars <= self.max_holding_bars <= 96:
            raise ValueError("holding windows are invalid")
        for name, value, maximum in (
            ("stop_loss_fraction", self.stop_loss_fraction, Decimal("0.05")),
            ("take_profit_fraction", self.take_profit_fraction, Decimal("0.10")),
            ("ioc_slippage_fraction", self.ioc_slippage_fraction, Decimal("0.01")),
            ("max_demo_drawdown", self.max_demo_drawdown, Decimal("0.10")),
            (
                "min_challenger_oos_improvement",
                self.min_challenger_oos_improvement,
                Decimal("0.05"),
            ),
            (
                "max_challenger_drawdown_regression",
                self.max_challenger_drawdown_regression,
                Decimal("0.05"),
            ),
        ):
            if not Decimal("0") < value <= maximum:
                raise ValueError(f"{name} is outside the hard limit")
        if not Decimal("5") <= self.round_trip_cost_bps <= Decimal("100"):
            raise ValueError("round-trip cost assumption is outside the hard limit")
        if not 1 <= self.max_exit_attempts <= 10:
            raise ValueError("exit attempt limit is invalid")
        if not 1 <= self.train_interval_hours <= 168:
            raise ValueError("training interval is invalid")
        if not 1 <= self.training_retry_hours <= self.train_interval_hours:
            raise ValueError("training retry interval is invalid")
        if not 1 <= self.supervisor_lease_hours <= 24:
            raise ValueError("supervisor lease exceeds 24 hours")
        if self.shadow_min_settled < 1 or self.shadow_min_days < 1:
            raise ValueError("shadow requirements are invalid")

    def to_dict(self) -> dict[str, Any]:
        return {
            "fixed_notional_usdt": str(self.fixed_notional_usdt),
            "hold_bars": self.hold_bars,
            "instrument": self.instrument,
            "ioc_slippage_fraction": str(self.ioc_slippage_fraction),
            "round_trip_cost_bps": str(self.round_trip_cost_bps),
            "max_daily_entries": self.max_daily_entries,
            "max_demo_drawdown": str(self.max_demo_drawdown),
            "max_exit_attempts": self.max_exit_attempts,
            "max_holding_bars": self.max_holding_bars,
            "max_challenger_drawdown_regression": str(
                self.max_challenger_drawdown_regression
            ),
            "min_challenger_oos_improvement": str(
                self.min_challenger_oos_improvement
            ),
            "shadow_min_days": self.shadow_min_days,
            "shadow_min_settled": self.shadow_min_settled,
            "stop_loss_fraction": str(self.stop_loss_fraction),
            "supervisor_lease_hours": self.supervisor_lease_hours,
            "take_profit_fraction": str(self.take_profit_fraction),
            "timeframe": self.timeframe,
            "train_interval_hours": self.train_interval_hours,
            "training_retry_hours": self.training_retry_hours,
        }

    @property
    def policy_sha256(self) -> str:
        return sha256_hex(canonical_json(self.to_dict()))


@dataclass(frozen=True)
class SupervisorDecision:
    kind: Literal["promote", "lease", "reject", "rollback", "suspend"]
    subject_model_id: str | None
    artifact_sha256: str | None
    expected_generation: int
    policy_sha256: str
    evidence_sha256: str
    rationale: str
    issued_at: datetime
    expires_at: datetime
    actor: str = SUPERVISOR_ACTOR

    def __post_init__(self) -> None:
        issued = _utc(self.issued_at, "issued_at")
        expires = _utc(self.expires_at, "expires_at")
        if expires <= issued or expires - issued > timedelta(hours=24):
            raise SupervisorDenied("supervisor decision lifetime must be within 24 hours")
        if self.actor != SUPERVISOR_ACTOR:
            raise SupervisorDenied("only the local Codex supervisor actor is accepted")
        if self.expected_generation < 0:
            raise SupervisorDenied("supervisor generation is invalid")
        _require_sha256(self.policy_sha256, "policy_sha256")
        _require_sha256(self.evidence_sha256, "evidence_sha256")
        if self.kind in {"promote", "lease", "rollback", "reject"}:
            if not self.subject_model_id or not self.subject_model_id.startswith("mdl_"):
                raise SupervisorDenied("decision requires a model identity")
            if not self.artifact_sha256:
                raise SupervisorDenied("decision requires an artifact hash")
            _require_sha256(self.artifact_sha256, "artifact_sha256")
        elif self.subject_model_id is not None or self.artifact_sha256 is not None:
            raise SupervisorDenied("suspend decisions cannot bind an executable model")
        if not 16 <= len(self.rationale.strip()) <= 2_000:
            raise SupervisorDenied("supervisor rationale must contain 16-2000 characters")

    def to_dict(self) -> dict[str, Any]:
        return {
            "actor": self.actor,
            "artifact_sha256": self.artifact_sha256,
            "evidence_sha256": self.evidence_sha256,
            "expected_generation": self.expected_generation,
            "expires_at": _iso(self.expires_at),
            "issued_at": _iso(self.issued_at),
            "kind": self.kind,
            "policy_sha256": self.policy_sha256,
            "rationale": self.rationale.strip(),
            "subject_model_id": self.subject_model_id,
        }

    @property
    def decision_id(self) -> str:
        return f"sup_{sha256_hex(canonical_json(self.to_dict()))[:28]}"


class AutonomyStore:
    """Persistent supervisor, shadow, and model-owned position state."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
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
        now = _iso(datetime.now(timezone.utc))
        with self._connection() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS autonomy_state (
                    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                    schema_version TEXT NOT NULL,
                    desired_mode TEXT NOT NULL CHECK(desired_mode IN ('disabled', 'shadow', 'demo')),
                    runtime_status TEXT NOT NULL CHECK(runtime_status IN (
                        'disabled', 'shadow', 'waiting_supervisor', 'waiting_champion',
                        'running', 'exit_only', 'suspended', 'manual_review'
                    )),
                    credential_fingerprint TEXT,
                    account_fingerprint TEXT,
                    suspended_reason TEXT,
                    state_version INTEGER NOT NULL CHECK(state_version >= 0),
                    enabled_at TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS processed_candles (
                    candle_closed_at TEXT PRIMARY KEY,
                    processed_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS training_runs (
                    run_id TEXT PRIMARY KEY,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    status TEXT NOT NULL CHECK(status IN ('running', 'completed', 'failed')),
                    model_id TEXT,
                    error_type TEXT,
                    result_json TEXT,
                    phase TEXT NOT NULL DEFAULT 'syncing',
                    progress_current INTEGER NOT NULL DEFAULT 0,
                    progress_total INTEGER,
                    snapshot_id TEXT,
                    data_rows INTEGER
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_running_training
                ON training_runs((1)) WHERE status = 'running';
                CREATE TABLE IF NOT EXISTS supervisor_decisions (
                    decision_id TEXT PRIMARY KEY,
                    decision_json TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    subject_model_id TEXT,
                    artifact_sha256 TEXT,
                    expected_generation INTEGER NOT NULL,
                    policy_sha256 TEXT NOT NULL,
                    evidence_sha256 TEXT NOT NULL,
                    issued_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    applied_at TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS shadow_signals (
                    signal_id TEXT PRIMARY KEY,
                    model_id TEXT NOT NULL,
                    artifact_sha256 TEXT NOT NULL,
                    candle_closed_at TEXT NOT NULL,
                    due_at TEXT NOT NULL,
                    action TEXT NOT NULL CHECK(action IN ('buy', 'sell', 'hold')),
                    score REAL NOT NULL,
                    entry_close TEXT NOT NULL,
                    exit_close TEXT,
                    net_return REAL,
                    settled_at TEXT,
                    UNIQUE(model_id, candle_closed_at)
                );
                CREATE TABLE IF NOT EXISTS positions (
                    position_id TEXT PRIMARY KEY,
                    model_id TEXT NOT NULL,
                    artifact_sha256 TEXT NOT NULL,
                    champion_generation INTEGER NOT NULL,
                    policy_sha256 TEXT NOT NULL,
                    credential_fingerprint TEXT NOT NULL,
                    account_fingerprint TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN (
                        'entry_submitted', 'entry_unfilled', 'long',
                        'exit_submitted', 'closed', 'closed_dust', 'manual_review'
                    )),
                    entry_signal_id TEXT NOT NULL UNIQUE,
                    entry_intent_id TEXT,
                    entry_ord_id TEXT,
                    entry_cl_ord_id TEXT,
                    supervisor_decision_id TEXT NOT NULL,
                    requested_size TEXT NOT NULL,
                    filled_size TEXT NOT NULL DEFAULT '0',
                    remaining_size TEXT NOT NULL DEFAULT '0',
                    entry_avg_price TEXT,
                    entry_fee TEXT NOT NULL DEFAULT '0',
                    entry_fee_currency TEXT,
                    entry_candle_at TEXT NOT NULL,
                    exit_due_at TEXT NOT NULL,
                    hard_exit_at TEXT NOT NULL,
                    stop_price TEXT,
                    take_profit_price TEXT,
                    exit_attempts INTEGER NOT NULL DEFAULT 0 CHECK(exit_attempts >= 0),
                    exit_intent_id TEXT,
                    exit_ord_id TEXT,
                    exit_cl_ord_id TEXT,
                    exit_avg_price TEXT,
                    exited_size TEXT NOT NULL DEFAULT '0',
                    exit_quote_value TEXT NOT NULL DEFAULT '0',
                    exit_fee TEXT NOT NULL DEFAULT '0',
                    exit_fee_currency TEXT,
                    realized_return REAL,
                    failure_reason TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    closed_at TEXT,
                    position_hash TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_model_position
                ON positions((1))
                WHERE status IN ('entry_submitted', 'long', 'exit_submitted', 'manual_review');
                CREATE TABLE IF NOT EXISTS daily_entry_counters (
                    utc_date TEXT PRIMARY KEY,
                    entries INTEGER NOT NULL CHECK(entries >= 0),
                    updated_at TEXT NOT NULL
                );
                """
            )
            db.execute(
                """
                INSERT OR IGNORE INTO autonomy_state
                (singleton, schema_version, desired_mode, runtime_status,
                 credential_fingerprint, account_fingerprint, suspended_reason,
                 state_version, enabled_at, updated_at)
                VALUES (1, ?, 'disabled', 'disabled', NULL, NULL, NULL, 0, NULL, ?)
                """,
                (AUTONOMY_SCHEMA_VERSION, now),
            )
            position_columns = {
                str(row["name"])
                for row in db.execute("PRAGMA table_info(positions)").fetchall()
            }
            if "position_hash" not in position_columns:
                db.execute("ALTER TABLE positions ADD COLUMN position_hash TEXT")
            training_columns = {
                str(row["name"])
                for row in db.execute("PRAGMA table_info(training_runs)").fetchall()
            }
            for name, declaration in (
                ("phase", "TEXT NOT NULL DEFAULT 'syncing'"),
                ("progress_current", "INTEGER NOT NULL DEFAULT 0"),
                ("progress_total", "INTEGER"),
                ("snapshot_id", "TEXT"),
                ("data_rows", "INTEGER"),
            ):
                if name not in training_columns:
                    db.execute(
                        f"ALTER TABLE training_runs ADD COLUMN {name} {declaration}"
                    )
            db.execute(
                """
                UPDATE training_runs
                SET phase = CASE status
                    WHEN 'completed' THEN 'completed'
                    WHEN 'failed' THEN 'failed'
                    ELSE phase
                END
                WHERE status IN ('completed', 'failed') AND phase = 'syncing'
                """
            )
            state_schema = db.execute(
                "SELECT schema_version FROM autonomy_state WHERE singleton = 1"
            ).fetchone()
            if state_schema is None:
                raise AutonomyError("autonomy state is missing")
            if state_schema["schema_version"] == LEGACY_AUTONOMY_SCHEMA_VERSION:
                db.execute(
                    "UPDATE autonomy_state SET schema_version = ?, updated_at = ? WHERE singleton = 1",
                    (AUTONOMY_SCHEMA_VERSION, now),
                )
            elif state_schema["schema_version"] != AUTONOMY_SCHEMA_VERSION:
                raise AutonomyError("autonomy state schema is unsupported")

    @staticmethod
    def _position_digest(row: sqlite3.Row) -> str:
        material = {
            key: row[key]
            for key in row.keys()
            if key != "position_hash"
        }
        return sha256_hex(canonical_json(material))

    @classmethod
    def _seal_position(cls, db: sqlite3.Connection, position_id: str) -> None:
        row = db.execute(
            "SELECT * FROM positions WHERE position_id = ?", (position_id,)
        ).fetchone()
        if row is None:
            raise PositionStateError("position cannot be sealed because it is missing")
        db.execute(
            "UPDATE positions SET position_hash = ? WHERE position_id = ?",
            (cls._position_digest(row), position_id),
        )

    @classmethod
    def _assert_position_integrity(
        cls, db: sqlite3.Connection, position_id: str
    ) -> sqlite3.Row:
        row = db.execute(
            "SELECT * FROM positions WHERE position_id = ?", (position_id,)
        ).fetchone()
        if row is None:
            raise PositionStateError("position is missing")
        stored = str(row["position_hash"] or "")
        expected = cls._position_digest(row)
        if len(stored) != 64 or not hmac.compare_digest(stored, expected):
            raise PositionStateError("position integrity hash mismatch")
        return row

    def state(self) -> dict[str, Any]:
        with self._connection() as db:
            row = db.execute(
                "SELECT * FROM autonomy_state WHERE singleton = 1"
            ).fetchone()
        if row is None or row["schema_version"] != AUTONOMY_SCHEMA_VERSION:
            raise AutonomyError("autonomy state schema is missing or unsupported")
        credential = str(row["credential_fingerprint"] or "")
        account = str(row["account_fingerprint"] or "")
        if bool(credential) != bool(account):
            raise AutonomyError("autonomy account binding is incomplete")
        return {
            "desiredMode": row["desired_mode"],
            "runtimeStatus": row["runtime_status"],
            "identityBound": bool(credential and account),
            "suspendedReason": row["suspended_reason"],
            "stateVersion": int(row["state_version"]),
            "enabledAt": row["enabled_at"],
            "updatedAt": row["updated_at"],
        }

    def bound_identity(self) -> tuple[str, str] | None:
        with self._connection() as db:
            row = db.execute(
                """
                SELECT credential_fingerprint, account_fingerprint
                FROM autonomy_state WHERE singleton = 1
                """
            ).fetchone()
        if row is None:
            raise AutonomyError("autonomy state is missing")
        credential = str(row["credential_fingerprint"] or "")
        account = str(row["account_fingerprint"] or "")
        if not credential and not account:
            return None
        if not credential or not account:
            raise AutonomyError("autonomy account binding is incomplete")
        return credential, account

    def enable_master(
        self,
        *,
        mode: Literal["shadow", "demo"],
        credential_fingerprint: str,
        account_fingerprint: str,
        confirmation: str,
        now: datetime,
    ) -> dict[str, Any]:
        if confirmation != AUTONOMY_ENABLE_CONFIRMATION:
            raise AutonomyError("long-run Demo confirmation does not match")
        if mode not in {"shadow", "demo"}:
            raise AutonomyError("autonomy mode is invalid")
        _require_sha256(credential_fingerprint, "credential_fingerprint")
        _require_sha256(account_fingerprint, "account_fingerprint")
        timestamp = _iso(now)
        initial_status = "shadow" if mode == "shadow" else "waiting_supervisor"
        with self._lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            active = db.execute(
                "SELECT 1 FROM positions WHERE status IN ('entry_submitted','long','exit_submitted','manual_review') LIMIT 1"
            ).fetchone()
            if active:
                raise AutonomyError("cannot change master mode while a model position is active")
            db.execute(
                """
                UPDATE autonomy_state
                SET desired_mode = ?, runtime_status = ?, credential_fingerprint = ?,
                    account_fingerprint = ?, suspended_reason = NULL,
                    state_version = state_version + 1, enabled_at = ?, updated_at = ?
                WHERE singleton = 1
                """,
                (
                    mode,
                    initial_status,
                    credential_fingerprint,
                    account_fingerprint,
                    timestamp,
                    timestamp,
                ),
            )
        return self.state()

    def disable_master(self, reason: str, *, now: datetime) -> dict[str, Any]:
        bounded_reason = reason.strip()[:512] or "disabled"
        timestamp = _iso(now)
        with self._lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                """
                UPDATE autonomy_state
                SET desired_mode = 'disabled', runtime_status = 'disabled',
                    suspended_reason = ?, state_version = state_version + 1,
                    updated_at = ? WHERE singleton = 1
                """,
                (bounded_reason, timestamp),
            )
        return self.state()

    def set_runtime_status(
        self,
        status: Literal[
            "disabled",
            "shadow",
            "waiting_supervisor",
            "waiting_champion",
            "running",
            "exit_only",
            "suspended",
            "manual_review",
        ],
        *,
        reason: str | None,
        now: datetime,
    ) -> None:
        timestamp = _iso(now)
        bounded_reason = reason.strip()[:512] if reason else None
        with self._lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                """
                UPDATE autonomy_state
                SET runtime_status = ?, suspended_reason = ?,
                    state_version = state_version + 1, updated_at = ?
                WHERE singleton = 1
                  AND (runtime_status <> ? OR suspended_reason IS NOT ?)
                """,
                (status, bounded_reason, timestamp, status, bounded_reason),
            )

    def claim_candle(self, candle_closed_at: datetime, *, now: datetime) -> bool:
        with self._lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute(
                    "INSERT INTO processed_candles (candle_closed_at, processed_at) VALUES (?, ?)",
                    (_iso(candle_closed_at), _iso(now)),
                )
            except sqlite3.IntegrityError:
                return False
        return True

    def start_training(self, *, now: datetime) -> str:
        timestamp = _iso(now)
        material = canonical_json({"started_at": timestamp, "schema": AUTONOMY_SCHEMA_VERSION})
        run_id = f"train_{sha256_hex(material)[:24]}"
        with self._lock, self._connection() as db:
            db.execute(
                """
                INSERT INTO training_runs
                (run_id, started_at, completed_at, status, model_id, error_type,
                 result_json, phase, progress_current, progress_total,
                 snapshot_id, data_rows)
                VALUES (?, ?, NULL, 'running', NULL, NULL, NULL,
                        'syncing', 0, NULL, NULL, NULL)
                """,
                (run_id, timestamp),
            )
        return run_id

    def update_training_progress(
        self,
        run_id: str,
        *,
        phase: str,
        current: int,
        total: int | None = None,
        snapshot_id: str | None = None,
        data_rows: int | None = None,
    ) -> None:
        allowed_phases = {
            "syncing",
            "snapshotting",
            "feature_build",
            "walk_forward",
            "final_fit",
            "registering",
        }
        if phase not in allowed_phases:
            raise AutonomyError("training phase is invalid")
        if isinstance(current, bool) or current < 0:
            raise AutonomyError("training progress is invalid")
        if total is not None and (
            isinstance(total, bool) or total < 1 or current > total
        ):
            raise AutonomyError("training progress total is invalid")
        if data_rows is not None and (
            isinstance(data_rows, bool) or data_rows < 1
        ):
            raise AutonomyError("training data row count is invalid")
        with self._lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            updated = db.execute(
                """
                UPDATE training_runs
                SET phase = ?, progress_current = ?, progress_total = ?,
                    snapshot_id = COALESCE(?, snapshot_id),
                    data_rows = COALESCE(?, data_rows)
                WHERE run_id = ? AND status = 'running'
                """,
                (phase, current, total, snapshot_id, data_rows, run_id),
            )
            if updated.rowcount != 1:
                raise AutonomyError("training run is not active")

    def finish_training(
        self,
        run_id: str,
        *,
        model_id: str | None,
        result: dict[str, Any] | None,
        error_type: str | None,
        now: datetime,
    ) -> None:
        status = "completed" if model_id and result and not error_type else "failed"
        payload = canonical_json(result) if result is not None else None
        with self._lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            updated = db.execute(
                """
                UPDATE training_runs
                SET completed_at = ?, status = ?, model_id = ?, error_type = ?,
                    result_json = ?, phase = ?
                WHERE run_id = ? AND status = 'running'
                """,
                (
                    _iso(now),
                    status,
                    model_id,
                    error_type,
                    payload,
                    "completed" if status == "completed" else "failed",
                    run_id,
                ),
            )
            if updated.rowcount != 1:
                raise AutonomyError("training run is not active")

    def latest_training(self) -> dict[str, Any] | None:
        with self._connection() as db:
            row = db.execute(
                "SELECT * FROM training_runs ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return None
        return {
            "runId": row["run_id"],
            "startedAt": row["started_at"],
            "completedAt": row["completed_at"],
            "status": row["status"],
            "modelId": row["model_id"],
            "errorType": row["error_type"],
            "phase": row["phase"],
            "progressCurrent": int(row["progress_current"]),
            "progressTotal": (
                int(row["progress_total"])
                if row["progress_total"] is not None
                else None
            ),
            "snapshotId": row["snapshot_id"],
            "dataRows": (
                int(row["data_rows"]) if row["data_rows"] is not None else None
            ),
            "result": json.loads(row["result_json"]) if row["result_json"] else None,
        }

    def training_due(
        self,
        *,
        now: datetime,
        interval_hours: int,
        retry_hours: int = 1,
    ) -> bool:
        if not 1 <= retry_hours <= interval_hours:
            raise AutonomyError("training retry interval is invalid")
        latest = self.latest_training()
        if latest is None:
            return True
        anchor = latest["completedAt"] or latest["startedAt"]
        effective_hours = retry_hours if latest["status"] != "completed" else interval_hours
        return _parse_iso(anchor, "training timestamp") + timedelta(
            hours=effective_hours
        ) <= _utc(now, "now")

    def recover_running_training(self, *, now: datetime) -> int:
        """Make a crash-interrupted training run visible and allow bounded retry."""

        with self._lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            updated = db.execute(
                """
                UPDATE training_runs
                SET completed_at = ?, status = 'failed', phase = 'failed',
                    error_type = 'ProcessRestart'
                WHERE status = 'running'
                """,
                (_iso(now),),
            )
        return updated.rowcount

    def record_supervisor_decision(
        self, decision: SupervisorDecision, *, now: datetime
    ) -> str:
        body = canonical_json(decision.to_dict())
        with self._lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT decision_json FROM supervisor_decisions WHERE decision_id = ?",
                (decision.decision_id,),
            ).fetchone()
            if existing:
                if not hmac.compare_digest(str(existing["decision_json"]), body):
                    raise SupervisorDenied("supervisor decision ID collision")
                return decision.decision_id
            db.execute(
                """
                INSERT INTO supervisor_decisions
                (decision_id, decision_json, kind, subject_model_id, artifact_sha256,
                 expected_generation, policy_sha256, evidence_sha256, issued_at,
                 expires_at, applied_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
                """,
                (
                    decision.decision_id,
                    body,
                    decision.kind,
                    decision.subject_model_id,
                    decision.artifact_sha256,
                    decision.expected_generation,
                    decision.policy_sha256,
                    decision.evidence_sha256,
                    _iso(decision.issued_at),
                    _iso(decision.expires_at),
                    _iso(now),
                ),
            )
        return decision.decision_id

    def mark_decision_applied(self, decision_id: str, *, now: datetime) -> None:
        with self._lock, self._connection() as db:
            updated = db.execute(
                """
                UPDATE supervisor_decisions SET applied_at = COALESCE(applied_at, ?)
                WHERE decision_id = ?
                """,
                (_iso(now), decision_id),
            )
            if updated.rowcount != 1:
                raise SupervisorDenied("supervisor decision does not exist")

    def active_lease(
        self,
        *,
        model_id: str,
        artifact_sha256: str,
        generation: int,
        policy_sha256: str,
        now: datetime,
    ) -> dict[str, Any] | None:
        timestamp = _iso(now)
        with self._connection() as db:
            rows = db.execute(
                """
                SELECT * FROM supervisor_decisions
                WHERE kind = 'lease' AND subject_model_id = ? AND artifact_sha256 = ?
                  AND expected_generation = ? AND policy_sha256 = ?
                  AND applied_at IS NOT NULL
                  AND issued_at <= ? AND expires_at > ?
                ORDER BY issued_at DESC
                """,
                (
                    model_id,
                    artifact_sha256,
                    generation,
                    policy_sha256,
                    timestamp,
                    timestamp,
                ),
            ).fetchall()
        for row in rows:
            body = str(row["decision_json"])
            try:
                value = json.loads(body)
            except json.JSONDecodeError as exc:
                raise SupervisorDenied("supervisor lease is not valid JSON") from exc
            expected_id = f"sup_{sha256_hex(canonical_json(value))[:28]}"
            if not hmac.compare_digest(expected_id, str(row["decision_id"])):
                raise SupervisorDenied("supervisor lease hash mismatch")
            return {
                "decisionId": row["decision_id"],
                "modelId": row["subject_model_id"],
                "artifactSha256": row["artifact_sha256"],
                "generation": int(row["expected_generation"]),
                "issuedAt": row["issued_at"],
                "expiresAt": row["expires_at"],
                "evidenceSha256": row["evidence_sha256"],
                "appliedAt": row["applied_at"],
            }
        return None

    def applied_champion_decision(
        self,
        *,
        model_id: str,
        artifact_sha256: str,
        generation: int,
    ) -> dict[str, Any] | None:
        """Return the applied Codex decision that created this champion generation.

        Promotion and rollback decisions bind the generation they reviewed.  A
        successful registry transition increments it exactly once, so the
        executable champion must resolve to an applied decision at generation-1.
        This makes a crash between registry promotion and supervisor audit
        fail closed instead of silently authorizing the new model.
        """

        if generation < 1:
            raise SupervisorDenied("champion generation is invalid")
        _require_sha256(artifact_sha256, "artifact_sha256")
        with self._connection() as db:
            rows = db.execute(
                """
                SELECT * FROM supervisor_decisions
                WHERE kind IN ('promote', 'rollback')
                  AND subject_model_id = ? AND artifact_sha256 = ?
                  AND expected_generation = ? AND applied_at IS NOT NULL
                ORDER BY applied_at DESC
                """,
                (model_id, artifact_sha256, generation - 1),
            ).fetchall()
        for row in rows:
            body = str(row["decision_json"])
            try:
                value = json.loads(body)
            except json.JSONDecodeError as exc:
                raise SupervisorDenied("champion supervisor decision is not valid JSON") from exc
            expected_id = f"sup_{sha256_hex(canonical_json(value))[:28]}"
            if not hmac.compare_digest(expected_id, str(row["decision_id"])):
                raise SupervisorDenied("champion supervisor decision hash mismatch")
            return {
                "decisionId": row["decision_id"],
                "kind": row["kind"],
                "modelId": row["subject_model_id"],
                "artifactSha256": row["artifact_sha256"],
                "reviewedGeneration": int(row["expected_generation"]),
                "championGeneration": generation,
                "evidenceSha256": row["evidence_sha256"],
                "appliedAt": row["applied_at"],
            }
        return None

    def recent_decisions(self, limit: int = 50) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 200))
        with self._connection() as db:
            rows = db.execute(
                """
                SELECT decision_id, kind, subject_model_id, artifact_sha256,
                       expected_generation, policy_sha256, evidence_sha256,
                       issued_at, expires_at, applied_at
                FROM supervisor_decisions ORDER BY issued_at DESC LIMIT ?
                """,
                (safe_limit,),
            ).fetchall()
        return [
            {
                "decisionId": row["decision_id"],
                "kind": row["kind"],
                "modelId": row["subject_model_id"],
                "artifactSha256": row["artifact_sha256"],
                "generation": int(row["expected_generation"]),
                "policySha256": row["policy_sha256"],
                "evidenceSha256": row["evidence_sha256"],
                "issuedAt": row["issued_at"],
                "expiresAt": row["expires_at"],
                "appliedAt": row["applied_at"],
            }
            for row in rows
        ]

    def record_shadow_signal(
        self,
        *,
        model_id: str,
        artifact_sha256: str,
        candle_closed_at: datetime,
        due_at: datetime,
        action: Literal["buy", "sell", "hold"],
        score: float,
        entry_close: Decimal,
    ) -> str:
        if action not in {"buy", "sell", "hold"} or not math.isfinite(float(score)):
            raise AutonomyError("shadow signal is invalid")
        _require_sha256(artifact_sha256, "artifact_sha256")
        entry = _decimal(entry_close, "entry_close", allow_zero=False)
        material = canonical_json(
            {
                "artifact_sha256": artifact_sha256,
                "candle_closed_at": _iso(candle_closed_at),
                "model_id": model_id,
            }
        )
        signal_id = f"shadow_{sha256_hex(material)[:24]}"
        with self._lock, self._connection() as db:
            db.execute(
                """
                INSERT OR IGNORE INTO shadow_signals
                (signal_id, model_id, artifact_sha256, candle_closed_at, due_at,
                 action, score, entry_close, exit_close, net_return, settled_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL)
                """,
                (
                    signal_id,
                    model_id,
                    artifact_sha256,
                    _iso(candle_closed_at),
                    _iso(due_at),
                    action,
                    float(score),
                    str(entry),
                ),
            )
        return signal_id

    def unsettled_shadow(self, *, due_at_or_before: datetime) -> list[dict[str, Any]]:
        with self._connection() as db:
            rows = db.execute(
                """
                SELECT * FROM shadow_signals
                WHERE settled_at IS NULL AND due_at <= ? ORDER BY due_at ASC
                """,
                (_iso(due_at_or_before),),
            ).fetchall()
        return [dict(row) for row in rows]

    def model_has_open_shadow_buy(self, model_id: str) -> bool:
        with self._connection() as db:
            row = db.execute(
                """
                SELECT 1 FROM shadow_signals
                WHERE model_id = ? AND action = 'buy' AND settled_at IS NULL
                LIMIT 1
                """,
                (model_id,),
            ).fetchone()
        return row is not None

    def settle_shadow(
        self,
        signal_id: str,
        *,
        exit_close: Decimal,
        round_trip_cost_bps: float,
        now: datetime,
    ) -> None:
        exit_value = _decimal(exit_close, "exit_close", allow_zero=False)
        if not math.isfinite(round_trip_cost_bps) or round_trip_cost_bps <= 0:
            raise AutonomyError("round-trip cost is invalid")
        with self._lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT entry_close, action FROM shadow_signals WHERE signal_id = ?",
                (signal_id,),
            ).fetchone()
            if row is None:
                raise AutonomyError("shadow signal does not exist")
            entry = _decimal(row["entry_close"], "entry_close", allow_zero=False)
            net_return = (
                float(exit_value / entry - Decimal("1")) - round_trip_cost_bps / 10_000.0
                if row["action"] == "buy"
                else 0.0
            )
            updated = db.execute(
                """
                UPDATE shadow_signals
                SET exit_close = ?, net_return = ?, settled_at = ?
                WHERE signal_id = ? AND settled_at IS NULL
                """,
                (str(exit_value), net_return, _iso(now), signal_id),
            )
            if updated.rowcount != 1:
                raise AutonomyError("shadow signal is already settled")

    def shadow_summary(self, model_id: str) -> dict[str, Any]:
        with self._connection() as db:
            rows = db.execute(
                """
                SELECT action, net_return, candle_closed_at, settled_at
                FROM shadow_signals
                WHERE model_id = ? AND settled_at IS NOT NULL
                ORDER BY candle_closed_at ASC
                """,
                (model_id,),
            ).fetchall()
        returns = [float(row["net_return"]) for row in rows if row["action"] == "buy"]
        equity = 1.0
        peak = 1.0
        max_drawdown = 0.0
        for value in returns:
            equity *= max(0.0, 1.0 + value)
            peak = max(peak, equity)
            max_drawdown = max(max_drawdown, (peak - equity) / peak if peak else 1.0)
        first = rows[0]["candle_closed_at"] if rows else None
        last = rows[-1]["settled_at"] if rows else None
        duration_days = 0.0
        if first and last:
            duration_days = max(
                0.0,
                (_parse_iso(last, "last shadow") - _parse_iso(first, "first shadow")).total_seconds()
                / 86_400.0,
            )
        return {
            "modelId": model_id,
            "settledSignals": len(rows),
            "settledBuys": len(returns),
            "netReturn": equity - 1.0,
            "maxDrawdown": max_drawdown,
            "durationDays": duration_days,
            "firstSignalAt": first,
            "lastSettledAt": last,
        }

    def claim_daily_entry(self, *, now: datetime, maximum: int) -> int:
        if not 1 <= maximum <= 12:
            raise AutonomyError("daily entry maximum is invalid")
        timestamp = _utc(now, "now")
        day = timestamp.date().isoformat()
        with self._lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT entries FROM daily_entry_counters WHERE utc_date = ?", (day,)
            ).fetchone()
            current = int(row["entries"]) if row else 0
            if current >= maximum:
                raise AutonomyError("daily entry budget is exhausted")
            next_value = current + 1
            db.execute(
                """
                INSERT INTO daily_entry_counters (utc_date, entries, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(utc_date) DO UPDATE
                SET entries = excluded.entries, updated_at = excluded.updated_at
                """,
                (day, next_value, _iso(timestamp)),
            )
        return next_value

    def create_entry_position(
        self,
        *,
        model_id: str,
        artifact_sha256: str,
        champion_generation: int,
        policy_sha256: str,
        credential_fingerprint: str,
        account_fingerprint: str,
        entry_signal_id: str,
        supervisor_decision_id: str,
        requested_size: Decimal,
        entry_candle_at: datetime,
        exit_due_at: datetime,
        hard_exit_at: datetime,
        now: datetime,
    ) -> str:
        for value, name in (
            (artifact_sha256, "artifact_sha256"),
            (policy_sha256, "policy_sha256"),
            (credential_fingerprint, "credential_fingerprint"),
            (account_fingerprint, "account_fingerprint"),
        ):
            _require_sha256(value, name)
        size = _decimal(requested_size, "requested_size", allow_zero=False)
        if champion_generation < 1:
            raise PositionStateError("position champion generation is invalid")
        if not (
            len(supervisor_decision_id) == 32
            and supervisor_decision_id.startswith("sup_")
            and all(character in "0123456789abcdef" for character in supervisor_decision_id[4:])
        ):
            raise PositionStateError("position supervisor decision is invalid")
        entry_at = _utc(entry_candle_at, "entry_candle_at")
        due_at = _utc(exit_due_at, "exit_due_at")
        hard_at = _utc(hard_exit_at, "hard_exit_at")
        if not entry_at < due_at <= hard_at:
            raise PositionStateError("position exit schedule is invalid")
        material = canonical_json(
            {
                "artifact_sha256": artifact_sha256,
                "entry_candle_at": _iso(entry_at),
                "entry_signal_id": entry_signal_id,
                "generation": champion_generation,
            }
        )
        position_id = f"pos_{sha256_hex(material)[:28]}"
        timestamp = _iso(now)
        try:
            with self._lock, self._connection() as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute(
                    """
                    INSERT INTO positions
                    (position_id, model_id, artifact_sha256, champion_generation,
                     policy_sha256, credential_fingerprint, account_fingerprint,
                     status, entry_signal_id, entry_intent_id, entry_ord_id,
                     entry_cl_ord_id, supervisor_decision_id, requested_size,
                     filled_size, remaining_size, entry_avg_price, entry_fee,
                     entry_fee_currency, entry_candle_at, exit_due_at, hard_exit_at,
                     stop_price, take_profit_price, exit_attempts, exit_intent_id,
                     exit_ord_id, exit_cl_ord_id, exit_avg_price, exited_size,
                     exit_quote_value, exit_fee, exit_fee_currency, realized_return,
                     failure_reason, created_at, updated_at, closed_at, position_hash)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'entry_submitted', ?, NULL, NULL,
                            NULL, ?, ?, '0', '0', NULL, '0', NULL, ?, ?, ?,
                            NULL, NULL, 0, NULL, NULL, NULL, NULL, '0', '0',
                            '0', NULL, NULL, NULL, ?, ?, NULL, ?)
                    """,
                    (
                        position_id,
                        model_id,
                        artifact_sha256,
                        champion_generation,
                        policy_sha256,
                        credential_fingerprint,
                        account_fingerprint,
                        entry_signal_id,
                        supervisor_decision_id,
                        str(size),
                        _iso(entry_at),
                        _iso(due_at),
                        _iso(hard_at),
                        timestamp,
                        timestamp,
                        "0" * 64,
                    ),
                )
                self._seal_position(db, position_id)
        except sqlite3.IntegrityError as exc:
            raise PositionStateError("another model position or signal is already active") from exc
        return position_id

    def attach_entry_order(
        self,
        position_id: str,
        *,
        intent_id: str,
        ord_id: str,
        cl_ord_id: str,
        now: datetime,
    ) -> None:
        self.attach_entry_intent(
            position_id,
            intent_id=intent_id,
            cl_ord_id=cl_ord_id,
            now=now,
        )
        self.confirm_entry_order(position_id, ord_id=ord_id, now=now)

    def attach_entry_intent(
        self,
        position_id: str,
        *,
        intent_id: str,
        cl_ord_id: str,
        now: datetime,
    ) -> None:
        with self._lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._assert_position_integrity(db, position_id)
            updated = db.execute(
                """
                UPDATE positions SET entry_intent_id = ?, entry_cl_ord_id = ?, updated_at = ?
                WHERE position_id = ? AND status = 'entry_submitted'
                  AND entry_intent_id IS NULL
                """,
                (intent_id, cl_ord_id, _iso(now), position_id),
            )
            if updated.rowcount != 1:
                raise PositionStateError("entry intent cannot be attached")
            self._seal_position(db, position_id)

    def confirm_entry_order(
        self, position_id: str, *, ord_id: str, now: datetime
    ) -> None:
        if not ord_id.strip():
            raise PositionStateError("entry order ID is empty")
        with self._lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._assert_position_integrity(db, position_id)
            updated = db.execute(
                """
                UPDATE positions SET entry_ord_id = ?, updated_at = ?
                WHERE position_id = ? AND status = 'entry_submitted'
                  AND entry_intent_id IS NOT NULL AND entry_ord_id IS NULL
                """,
                (ord_id.strip(), _iso(now), position_id),
            )
            if updated.rowcount != 1:
                raise PositionStateError("entry order cannot be confirmed")
            self._seal_position(db, position_id)

    def resolve_entry(
        self,
        position_id: str,
        *,
        filled_size: Decimal,
        average_price: Decimal | None,
        terminal_state: str,
        policy: AutonomyPolicy,
        now: datetime,
        fee: Decimal = Decimal("0"),
        fee_currency: str = "",
    ) -> dict[str, Any]:
        filled = _decimal(filled_size, "filled_size")
        if terminal_state not in {"filled", "canceled", "mmp_canceled"}:
            raise PositionStateError("entry order is not terminal")
        average = (
            _decimal(average_price, "average_price", allow_zero=False)
            if filled > 0 and average_price is not None
            else None
        )
        if filled > 0 and average is None:
            raise PositionStateError("filled entry has no average price")
        fee_value = _signed_decimal(fee, "entry_fee")
        normalized_fee_currency = fee_currency.strip().upper()
        if normalized_fee_currency not in {"", "BTC", "USDT"}:
            raise PositionStateError("entry fee currency is unsupported")
        timestamp = _iso(now)
        with self._lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._assert_position_integrity(db, position_id)
            row = db.execute(
                "SELECT requested_size FROM positions WHERE position_id = ? AND status = 'entry_submitted'",
                (position_id,),
            ).fetchone()
            if row is None:
                raise PositionStateError("entry position is not awaiting resolution")
            requested = _decimal(row["requested_size"], "requested_size", allow_zero=False)
            if filled > requested:
                raise PositionStateError("entry fill exceeds the requested model quantity")
            owned_size = filled
            if normalized_fee_currency == "BTC":
                owned_size += fee_value
            if owned_size < 0 or owned_size > filled:
                raise PositionStateError("entry fee makes model-owned quantity invalid")
            if owned_size == 0:
                db.execute(
                    """
                    UPDATE positions SET status = 'entry_unfilled', filled_size = '0',
                        remaining_size = '0', entry_fee = ?,
                        entry_fee_currency = ?, updated_at = ?, closed_at = ?
                    WHERE position_id = ?
                    """,
                    (
                        str(fee_value),
                        normalized_fee_currency or None,
                        timestamp,
                        timestamp,
                        position_id,
                    ),
                )
            else:
                stop_price = average * (Decimal("1") - policy.stop_loss_fraction)
                take_price = average * (Decimal("1") + policy.take_profit_fraction)
                db.execute(
                    """
                    UPDATE positions SET status = 'long', filled_size = ?,
                        remaining_size = ?, entry_avg_price = ?, entry_fee = ?,
                        entry_fee_currency = ?, stop_price = ?, take_profit_price = ?,
                        updated_at = ?
                    WHERE position_id = ?
                    """,
                    (
                        str(filled),
                        str(owned_size),
                        str(average),
                        str(fee_value),
                        normalized_fee_currency or None,
                        str(stop_price),
                        str(take_price),
                        timestamp,
                        position_id,
                    ),
                )
            self._seal_position(db, position_id)
        return self.get_position(position_id) or {}

    def mark_exit_submitted(
        self,
        position_id: str,
        *,
        intent_id: str,
        ord_id: str,
        cl_ord_id: str,
        now: datetime,
    ) -> None:
        self.begin_exit_dispatch(
            position_id,
            intent_id=intent_id,
            cl_ord_id=cl_ord_id,
            now=now,
        )
        self.confirm_exit_order(position_id, ord_id=ord_id, now=now)

    def begin_exit_dispatch(
        self,
        position_id: str,
        *,
        intent_id: str,
        cl_ord_id: str,
        now: datetime,
    ) -> None:
        with self._lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._assert_position_integrity(db, position_id)
            updated = db.execute(
                """
                UPDATE positions
                SET status = 'exit_submitted', exit_attempts = exit_attempts + 1,
                    exit_intent_id = ?, exit_ord_id = NULL, exit_cl_ord_id = ?,
                    updated_at = ?
                WHERE position_id = ? AND status = 'long'
                """,
                (intent_id, cl_ord_id, _iso(now), position_id),
            )
            if updated.rowcount != 1:
                raise PositionStateError("position is not ready for exit dispatch")
            self._seal_position(db, position_id)

    def confirm_exit_order(
        self, position_id: str, *, ord_id: str, now: datetime
    ) -> None:
        if not ord_id.strip():
            raise PositionStateError("exit order ID is empty")
        with self._lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._assert_position_integrity(db, position_id)
            updated = db.execute(
                """
                UPDATE positions SET exit_ord_id = ?, updated_at = ?
                WHERE position_id = ? AND status = 'exit_submitted'
                  AND exit_intent_id IS NOT NULL AND exit_ord_id IS NULL
                """,
                (ord_id.strip(), _iso(now), position_id),
            )
            if updated.rowcount != 1:
                raise PositionStateError("exit order cannot be confirmed")
            self._seal_position(db, position_id)

    def abandon_exit_before_dispatch(self, position_id: str, *, now: datetime) -> None:
        """Return a conclusively uncommitted exit preview to the long state."""

        with self._lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._assert_position_integrity(db, position_id)
            updated = db.execute(
                """
                UPDATE positions
                SET status = 'long', exit_attempts = CASE
                        WHEN exit_attempts > 0 THEN exit_attempts - 1 ELSE 0 END,
                    exit_intent_id = NULL, exit_ord_id = NULL,
                    exit_cl_ord_id = NULL, updated_at = ?
                WHERE position_id = ? AND status = 'exit_submitted'
                  AND exit_ord_id IS NULL
                """,
                (_iso(now), position_id),
            )
            if updated.rowcount != 1:
                raise PositionStateError("exit preview cannot be abandoned safely")
            self._seal_position(db, position_id)

    def resolve_exit(
        self,
        position_id: str,
        *,
        filled_size: Decimal,
        average_price: Decimal | None,
        terminal_state: str,
        max_exit_attempts: int,
        now: datetime,
        fee: Decimal = Decimal("0"),
        fee_currency: str = "",
    ) -> dict[str, Any]:
        filled = _decimal(filled_size, "filled_size")
        if terminal_state not in {"filled", "canceled", "mmp_canceled"}:
            raise PositionStateError("exit order is not terminal")
        average = (
            _decimal(average_price, "average_price", allow_zero=False)
            if filled > 0 and average_price is not None
            else None
        )
        if filled > 0 and average is None:
            raise PositionStateError("filled exit has no average price")
        fee_value = _signed_decimal(fee, "exit_fee")
        normalized_fee_currency = fee_currency.strip().upper()
        if normalized_fee_currency not in {"", "BTC", "USDT"}:
            raise PositionStateError("exit fee currency is unsupported")
        timestamp = _iso(now)
        with self._lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._assert_position_integrity(db, position_id)
            row = db.execute(
                """
                SELECT remaining_size, filled_size, entry_avg_price, entry_fee,
                       entry_fee_currency, exit_attempts, exited_size,
                       exit_quote_value, exit_fee, exit_fee_currency
                FROM positions WHERE position_id = ? AND status = 'exit_submitted'
                """,
                (position_id,),
            ).fetchone()
            if row is None:
                raise PositionStateError("position is not awaiting exit resolution")
            remaining = _decimal(row["remaining_size"], "remaining_size", allow_zero=False)
            base_debit = filled
            if normalized_fee_currency == "BTC":
                base_debit -= fee_value
            if base_debit < 0 or base_debit > remaining:
                raise PositionStateError("exit fill exceeds the model-owned remaining quantity")
            next_remaining = remaining - base_debit
            attempts = int(row["exit_attempts"])
            prior_exited = _decimal(row["exited_size"], "exited_size")
            prior_quote = _decimal(row["exit_quote_value"], "exit_quote_value")
            prior_fee = _signed_decimal(row["exit_fee"], "exit_fee")
            prior_fee_currency = str(row["exit_fee_currency"] or "").upper()
            if (
                prior_fee != 0
                and fee_value != 0
                and prior_fee_currency
                and normalized_fee_currency
                and prior_fee_currency != normalized_fee_currency
            ):
                raise PositionStateError("exit fee currency changed across partial fills")
            next_exited = prior_exited + filled
            quote_proceeds = filled * average if average is not None else Decimal("0")
            if normalized_fee_currency == "USDT":
                quote_proceeds += fee_value
            if quote_proceeds < 0:
                raise PositionStateError("exit fee makes quote proceeds invalid")
            next_quote = prior_quote + quote_proceeds
            next_fee = prior_fee + fee_value
            combined_fee_currency = normalized_fee_currency or prior_fee_currency or None
            weighted_exit = next_quote / next_exited if next_exited > 0 else None
            if next_remaining == 0:
                entry_price = _decimal(
                    row["entry_avg_price"], "entry_avg_price", allow_zero=False
                )
                entry_filled = _decimal(
                    row["filled_size"], "filled_size", allow_zero=False
                )
                entry_fee = _signed_decimal(row["entry_fee"], "entry_fee")
                entry_fee_currency = str(row["entry_fee_currency"] or "").upper()
                entry_quote_cost = entry_filled * entry_price
                if entry_fee_currency == "USDT":
                    entry_quote_cost -= entry_fee
                if entry_quote_cost <= 0:
                    raise PositionStateError("entry quote cost is invalid")
                realized = float(next_quote / entry_quote_cost - Decimal("1"))
                db.execute(
                    """
                    UPDATE positions SET status = 'closed', remaining_size = '0',
                        exit_avg_price = ?, exited_size = ?, exit_quote_value = ?,
                        exit_fee = ?, exit_fee_currency = ?, realized_return = ?, updated_at = ?,
                        closed_at = ? WHERE position_id = ?
                    """,
                    (
                        str(weighted_exit) if weighted_exit is not None else None,
                        str(next_exited),
                        str(next_quote),
                        str(next_fee),
                        combined_fee_currency,
                        realized,
                        timestamp,
                        timestamp,
                        position_id,
                    ),
                )
            elif attempts >= max_exit_attempts:
                db.execute(
                    """
                    UPDATE positions SET status = 'manual_review', remaining_size = ?,
                        exit_avg_price = ?, exited_size = ?, exit_quote_value = ?,
                        exit_fee = ?, exit_fee_currency = ?,
                        failure_reason = 'exit_attempts_exhausted',
                        updated_at = ? WHERE position_id = ?
                    """,
                    (
                        str(next_remaining),
                        str(weighted_exit) if weighted_exit is not None else None,
                        str(next_exited),
                        str(next_quote),
                        str(next_fee),
                        combined_fee_currency,
                        timestamp,
                        position_id,
                    ),
                )
            else:
                db.execute(
                    """
                    UPDATE positions SET status = 'long', remaining_size = ?,
                        exit_avg_price = ?, exited_size = ?, exit_quote_value = ?,
                        exit_fee = ?, exit_fee_currency = ?, updated_at = ?
                    WHERE position_id = ?
                    """,
                    (
                        str(next_remaining),
                        str(weighted_exit) if weighted_exit is not None else None,
                        str(next_exited),
                        str(next_quote),
                        str(next_fee),
                        combined_fee_currency,
                        timestamp,
                        position_id,
                    ),
                )
            self._seal_position(db, position_id)
        return self.get_position(position_id) or {}

    def require_manual_review(
        self, position_id: str, reason: str, *, now: datetime
    ) -> None:
        bounded_reason = reason.strip()[:512] or "manual_review"
        with self._lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._assert_position_integrity(db, position_id)
            updated = db.execute(
                """
                UPDATE positions SET status = 'manual_review', failure_reason = ?,
                    updated_at = ? WHERE position_id = ?
                      AND status IN ('entry_submitted','long','exit_submitted')
                """,
                (bounded_reason, _iso(now), position_id),
            )
            if updated.rowcount != 1:
                raise PositionStateError("position cannot enter manual review")
            self._seal_position(db, position_id)

    def close_dust(
        self, position_id: str, *, dust_size: Decimal, reason: str, now: datetime
    ) -> dict[str, Any]:
        dust = _decimal(dust_size, "dust_size", allow_zero=False)
        bounded_reason = reason.strip()[:512] or "below_exchange_minimum"
        timestamp = _iso(now)
        with self._lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._assert_position_integrity(db, position_id)
            row = db.execute(
                "SELECT remaining_size FROM positions WHERE position_id = ? AND status = 'long'",
                (position_id,),
            ).fetchone()
            if row is None:
                raise PositionStateError("position is not eligible for dust closure")
            remaining = _decimal(row["remaining_size"], "remaining_size", allow_zero=False)
            if dust != remaining:
                raise PositionStateError("dust quantity does not match model-owned remainder")
            db.execute(
                """
                UPDATE positions SET status = 'manual_review', failure_reason = ?,
                    updated_at = ? WHERE position_id = ?
                """,
                (bounded_reason, timestamp, position_id),
            )
            self._seal_position(db, position_id)
        return self.get_position(position_id) or {}

    def get_position(self, position_id: str) -> dict[str, Any] | None:
        with self._connection() as db:
            row = db.execute(
                "SELECT * FROM positions WHERE position_id = ?", (position_id,)
            ).fetchone()
        return self._safe_position(row) if row else None

    def position_for_signal(self, entry_signal_id: str) -> dict[str, Any] | None:
        with self._connection() as db:
            row = db.execute(
                "SELECT * FROM positions WHERE entry_signal_id = ?", (entry_signal_id,)
            ).fetchone()
        return self._safe_position(row) if row else None

    def active_position(self) -> dict[str, Any] | None:
        with self._connection() as db:
            rows = db.execute(
                "SELECT * FROM positions ORDER BY created_at ASC"
            ).fetchall()
        safe_rows = [self._safe_position(row) for row in rows]
        active = [row for row in safe_rows if row["status"] in ACTIVE_POSITION_STATES]
        if len(active) > 1:
            raise PositionStateError("multiple active model positions violate the invariant")
        return active[0] if active else None

    def recent_positions(self, limit: int = 50) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 200))
        with self._connection() as db:
            rows = db.execute(
                "SELECT * FROM positions ORDER BY created_at DESC LIMIT ?", (safe_limit,)
            ).fetchall()
        return [self._safe_position(row) for row in rows]

    def demo_performance(self) -> dict[str, Any]:
        with self._connection() as db:
            rows = db.execute(
                """
                SELECT * FROM positions
                WHERE status = 'closed' AND realized_return IS NOT NULL
                ORDER BY closed_at ASC
                """
            ).fetchall()
        safe_rows = [self._safe_position(row) for row in rows]
        returns = [float(row["realizedReturn"]) for row in safe_rows]
        equity = 1.0
        peak = 1.0
        max_drawdown = 0.0
        for value in returns:
            equity *= max(0.0, 1.0 + value)
            peak = max(peak, equity)
            max_drawdown = max(max_drawdown, (peak - equity) / peak if peak else 1.0)
        return {
            "closedPositions": len(returns),
            "netReturn": equity - 1.0,
            "maxDrawdown": max_drawdown,
            "lastClosedAt": safe_rows[-1]["closedAt"] if safe_rows else None,
        }

    @staticmethod
    def _safe_position(row: sqlite3.Row) -> dict[str, Any]:
        stored = str(row["position_hash"] or "")
        expected = AutonomyStore._position_digest(row)
        if len(stored) != 64 or not hmac.compare_digest(stored, expected):
            raise PositionStateError("position integrity hash mismatch")
        return {
            "positionId": row["position_id"],
            "modelId": row["model_id"],
            "artifactSha256": row["artifact_sha256"],
            "championGeneration": int(row["champion_generation"]),
            "policySha256": row["policy_sha256"],
            "status": row["status"],
            "entrySignalId": row["entry_signal_id"],
            "entryIntentId": row["entry_intent_id"],
            "entryOrdId": row["entry_ord_id"],
            "entryClOrdId": row["entry_cl_ord_id"],
            "supervisorDecisionId": row["supervisor_decision_id"],
            "requestedSize": row["requested_size"],
            "filledSize": row["filled_size"],
            "remainingSize": row["remaining_size"],
            "entryAvgPrice": row["entry_avg_price"],
            "entryFee": row["entry_fee"],
            "entryFeeCurrency": row["entry_fee_currency"],
            "entryCandleAt": row["entry_candle_at"],
            "exitDueAt": row["exit_due_at"],
            "hardExitAt": row["hard_exit_at"],
            "stopPrice": row["stop_price"],
            "takeProfitPrice": row["take_profit_price"],
            "exitAttempts": int(row["exit_attempts"]),
            "exitIntentId": row["exit_intent_id"],
            "exitOrdId": row["exit_ord_id"],
            "exitClOrdId": row["exit_cl_ord_id"],
            "exitAvgPrice": row["exit_avg_price"],
            "exitedSize": row["exited_size"],
            "exitQuoteValue": row["exit_quote_value"],
            "exitFee": row["exit_fee"],
            "exitFeeCurrency": row["exit_fee_currency"],
            "realizedReturn": row["realized_return"],
            "failureReason": row["failure_reason"],
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
            "closedAt": row["closed_at"],
        }

    def summary(self) -> dict[str, Any]:
        return {
            "schemaVersion": AUTONOMY_SCHEMA_VERSION,
            "state": self.state(),
            "activePosition": self.active_position(),
            "latestTraining": self.latest_training(),
            "recentDecisions": self.recent_decisions(limit=10),
            "recentPositions": self.recent_positions(limit=10),
            "demoPerformance": self.demo_performance(),
        }
