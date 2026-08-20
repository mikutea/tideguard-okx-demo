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
    ordType: Literal["limit", "post_only", "ioc"] = "limit"
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


class ModelTrainRequest(StrictModel):
    candleLimit: int = Field(default=2_000, ge=1_600, le=5_000)


class ModelPromoteRequest(StrictModel):
    modelId: str = Field(pattern=r"^mdl_[0-9a-f]{24}$")
    reviewer: str = Field(min_length=1, max_length=128)
    rationale: str = Field(min_length=16, max_length=2_000)
    confirmation: str = Field(min_length=1, max_length=64)
    expectedGeneration: int = Field(ge=0)


class AutomationAuthorizeRequest(StrictModel):
    issuedBy: str = Field(min_length=1, max_length=128)
    confirmation: str = Field(min_length=1, max_length=64)
    ttlSeconds: int = Field(default=300, ge=30, le=600)
    maxOrders: int = Field(default=1, ge=1, le=1)
    maxTotalNotionalUsdt: Decimal = Field(default=Decimal("10"), gt=0, le=10)


class AutonomyEnableRequest(StrictModel):
    mode: Literal["demo"] = "demo"
    confirmation: str = Field(min_length=1, max_length=64)


class AutonomyDisableRequest(StrictModel):
    reason: str = Field(default="用户停止长期自动量化", min_length=1, max_length=512)


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
