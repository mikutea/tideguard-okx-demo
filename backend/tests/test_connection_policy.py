from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from okx_demo_lab import main as main_module
from okx_demo_lab.profile import LIVE_PROFILE


class RecordingStore:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def append(
        self, event_type: str, payload: dict[str, Any], *, actor: str
    ) -> None:
        del actor
        self.events.append((event_type, payload))


class ConnectionClient:
    def __init__(self, config: dict[str, str]) -> None:
        self.config = config

    async def public_get(self, path: str) -> list[dict[str, str]]:
        assert path == "/api/v5/public/time"
        return [{"ts": "1"}]

    def current_credential_fingerprint(self) -> str:
        return "e" * 64

    async def get_account_config(
        self, *, expected_credential_fingerprint: str | None = None
    ) -> list[dict[str, str]]:
        assert expected_credential_fingerprint == "e" * 64
        return [self.config]


def request_for(config: dict[str, str]) -> tuple[Any, RecordingStore]:
    store = RecordingStore()
    state = SimpleNamespace(
        client=ConnectionClient(config),
        environment_runtime=SimpleNamespace(active_profile=LIVE_PROFILE),
        store=store,
    )
    return SimpleNamespace(app=SimpleNamespace(state=state)), store


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("override", "expected_reason"),
    [
        ({"perm": "read_only,trade,withdraw"}, "withdraw"),
        ({"ip": ""}, "绑定至少一个 IP"),
        ({"acctLv": "2"}, "Spot mode"),
    ],
)
async def test_live_connection_separates_reachability_from_policy(
    monkeypatch: pytest.MonkeyPatch,
    override: dict[str, str],
    expected_reason: str,
) -> None:
    config = {
        "uid": "10002",
        "mainUid": "10002",
        "perm": "read_only,trade",
        "ip": "203.0.113.4",
        "acctLv": "1",
    }
    config.update(override)
    request, store = request_for(config)
    monkeypatch.setattr(main_module, "credentials_configured", lambda *args: True)

    result = await main_module.connection_test(request)

    assert result["public"] is True
    assert result["privateReachable"] is True
    assert result["private"] is True
    assert result["policyValid"] is False
    assert expected_reason in result["policyReason"]
    assert store.events[-1][1]["policyValid"] is False


@pytest.mark.asyncio
async def test_live_connection_reports_valid_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, _ = request_for(
        {
            "uid": "10002",
            "mainUid": "10002",
            "perm": "read_only,trade",
            "ip": "203.0.113.4",
            "acctLv": "1",
        }
    )
    monkeypatch.setattr(main_module, "credentials_configured", lambda *args: True)

    result = await main_module.connection_test(request)

    assert result["privateReachable"] is True
    assert result["policyValid"] is True
    assert result["policyReason"] is None
