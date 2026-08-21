"""Auditable research, Codex supervision, and Demo-only execution contracts.

Frozen models remain data-only.  The long-run coordinator may orchestrate the
existing TradingService, which remains the sole exchange order path.
"""

from .autonomy import (
    AUTONOMY_ENABLE_CONFIRMATION,
    AutonomyPolicy,
    AutonomyStore,
    SupervisorDecision,
)

from .execution import (
    AutomationDenied,
    AutomationLedger,
    DemoAutoExecutor,
    DemoAutomationPermit,
    ManualReviewRequired,
    authorize_demo_session,
)
from .historical_replay import (
    HISTORICAL_REPLAY_SCHEMA_VERSION,
    HistoricalReplayError,
    ReplayBrokerConfig,
    ReplayEpisodeBinding,
    ReplayPolicy,
    run_historical_replay,
)
from .registry import ModelRegistry, PromotionDenied, PromotionPolicy
from .long_run import LongRunCoordinator
from .supervisor import CodexSupervisor
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
    "AUTONOMY_ENABLE_CONFIRMATION",
    "AutonomyPolicy",
    "AutonomyStore",
    "CodexSupervisor",
    "DEMO_ENVIRONMENT",
    "DemoAutoExecutor",
    "DemoAutomationPermit",
    "DemoStrategyPolicy",
    "FrozenLinearModel",
    "FrozenModelBundle",
    "HISTORICAL_REPLAY_SCHEMA_VERSION",
    "HistoricalReplayError",
    "ManualReviewRequired",
    "MarketSnapshot",
    "ModelManifest",
    "ModelRegistry",
    "LongRunCoordinator",
    "OrderProposal",
    "PromotionDenied",
    "PromotionPolicy",
    "ReplayBrokerConfig",
    "ReplayEpisodeBinding",
    "ReplayPolicy",
    "SupervisorDecision",
    "TrainingConfig",
    "ValidationReport",
    "WalkForwardSpec",
    "authorize_demo_session",
    "build_order_proposal",
    "plan_walk_forward",
    "run_walk_forward",
    "run_historical_replay",
]
