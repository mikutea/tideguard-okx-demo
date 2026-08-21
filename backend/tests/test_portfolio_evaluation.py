from __future__ import annotations

import numpy as np
import pytest

from okx_demo_lab.ml.portfolio import PortfolioValidationError, evaluate_portfolio_scores


def test_portfolio_uses_one_shared_cash_account_and_respects_asset_caps() -> None:
    scores = np.asarray(
        [
            [0.9, 0.8, 0.1],
            [0.9, 0.9, 0.9],
            [0.9, 0.9, 0.9],
            [0.1, 0.1, 0.1],
            [0.9, 0.8, 0.7],
            [0.1, 0.1, 0.1],
        ]
    )
    returns = np.asarray(
        [
            [0.10, 0.10, 0.10],
            [0.50, 0.50, 0.50],
            [-0.10, -0.10, -0.10],
            [0.00, 0.00, 0.00],
            [0.50, 0.50, 0.50],
            [0.00, 0.00, 0.00],
        ]
    )
    volatility = np.ones_like(scores)

    result = evaluate_portfolio_scores(
        ("BTC-USDT", "ETH-USDT", "SOL-USDT"),
        scores,
        returns,
        volatility,
        buy_threshold=0.6,
        holding_period_bars=2,
        cost_bps=0.0001,
        max_positions=2,
        gross_exposure=0.5,
        max_asset_weight=0.25,
    )

    # At t0 only 50% total capital is used: +5%, not +20% from adding two
    # independent 100%-capital backtests. Signals at t1 are ignored while held.
    assert result.rebalances == 2
    assert result.entries == 4
    assert result.net_return == pytest.approx(1.05 * 0.95 - 1.0, abs=1e-7)
    assert result.max_drawdown == pytest.approx(0.05)
    assert result.to_dict()["schemaVersion"] == "moheng.portfolio-oos.v1"


def test_portfolio_ranks_equal_time_signals_deterministically() -> None:
    scores = np.asarray([[0.8, 0.9, 0.9], [0.0, 0.0, 0.0], [0.0, 0.0, 0.0]])
    returns = np.asarray([[0.9, 0.2, -0.2], [0.0, 0.0, 0.0], [0.0, 0.0, 0.0]])
    result = evaluate_portfolio_scores(
        ("BTC", "SOL", "ETH"),
        scores,
        returns,
        np.ones_like(scores),
        buy_threshold=0.6,
        holding_period_bars=1,
        cost_bps=1,
        max_positions=1,
        gross_exposure=0.25,
        max_asset_weight=0.25,
    )
    contributions = dict(result.contribution_by_instrument)
    assert contributions["ETH"] < 0
    assert contributions["SOL"] == 0
    assert contributions["BTC"] == 0


def test_portfolio_rejects_misaligned_or_invalid_matrices() -> None:
    with pytest.raises(PortfolioValidationError, match="aligned"):
        evaluate_portfolio_scores(
            ("BTC", "ETH"),
            [[0.6, 0.7]],
            [[0.1]],
            [[0.1, 0.1]],
            buy_threshold=0.6,
            holding_period_bars=1,
            cost_bps=24,
        )
