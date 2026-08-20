from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from okx_demo_lab.ml.autonomy import (
    AUTONOMY_ENABLE_CONFIRMATION,
    AutonomyPolicy,
    AutonomyStore,
    SupervisorDecision,
)
from okx_demo_lab.ml.long_run import LongRunCoordinator, LongRunError
from okx_demo_lab.ml.pipeline import BAR_MILLISECONDS, FEATURE_NAMES, ParsedCandle
from okx_demo_lab.ml.registry import PromotionPolicy
from okx_demo_lab.main import recover_autonomy_position_before_loop
from okx_demo_lab.service import CommitBlockedBeforeDispatch


NOW = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
MODEL_ID = "mdl_" + "1" * 24
ARTIFACT = "a" * 64
CREDENTIAL = "c" * 64
ACCOUNT = "d" * 64


class AlwaysBuyModel:
    feature_names = FEATURE_NAMES

    def action(self, _features):
        return "buy", 0.9


class FakeBundle:
    model_id = MODEL_ID
    artifact_sha256 = ARTIFACT
    model = AlwaysBuyModel()
    manifest = SimpleNamespace(market_snapshot_sha256="f" * 64)


class FakeRegistry:
    def __init__(self):
        self.bundle = FakeBundle()
        self.champion = SimpleNamespace(
            model_id=MODEL_ID,
            artifact_sha256=ARTIFACT,
            generation=1,
            bundle=self.bundle,
        )

    def champion_summary(self):
        return {
            "modelId": MODEL_ID,
            "artifactSha256": ARTIFACT,
            "generation": 1,
            "promotionId": "prom-test",
            "reviewer": "codex-supervisor",
            "rationale": "test",
            "policySha256": "b" * 64,
            "validationRunId": "val-test",
            "reportSha256": "e" * 64,
            "approvedAt": NOW.isoformat(),
        }

    def load_champion(self):
        return self.champion

    def list_models(self, limit=100):
        return [
            {
                "modelId": MODEL_ID,
                "artifactSha256": ARTIFACT,
                "state": "champion",
                "createdAt": NOW.isoformat(),
                "trainedThrough": NOW.isoformat(),
                "trainer": "test",
                "validationRunId": None,
            }
        ]

    def load_model(self, model_id):
        assert model_id == MODEL_ID
        return self.bundle

    def get_generation(self):
        return 1

    def get_validation(self, _):
        return None


class FakeAudit:
    def __init__(self):
        self.intents = {}
        self.events = []

    def verify_chain(self):
        return True

    def append(self, event_type, payload, **kwargs):
        self.events.append((event_type, payload, kwargs))

    def get_intent(self, intent_id):
        return self.intents.get(intent_id)


def candle_rows(closed_at: datetime, *, close: Decimal) -> list[list[str]]:
    rows = []
    last_open_ms = int(closed_at.timestamp() * 1000) - BAR_MILLISECONDS
    for offset in range(96):
        opened_ms = last_open_ms - offset * BAR_MILLISECONDS
        value = close - Decimal(offset)
        rows.append(
            [
                str(opened_ms),
                str(value),
                str(value + Decimal("10")),
                str(value - Decimal("10")),
                str(value),
                "100",
                "0",
                "0",
                "1",
            ]
        )
    return rows


class FakeClient:
    def __init__(self, now: datetime):
        self.now = now
        self.price = Decimal("100000")
        self.market_calls = 0

    async def get_market_bundle(self, _):
        self.market_calls += 1
        return {
            "ticker": {
                "last": str(self.price),
                "bidPx": str(self.price - Decimal("1")),
                "askPx": str(self.price + Decimal("1")),
                "ts": str(int(self.now.timestamp() * 1000)),
            },
            "instrument": {
                "instId": "BTC-USDT",
                "instType": "SPOT",
                "state": "live",
                "tickSz": "0.1",
                "lotSz": "0.00001",
                "minSz": "0.00001",
            },
            "candles": candle_rows(self.now, close=self.price),
        }

    async def get_history_candles(self, *, limit):
        raise AssertionError(f"training/history was unexpectedly requested: {limit}")


class FakeService:
    def __init__(self, audit: FakeAudit):
        self.audit = audit
        self.drafts = []
        self.arm_calls = []
        self.commit_calls = 0
        self.emergency_calls = 0
        self._intent = 0
        self.commit_error_on_side = None
        self.before_dispatch = None
        self.http_dispatch_calls = 0

    async def arm_supervised(self, decision_id, binding, *, purpose):
        self.arm_calls.append((decision_id, binding, purpose))
        return {"mode": "armed"}

    async def preview(self, draft, **_authorization):
        self.drafts.append(draft)
        self._intent += 1
        intent_id = f"intent-{self._intent}"
        self.audit.intents[intent_id] = {"cl_ord_id": f"cl-{self._intent}"}
        return {
            "intentId": intent_id,
            "digest": "f" * 64,
            "decision": {"allowed": True, "reasonCodes": []},
        }

    async def commit(
        self,
        intent_id,
        digest,
        idempotency_key,
        *,
        additional_dispatch_guard=None,
    ):
        self.commit_calls += 1
        if self.before_dispatch:
            self.before_dispatch()
        try:
            if additional_dispatch_guard:
                additional_dispatch_guard()
        except BaseException as exc:
            raise CommitBlockedBeforeDispatch("blocked before fake HTTP") from exc
        self.http_dispatch_calls += 1
        side = self.drafts[-1].side
        if self.commit_error_on_side == side:
            raise TimeoutError("unknown after dispatch")
        return {
            "intentId": intent_id,
            "status": "accepted",
            "ordId": f"ord-{self.commit_calls}",
            "replayed": False,
        }

    async def inspect_intent_order(self, intent_id, binding):
        draft = self.drafts[-1]
        return {
            "intentId": intent_id,
            "clOrdId": self.audit.intents[intent_id]["cl_ord_id"],
            "ordId": f"ord-{self.commit_calls}",
            "instId": "BTC-USDT",
            "side": draft.side,
            "ordType": "ioc",
            "requestedSize": str(draft.size),
            "filledSize": str(draft.size),
            "averagePrice": str(draft.price),
            "state": "filled",
            "fee": "-0.01" if draft.side == "buy" else "-0.005",
            "feeCurrency": "USDT",
            "updatedAt": "1787184000000",
        }

    async def emergency_stop(self, *_, **__):
        self.emergency_calls += 1
        return {"safety": {"mode": "killed"}}


def setup_coordinator(tmp_path, *, market_data_path=None):
    audit = FakeAudit()
    autonomy = AutonomyStore(tmp_path / "autonomy.sqlite3")
    registry = FakeRegistry()
    client = FakeClient(NOW)
    service = FakeService(audit)
    policy = AutonomyPolicy(shadow_min_settled=1, shadow_min_days=1)
    coordinator = LongRunCoordinator(
        client=client,  # type: ignore[arg-type]
        service=service,  # type: ignore[arg-type]
        audit=audit,  # type: ignore[arg-type]
        registry=registry,  # type: ignore[arg-type]
        autonomy=autonomy,
        market_data_path=market_data_path,
        market_snapshot_validator=lambda _sha: True,
        promotion_policy=PromotionPolicy(),
        policy=policy,
    )
    coordinator._now = lambda: client.now
    run_id = autonomy.start_training(now=NOW)
    autonomy.finish_training(
        run_id,
        model_id=MODEL_ID,
        result={"test": True},
        error_type=None,
        now=NOW,
    )
    autonomy.enable_master(
        mode="demo",
        credential_fingerprint=CREDENTIAL,
        account_fingerprint=ACCOUNT,
        confirmation=AUTONOMY_ENABLE_CONFIRMATION,
        now=NOW,
    )
    decision = SupervisorDecision(
        kind="lease",
        subject_model_id=MODEL_ID,
        artifact_sha256=ARTIFACT,
        expected_generation=1,
        policy_sha256=policy.policy_sha256,
        evidence_sha256="e" * 64,
        rationale="Codex test lease binds the exact champion and fixed policy.",
        issued_at=NOW,
        expires_at=NOW + timedelta(hours=24),
    )
    autonomy.record_supervisor_decision(decision, now=NOW)
    autonomy.mark_decision_applied(decision.decision_id, now=NOW)
    promotion = SupervisorDecision(
        kind="promote",
        subject_model_id=MODEL_ID,
        artifact_sha256=ARTIFACT,
        expected_generation=0,
        policy_sha256=policy.policy_sha256,
        evidence_sha256="b" * 64,
        rationale="Codex applied this exact champion before the test execution lease.",
        issued_at=NOW - timedelta(minutes=5),
        expires_at=NOW + timedelta(minutes=55),
    )
    autonomy.record_supervisor_decision(promotion, now=NOW - timedelta(minutes=5))
    autonomy.mark_decision_applied(promotion.decision_id, now=NOW - timedelta(minutes=4))
    return coordinator, client, service, autonomy, decision


def test_long_run_accepts_an_explicit_shared_public_market_database(tmp_path):
    shared = tmp_path / "shared-research" / "market-data.sqlite3"
    coordinator, _client, _service, _autonomy, _decision = setup_coordinator(
        tmp_path,
        market_data_path=shared,
    )
    assert coordinator.market_data.path == shared


@pytest.mark.asyncio
async def test_closed_loop_uses_ioc_and_exact_model_owned_inventory(tmp_path):
    coordinator, client, service, autonomy, decision = setup_coordinator(tmp_path)

    await coordinator.tick()
    position = autonomy.active_position()
    assert position and position["status"] == "long"
    assert service.drafts[0].side == "buy"
    assert service.drafts[0].ordType == "ioc"
    assert service.arm_calls[0] == (
        decision.decision_id,
        (CREDENTIAL, ACCOUNT),
        "entry",
    )
    owned = Decimal(position["remainingSize"])

    client.now = NOW + timedelta(hours=1)
    client.price = Decimal("103000")
    await coordinator.tick()
    assert service.drafts[1].side == "sell"
    assert service.drafts[1].ordType == "ioc"
    assert service.drafts[1].size == owned
    assert service.arm_calls[1][2] == "exit"
    assert autonomy.active_position() is None
    closed = autonomy.recent_positions()[0]
    assert closed["status"] == "closed"
    assert Decimal(closed["remainingSize"]) == 0


@pytest.mark.asyncio
async def test_unknown_exit_is_persisted_and_never_retried(tmp_path):
    coordinator, client, service, autonomy, _ = setup_coordinator(tmp_path)
    await coordinator.tick()
    assert autonomy.active_position()["status"] == "long"

    service.commit_error_on_side = "sell"
    client.now = NOW + timedelta(hours=1)
    client.price = Decimal("103000")
    with pytest.raises(TimeoutError):
        await coordinator.tick()

    position = autonomy.active_position()
    assert position and position["status"] == "manual_review"
    assert service.emergency_calls == 1
    commits = service.commit_calls

    client.now += timedelta(seconds=5)
    await coordinator.tick()
    assert service.commit_calls == commits
    assert autonomy.state()["runtimeStatus"] == "manual_review"


@pytest.mark.asyncio
async def test_disabled_master_never_calls_private_service(tmp_path):
    coordinator, client, service, autonomy, _ = setup_coordinator(tmp_path)
    autonomy.disable_master("test disabled", now=NOW)
    await coordinator.tick()
    assert client.market_calls == 1
    assert service.arm_calls == []
    assert service.commit_calls == 0
    assert autonomy.state()["runtimeStatus"] == "disabled"


@pytest.mark.asyncio
async def test_master_disable_wins_before_entry_http_dispatch(tmp_path):
    coordinator, _, service, autonomy, _ = setup_coordinator(tmp_path)
    service.before_dispatch = lambda: autonomy.disable_master(
        "concurrent user stop", now=NOW
    )
    with pytest.raises(LongRunError, match="blocked before HTTP"):
        await coordinator.tick()
    assert service.commit_calls == 1
    assert service.http_dispatch_calls == 0
    assert autonomy.active_position() is None
    assert autonomy.recent_positions()[0]["status"] == "entry_unfilled"


@pytest.mark.asyncio
async def test_shadow_uses_same_conservative_stop_take_path_as_live(tmp_path):
    coordinator, _, _, autonomy, _ = setup_coordinator(tmp_path)
    entry_at = NOW - timedelta(hours=1)
    autonomy.record_shadow_signal(
        model_id=MODEL_ID,
        artifact_sha256=ARTIFACT,
        candle_closed_at=entry_at,
        due_at=NOW,
        action="buy",
        score=0.9,
        entry_close=Decimal("100"),
    )
    candles = []
    for offset in range(13):
        closed_at = entry_at + timedelta(minutes=5 * offset)
        low = 98.0 if offset == 3 else 99.0
        high = 103.0 if offset == 3 else 101.0
        candles.append(
            ParsedCandle(
                opened_at=closed_at - timedelta(minutes=5),
                closed_at=closed_at,
                open=100.0,
                high=high,
                low=low,
                close=100.0,
                volume=10.0,
            )
        )
    await coordinator._settle_shadow(tuple(candles), now=NOW)
    summary = autonomy.shadow_summary(MODEL_ID)
    assert summary["settledBuys"] == 1
    assert summary["netReturn"] == pytest.approx(-0.0174)


@pytest.mark.asyncio
async def test_training_never_blocks_monitoring_an_active_position(tmp_path):
    coordinator, _, _, autonomy, _ = setup_coordinator(tmp_path)
    await coordinator.tick()
    assert autonomy.active_position()["status"] == "long"
    calls = 0

    async def unexpected_training(**_):
        nonlocal calls
        calls += 1

    coordinator.train_if_due = unexpected_training  # type: ignore[method-assign]
    await coordinator.tick()
    assert calls == 0
    with pytest.raises(LongRunError, match="needs monitoring"):
        await coordinator.train_now()


@pytest.mark.asyncio
async def test_market_freshness_uses_time_after_sequential_public_requests(tmp_path):
    coordinator, client, _, autonomy, _ = setup_coordinator(tmp_path)
    autonomy.disable_master("public freshness test", now=NOW)
    client.now = NOW + timedelta(seconds=5)
    times = iter([NOW, NOW + timedelta(seconds=5)])
    coordinator._now = lambda: next(times)
    await coordinator.tick()
    assert autonomy.state()["runtimeStatus"] == "disabled"


def test_startup_abandons_only_a_proven_pre_dispatch_entry(tmp_path):
    coordinator, _, _, autonomy, _ = setup_coordinator(tmp_path)
    position_id = autonomy.create_entry_position(
        model_id=MODEL_ID,
        artifact_sha256=ARTIFACT,
        champion_generation=1,
        policy_sha256=coordinator.policy.policy_sha256,
        credential_fingerprint=CREDENTIAL,
        account_fingerprint=ACCOUNT,
        entry_signal_id="startup-pre-dispatch",
        supervisor_decision_id="sup_" + "9" * 28,
        requested_size=Decimal("0.0001"),
        entry_candle_at=NOW,
        exit_due_at=NOW + timedelta(hours=1),
        hard_exit_at=NOW + timedelta(hours=2),
        now=NOW,
    )
    audit = FakeAudit()
    assert recover_autonomy_position_before_loop(autonomy, audit, coordinator) is None
    assert autonomy.get_position(position_id)["status"] == "entry_unfilled"


def test_startup_keeps_accepted_ioc_for_terminal_reconciliation(tmp_path):
    coordinator, _, _, autonomy, _ = setup_coordinator(tmp_path)
    position_id = autonomy.create_entry_position(
        model_id=MODEL_ID,
        artifact_sha256=ARTIFACT,
        champion_generation=1,
        policy_sha256=coordinator.policy.policy_sha256,
        credential_fingerprint=CREDENTIAL,
        account_fingerprint=ACCOUNT,
        entry_signal_id="startup-accepted",
        supervisor_decision_id="sup_" + "8" * 28,
        requested_size=Decimal("0.0001"),
        entry_candle_at=NOW,
        exit_due_at=NOW + timedelta(hours=1),
        hard_exit_at=NOW + timedelta(hours=2),
        now=NOW,
    )
    autonomy.attach_entry_intent(
        position_id, intent_id="intent-accepted", cl_ord_id="cl-accepted", now=NOW
    )
    audit = FakeAudit()
    audit.intents["intent-accepted"] = {
        "commit_key": "k" * 16,
        "status": "accepted",
    }
    assert recover_autonomy_position_before_loop(autonomy, audit, coordinator) is None
    assert autonomy.get_position(position_id)["status"] == "entry_submitted"
