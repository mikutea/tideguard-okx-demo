from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .config import ALLOWED_INSTRUMENTS


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ArmRequest(StrictModel):
    confirmation: str = Field(min_length=1, max_length=32)


class ResetKillRequest(StrictModel):
    confirmation: str = Field(min_length=1, max_length=32)


class OrderDraft(StrictModel):
    instId: str
    side: Literal["buy", "sell"]
    ordType: Literal["limit", "post_only"] = "limit"
    price: Decimal = Field(gt=0)
    size: Decimal = Field(gt=0)

    @field_validator("instId")
    @classmethod
    def validate_instrument(cls, value: str) -> str:
        normalized = value.upper().strip()
        if normalized not in ALLOWED_INSTRUMENTS:
            raise ValueError("首版仅允许 BTC-USDT 现货")
        return normalized


class CommitRequest(StrictModel):
    digest: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")


class RiskCheck(BaseModel):
    key: str
    label: str
    passed: bool
    current: str
    limit: str
    reason: str


class RiskDecision(BaseModel):
    allowed: bool
    policyVersion: str
    checks: list[RiskCheck]
    reasonCodes: list[str]
