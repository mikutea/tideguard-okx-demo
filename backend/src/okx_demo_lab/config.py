from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from .profile import DEMO_PROFILE, EnvironmentProfile


APP_NAME = "Tideguard"
PUBLIC_APP_NAME = "墨衡 MOHENG"
APP_VERSION = "0.4.0"
OKX_BASE_URL = "https://openapi.okx.com"
SIMULATED_HEADER = DEMO_PROFILE.private_headers["x-simulated-trading"]
ENVIRONMENT_SELECTOR_FILE = "environment.json"
ALLOWED_INSTRUMENTS = frozenset({"BTC-USDT"})
ALLOWED_PRIVATE_ENDPOINTS = frozenset(
    {
        ("GET", "/api/v5/account/balance"),
        ("GET", "/api/v5/account/config"),
        ("GET", "/api/v5/account/instruments"),
        ("GET", "/api/v5/trade/order"),
        ("GET", "/api/v5/trade/orders-pending"),
        ("POST", "/api/v5/trade/order"),
        ("POST", "/api/v5/trade/cancel-order"),
        ("POST", "/api/v5/trade/cancel-all-after"),
    }
)
ALLOWED_PUBLIC_ENDPOINTS = frozenset(
    {
        "/api/v5/public/time",
        "/api/v5/public/instruments",
        "/api/v5/market/ticker",
        "/api/v5/market/candles",
        "/api/v5/market/history-candles",
    }
)


def app_data_dir() -> Path:
    override = os.environ.get("TIDEGUARD_DATA_DIR", "").strip()
    if override:
        path = Path(override).expanduser()
    else:
        root = os.environ.get("LOCALAPPDATA")
        if not root:
            root = str(Path.home() / "AppData" / "Local")
        path = Path(root) / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


@dataclass(frozen=True)
class RiskPolicy:
    policy_version: str = "demo-v2-autonomy"
    stale_market_seconds: int = 8
    max_price_deviation: Decimal = Decimal("0.015")
    max_order_notional_usdt: Decimal = Decimal("25")
    max_order_equity_fraction: Decimal = Decimal("0.001")
    max_open_orders: int = 3
    preview_ttl_seconds: int = 45
    arm_ttl_seconds: int = 600
    automation_arm_ttl_seconds: int = 90
    request_expiry_ms: int = 4_000
    deadman_seconds: int = 20
    deadman_local_lease_seconds: int = 12
    emergency_deadman_seconds: int = 10


POLICY = RiskPolicy()
LIVE_POLICY = RiskPolicy(
    policy_version="live-v1-observe-locked",
    max_order_notional_usdt=Decimal("10"),
    max_order_equity_fraction=Decimal("0.0005"),
    max_open_orders=1,
    arm_ttl_seconds=60,
    automation_arm_ttl_seconds=60,
)


def policy_for_profile(profile: EnvironmentProfile) -> RiskPolicy:
    return POLICY if profile.name == "demo" else LIVE_POLICY
