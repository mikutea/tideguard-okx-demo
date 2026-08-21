from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from okx_demo_lab.ml.pipeline import (
    build_observations,
    DatasetError,
    FEATURE_NAMES,
    latest_features,
    parse_completed_candles,
)
from okx_demo_lab.ml.execution import AUTO_SESSION_CONFIRMATION, DemoAutomationPermit
from okx_demo_lab.ml.runtime import MLCoordinator


def _candles(count: int = 64) -> list[list[str]]:
    start = datetime.now(timezone.utc) - timedelta(minutes=5 * (count + 2))
    start_ms = int(start.timestamp() * 1000)
    start_ms -= start_ms % 300_000
    rows: list[list[str]] = []
    for index in range(count):
        close = 50_000 + index * 3
        rows.append(
            [
                str(start_ms + index * 300_000),
                str(close - 2),
                str(close + 5),
                str(close - 7),
                str(close),
                str(100 + index),
                "0",
                "0",
                "1",
            ]
        )
    return rows


def test_runtime_features_require_confirmed_contiguous_candles() -> None:
    now = datetime.now(timezone.utc)
    parsed = parse_completed_candles(_candles(), now=now)
    features, closed_at = latest_features(parsed)
    assert tuple(features) == FEATURE_NAMES
    assert all(isinstance(value, float) for value in features.values())
    assert closed_at <= now

    unconfirmed = _candles()
    unconfirmed[-1][8] = "0"
    with pytest.raises(DatasetError, match="confirmed"):
        parse_completed_candles(unconfirmed, now=now)

    gapped = _candles()
    gapped[20][0] = str(int(gapped[20][0]) + 1)
    with pytest.raises(DatasetError, match="exactly 5 minutes"):
        parse_completed_candles(gapped, now=now)


def test_training_label_uses_the_live_bracket_with_adverse_same_bar_ordering() -> None:
    now = datetime.now(timezone.utc)
    rows = _candles(80)
    entry_close = Decimal(rows[48][4])
    rows[49][2] = str(entry_close * Decimal("1.03"))
    rows[49][3] = str(entry_close * Decimal("0.98"))
    observations = build_observations(
        parse_completed_candles(rows, now=now),
        label_horizon=12,
        round_trip_cost_bps=24,
        stop_loss_fraction=0.015,
        take_profit_fraction=0.025,
    )
    assert observations[0].forward_return == pytest.approx(-0.015)
    assert observations[0].label == 0


@pytest.mark.asyncio
async def test_coordinator_is_network_idle_without_explicit_permit(tmp_path) -> None:
    class NoNetworkClient:
        calls = 0

        async def get_market_bundle(self, *_args, **_kwargs):
            self.calls += 1
            raise AssertionError("market must not be read while automation is disabled")

    class DummyService:
        pass

    class DummyStore:
        pass

    client = NoNetworkClient()
    coordinator = MLCoordinator(
        data_dir=tmp_path,
        client=client,  # type: ignore[arg-type]
        service=DummyService(),  # type: ignore[arg-type]
        store=DummyStore(),  # type: ignore[arg-type]
    )
    await coordinator.tick()
    assert client.calls == 0
    status = coordinator.status()
    assert status["automation"]["permit"] is None
    assert status["champion"] is None
    assert status["engine"]["profitGuarantee"] is False
    await coordinator.close()


def test_ml_status_route_and_training_bounds(tmp_path, monkeypatch) -> None:
    import okx_demo_lab.main as main_module

    monkeypatch.setattr(main_module, "app_data_dir", lambda: tmp_path)
    with TestClient(main_module.app) as client:
        system = client.get("/api/v1/system/status")
        assert system.status_code == 200
        csrf = system.json()["csrfToken"]
        response = client.get("/api/v1/ml/status")
        assert response.status_code == 200
        assert response.json()["automation"]["demoOnly"] is True
        assert response.json()["longRun"]["state"]["desiredMode"] == "disabled"
        research = client.get("/api/v1/research/status")
        assert research.status_code == 200
        assert research.json()["available"] is False
        assert research.json()["safety"]["executionAllowlist"] == ["BTC-USDT"]
        assert research.json()["safety"]["privateApi"] is False
        autonomy = client.get("/api/v1/autonomy/status")
        assert autonomy.status_code == 200
        assert autonomy.json()["activePosition"] is None
        review = client.get("/api/v1/autonomy/review-pack")
        assert review.status_code == 200
        assert review.json()["schemaVersion"] == "tideguard.codex-review.v1"
        enable = client.post(
            "/api/v1/autonomy/master/enable",
            headers={"X-Tideguard-CSRF": csrf},
            json={"mode": "demo", "confirmation": "ENABLE LONG-RUN OKX DEMO"},
        )
        assert enable.status_code == 409
        invalid = client.post(
            "/api/v1/ml/train",
            headers={"X-Tideguard-CSRF": csrf},
            json={"candleLimit": 1_000},
        )
        assert invalid.status_code == 422
        legacy_promote = client.post(
            "/api/v1/ml/promote",
            headers={"X-Tideguard-CSRF": csrf},
            json={
                "modelId": "mdl_" + "1" * 24,
                "reviewer": "tester",
                "rationale": "This legacy browser promotion is intentionally disabled.",
                "confirmation": "PROMOTE OKX DEMO CHAMPION",
                "expectedGeneration": 0,
            },
        )
        assert legacy_promote.status_code == 410
        legacy_authorize = client.post(
            "/api/v1/ml/automation/authorize",
            headers={"X-Tideguard-CSRF": csrf},
            json={
                "issuedBy": "tester",
                "confirmation": "ENABLE OKX DEMO AUTO",
                "ttlSeconds": 60,
                "maxOrders": 1,
                "maxTotalNotionalUsdt": "10",
            },
        )
        assert legacy_authorize.status_code == 410


def _permit() -> DemoAutomationPermit:
    now = datetime.now(timezone.utc)
    return DemoAutomationPermit(
        permit_id="permit_" + "1" * 24,
        model_id="mdl_" + "2" * 24,
        artifact_sha256="3" * 64,
        champion_generation=1,
        policy_sha256="4" * 64,
        issued_by="tester",
        issued_at=now,
        expires_at=now + timedelta(minutes=2),
        max_orders=1,
        max_total_notional_usdt=Decimal("10"),
    )


@pytest.mark.asyncio
async def test_authorize_does_not_publish_hidden_permit_when_audit_fails(
    tmp_path, monkeypatch
) -> None:
    import okx_demo_lab.ml.runtime as runtime_module

    permit = _permit()

    class Safety:
        @staticmethod
        def status():
            return {"mode": "armed"}

    class Service:
        safety = Safety()

    class Store:
        @staticmethod
        def verify_chain():
            return True

        @staticmethod
        def append(*_args, **_kwargs):
            raise OSError("audit unavailable")

    coordinator = MLCoordinator(
        data_dir=tmp_path,
        client=object(),  # type: ignore[arg-type]
        service=Service(),  # type: ignore[arg-type]
        store=Store(),  # type: ignore[arg-type]
    )
    monkeypatch.setattr(runtime_module, "credentials_configured", lambda: True)
    monkeypatch.setattr(runtime_module, "authorize_demo_session", lambda *_a, **_k: permit)
    monkeypatch.setattr(coordinator.registry, "load_champion", lambda: object())
    with pytest.raises(OSError, match="audit unavailable"):
        await coordinator.authorize(
            issued_by="tester",
            confirmation=AUTO_SESSION_CONFIRMATION,
            ttl_seconds=120,
            max_orders=1,
            max_total_notional_usdt=Decimal("10"),
        )
    assert coordinator._active_permit is None
    assert coordinator.ledger.permit_status(permit.permit_id)["revokedAt"] is not None


@pytest.mark.asyncio
async def test_authorize_does_not_publish_when_permit_status_read_fails(
    tmp_path, monkeypatch
) -> None:
    import okx_demo_lab.ml.runtime as runtime_module

    permit = _permit()

    class Safety:
        @staticmethod
        def status():
            return {"mode": "armed"}

    class Service:
        safety = Safety()

    class Store:
        @staticmethod
        def verify_chain():
            return True

        @staticmethod
        def append(*_args, **_kwargs):
            return None

    coordinator = MLCoordinator(
        data_dir=tmp_path,
        client=object(),  # type: ignore[arg-type]
        service=Service(),  # type: ignore[arg-type]
        store=Store(),  # type: ignore[arg-type]
    )
    monkeypatch.setattr(runtime_module, "credentials_configured", lambda: True)
    monkeypatch.setattr(runtime_module, "authorize_demo_session", lambda *_a, **_k: permit)
    monkeypatch.setattr(coordinator.registry, "load_champion", lambda: object())
    original_status = coordinator.ledger.permit_status
    status_calls = 0

    def failing_status(permit_id):
        nonlocal status_calls
        status_calls += 1
        if status_calls == 1:
            raise OSError("status unavailable")
        return original_status(permit_id)

    monkeypatch.setattr(coordinator.ledger, "permit_status", failing_status)
    with pytest.raises(OSError, match="status unavailable"):
        await coordinator.authorize(
            issued_by="tester",
            confirmation=AUTO_SESSION_CONFIRMATION,
            ttl_seconds=120,
            max_orders=1,
            max_total_notional_usdt=Decimal("10"),
        )
    assert coordinator._active_permit is None
    assert original_status(permit.permit_id)["revokedAt"] is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["ledger", "audit"])
async def test_stop_always_attempts_emergency_when_local_persistence_fails(
    tmp_path, monkeypatch, failure
) -> None:
    permit = _permit()

    class Service:
        emergency_calls = 0

        async def emergency_stop(self, *_args, **_kwargs):
            self.emergency_calls += 1
            return {"safety": {"mode": "killed"}}

    class Store:
        @staticmethod
        def append(*_args, **_kwargs):
            if failure == "audit":
                raise OSError("audit unavailable")

    service = Service()
    coordinator = MLCoordinator(
        data_dir=tmp_path,
        client=object(),  # type: ignore[arg-type]
        service=service,  # type: ignore[arg-type]
        store=Store(),  # type: ignore[arg-type]
    )
    coordinator.ledger.register_permit(permit, now=datetime.now(timezone.utc))
    coordinator._active_permit = permit
    if failure == "ledger":
        monkeypatch.setattr(
            coordinator.ledger,
            "revoke_permit",
            lambda *_a, **_k: (_ for _ in ()).throw(OSError("ledger unavailable")),
        )
    result = await coordinator.stop_automation()
    assert service.emergency_calls == 1
    assert coordinator._active_permit is None
    assert result["localPersistenceFailures"] == 1
