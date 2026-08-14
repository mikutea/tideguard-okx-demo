from __future__ import annotations

import asyncio
import hmac
import json
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator, Protocol

from ..models import OrderDraft
from .registry import ChampionSnapshot
from .strategy import (
    ALLOWED_INSTRUMENT,
    DEMO_ENVIRONMENT,
    DemoStrategyPolicy,
    OrderProposal,
    canonical_json,
    sha256_hex,
)


AUTO_SESSION_CONFIRMATION = "ENABLE OKX DEMO AUTO"
MAX_SESSION_SECONDS = 600
MAX_SESSION_ORDERS = 1
MAX_SESSION_NOTIONAL_USDT = Decimal("10")
MAX_EXECUTION_MARKET_AGE_SECONDS = 8


class AutomationDenied(RuntimeError):
    pass


class ManualReviewRequired(AutomationDenied):
    pass


def _utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise AutomationDenied(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return _utc(value, "timestamp").isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class DemoAutomationPermit:
    permit_id: str
    model_id: str
    artifact_sha256: str
    champion_generation: int
    policy_sha256: str
    issued_by: str
    issued_at: datetime
    expires_at: datetime
    max_orders: int
    max_total_notional_usdt: Decimal
    environment: str = DEMO_ENVIRONMENT
    instrument: str = ALLOWED_INSTRUMENT

    def __post_init__(self) -> None:
        issued = _utc(self.issued_at, "issued_at")
        expires = _utc(self.expires_at, "expires_at")
        if self.environment != DEMO_ENVIRONMENT or self.instrument != ALLOWED_INSTRUMENT:
            raise AutomationDenied("automation permits are demo-only and BTC-USDT-only")
        if not self.permit_id.startswith("permit_") or len(self.permit_id) != 31:
            raise AutomationDenied("permit_id is invalid")
        if not self.model_id.startswith("mdl_") or self.champion_generation < 1:
            raise AutomationDenied("permit champion binding is invalid")
        for value in (self.artifact_sha256, self.policy_sha256):
            if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
                raise AutomationDenied("permit hashes must be lowercase sha256")
        if not self.issued_by.strip() or len(self.issued_by) > 128:
            raise AutomationDenied("permit requires a bounded human actor")
        ttl = (expires - issued).total_seconds()
        if not 0 < ttl <= MAX_SESSION_SECONDS:
            raise AutomationDenied("permit TTL is outside the hard limit")
        if not 1 <= self.max_orders <= MAX_SESSION_ORDERS:
            raise AutomationDenied("permit order count exceeds the hard limit")
        if not Decimal("0") < self.max_total_notional_usdt <= MAX_SESSION_NOTIONAL_USDT:
            raise AutomationDenied("permit notional exceeds the hard limit")

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_sha256": self.artifact_sha256,
            "champion_generation": self.champion_generation,
            "environment": self.environment,
            "expires_at": _iso(self.expires_at),
            "instrument": self.instrument,
            "issued_at": _iso(self.issued_at),
            "issued_by": self.issued_by,
            "max_orders": self.max_orders,
            "max_total_notional_usdt": str(self.max_total_notional_usdt),
            "model_id": self.model_id,
            "permit_id": self.permit_id,
            "policy_sha256": self.policy_sha256,
        }


def authorize_demo_session(
    champion: ChampionSnapshot,
    policy: DemoStrategyPolicy,
    *,
    issued_by: str,
    confirmation: str,
    issued_at: datetime,
    ttl_seconds: int,
    max_orders: int,
    max_total_notional_usdt: Decimal,
) -> DemoAutomationPermit:
    """Mint a short, champion-bound permit after an explicit human action."""

    if confirmation != AUTO_SESSION_CONFIRMATION:
        raise AutomationDenied("manual demo automation confirmation does not match")
    issued = _utc(issued_at, "issued_at")
    if not 1 <= ttl_seconds <= MAX_SESSION_SECONDS:
        raise AutomationDenied("requested session TTL exceeds the hard limit")
    if max_total_notional_usdt > policy.fixed_notional_usdt * max_orders:
        raise AutomationDenied("session notional exceeds frozen sizing times order count")
    body = {
        "artifact_sha256": champion.artifact_sha256,
        "champion_generation": champion.generation,
        "expires_at": _iso(issued + timedelta(seconds=ttl_seconds)),
        "issued_at": _iso(issued),
        "issued_by": issued_by.strip(),
        "max_orders": max_orders,
        "max_total_notional_usdt": str(max_total_notional_usdt),
        "model_id": champion.model_id,
        "policy_sha256": policy.policy_sha256,
    }
    permit_id = f"permit_{sha256_hex(canonical_json(body))[:24]}"
    return DemoAutomationPermit(
        permit_id=permit_id,
        model_id=champion.model_id,
        artifact_sha256=champion.artifact_sha256,
        champion_generation=champion.generation,
        policy_sha256=policy.policy_sha256,
        issued_by=issued_by.strip(),
        issued_at=issued,
        expires_at=issued + timedelta(seconds=ttl_seconds),
        max_orders=max_orders,
        max_total_notional_usdt=max_total_notional_usdt,
    )


class TradingPort(Protocol):
    async def preview(self, draft: OrderDraft) -> dict[str, Any]: ...

    async def commit(
        self, intent_id: str, digest: str, idempotency_key: str
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class ExecutionResult:
    signal_id: str
    intent_id: str | None
    status: str
    ord_id: str | None


class AutomationLedger:
    """Crash-visible, single-claim ledger; it never retries an unknown commit itself."""

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
        with self._connection() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS automation_permits (
                    permit_id TEXT PRIMARY KEY,
                    permit_json TEXT NOT NULL,
                    used_orders INTEGER NOT NULL DEFAULT 0 CHECK(used_orders >= 0),
                    used_notional TEXT NOT NULL DEFAULT '0',
                    revoked_at TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS automation_executions (
                    signal_id TEXT PRIMARY KEY,
                    permit_id TEXT NOT NULL REFERENCES automation_permits(permit_id),
                    model_id TEXT NOT NULL,
                    artifact_sha256 TEXT NOT NULL,
                    evidence_sha256 TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    notional_usdt TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN (
                        'claimed', 'previewed', 'preview_rejected', 'commit_requested',
                        'completed', 'manual_review'
                    )),
                    intent_id TEXT,
                    digest TEXT,
                    result_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )

    def register_permit(self, permit: DemoAutomationPermit, *, now: datetime) -> None:
        """Persist a newly authorized permit before it is returned to a caller."""

        permit_json = canonical_json(permit.to_dict())
        with self._lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT permit_json FROM automation_permits WHERE permit_id = ?",
                (permit.permit_id,),
            ).fetchone()
            if existing:
                if not hmac.compare_digest(str(existing["permit_json"]), permit_json):
                    raise AutomationDenied("persisted permit body does not match")
                return
            db.execute(
                """
                INSERT INTO automation_permits
                (permit_id, permit_json, used_orders, used_notional, revoked_at, created_at)
                VALUES (?, ?, 0, '0', NULL, ?)
                """,
                (permit.permit_id, permit_json, _iso(now)),
            )

    def revoke_permit(self, permit_id: str, *, now: datetime) -> None:
        """Persist revocation before any exchange-side emergency cleanup is attempted."""

        with self._lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            updated = db.execute(
                """
                UPDATE automation_permits SET revoked_at = COALESCE(revoked_at, ?)
                WHERE permit_id = ?
                """,
                (_iso(now), permit_id),
            )
            if updated.rowcount != 1:
                raise AutomationDenied("automation permit does not exist")

    def claim(
        self, proposal: OrderProposal, permit: DemoAutomationPermit, *, now: datetime
    ) -> None:
        current = _utc(now, "now")
        if current < permit.issued_at or current >= permit.expires_at:
            raise AutomationDenied("demo automation permit is not currently valid")
        timestamp = _iso(current)
        permit_json = canonical_json(permit.to_dict())
        with self._lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            existing_permit = db.execute(
                "SELECT * FROM automation_permits WHERE permit_id = ?", (permit.permit_id,)
            ).fetchone()
            if existing_permit is None:
                db.execute(
                    """
                    INSERT INTO automation_permits
                    (permit_id, permit_json, used_orders, used_notional, revoked_at, created_at)
                    VALUES (?, ?, 0, '0', NULL, ?)
                    """,
                    (permit.permit_id, permit_json, timestamp),
                )
                used_orders = 0
                used_notional = Decimal("0")
            else:
                if not hmac.compare_digest(str(existing_permit["permit_json"]), permit_json):
                    raise AutomationDenied("persisted permit body does not match")
                if existing_permit["revoked_at"] is not None:
                    raise AutomationDenied("demo automation permit is revoked")
                used_orders = int(existing_permit["used_orders"])
                try:
                    used_notional = Decimal(str(existing_permit["used_notional"]))
                except Exception as exc:
                    raise AutomationDenied("persisted permit accounting is invalid") from exc
                if not used_notional.is_finite() or used_notional < 0:
                    raise AutomationDenied("persisted permit accounting is invalid")
            if used_orders >= permit.max_orders:
                raise AutomationDenied("demo automation order budget is exhausted")
            if used_notional + proposal.notional_usdt > permit.max_total_notional_usdt:
                raise AutomationDenied("demo automation notional budget is exhausted")
            try:
                db.execute(
                    """
                    INSERT INTO automation_executions
                    (signal_id, permit_id, model_id, artifact_sha256, evidence_sha256,
                     idempotency_key, notional_usdt, status, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'claimed', ?, ?)
                    """,
                    (
                        proposal.signal_id,
                        permit.permit_id,
                        proposal.model_id,
                        proposal.artifact_sha256,
                        proposal.evidence_sha256,
                        proposal.idempotency_key,
                        str(proposal.notional_usdt),
                        timestamp,
                        timestamp,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise AutomationDenied("signal or idempotency key was already claimed") from exc
            db.execute(
                """
                UPDATE automation_permits
                SET used_orders = ?, used_notional = ?
                WHERE permit_id = ?
                """,
                (used_orders + 1, str(used_notional + proposal.notional_usdt), permit.permit_id),
            )

    def record_preview(
        self, signal_id: str, intent_id: str, digest: str, *, now: datetime
    ) -> None:
        with self._lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            updated = db.execute(
                """
                UPDATE automation_executions
                SET status = 'previewed', intent_id = ?, digest = ?, updated_at = ?
                WHERE signal_id = ? AND status = 'claimed'
                """,
                (intent_id, digest, _iso(now), signal_id),
            )
            if updated.rowcount != 1:
                raise AutomationDenied("signal is not in the claimed state")

    def record_preview_rejected(
        self, signal_id: str, preview: dict[str, Any] | None, *, now: datetime
    ) -> None:
        result_json = canonical_json(preview or {"reason": "preview_failed"})
        with self._lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                """
                UPDATE automation_executions
                SET status = 'preview_rejected', result_json = ?, updated_at = ?
                WHERE signal_id = ? AND status = 'claimed'
                """,
                (result_json, _iso(now), signal_id),
            )

    def mark_commit_requested(self, signal_id: str, *, now: datetime) -> None:
        with self._lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            updated = db.execute(
                """
                UPDATE automation_executions
                SET status = 'commit_requested', updated_at = ?
                WHERE signal_id = ? AND status = 'previewed'
                """,
                (_iso(now), signal_id),
            )
            if updated.rowcount != 1:
                raise AutomationDenied("signal is not ready to commit")

    def finish(
        self,
        signal_id: str,
        result: dict[str, Any],
        *,
        manual_review: bool,
        now: datetime,
    ) -> None:
        status = "manual_review" if manual_review else "completed"
        with self._lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            updated = db.execute(
                """
                UPDATE automation_executions
                SET status = ?, result_json = ?, updated_at = ?
                WHERE signal_id = ? AND status = 'commit_requested'
                """,
                (status, canonical_json(result), _iso(now), signal_id),
            )
            if updated.rowcount != 1:
                raise AutomationDenied("commit result cannot be recorded from the current state")

    def require_manual_review(
        self, signal_id: str, error_type: str, *, now: datetime
    ) -> None:
        payload = canonical_json({"error_type": error_type, "raw_error_omitted": True})
        try:
            with self._lock, self._connection() as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute(
                    """
                    UPDATE automation_executions
                    SET status = 'manual_review', result_json = ?, updated_at = ?
                    WHERE signal_id = ? AND status = 'commit_requested'
                    """,
                    (payload, _iso(now), signal_id),
                )
        except Exception:
            # The caller must still stop; ledger failure must not trigger a retry.
            pass

    def pending_manual_review(self) -> list[dict[str, Any]]:
        with self._connection() as db:
            rows = db.execute(
                """
                SELECT signal_id, permit_id, intent_id, digest, idempotency_key, status
                FROM automation_executions
                WHERE status IN ('claimed', 'previewed', 'commit_requested', 'manual_review')
                ORDER BY created_at ASC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def permit_status(self, permit_id: str) -> dict[str, Any] | None:
        with self._connection() as db:
            row = db.execute(
                """
                SELECT permit_json, used_orders, used_notional, revoked_at, created_at
                FROM automation_permits WHERE permit_id = ?
                """,
                (permit_id,),
            ).fetchone()
        if row is None:
            return None
        try:
            permit = json.loads(str(row["permit_json"]))
            used_notional = Decimal(str(row["used_notional"]))
        except Exception as exc:
            raise AutomationDenied("persisted permit status is invalid") from exc
        if not isinstance(permit, dict) or not used_notional.is_finite() or used_notional < 0:
            raise AutomationDenied("persisted permit status is invalid")
        return {
            "permitId": permit_id,
            "modelId": permit.get("model_id"),
            "artifactSha256": permit.get("artifact_sha256"),
            "championGeneration": permit.get("champion_generation"),
            "policySha256": permit.get("policy_sha256"),
            "issuedAt": permit.get("issued_at"),
            "expiresAt": permit.get("expires_at"),
            "maxOrders": permit.get("max_orders"),
            "maxTotalNotionalUsdt": permit.get("max_total_notional_usdt"),
            "usedOrders": int(row["used_orders"]),
            "usedNotionalUsdt": str(used_notional),
            "revokedAt": row["revoked_at"],
            "createdAt": row["created_at"],
        }

    def recent_executions(self, limit: int = 100) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 200))
        with self._connection() as db:
            rows = db.execute(
                """
                SELECT signal_id, permit_id, model_id, artifact_sha256,
                       evidence_sha256, idempotency_key, notional_usdt, status,
                       intent_id, digest, result_json, created_at, updated_at
                FROM automation_executions
                ORDER BY created_at DESC LIMIT ?
                """,
                (safe_limit,),
            ).fetchall()
        return [
            {
                "signalId": row["signal_id"],
                "permitId": row["permit_id"],
                "modelId": row["model_id"],
                "artifactSha256": row["artifact_sha256"],
                "evidenceSha256": row["evidence_sha256"],
                "idempotencyKey": row["idempotency_key"],
                "notionalUsdt": row["notional_usdt"],
                "status": row["status"],
                "intentId": row["intent_id"],
                "digest": row["digest"],
                "result": json.loads(row["result_json"]) if row["result_json"] else None,
                "createdAt": row["created_at"],
                "updatedAt": row["updated_at"],
            }
            for row in rows
        ]


class DemoAutoExecutor:
    """One-shot adapter to TradingService; unknown results always stop for review."""

    def __init__(self, ledger: AutomationLedger):
        self.ledger = ledger

    @staticmethod
    def _validate_bindings(
        proposal: OrderProposal,
        permit: DemoAutomationPermit,
        champion: ChampionSnapshot,
        now: datetime,
    ) -> None:
        current = _utc(now, "now")
        if current < permit.issued_at or current >= permit.expires_at:
            raise AutomationDenied("demo automation permit is not currently valid")
        if proposal.environment != DEMO_ENVIRONMENT or proposal.instrument != ALLOWED_INSTRUMENT:
            raise AutomationDenied("proposal escaped the demo-only instrument boundary")
        if proposal.side != "buy":
            raise AutomationDenied("v0.2 model automation is BUY-entry-only; SELL is forbidden")
        proposal_age = (
            current - _utc(proposal.observed_at, "observed_at")
        ).total_seconds()
        if proposal_age < 0 or proposal_age > MAX_EXECUTION_MARKET_AGE_SECONDS:
            raise AutomationDenied("proposal market snapshot is stale at execution time")
        if proposal.notional_usdt <= 0 or proposal.notional_usdt > Decimal("25"):
            raise AutomationDenied("proposal exceeds the hard per-order demo cap")
        expected = (
            permit.model_id,
            permit.artifact_sha256,
            permit.champion_generation,
        )
        current_champion = (
            champion.model_id,
            champion.artifact_sha256,
            champion.generation,
        )
        if expected != current_champion:
            raise AutomationDenied("champion changed after the session was authorized")
        if proposal.model_id != champion.model_id or not hmac.compare_digest(
            proposal.artifact_sha256, champion.artifact_sha256
        ):
            raise AutomationDenied("proposal is not from the authorized frozen champion")
        if not hmac.compare_digest(proposal.policy_sha256, permit.policy_sha256):
            raise AutomationDenied("proposal sizing policy does not match the permit")

    async def execute(
        self,
        proposal: OrderProposal,
        permit: DemoAutomationPermit,
        champion: ChampionSnapshot,
        port: TradingPort,
        *,
        now: datetime,
    ) -> ExecutionResult:
        self._validate_bindings(proposal, permit, champion, now)
        self.ledger.claim(proposal, permit, now=now)
        draft = OrderDraft(
            instId=proposal.instrument,
            side=proposal.side,
            ordType=proposal.order_type,
            price=proposal.price,
            size=proposal.size,
        )
        try:
            preview = await port.preview(draft)
        except BaseException:
            self.ledger.record_preview_rejected(proposal.signal_id, None, now=now)
            raise
        if not isinstance(preview, dict):
            self.ledger.record_preview_rejected(proposal.signal_id, None, now=now)
            raise AutomationDenied("preview response is not an object")
        decision = preview.get("decision")
        intent_id = str(preview.get("intentId", "")).strip()
        digest = str(preview.get("digest", "")).strip()
        allowed = isinstance(decision, dict) and decision.get("allowed") is True
        if not allowed:
            self.ledger.record_preview_rejected(proposal.signal_id, preview, now=now)
            return ExecutionResult(
                signal_id=proposal.signal_id,
                intent_id=intent_id or None,
                status="preview_rejected",
                ord_id=None,
            )
        if not intent_id or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            self.ledger.record_preview_rejected(proposal.signal_id, preview, now=now)
            raise AutomationDenied("allowed preview is missing a valid intent identity")
        self.ledger.record_preview(proposal.signal_id, intent_id, digest, now=now)
        self.ledger.mark_commit_requested(proposal.signal_id, now=now)
        try:
            result = await port.commit(intent_id, digest, proposal.idempotency_key)
        except asyncio.CancelledError:
            self.ledger.require_manual_review(
                proposal.signal_id, "CancelledError", now=datetime.now(timezone.utc)
            )
            raise
        except BaseException as exc:
            self.ledger.require_manual_review(
                proposal.signal_id, type(exc).__name__, now=datetime.now(timezone.utc)
            )
            raise ManualReviewRequired(
                "commit outcome is unknown; automatic retry is forbidden pending reconciliation"
            ) from exc
        if not isinstance(result, dict):
            self.ledger.require_manual_review(
                proposal.signal_id, "InvalidCommitResponse", now=datetime.now(timezone.utc)
            )
            raise ManualReviewRequired("commit response is invalid and requires reconciliation")
        status = str(result.get("status", "")).strip()
        ord_id = str(result.get("ordId") or "").strip()
        manual_review = status not in {"accepted", "reconciled"} or not ord_id
        self.ledger.finish(
            proposal.signal_id,
            result,
            manual_review=manual_review,
            now=datetime.now(timezone.utc),
        )
        if manual_review:
            raise ManualReviewRequired("TradingService did not return a final accepted state")
        return ExecutionResult(
            signal_id=proposal.signal_id,
            intent_id=intent_id,
            status=status,
            ord_id=ord_id or None,
        )
