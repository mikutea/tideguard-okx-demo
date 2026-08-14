"""Offline-only model research and demo-execution contracts.

This package intentionally has no exchange client dependency.  A frozen model may
produce a proposal, but the existing TradingService remains the only order path.
"""

from .execution import (
    AutomationDenied,
    AutomationLedger,
    DemoAutoExecutor,
    DemoAutomationPermit,
    ManualReviewRequired,
    authorize_demo_session,
)
from .registry import ModelRegistry, PromotionDenied, PromotionPolicy
from .strategy import (
    DEMO_ENVIRONMENT,
    DemoStrategyPolicy,
    FrozenLinearModel,
    FrozenModelBundle,
    MarketSnapshot,
    ModelManifest,
    OrderProposal,
    build_order_proposal,
)
from .walk_forward import (
    TrainingConfig,
    ValidationReport,
    WalkForwardSpec,
    plan_walk_forward,
    run_walk_forward,
)

__all__ = [
    "AutomationDenied",
    "AutomationLedger",
    "DEMO_ENVIRONMENT",
    "DemoAutoExecutor",
    "DemoAutomationPermit",
    "DemoStrategyPolicy",
    "FrozenLinearModel",
    "FrozenModelBundle",
    "ManualReviewRequired",
    "MarketSnapshot",
    "ModelManifest",
    "ModelRegistry",
    "OrderProposal",
    "PromotionDenied",
    "PromotionPolicy",
    "TrainingConfig",
    "ValidationReport",
    "WalkForwardSpec",
    "authorize_demo_session",
    "build_order_proposal",
    "plan_walk_forward",
    "run_walk_forward",
]
