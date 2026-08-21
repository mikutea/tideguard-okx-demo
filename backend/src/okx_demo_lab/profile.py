from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal


EnvironmentName = Literal["demo", "live"]


@dataclass(frozen=True)
class EnvironmentProfile:
    name: EnvironmentName
    display_name: str
    credential_service: str
    data_subdirectory: str | None
    simulated_trading: bool
    order_tag: str

    def runtime_data_dir(self, root: Path) -> Path:
        path = root if self.data_subdirectory is None else root / self.data_subdirectory
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def private_headers(self) -> dict[str, str]:
        return {"x-simulated-trading": "1"} if self.simulated_trading else {}


DEMO_PROFILE = EnvironmentProfile(
    name="demo",
    display_name="OKX 模拟盘",
    credential_service="Tideguard.OKX.Demo",
    data_subdirectory=None,
    simulated_trading=True,
    order_tag="tideguarddemo",
)

LIVE_PROFILE = EnvironmentProfile(
    name="live",
    display_name="OKX 实盘",
    credential_service="Tideguard.OKX.Live",
    data_subdirectory="live",
    simulated_trading=False,
    order_tag="tideguardlive",
)

PROFILES: dict[EnvironmentName, EnvironmentProfile] = {
    "demo": DEMO_PROFILE,
    "live": LIVE_PROFILE,
}


def profile_for(value: str | EnvironmentProfile) -> EnvironmentProfile:
    if isinstance(value, EnvironmentProfile):
        return value
    normalized = value.strip().lower()
    if normalized not in PROFILES:
        raise ValueError("环境必须是 demo 或 live")
    return PROFILES[normalized]  # type: ignore[index]
