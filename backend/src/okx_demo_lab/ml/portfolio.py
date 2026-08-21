from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Sequence

import numpy as np


PORTFOLIO_EVALUATION_SCHEMA_VERSION = "moheng.portfolio-oos.v1"


class PortfolioValidationError(ValueError):
    pass


@dataclass(frozen=True)
class PortfolioEvaluation:
    instruments: tuple[str, ...]
    evaluated_decisions: int
    rebalances: int
    entries: int
    net_return: float
    max_drawdown: float
    worst_rebalance_return: float
    contribution_by_instrument: tuple[tuple[str, float], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "contributionByInstrument": dict(self.contribution_by_instrument),
            "entries": self.entries,
            "evaluatedDecisions": self.evaluated_decisions,
            "instruments": list(self.instruments),
            "maxDrawdown": self.max_drawdown,
            "netReturn": self.net_return,
            "rebalances": self.rebalances,
            "schemaVersion": PORTFOLIO_EVALUATION_SCHEMA_VERSION,
            "worstRebalanceReturn": self.worst_rebalance_return,
        }


def _capped_inverse_volatility_weights(
    volatility: np.ndarray,
    *,
    gross_exposure: float,
    max_asset_weight: float,
) -> np.ndarray:
    inverse = 1.0 / volatility
    raw = inverse / inverse.sum() * gross_exposure
    weights = np.minimum(raw, max_asset_weight)
    # Redistribute unused exposure only to uncapped assets.  If every selected
    # asset is capped, the rest remains cash rather than breaking the cap.
    for _ in range(len(weights)):
        remaining = gross_exposure - float(weights.sum())
        eligible = weights < max_asset_weight - 1e-15
        if remaining <= 1e-12 or not np.any(eligible):
            break
        capacity = max_asset_weight - weights[eligible]
        allocation = inverse[eligible] / inverse[eligible].sum() * remaining
        weights[eligible] += np.minimum(allocation, capacity)
    return weights


def evaluate_portfolio_scores(
    instruments: Sequence[str],
    scores: Sequence[Sequence[float]] | np.ndarray,
    forward_returns: Sequence[Sequence[float]] | np.ndarray,
    trailing_volatility: Sequence[Sequence[float]] | np.ndarray,
    *,
    buy_threshold: float,
    holding_period_bars: int,
    cost_bps: float,
    max_positions: int = 2,
    gross_exposure: float = 0.50,
    max_asset_weight: float = 0.25,
) -> PortfolioEvaluation:
    """Evaluate aligned assets against one cash account and one decision clock.

    All selected positions open and close together for a fixed horizon. Signals
    during that interval are ignored, preventing the common error of adding
    several single-asset backtests that each reuse 100% of the same capital.
    Volatility inputs must be computed strictly from data before each row.
    """

    names = tuple(instruments)
    if not names or len(names) != len(set(names)) or any(not item for item in names):
        raise PortfolioValidationError("portfolio instruments must be present and unique")
    score_values = np.asarray(scores, dtype=np.float64)
    return_values = np.asarray(forward_returns, dtype=np.float64)
    volatility_values = np.asarray(trailing_volatility, dtype=np.float64)
    expected_shape = (score_values.shape[0], len(names)) if score_values.ndim == 2 else None
    if (
        score_values.ndim != 2
        or expected_shape != score_values.shape
        or return_values.shape != score_values.shape
        or volatility_values.shape != score_values.shape
    ):
        raise PortfolioValidationError("portfolio matrices must be aligned time by asset")
    if score_values.shape[0] <= holding_period_bars or holding_period_bars < 1:
        raise PortfolioValidationError("portfolio evaluation window is too short")
    if (
        not np.all(np.isfinite(score_values))
        or np.any(score_values < 0.0)
        or np.any(score_values > 1.0)
        or not np.all(np.isfinite(return_values))
        or np.any(return_values <= -1.0)
        or np.any(return_values > 1.0)
        or not np.all(np.isfinite(volatility_values))
        or np.any(volatility_values <= 0.0)
    ):
        raise PortfolioValidationError("portfolio matrices contain invalid values")
    scalar_values = (
        buy_threshold,
        cost_bps,
        gross_exposure,
        max_asset_weight,
    )
    if any(isinstance(value, bool) or not math.isfinite(float(value)) for value in scalar_values):
        raise PortfolioValidationError("portfolio policy contains non-finite values")
    if not 0.5 < buy_threshold < 1.0 or not 0.0 < cost_bps <= 1_000.0:
        raise PortfolioValidationError("portfolio threshold or cost is invalid")
    if not 1 <= max_positions <= len(names):
        raise PortfolioValidationError("portfolio position count is invalid")
    if not 0.0 < gross_exposure <= 1.0 or not 0.0 < max_asset_weight <= 1.0:
        raise PortfolioValidationError("portfolio exposure bounds are invalid")

    cursor = 0
    entry_stop = score_values.shape[0] - holding_period_bars
    equity = 1.0
    peak = 1.0
    drawdown = 0.0
    evaluated = 0
    rebalances = 0
    entries = 0
    worst = 0.0
    contributions = np.zeros(len(names), dtype=np.float64)
    per_unit_cost = cost_bps / 10_000.0
    while cursor < entry_stop:
        evaluated += 1
        candidates = np.flatnonzero(score_values[cursor] >= buy_threshold)
        if candidates.size == 0:
            cursor += 1
            continue
        ranked = sorted(
            (int(index) for index in candidates),
            key=lambda index: (-float(score_values[cursor, index]), names[index]),
        )[:max_positions]
        selected = np.asarray(ranked, dtype=np.int64)
        weights = _capped_inverse_volatility_weights(
            volatility_values[cursor, selected],
            gross_exposure=gross_exposure,
            max_asset_weight=max_asset_weight,
        )
        asset_net = return_values[cursor, selected] - per_unit_cost
        rebalance_return = float(np.dot(weights, asset_net))
        if rebalance_return <= -1.0:
            rebalance_return = -1.0
        equity *= max(0.0, 1.0 + rebalance_return)
        peak = max(peak, equity)
        drawdown = max(drawdown, (peak - equity) / peak if peak else 1.0)
        worst = min(worst, rebalance_return)
        contributions[selected] += weights * asset_net
        rebalances += 1
        entries += len(selected)
        cursor += holding_period_bars
    return PortfolioEvaluation(
        instruments=names,
        evaluated_decisions=evaluated,
        rebalances=rebalances,
        entries=entries,
        net_return=equity - 1.0,
        max_drawdown=drawdown,
        worst_rebalance_return=worst,
        contribution_by_instrument=tuple(
            (name, float(contributions[index])) for index, name in enumerate(names)
        ),
    )


__all__ = [
    "PORTFOLIO_EVALUATION_SCHEMA_VERSION",
    "PortfolioEvaluation",
    "PortfolioValidationError",
    "evaluate_portfolio_scores",
]
