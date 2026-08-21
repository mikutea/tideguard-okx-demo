from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from okx_demo_lab.audit import AuditStore
from okx_demo_lab.environment import (
    EnvironmentAcknowledgements,
    EnvironmentSelectionStore,
    EnvironmentSwitchError,
    RuntimeEnvironment,
    SwitchChallengeStore,
)
from okx_demo_lab.models import OrderDraft
from okx_demo_lab.okx_client import CredentialIdentityError, OkxClient
from okx_demo_lab.profile import DEMO_PROFILE, LIVE_PROFILE
from okx_demo_lab.secrets import (
    Credentials,
    credential_fingerprint,
    get_credentials,
    set_credentials,
)
from okx_demo_lab.service import EnvironmentSwitchService, TradingService
from okx_demo_lab.state import SafetyController, SafetyError


class FakeAutonomy:
    def __init__(self) -> None:
        self.position: dict[str, Any] | None = None
        self.desired_mode = "demo"

    def disable_master(self, reason: str, *, now: datetime) -> dict[str, Any]:
        del reason, now
        self.desired_mode = "disabled"
        return {"desiredMode": "disabled", "runtimeStatus": "disabled"}

    def active_position(self) -> dict[str, Any] | None:
        return self.position


class FakeProfileClient:
    def __init__(
        self,
        profile=DEMO_PROFILE,
        *,
        permissions: str = "read_only,trade",
        ip: str | None = None,
        pending: list[dict[str, Any]] | None = None,
    ) -> None:
        self.profile = profile
        self.policy = None
        self.fingerprint = ("d" if profile.name == "demo" else "e") * 64
        self.permissions = permissions
        if ip is None:
            self.ip = "" if profile.name == "demo" else "203.0.113.4"
        else:
            self.ip = ip
        self.pending = list(pending or [])
        self.caa_calls: list[int] = []
        self.closed = False

    def current_credential_fingerprint(self) -> str:
        return self.fingerprint

    def _check(self, expected: str | None) -> None:
        if expected and expected != self.fingerprint:
            raise CredentialIdentityError("mock identity mismatch")

    async def get_account_config(
        self, *, expected_credential_fingerprint: str | None = None
    ) -> list[dict[str, Any]]:
        self._check(expected_credential_fingerprint)
        suffix = "1" if self.profile.name == "demo" else "2"
        return [
            {
                "uid": f"1000{suffix}",
                "mainUid": f"1000{suffix}",
                "acctLv": "1",
                "perm": self.permissions,
                "ip": self.ip,
            }
        ]

    async def get_pending_orders(
        self,
        inst_id: str | None = "BTC-USDT",
        *,
        expected_credential_fingerprint: str | None = None,
    ) -> list[dict[str, Any]]:
        assert inst_id in {None, "BTC-USDT"}
        self._check(expected_credential_fingerprint)
        if inst_id is None:
            return list(self.pending)
        return [
            order
            for order in self.pending
            if not order.get("instId") or order.get("instId") == inst_id
        ]

    async def cancel_all_after(
        self,
        seconds: int,
        *,
        expected_credential_fingerprint: str | None = None,
    ) -> None:
        self._check(expected_credential_fingerprint)
        self.caa_calls.append(seconds)

    async def get_market_bundle(self, inst_id: str) -> dict[str, Any]:
        assert inst_id == "BTC-USDT"
        return {
            "ticker": {
                "last": "100",
                "ts": str(int(datetime.now(timezone.utc).timestamp() * 1_000)),
            },
            "candles": [],
            "instrument": {
                "instId": "BTC-USDT",
                "instType": "SPOT",
                "state": "live",
                "tickSz": "0.1",
                "lotSz": "0.01",
                "minSz": "0.01",
            },
        }

    async def get_account_balance(
        self, *, expected_credential_fingerprint: str | None = None
    ) -> list[dict[str, Any]]:
        self._check(expected_credential_fingerprint)
        return [
            {
                "totalEq": "100000",
                "details": [
                    {"ccy": "USDT", "availBal": "100000"},
                    {"ccy": "BTC", "availBal": "10"},
                ],
            }
        ]

    async def close(self) -> None:
        self.closed = True


def _switch_service(
    root: Path,
    *,
    clock: list[datetime] | None = None,
    target_client_factory=None,
) -> tuple[
    EnvironmentSwitchService,
    RuntimeEnvironment,
    AuditStore,
    AuditStore,
    FakeAutonomy,
]:
    selector = EnvironmentSelectionStore(root / "environment.json")
    runtime = RuntimeEnvironment(selector)
    current_audit = AuditStore(root / "state.sqlite3")
    current_autonomy = FakeAutonomy()
    current_client = FakeProfileClient(DEMO_PROFILE)
    safety = SafetyController(current_audit, DEMO_PROFILE)
    trading = TradingService(
        current_client,  # type: ignore[arg-type]
        current_audit,
        safety,
        execution_environment_guard=runtime.assert_execution_allowed,
    )
    target_audit = AuditStore(root / "live" / "state.sqlite3")
    target_autonomy = FakeAutonomy()

    async def stop_legacy(_: str) -> dict[str, bool]:
        return {"stopped": True}

    target_factory = target_client_factory or (
        lambda profile: FakeProfileClient(profile)
    )
    challenges = (
        SwitchChallengeStore(now=lambda: clock[0]) if clock is not None else None
    )
    switcher = EnvironmentSwitchService(
        runtime=runtime,
        data_root=root,
        trading=trading,
        audit=current_audit,
        autonomy=current_autonomy,
        client_factory=target_factory,
        stop_legacy_automation=stop_legacy,
        challenges=challenges,
    )
    switcher._target_stores = lambda _: (target_audit, target_autonomy)  # type: ignore[method-assign]
    return switcher, runtime, current_audit, target_audit, current_autonomy


def test_selector_defaults_to_demo_and_requires_restart_after_persist(
    tmp_path: Path,
) -> None:
    selector = EnvironmentSelectionStore(tmp_path / "environment.json")
    runtime = RuntimeEnvironment(selector)

    assert runtime.status()["activeEnvironment"] == "demo"
    assert runtime.status()["restartRequired"] is False

    selector.persist(LIVE_PROFILE, switch_id="env_" + "a" * 28)
    assert runtime.status()["activeEnvironment"] == "demo"
    assert runtime.status()["configuredEnvironment"] == "live"
    assert runtime.status()["restartRequired"] is True
    with pytest.raises(EnvironmentSwitchError, match="重启"):
        runtime.assert_execution_allowed()

    restarted = RuntimeEnvironment(selector)
    assert restarted.status()["activeEnvironment"] == "live"
    assert restarted.status()["restartRequired"] is False


def test_runtime_transition_gate_is_monotonic_until_restart(tmp_path: Path) -> None:
    runtime = RuntimeEnvironment(EnvironmentSelectionStore(tmp_path / "environment.json"))

    state = runtime.begin_transition(LIVE_PROFILE)

    assert state["transitionPending"] is True
    assert state["transitionTarget"] == "live"
    assert state["operatingMode"] == "transition_locked"
    with pytest.raises(EnvironmentSwitchError, match="最终核对"):
        runtime.assert_execution_allowed()
    with pytest.raises(EnvironmentSwitchError, match="正在进行"):
        runtime.begin_transition(LIVE_PROFILE)


def test_corrupt_selector_fails_closed_to_demo(tmp_path: Path) -> None:
    path = tmp_path / "environment.json"
    path.write_text('{"environment":"live"}', encoding="utf-8")

    runtime = RuntimeEnvironment(EnvironmentSelectionStore(path))

    assert runtime.active_profile.name == "demo"
    assert runtime.status()["selectorValid"] is False
    with pytest.raises(EnvironmentSwitchError, match="无效"):
        runtime.assert_execution_allowed()


def test_demo_and_live_credentials_use_separate_vault_services(monkeypatch) -> None:
    vault: dict[tuple[str, str], str] = {}
    monkeypatch.setattr("okx_demo_lab.secrets._assert_windows_native_backend", lambda: None)
    monkeypatch.setattr(
        "okx_demo_lab.secrets.keyring.set_password",
        lambda service, name, value: vault.__setitem__((service, name), value),
    )
    monkeypatch.setattr(
        "okx_demo_lab.secrets.keyring.get_password",
        lambda service, name: vault.get((service, name)),
    )
    demo = Credentials("demo-key", "demo-secret", "demo-pass")
    live = Credentials("live-key", "live-secret", "live-pass")

    set_credentials(demo, DEMO_PROFILE)
    set_credentials(live, LIVE_PROFILE)

    assert get_credentials(DEMO_PROFILE) == demo
    assert get_credentials(LIVE_PROFILE) == live
    assert credential_fingerprint(demo, DEMO_PROFILE) != credential_fingerprint(
        demo, LIVE_PROFILE
    )
    assert {service for service, _ in vault} == {
        "Tideguard.OKX.Demo",
        "Tideguard.OKX.Live",
    }


@pytest.mark.asyncio
async def test_live_profile_never_sends_simulated_header_and_uses_live_tag() -> None:
    captured: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if request.url.path == "/api/v5/trade/order":
            body = request.content.decode("utf-8")
            assert '"tag":"tideguardlive"' in body
            return httpx.Response(
                200,
                json={
                    "code": "0",
                    "msg": "",
                    "data": [
                        {
                            "sCode": "0",
                            "ordId": "42",
                            "clOrdId": "tg-live",
                            "tag": "tideguardlive",
                        }
                    ],
                },
            )
        return httpx.Response(
            200, json={"code": "0", "msg": "", "data": [{"acctLv": "1"}]}
        )

    client = OkxClient(
        profile=LIVE_PROFILE,
        credentials_provider=lambda: Credentials("live", "secret", "pass"),
        transport=httpx.MockTransport(handler),
    )
    try:
        await client.public_get("/api/v5/public/time")
        await client.get_account_config()
        await client.place_order(
            inst_id="BTC-USDT",
            side="buy",
            ord_type="limit",
            price="100",
            size="0.01",
            cl_ord_id="tg-live",
        )
    finally:
        await client.close()

    assert captured
    assert all("x-simulated-trading" not in request.headers for request in captured)
    assert all(request.url.host == "openapi.okx.com" for request in captured)


def test_live_and_demo_manual_arm_phrases_are_not_interchangeable(
    tmp_path: Path,
) -> None:
    demo = SafetyController(AuditStore(tmp_path / "demo.sqlite3"), DEMO_PROFILE)
    live = SafetyController(AuditStore(tmp_path / "live.sqlite3"), LIVE_PROFILE)

    with pytest.raises(SafetyError):
        demo.arm("我确认使用真实资金", "a" * 64, "b" * 64)
    with pytest.raises(SafetyError):
        live.arm("DEMO", "c" * 64, "d" * 64)

    live_state = live.arm("我确认使用真实资金", "c" * 64, "d" * 64)
    assert live_state["mode"] == "armed"
    assert 0 < int(live_state["armedRemainingSeconds"]) <= 60
    with pytest.raises(SafetyError, match="独立实盘授权"):
        live.arm_supervised(
            "sup_" + "a" * 28,
            "c" * 64,
            "d" * 64,
            purpose="entry",
        )


@pytest.mark.asyncio
async def test_live_manual_arm_uses_deadman_and_strict_live_risk(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "okx_demo_lab.service.credentials_configured", lambda *args: True
    )
    client = FakeProfileClient(LIVE_PROFILE)
    audit = AuditStore(tmp_path / "live-state.sqlite3")
    safety = SafetyController(audit, LIVE_PROFILE)
    service = TradingService(client, audit, safety)  # type: ignore[arg-type]

    state = await service.arm("我确认使用真实资金")

    assert state["mode"] == "armed"
    assert client.caa_calls == [service.policy.deadman_seconds]
    assert service.policy.max_order_notional_usdt == Decimal("10")
    assert service.policy.max_order_equity_fraction == Decimal("0.0005")
    assert service.policy.max_open_orders == 1
    assert service.policy.automation_arm_ttl_seconds <= 60

    rejected = await service.preview(
        OrderDraft(
            instId="BTC-USDT",
            side="buy",
            ordType="limit",
            price=Decimal("100"),
            size=Decimal("0.11"),
        )
    )
    assert rejected["decision"]["allowed"] is False
    assert rejected["decision"]["policyVersion"] == "live-v1-observe-locked"
    assert "single_order" in rejected["decision"]["reasonCodes"]

    client.pending = [{"ordId": "1", "tag": "external"}]
    pending_rejected = await service.preview(
        OrderDraft(
            instId="BTC-USDT",
            side="buy",
            ordType="limit",
            price=Decimal("100"),
            size=Decimal("0.01"),
        )
    )
    assert "open_orders" in pending_rejected["decision"]["reasonCodes"]
    client.pending = []

    client.fingerprint = "f" * 64
    with pytest.raises(CredentialIdentityError):
        await service.preview(
            OrderDraft(
                instId="BTC-USDT",
                side="buy",
                ordType="limit",
                price=Decimal("100"),
                size=Decimal("0.01"),
            )
        )
    assert safety.status()["killActive"] is True


@pytest.mark.asyncio
async def test_live_arm_rejects_withdraw_missing_ip_or_non_spot(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "okx_demo_lab.service.credentials_configured", lambda *args: True
    )
    cases = [
        FakeProfileClient(LIVE_PROFILE, permissions="read_only,trade,withdraw"),
        FakeProfileClient(LIVE_PROFILE, ip=""),
    ]
    for index, client in enumerate(cases):
        audit = AuditStore(tmp_path / f"state-{index}.sqlite3")
        service = TradingService(
            client, audit, SafetyController(audit, LIVE_PROFILE)  # type: ignore[arg-type]
        )
        with pytest.raises(EnvironmentSwitchError):
            await service.arm("我确认使用真实资金")


@pytest.mark.asyncio
async def test_switch_challenge_is_delayed_one_time_and_locks_both_profiles(
    tmp_path: Path,
) -> None:
    clock = [datetime(2026, 8, 21, 0, 0, tzinfo=timezone.utc)]
    switcher, runtime, current_audit, target_audit, _ = _switch_service(
        tmp_path, clock=clock
    )

    challenge = await switcher.challenge("live")
    assert challenge["preflight"]["allowed"] is True
    assert challenge["confirmationPhrase"] == "切换到 OKX 实盘"

    acknowledgements = EnvironmentAcknowledgements(
        automationStopped=True,
        noOutstandingState=True,
        restartRequired=True,
        liveFundsAtRisk=True,
    )
    with pytest.raises(EnvironmentSwitchError, match="冷静期"):
        await switcher.confirm(
            target_name="live",
            nonce=challenge["nonce"],
            confirmation="切换到 OKX 实盘",
            acknowledgements=acknowledgements,
        )

    clock[0] += timedelta(seconds=10)
    result = await switcher.confirm(
        target_name="live",
        nonce=challenge["nonce"],
        confirmation="切换到 OKX 实盘",
        acknowledgements=acknowledgements,
    )

    assert result["activeEnvironment"] == "demo"
    assert result["configuredEnvironment"] == "live"
    assert result["restartRequired"] is True
    assert result["operatingMode"] == "observe"
    assert result["killActive"] is True
    assert current_audit.get_kill_state()[0] is True
    assert target_audit.get_kill_state()[0] is True
    assert current_audit.get_kill_identity() is not None
    assert target_audit.get_kill_identity() is not None
    with pytest.raises(EnvironmentSwitchError, match="已使用"):
        await switcher.confirm(
            target_name="live",
            nonce=challenge["nonce"],
            confirmation="切换到 OKX 实盘",
            acknowledgements=acknowledgements,
        )
    assert runtime.status()["restartRequired"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("permissions", "ip"),
    [("read_only,trade,withdraw", "203.0.113.4"), ("read_only,trade", "")],
)
async def test_live_switch_preflight_rejects_dangerous_target_key(
    tmp_path: Path, permissions: str, ip: str
) -> None:
    switcher, *_ = _switch_service(
        tmp_path,
        target_client_factory=lambda profile: FakeProfileClient(
            profile, permissions=permissions, ip=ip
        ),
    )

    result = await switcher.preflight("live")

    assert result["allowed"] is False
    assert any(
        check["key"] == "target_permissions_valid" and not check["passed"]
        for check in result["checks"]
    )


@pytest.mark.asyncio
async def test_switch_preflight_blocks_unknown_intent_position_and_tagged_order(
    tmp_path: Path,
) -> None:
    switcher, _, current_audit, _, current_autonomy = _switch_service(tmp_path)
    current_audit.save_intent(
        {
            "intent_id": "intent-unknown",
            "created_at": "2026-08-21T00:00:00Z",
            "expires_at": "2026-08-21T00:01:00Z",
            "payload_json": "{}",
            "decision_json": "{}",
            "digest": "a" * 64,
            "cl_ord_id": "tg-unknown",
            "status": "uncertain",
            "credential_fingerprint": "d" * 64,
            "account_fingerprint": "a" * 64,
        }
    )
    current_autonomy.position = {"status": "manual_review"}
    switcher.trading.client.pending = [  # type: ignore[attr-defined]
        {"ordId": "1", "clOrdId": "tg-open", "tag": "tideguarddemo"}
    ]

    result = await switcher.preflight("live")

    failed = {check["key"] for check in result["checks"] if not check["passed"]}
    assert {
        "current_no_unresolved_intents",
        "current_no_model_position",
        "current_pending_orders_zero",
    }.issubset(failed)


@pytest.mark.asyncio
async def test_switch_preflight_blocks_external_pending_orders_on_both_sides(
    tmp_path: Path,
) -> None:
    switcher, *_ = _switch_service(
        tmp_path,
        target_client_factory=lambda profile: FakeProfileClient(
            profile, pending=[{"ordId": "target-1", "tag": "external"}]
        ),
    )
    switcher.trading.client.pending = [  # type: ignore[attr-defined]
        {"ordId": "current-1", "tag": "external"}
    ]

    result = await switcher.preflight("live")

    failed = {check["key"] for check in result["checks"] if not check["passed"]}
    assert "current_pending_orders_zero" in failed
    assert "target_pending_orders_zero" in failed


@pytest.mark.asyncio
async def test_switch_preflight_blocks_pending_orders_on_other_spot_pairs(
    tmp_path: Path,
) -> None:
    switcher, *_ = _switch_service(
        tmp_path,
        target_client_factory=lambda profile: FakeProfileClient(
            profile,
            pending=[
                {"ordId": "target-eth", "instId": "ETH-USDT", "tag": "external"}
            ],
        ),
    )
    switcher.trading.client.pending = [  # type: ignore[attr-defined]
        {"ordId": "current-sol", "instId": "SOL-USDT", "tag": "external"}
    ]

    result = await switcher.preflight("live")

    failed = {check["key"] for check in result["checks"] if not check["passed"]}
    assert "current_pending_orders_zero" in failed
    assert "target_pending_orders_zero" in failed


@pytest.mark.asyncio
async def test_live_preview_vetoes_pending_order_on_other_spot_pair(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "okx_demo_lab.service.credentials_configured", lambda *args: True
    )
    client = FakeProfileClient(
        LIVE_PROFILE,
        pending=[{"ordId": "eth-live", "instId": "ETH-USDT", "tag": "external"}],
    )
    audit = AuditStore(tmp_path / "live-other-pair.sqlite3")
    safety = SafetyController(audit, LIVE_PROFILE)
    service = TradingService(client, audit, safety)  # type: ignore[arg-type]

    await service.arm("我确认使用真实资金")
    result = await service.preview(
        OrderDraft(
            instId="BTC-USDT",
            side="buy",
            ordType="limit",
            price=Decimal("100"),
            size=Decimal("0.01"),
        )
    )

    assert result["decision"]["allowed"] is False
    assert "open_orders" in result["decision"]["reasonCodes"]


@pytest.mark.asyncio
async def test_final_switch_gate_blocks_concurrent_arm_during_recheck(
    tmp_path: Path, monkeypatch
) -> None:
    clock = [datetime(2026, 8, 21, 0, 0, tzinfo=timezone.utc)]
    pending_recheck_started = asyncio.Event()
    release_recheck = asyncio.Event()

    class BlockingTargetClient(FakeProfileClient):
        def __init__(self, profile) -> None:
            super().__init__(profile)
            self.pending_calls = 0

        async def get_pending_orders(self, *args, **kwargs):
            self.pending_calls += 1
            if self.pending_calls == 2:
                pending_recheck_started.set()
                await release_recheck.wait()
            return await super().get_pending_orders(*args, **kwargs)

    target_client = BlockingTargetClient(LIVE_PROFILE)
    switcher, runtime, *_ = _switch_service(
        tmp_path,
        clock=clock,
        target_client_factory=lambda _profile: target_client,
    )
    challenge = await switcher.challenge("live")
    clock[0] += timedelta(seconds=10)
    acknowledgements = EnvironmentAcknowledgements(
        automationStopped=True,
        noOutstandingState=True,
        restartRequired=True,
        liveFundsAtRisk=True,
    )

    confirm_task = asyncio.create_task(
        switcher.confirm(
            target_name="live",
            nonce=challenge["nonce"],
            confirmation="切换到 OKX 实盘",
            acknowledgements=acknowledgements,
        )
    )
    await asyncio.wait_for(pending_recheck_started.wait(), timeout=2)
    assert runtime.status()["transitionPending"] is True
    with pytest.raises(SafetyError, match="交易执行保持锁定"):
        await switcher.trading.arm("DEMO")
    release_recheck.set()
    result = await asyncio.wait_for(confirm_task, timeout=3)

    assert result["configuredEnvironment"] == "live"
    assert result["restartRequired"] is True
