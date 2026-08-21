"""Auditable research, Codex supervision, and Demo-only execution contracts.

Frozen models remain data-only.  The long-run coordinator may orchestrate the
existing TradingService, which remains the sole exchange order path.

Public exports are resolved lazily so importing a research-only submodule does
not also import the credential or order-execution graph.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any


_LAZY_EXPORTS: dict[str, tuple[str, str]] = {
    "AutomationDenied": (".execution", "AutomationDenied"),
    "AutomationLedger": (".execution", "AutomationLedger"),
    "AUTONOMY_ENABLE_CONFIRMATION": (
        ".autonomy",
        "AUTONOMY_ENABLE_CONFIRMATION",
    ),
    "AutonomyPolicy": (".autonomy", "AutonomyPolicy"),
    "AutonomyStore": (".autonomy", "AutonomyStore"),
    "CodexSupervisor": (".supervisor", "CodexSupervisor"),
    "CHECKPOINT_VALUATION_BASIS": (
        ".historical_replay",
        "CHECKPOINT_VALUATION_BASIS",
    ),
    "DEMO_ENVIRONMENT": (".strategy", "DEMO_ENVIRONMENT"),
    "DemoAutoExecutor": (".execution", "DemoAutoExecutor"),
    "DemoAutomationPermit": (".execution", "DemoAutomationPermit"),
    "DemoStrategyPolicy": (".strategy", "DemoStrategyPolicy"),
    "FrozenLinearModel": (".strategy", "FrozenLinearModel"),
    "FrozenModelBundle": (".strategy", "FrozenModelBundle"),
    "HISTORICAL_REPLAY_SCHEMA_VERSION": (
        ".historical_replay",
        "HISTORICAL_REPLAY_SCHEMA_VERSION",
    ),
    "HistoricalReplayError": (".historical_replay", "HistoricalReplayError"),
    "ManualReviewRequired": (".execution", "ManualReviewRequired"),
    "MarketSnapshot": (".strategy", "MarketSnapshot"),
    "ModelManifest": (".strategy", "ModelManifest"),
    "ModelRegistry": (".registry", "ModelRegistry"),
    "LongRunCoordinator": (".long_run", "LongRunCoordinator"),
    "OrderProposal": (".strategy", "OrderProposal"),
    "PromotionDenied": (".registry", "PromotionDenied"),
    "PromotionPolicy": (".registry", "PromotionPolicy"),
    "ReplayBrokerConfig": (".historical_replay", "ReplayBrokerConfig"),
    "ReplayEpisodeBinding": (".historical_replay", "ReplayEpisodeBinding"),
    "ReplayPolicy": (".historical_replay", "ReplayPolicy"),
    "SupervisorDecision": (".autonomy", "SupervisorDecision"),
    "TrainingConfig": (".walk_forward", "TrainingConfig"),
    "ValidationReport": (".walk_forward", "ValidationReport"),
    "WalkForwardSpec": (".walk_forward", "WalkForwardSpec"),
    "authorize_demo_session": (".execution", "authorize_demo_session"),
    "build_order_proposal": (".strategy", "build_order_proposal"),
    "plan_walk_forward": (".walk_forward", "plan_walk_forward"),
    "run_walk_forward": (".walk_forward", "run_walk_forward"),
    "run_historical_replay": (".historical_replay", "run_historical_replay"),
}

__all__ = [
    "AutomationDenied",
    "AutomationLedger",
    "AUTONOMY_ENABLE_CONFIRMATION",
    "AutonomyPolicy",
    "AutonomyStore",
    "CodexSupervisor",
    "CHECKPOINT_VALUATION_BASIS",
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


def __getattr__(name: str) -> Any:
    """Resolve a public compatibility export only when it is requested."""

    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute_name = target
    value = getattr(import_module(module_name, __name__), attribute_name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
