from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timezone

import pytest

from okx_demo_lab.audit import AuditStore, IntentIdentityConflict
from okx_demo_lab.models import OrderDraft
from okx_demo_lab.okx_client import (
    AmbiguousOrderError,
    CredentialIdentityError,
    DispatchBlockedError,
    OkxClientError,
)
from okx_demo_lab.service import TradingService, _account_fingerprint
from okx_demo_lab.state import SafetyController, SafetyError


CREDENTIAL_A = "a" * 64
CREDENTIAL_B = "b" * 64
ACCOUNT_CONFIG = [{"uid": "10001", "mainUid": "10001", "acctLv": "1"}]
ACCOUNT_A = _account_fingerprint(ACCOUNT_CONFIG)
ACCOUNT_B = "d" * 64


def tamper_first_audit_event(store: AuditStore) -> None:
    connection = sqlite3.connect(store.path)
    try:
        with connection:
            connection.execute(
                """
                UPDATE audit_events SET payload_json = '{"tampered":true}'
                WHERE id = (SELECT MIN(id) FROM audit_events)
                """
            )
    finally:
        connection.close()


class FakeOkxClient:
    def __init__(self) -> None:
        self.place_calls = 0
        self.credential_fingerprint = CREDENTIAL_A
        self.caa_calls: list[int] = []

    def current_credential_fingerprint(self) -> str:
        return self.credential_fingerprint

    def _check_identity(self, expected_credential_fingerprint: str | None) -> None:
        if (
            expected_credential_fingerprint
            and expected_credential_fingerprint != self.credential_fingerprint
        ):
            raise CredentialIdentityError("test credential mismatch")

    async def get_market_bundle(self, _: str) -> dict:
        return {
            "ticker": {
                "last": "64000.0",
                "ts": str(int(datetime.now(timezone.utc).timestamp() * 1000)),
            },
            "candles": [],
            "instrument": {
                "instId": "BTC-USDT",
                "instType": "SPOT",
                "state": "live",
                "tickSz": "0.1",
                "lotSz": "0.00001",
                "minSz": "0.00001",
            },
        }

    async def get_account_balance(
        self, *, expected_credential_fingerprint: str | None = None
    ) -> list[dict]:
        self._check_identity(expected_credential_fingerprint)
        return [
            {
                "totalEq": "100000",
                "details": [
                    {"ccy": "USDT", "availBal": "1000"},
                    {"ccy": "BTC", "availBal": "1"},
                ],
            }
        ]

    async def get_account_config(
        self, *, expected_credential_fingerprint: str | None = None
    ) -> list[dict]:
        self._check_identity(expected_credential_fingerprint)
        return ACCOUNT_CONFIG

    async def get_pending_orders(
        self,
        _: str = "BTC-USDT",
        *,
        expected_credential_fingerprint: str | None = None,
    ) -> list[dict]:
        self._check_identity(expected_credential_fingerprint)
        return []

    async def place_order(self, **values: object) -> dict:
        self._check_identity(values.get("expected_credential_fingerprint"))
        guard = values.get("dispatch_guard")
        if callable(guard):
            guard()
        self.place_calls += 1
        await asyncio.sleep(0.03)
        return {"sCode": "0", "ordId": "42"}

    async def cancel_order(
        self,
        _: str,
        __: str,
        *,
        expected_credential_fingerprint: str | None = None,
    ) -> dict:
        self._check_identity(expected_credential_fingerprint)
        return {"sCode": "0"}

    async def cancel_all_after(
        self,
        _: int,
        *,
        expected_credential_fingerprint: str | None = None,
    ) -> None:
        self._check_identity(expected_credential_fingerprint)
        self.caa_calls.append(_)


class BlockingResetClient(FakeOkxClient):
    def __init__(self) -> None:
        super().__init__()
        self.pending_started = asyncio.Event()
        self.pending_release = asyncio.Event()
        self.pending_calls = 0

    async def get_pending_orders(
        self,
        _: str = "BTC-USDT",
        *,
        expected_credential_fingerprint: str | None = None,
    ) -> list[dict]:
        self._check_identity(expected_credential_fingerprint)
        self.pending_calls += 1
        if self.pending_calls == 1:
            self.pending_started.set()
            await self.pending_release.wait()
        return []

    async def cancel_all_after(
        self,
        _: int,
        *,
        expected_credential_fingerprint: str | None = None,
    ) -> None:
        self._check_identity(expected_credential_fingerprint)
        return None


class CancellableDispatchClient(FakeOkxClient):
    def __init__(self) -> None:
        super().__init__()
        self.dispatch_started = asyncio.Event()

    async def place_order(self, **_: str) -> dict:
        self.place_calls += 1
        self.dispatch_started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class CancellableReconcileClient(FakeOkxClient):
    def __init__(self) -> None:
        super().__init__()
        self.reconcile_started = asyncio.Event()

    async def place_order(self, **_: str) -> dict:
        raise AmbiguousOrderError("test ambiguity")

    async def get_order(
        self,
        _: str,
        __: str,
        *,
        expected_credential_fingerprint: str | None = None,
    ) -> list[dict]:
        self._check_identity(expected_credential_fingerprint)
        self.reconcile_started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class ImmediateAmbiguousClient(FakeOkxClient):
    async def place_order(self, **_: object) -> dict:
        raise AmbiguousOrderError("test ambiguity")

    async def get_order(
        self,
        _: str,
        cl_ord_id: str,
        *,
        expected_credential_fingerprint: str | None = None,
    ) -> list[dict]:
        self._check_identity(expected_credential_fingerprint)
        return [{"ordId": "42", "clOrdId": cl_ord_id, "state": "live"}]


class CancellableArmClient(FakeOkxClient):
    def __init__(self) -> None:
        super().__init__()
        self.deadman_started = asyncio.Event()

    async def get_account_config(
        self, *, expected_credential_fingerprint: str | None = None
    ) -> list[dict]:
        self._check_identity(expected_credential_fingerprint)
        return ACCOUNT_CONFIG

    async def cancel_all_after(
        self,
        _: int,
        *,
        expected_credential_fingerprint: str | None = None,
    ) -> None:
        self._check_identity(expected_credential_fingerprint)
        self.deadman_started.set()
        await asyncio.Event().wait()


class ManualReviewClient(FakeOkxClient):
    def __init__(self) -> None:
        super().__init__()
        self.order_state = "live"
        self.order_id = "terminal-42"
        self.order_tag = "tideguarddemo"

    async def get_order(
        self,
        _: str,
        cl_ord_id: str,
        *,
        expected_credential_fingerprint: str | None = None,
    ) -> list[dict]:
        self._check_identity(expected_credential_fingerprint)
        return [
            {
                "instId": "BTC-USDT",
                "clOrdId": cl_ord_id,
                "ordId": self.order_id,
                "tag": self.order_tag,
                "state": self.order_state,
            }
        ]


class DispatchGateClient(FakeOkxClient):
    def __init__(self) -> None:
        super().__init__()
        self.before_guard = asyncio.Event()
        self.release_guard = asyncio.Event()

    async def place_order(self, **values: object) -> dict:
        self._check_identity(values.get("expected_credential_fingerprint"))
        self.before_guard.set()
        await self.release_guard.wait()
        guard = values.get("dispatch_guard")
        if callable(guard):
            guard()
        self.place_calls += 1
        return {"sCode": "0", "ordId": "too-late"}


class BlockingCaaClient(FakeOkxClient):
    def __init__(self) -> None:
        super().__init__()
        self.renewal_started = asyncio.Event()
        self.release_renewal = asyncio.Event()

    async def cancel_all_after(
        self,
        seconds: int,
        *,
        expected_credential_fingerprint: str | None = None,
    ) -> None:
        self._check_identity(expected_credential_fingerprint)
        if seconds == 20 and not self.renewal_started.is_set():
            self.renewal_started.set()
            await self.release_renewal.wait()
        self.caa_calls.append(seconds)


class AmbiguousCaaClient(FakeOkxClient):
    async def cancel_all_after(
        self,
        seconds: int,
        *,
        expected_credential_fingerprint: str | None = None,
    ) -> None:
        self._check_identity(expected_credential_fingerprint)
        self.caa_calls.append(seconds)
        if seconds == 20:
            raise OkxClientError("response lost after server acceptance")


@pytest.mark.parametrize(
    ("name", "value"),
    [("kill_active", "CORRUPT"), ("kill_generation", "not-a-number")],
)
def test_malformed_persistent_safety_state_blocks_arming(
    tmp_path, name: str, value: str
) -> None:
    store = AuditStore(tmp_path / "state.sqlite3")
    store.set_flag(name, value)
    safety = SafetyController(store)

    assert safety.status()["mode"] == "killed"
    assert safety.status()["killActive"] is True
    with pytest.raises(SafetyError, match="急停"):
        safety.arm("DEMO", CREDENTIAL_A, ACCOUNT_A)


@pytest.mark.asyncio
async def test_tampered_audit_chain_blocks_arm_and_persists_kill(tmp_path) -> None:
    store = AuditStore(tmp_path / "state.sqlite3")
    store.append("seed", {"safe": True})
    tamper_first_audit_event(store)
    assert not store.verify_chain()
    safety = SafetyController(store)
    service = TradingService(FakeOkxClient(), store, safety)  # type: ignore[arg-type]

    with pytest.raises(SafetyError, match="审计链"):
        await service.arm("DEMO")

    assert store.get_flag("kill_active") == "true"


def test_tampered_audit_chain_blocks_http_dispatch_and_persists_kill(tmp_path) -> None:
    store = AuditStore(tmp_path / "state.sqlite3")
    safety = SafetyController(store)
    safety.arm("DEMO", CREDENTIAL_A, ACCOUNT_A)
    service = TradingService(FakeOkxClient(), store, safety)  # type: ignore[arg-type]
    service._record_deadman_success()
    tamper_first_audit_event(store)
    assert not store.verify_chain()

    with pytest.raises(DispatchBlockedError, match="审计链"):
        service._dispatch_guard()

    assert store.get_flag("kill_active") == "true"


@pytest.mark.asyncio
async def test_concurrent_commits_dispatch_only_once(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("okx_demo_lab.service.credentials_configured", lambda: True)
    store = AuditStore(tmp_path / "state.sqlite3")
    safety = SafetyController(store)
    safety.arm("DEMO", CREDENTIAL_A, ACCOUNT_A)
    client = FakeOkxClient()
    service = TradingService(client, store, safety)  # type: ignore[arg-type]
    service._record_deadman_success()
    preview = await service.preview(
        OrderDraft(instId="BTC-USDT", side="buy", price="64000.0", size="0.00020")
    )

    first, second = await asyncio.gather(
        service.commit(preview["intentId"], preview["digest"], "a" * 16),
        service.commit(preview["intentId"], preview["digest"], "b" * 16),
        return_exceptions=True,
    )

    assert client.place_calls == 1
    assert sum(isinstance(item, ValueError) for item in (first, second)) == 1
    accepted = first if isinstance(first, dict) else second
    replay = await service.commit(preview["intentId"], preview["digest"], "a" * 16)
    if not isinstance(first, dict):
        replay = await service.commit(preview["intentId"], preview["digest"], "b" * 16)
    assert accepted["status"] == "accepted"
    assert replay["replayed"] is True


@pytest.mark.asyncio
async def test_emergency_linearizes_before_pending_http_dispatch(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr("okx_demo_lab.service.credentials_configured", lambda: True)
    store = AuditStore(tmp_path / "state.sqlite3")
    safety = SafetyController(store)
    safety.arm("DEMO", CREDENTIAL_A, ACCOUNT_A)
    client = DispatchGateClient()
    service = TradingService(client, store, safety)  # type: ignore[arg-type]
    service._record_deadman_success()
    preview = await service.preview(
        OrderDraft(instId="BTC-USDT", side="buy", price="64000.0", size="0.00020")
    )

    commit_task = asyncio.create_task(
        service.commit(preview["intentId"], preview["digest"], "g" * 16)
    )
    await client.before_guard.wait()
    emergency_task = asyncio.create_task(service.emergency_stop("test linearization"))
    await asyncio.sleep(0)
    client.release_guard.set()

    with pytest.raises(ValueError, match="发送前"):
        await commit_task
    await emergency_task
    assert client.place_calls == 0
    assert store.get_intent(preview["intentId"])["status"] == "blocked_before_dispatch"
    assert store.get_flag("kill_active") == "true"


@pytest.mark.asyncio
async def test_expired_local_deadman_lease_blocks_dispatch(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("okx_demo_lab.service.credentials_configured", lambda: True)
    clock = [100.0]
    monkeypatch.setattr("okx_demo_lab.service.time.monotonic", lambda: clock[0])
    store = AuditStore(tmp_path / "state.sqlite3")
    safety = SafetyController(store)
    safety.arm("DEMO", CREDENTIAL_A, ACCOUNT_A)
    client = FakeOkxClient()
    service = TradingService(client, store, safety)  # type: ignore[arg-type]
    service._record_deadman_success()
    preview = await service.preview(
        OrderDraft(instId="BTC-USDT", side="buy", price="64000.0", size="0.00020")
    )

    clock[0] += 13.0
    with pytest.raises(ValueError, match="发送前"):
        await service.commit(preview["intentId"], preview["digest"], "l" * 16)

    assert client.place_calls == 0
    assert store.get_flag("kill_active") == "true"


@pytest.mark.asyncio
async def test_emergency_does_not_postpone_near_deadline_caa(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("okx_demo_lab.service.credentials_configured", lambda: True)
    clock = [100.0]
    monkeypatch.setattr("okx_demo_lab.service.time.monotonic", lambda: clock[0])
    store = AuditStore(tmp_path / "state.sqlite3")
    safety = SafetyController(store)
    safety.arm("DEMO", CREDENTIAL_A, ACCOUNT_A)
    client = FakeOkxClient()
    service = TradingService(client, store, safety)  # type: ignore[arg-type]
    service._record_deadman_success()

    clock[0] = 119.0
    await service.emergency_stop("near exchange deadline")

    assert client.caa_calls == []
    assert store.get_flag("kill_active") == "true"


@pytest.mark.asyncio
async def test_emergency_is_final_caa_writer_after_inflight_renewal(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr("okx_demo_lab.service.credentials_configured", lambda: True)
    clock = [100.0]
    monkeypatch.setattr("okx_demo_lab.service.time.monotonic", lambda: clock[0])
    store = AuditStore(tmp_path / "state.sqlite3")
    safety = SafetyController(store)
    safety.arm("DEMO", CREDENTIAL_A, ACCOUNT_A)
    client = BlockingCaaClient()
    service = TradingService(client, store, safety)  # type: ignore[arg-type]
    waits: list[float] = []

    async def advance_clock(delay: float) -> None:
        waits.append(delay)
        clock[0] += delay

    service._caa_sleep = advance_clock
    service._record_deadman_success()

    renewal = asyncio.create_task(service.renew_deadman())
    await client.renewal_started.wait()
    emergency = asyncio.create_task(service.emergency_stop("during renewal"))
    await asyncio.sleep(0)
    client.release_renewal.set()

    with pytest.raises(DispatchBlockedError):
        await renewal
    await emergency
    assert client.caa_calls == [20, 10]
    assert client.caa_calls[-1] == 10
    assert waits == pytest.approx([1.05])
    assert store.get_flag("kill_active") == "true"


@pytest.mark.asyncio
async def test_emergency_overwrites_ambiguous_normal_caa_with_shorter_timeout(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr("okx_demo_lab.service.credentials_configured", lambda: True)
    clock = [100.0]
    monkeypatch.setattr("okx_demo_lab.service.time.monotonic", lambda: clock[0])
    store = AuditStore(tmp_path / "state.sqlite3")
    safety = SafetyController(store)
    safety.arm("DEMO", CREDENTIAL_A, ACCOUNT_A)
    client = AmbiguousCaaClient()
    service = TradingService(client, store, safety)  # type: ignore[arg-type]

    async def advance_clock(delay: float) -> None:
        clock[0] += delay

    service._caa_sleep = advance_clock
    service._record_deadman_success()
    clock[0] = 119.0

    with pytest.raises(OkxClientError, match="response lost"):
        await service.renew_deadman()
    await service.emergency_stop("ambiguous renewal")

    assert client.caa_calls == [20, 10]
    assert store.get_flag("kill_active") == "true"


@pytest.mark.asyncio
async def test_caa_writes_are_throttled_before_emergency(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("okx_demo_lab.service.credentials_configured", lambda: True)
    clock = [100.0]
    monkeypatch.setattr("okx_demo_lab.service.time.monotonic", lambda: clock[0])
    store = AuditStore(tmp_path / "state.sqlite3")
    safety = SafetyController(store)
    safety.arm("DEMO", CREDENTIAL_A, ACCOUNT_A)
    client = FakeOkxClient()
    service = TradingService(client, store, safety)  # type: ignore[arg-type]
    accepted_at: list[float] = []
    waits: list[float] = []

    async def rate_limited_caa(
        seconds: int,
        *,
        expected_credential_fingerprint: str | None = None,
    ) -> None:
        client._check_identity(expected_credential_fingerprint)
        if accepted_at and clock[0] - accepted_at[-1] < 1.0:
            raise OkxClientError("51071")
        accepted_at.append(clock[0])
        client.caa_calls.append(seconds)

    async def advance_clock(delay: float) -> None:
        waits.append(delay)
        clock[0] += delay

    monkeypatch.setattr(client, "cancel_all_after", rate_limited_caa)
    service._caa_sleep = advance_clock
    service._record_deadman_success()

    await service.renew_deadman()
    await service.emergency_stop("rate-limit boundary")

    assert client.caa_calls == [20, 10]
    assert accepted_at[1] - accepted_at[0] >= 1.0
    assert waits == pytest.approx([1.05])
    assert store.get_flag("kill_active") == "true"


def test_startup_recovery_marks_unresolved_intent_for_review(tmp_path) -> None:
    store = AuditStore(tmp_path / "state.sqlite3")
    store.save_intent(
        {
            "intent_id": "intent-1",
            "created_at": "2026-08-14T00:00:00Z",
            "expires_at": "2026-08-14T00:01:00Z",
            "payload_json": "{}",
            "decision_json": "{}",
            "digest": "0" * 64,
            "cl_ord_id": "tg-recovery",
            "status": "previewed",
            "credential_fingerprint": CREDENTIAL_A,
            "account_fingerprint": ACCOUNT_A,
        }
    )
    assert store.claim_intent("intent-1", "k" * 16)
    recovered = store.recover_unresolved_intents()
    assert recovered == [
        {"intent_id": "intent-1", "cl_ord_id": "tg-recovery", "status": "dispatching"}
    ]
    assert store.get_intent("intent-1")["status"] == "manual_review"
    assert store.get_flag("kill_active") == "true"
    assert store.has_manual_reviews()
    assert store.has_dispatched_intents()
    generation = store.get_kill_generation()
    assert store.close_manual_reviews_and_clear_kill(generation)
    assert store.get_intent("intent-1")["status"] == "review_closed"
    assert store.get_flag("kill_active") == "false"
    assert not store.has_manual_reviews()
    assert store.has_dispatched_intents()
    assert store.recover_unresolved_intents() == []


@pytest.mark.asyncio
async def test_emergency_stop_cannot_be_overwritten_by_concurrent_reset(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("okx_demo_lab.service.credentials_configured", lambda: True)
    store = AuditStore(tmp_path / "state.sqlite3")
    safety = SafetyController(store)
    safety.engage_kill("test setup")
    client = BlockingResetClient()
    service = TradingService(client, store, safety)  # type: ignore[arg-type]

    reset_task = asyncio.create_task(service.reset_kill("解除模拟盘急停"))
    await client.pending_started.wait()
    emergency_task = asyncio.create_task(service.emergency_stop("concurrent emergency"))
    await asyncio.sleep(0)
    client.pending_release.set()
    with pytest.raises(SafetyError):
        await reset_task
    await emergency_task

    assert store.get_flag("kill_active") == "true"
    assert safety.status()["mode"] == "killed"


@pytest.mark.asyncio
async def test_emergency_epoch_defeats_reset_when_first_latch_write_fails(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr("okx_demo_lab.service.credentials_configured", lambda: True)
    store = AuditStore(tmp_path / "state.sqlite3")
    safety = SafetyController(store)
    safety.engage_kill("test setup")
    client = BlockingResetClient()
    service = TradingService(client, store, safety)  # type: ignore[arg-type]
    original_engage = store.engage_kill_latch
    attempts = 0

    def fail_once(
        credential_fingerprint: str | None = None,
        account_fingerprint: str | None = None,
    ) -> int:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("simulated first latch failure")
        return original_engage(credential_fingerprint, account_fingerprint)

    monkeypatch.setattr(store, "engage_kill_latch", fail_once)
    reset_task = asyncio.create_task(service.reset_kill("解除模拟盘急停"))
    await client.pending_started.wait()
    emergency_task = asyncio.create_task(service.emergency_stop("concurrent emergency"))
    await asyncio.sleep(0)
    client.pending_release.set()

    with pytest.raises(SafetyError, match="新的急停"):
        await reset_task
    await emergency_task
    assert attempts >= 2
    assert service._kill_requested.is_set()
    assert store.get_flag("kill_active") == "true"
    assert safety.status()["mode"] == "killed"


@pytest.mark.asyncio
async def test_cancelled_emergency_wait_still_leaves_persistent_kill(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("okx_demo_lab.service.credentials_configured", lambda: False)
    store = AuditStore(tmp_path / "state.sqlite3")
    safety = SafetyController(store)
    service = TradingService(FakeOkxClient(), store, safety)  # type: ignore[arg-type]
    await service._trade_lock.acquire()
    try:
        task = asyncio.create_task(service.emergency_stop("cancel while queued"))
        await asyncio.sleep(0)
        assert store.get_flag("kill_active") == "true"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        service._trade_lock.release()

    assert store.get_flag("kill_active") == "true"
    assert safety.status()["mode"] == "killed"


@pytest.mark.asyncio
async def test_cancelled_dispatch_is_locked_for_manual_review(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("okx_demo_lab.service.credentials_configured", lambda: True)
    store = AuditStore(tmp_path / "state.sqlite3")
    safety = SafetyController(store)
    safety.arm("DEMO", CREDENTIAL_A, ACCOUNT_A)
    client = CancellableDispatchClient()
    service = TradingService(client, store, safety)  # type: ignore[arg-type]
    service._record_deadman_success()
    preview = await service.preview(
        OrderDraft(instId="BTC-USDT", side="buy", price="64000.0", size="0.00020")
    )
    task = asyncio.create_task(
        service.commit(preview["intentId"], preview["digest"], "c" * 16)
    )
    await client.dispatch_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert store.get_intent(preview["intentId"])["status"] == "manual_review"
    assert store.get_flag("kill_active") == "true"


@pytest.mark.asyncio
async def test_cancelled_reconciliation_is_locked_for_manual_review(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("okx_demo_lab.service.credentials_configured", lambda: True)
    store = AuditStore(tmp_path / "state.sqlite3")
    safety = SafetyController(store)
    safety.arm("DEMO", CREDENTIAL_A, ACCOUNT_A)
    client = CancellableReconcileClient()
    service = TradingService(client, store, safety)  # type: ignore[arg-type]
    service._record_deadman_success()
    preview = await service.preview(
        OrderDraft(instId="BTC-USDT", side="buy", price="64000.0", size="0.00020")
    )
    task = asyncio.create_task(
        service.commit(preview["intentId"], preview["digest"], "d" * 16)
    )
    await client.reconcile_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert store.get_intent(preview["intentId"])["status"] == "manual_review"
    assert store.get_flag("kill_active") == "true"


@pytest.mark.asyncio
async def test_reconciliation_local_failure_is_fail_closed(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("okx_demo_lab.service.credentials_configured", lambda: True)
    store = AuditStore(tmp_path / "state.sqlite3")
    safety = SafetyController(store)
    safety.arm("DEMO", CREDENTIAL_A, ACCOUNT_A)
    service = TradingService(ImmediateAmbiguousClient(), store, safety)  # type: ignore[arg-type]
    service._record_deadman_success()
    preview = await service.preview(
        OrderDraft(instId="BTC-USDT", side="buy", price="64000.0", size="0.00020")
    )

    monkeypatch.setattr(
        store,
        "mark_manual_review_and_kill",
        lambda _: (_ for _ in ()).throw(OSError("simulated local failure")),
    )
    with pytest.raises(OkxClientError, match="本地记录失败"):
        await service.commit(preview["intentId"], preview["digest"], "u" * 16)

    assert store.get_flag("kill_active") == "true"
    assert service._kill_requested.is_set()


@pytest.mark.asyncio
async def test_cancelled_deadman_start_engages_kill(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("okx_demo_lab.service.credentials_configured", lambda: True)
    store = AuditStore(tmp_path / "state.sqlite3")
    safety = SafetyController(store)
    client = CancellableArmClient()
    service = TradingService(client, store, safety)  # type: ignore[arg-type]
    task = asyncio.create_task(service.arm("DEMO"))
    await client.deadman_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert safety.status()["mode"] == "killed"
    assert store.get_flag("kill_active") == "true"


@pytest.mark.asyncio
async def test_arm_audit_failure_cannot_leave_memory_armed(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("okx_demo_lab.service.credentials_configured", lambda: True)
    store = AuditStore(tmp_path / "state.sqlite3")
    original_append = store.append

    def faulty_append(event_type: str, *args, **kwargs):
        if event_type == "safety.armed":
            raise OSError("simulated audit failure")
        return original_append(event_type, *args, **kwargs)

    monkeypatch.setattr(store, "append", faulty_append)
    safety = SafetyController(store)
    service = TradingService(FakeOkxClient(), store, safety)  # type: ignore[arg-type]

    with pytest.raises(OSError, match="audit failure"):
        await service.arm("DEMO")

    assert safety.status()["mode"] == "killed"
    assert store.get_flag("kill_active") == "true"
    assert service._kill_requested.is_set()


@pytest.mark.asyncio
async def test_reset_requires_credentials_after_any_dispatch_history(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("okx_demo_lab.service.credentials_configured", lambda: False)
    store = AuditStore(tmp_path / "state.sqlite3")
    store.save_intent(
        {
            "intent_id": "historical-dispatch",
            "created_at": "2026-08-14T00:00:00Z",
            "expires_at": "2026-08-14T00:01:00Z",
            "payload_json": "{}",
            "decision_json": "{}",
            "digest": "0" * 64,
            "cl_ord_id": "tg-history",
            "status": "previewed",
            "credential_fingerprint": CREDENTIAL_A,
            "account_fingerprint": ACCOUNT_A,
        }
    )
    assert store.claim_intent("historical-dispatch", "h" * 16)
    safety = SafetyController(store)
    safety.engage_kill("test")
    service = TradingService(FakeOkxClient(), store, safety)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="恢复原模拟盘凭证"):
        await service.reset_kill("解除模拟盘急停")

    assert safety.status()["mode"] == "killed"


@pytest.mark.asyncio
async def test_commit_fails_closed_after_credential_swap(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("okx_demo_lab.service.credentials_configured", lambda: True)
    store = AuditStore(tmp_path / "state.sqlite3")
    safety = SafetyController(store)
    safety.arm("DEMO", CREDENTIAL_A, ACCOUNT_A)
    client = FakeOkxClient()
    service = TradingService(client, store, safety)  # type: ignore[arg-type]
    service._record_deadman_success()
    preview = await service.preview(
        OrderDraft(instId="BTC-USDT", side="buy", price="64000.0", size="0.00020")
    )

    client.credential_fingerprint = CREDENTIAL_B
    with pytest.raises(CredentialIdentityError, match="身份已变化"):
        await service.commit(preview["intentId"], preview["digest"], "s" * 16)

    assert client.place_calls == 0
    assert store.get_flag("kill_active") == "true"


@pytest.mark.asyncio
async def test_reset_rejects_different_credential_identity(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("okx_demo_lab.service.credentials_configured", lambda: True)
    store = AuditStore(tmp_path / "state.sqlite3")
    store.engage_kill_latch(CREDENTIAL_A, ACCOUNT_A)
    safety = SafetyController(store)
    client = FakeOkxClient()
    client.credential_fingerprint = CREDENTIAL_B
    service = TradingService(client, store, safety)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="不是触发急停时绑定"):
        await service.reset_kill("解除模拟盘急停")

    assert store.get_flag("kill_active") == "true"


@pytest.mark.asyncio
async def test_manual_review_requires_verified_terminal_order(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("okx_demo_lab.service.credentials_configured", lambda: True)
    store = AuditStore(tmp_path / "state.sqlite3")
    store.save_intent(
        {
            "intent_id": "manual-1",
            "created_at": "2026-08-14T00:00:00Z",
            "expires_at": "2026-08-14T00:01:00Z",
            "payload_json": "{}",
            "decision_json": "{}",
            "digest": "0" * 64,
            "cl_ord_id": "tg-manual-1",
            "status": "previewed",
            "credential_fingerprint": CREDENTIAL_A,
            "account_fingerprint": ACCOUNT_A,
        }
    )
    assert store.claim_intent("manual-1", "m" * 16)
    store.mark_manual_review_and_kill("manual-1")
    safety = SafetyController(store)
    client = ManualReviewClient()
    service = TradingService(client, store, safety)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="终态"):
        await service.reset_kill("解除模拟盘急停")
    assert store.get_flag("kill_active") == "true"

    client.order_state = "canceled"
    result = await service.reset_kill("解除模拟盘急停")
    assert result["mode"] == "observe"
    assert store.get_intent("manual-1")["status"] == "terminal_verified"
    assert store.get_flag("kill_active") == "false"


@pytest.mark.asyncio
async def test_accepted_order_requires_terminal_query_even_when_pending_is_empty(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr("okx_demo_lab.service.credentials_configured", lambda: True)
    store = AuditStore(tmp_path / "state.sqlite3")
    store.save_intent(
        {
            "intent_id": "accepted-1",
            "created_at": "2026-08-14T00:00:00Z",
            "expires_at": "2026-08-14T00:01:00Z",
            "payload_json": "{}",
            "decision_json": "{}",
            "digest": "0" * 64,
            "cl_ord_id": "tg-accepted-1",
            "status": "previewed",
            "credential_fingerprint": CREDENTIAL_A,
            "account_fingerprint": ACCOUNT_A,
        }
    )
    assert store.claim_intent("accepted-1", "z" * 16)
    store.update_intent("accepted-1", status="accepted", okx_ord_id="live-42")
    store.engage_kill_latch(CREDENTIAL_A, ACCOUNT_A)
    safety = SafetyController(store)
    client = ManualReviewClient()
    service = TradingService(client, store, safety)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="终态"):
        await service.reset_kill("解除模拟盘急停")

    assert store.get_flag("kill_active") == "true"
    assert store.get_intent("accepted-1")["status"] == "accepted"

    client.order_state = "canceled"
    with pytest.raises(ValueError, match="终态"):
        await service.reset_kill("解除模拟盘急停")
    assert store.get_intent("accepted-1")["okx_ord_id"] == "live-42"

    client.order_id = "live-42"
    client.order_tag = "other-app"
    with pytest.raises(ValueError, match="终态"):
        await service.reset_kill("解除模拟盘急停")
    assert store.get_flag("kill_active") == "true"

    client.order_tag = "tideguarddemo"
    result = await service.reset_kill("解除模拟盘急停")
    assert result["mode"] == "observe"
    assert store.get_intent("accepted-1")["status"] == "terminal_verified"


def test_dispatch_claim_serializes_account_identity(tmp_path) -> None:
    store = AuditStore(tmp_path / "state.sqlite3")
    base = {
        "created_at": "2026-08-14T00:00:00Z",
        "expires_at": "2026-08-14T00:01:00Z",
        "payload_json": "{}",
        "decision_json": "{}",
        "digest": "0" * 64,
        "status": "previewed",
    }
    store.save_intent(
        {
            **base,
            "intent_id": "account-a",
            "cl_ord_id": "tg-account-a",
            "credential_fingerprint": CREDENTIAL_A,
            "account_fingerprint": ACCOUNT_A,
        }
    )
    store.save_intent(
        {
            **base,
            "intent_id": "account-b",
            "cl_ord_id": "tg-account-b",
            "credential_fingerprint": CREDENTIAL_B,
            "account_fingerprint": ACCOUNT_B,
        }
    )

    assert store.claim_intent("account-a", "a" * 16)
    with pytest.raises(IntentIdentityConflict, match="其他模拟账户"):
        store.claim_intent("account-b", "b" * 16)

    assert store.get_intent("account-b")["status"] == "previewed"


@pytest.mark.asyncio
async def test_kill_and_reset_fail_closed_for_multiple_account_identities(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr("okx_demo_lab.service.credentials_configured", lambda: True)
    store = AuditStore(tmp_path / "state.sqlite3")
    base = {
        "created_at": "2026-08-14T00:00:00Z",
        "expires_at": "2026-08-14T00:01:00Z",
        "payload_json": "{}",
        "decision_json": "{}",
        "digest": "0" * 64,
        "status": "previewed",
    }
    for suffix, credential, account in (
        ("a", CREDENTIAL_A, ACCOUNT_A),
        ("b", CREDENTIAL_B, ACCOUNT_B),
    ):
        store.save_intent(
            {
                **base,
                "intent_id": f"mixed-{suffix}",
                "cl_ord_id": f"tg-mixed-{suffix}",
                "credential_fingerprint": credential,
                "account_fingerprint": account,
            }
        )
        store.update_intent(f"mixed-{suffix}", status="accepted")
    store.engage_kill_latch(CREDENTIAL_A, ACCOUNT_A)
    safety = SafetyController(store)
    service = TradingService(FakeOkxClient(), store, safety)  # type: ignore[arg-type]

    result = await service.emergency_stop("mixed identity test")
    assert result["remainingAppOrders"] is None
    assert result["failures"] >= 1
    with pytest.raises(ValueError, match="唯一账户身份"):
        await service.reset_kill("解除模拟盘急停")
    assert store.get_flag("kill_active") == "true"
