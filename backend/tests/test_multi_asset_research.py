from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pytest

from okx_demo_lab.ml.multi_asset_cohort import ValidatedCohort
from okx_demo_lab.ml.multi_asset_research import (
    aggregate_portfolio_folds,
    evaluate_cost_aware_portfolio,
    evaluate_portfolio_scores,
    portfolio_gate_failures,
    prepare_multi_asset_dataset,
)
from okx_demo_lab.ml.pipeline import FEATURE_NAMES
from okx_demo_lab.ml.walk_forward import TrainingConfig, ValidationError


NOW = datetime(2026, 8, 21, 14, 0, tzinfo=timezone.utc)


def _cohort(rows: int = 120) -> ValidatedCohort:
    instruments = ["BTC-USDT", "ETH-USDT", "SOL-USDT"]
    end = round(NOW.timestamp() * 1_000) - 600_000
    timestamps = np.arange(
        end - (rows - 1) * 300_000,
        end + 300_000,
        300_000,
        dtype=np.int64,
    )
    candles = np.empty((rows, len(instruments), 7), dtype=np.float64)
    for asset_index in range(len(instruments)):
        for index in range(rows):
            close = 100.0 * (asset_index + 1) + index * 0.1 + (index % 7) * 0.03
            candles[index, asset_index] = [
                close - 0.05,
                close + 1.0,
                close - 1.0,
                close,
                10.0 + index % 5,
                20.0 + index % 5,
                30.0 + index % 5,
            ]
    return ValidatedCohort(
        manifest_path=Path("cohort_test/manifest.json"),
        manifest={
            "cohortId": "cohort_" + "a" * 24,
            "contentSha256": "a" * 64,
            "instruments": instruments,
            "promotable": False,
        },
        timestamps=timestamps,
        candles=candles,
        correlation=np.eye(len(instruments), dtype=np.float64),
    )


def test_prepares_causal_asset_market_and_identity_features() -> None:
    prepared = prepare_multi_asset_dataset(
        _cohort(),
        now=NOW,
        training_config=TrainingConfig(round_trip_cost_bps=24.0),
    )

    assert prepared.time_rows == 120 - 48 - 12
    assert prepared.asset_rows == 3
    assert prepared.features.shape == (60, 3, len(FEATURE_NAMES) + 6 + 3)
    np.testing.assert_allclose(
        prepared.features[:, 0, len(FEATURE_NAMES)],
        np.mean(prepared.features[:, :, 0], axis=1),
        rtol=1e-6,
        atol=1e-8,
    )
    identity = prepared.features[0, :, -3:]
    np.testing.assert_array_equal(identity, np.eye(3, dtype=np.float32))
    flat_features, flat_labels = prepared.flat_window(0, 10)
    assert flat_features.shape == (30, len(prepared.feature_names))
    assert flat_labels.shape == (30,)
    assert prepared.cohort_id == "cohort_" + "a" * 24


def test_portfolio_evaluation_uses_one_non_overlapping_cash_position() -> None:
    labels = np.ones((10, 3), dtype=np.uint8)
    returns = np.full((10, 3), 0.01, dtype=np.float64)
    scores = np.full((10, 3), 0.1, dtype=np.float64)
    choices = (1, 0, 2, 0)
    for cursor, asset_index in zip((0, 2, 4, 6), choices, strict=True):
        scores[cursor, asset_index] = 0.9

    metrics = evaluate_portfolio_scores(
        labels,
        returns,
        scores,
        buy_threshold=0.6,
        cost_bps=20.0,
        holding_period_bars=2,
    )

    assert metrics.trades == 4
    assert metrics.evaluated_decisions == 4
    assert metrics.trades_by_instrument == (2, 1, 1)
    assert metrics.label_precision == 1.0
    assert metrics.profitable_trade_rate == 1.0
    assert metrics.gross_return == pytest.approx(1.01**4 - 1.0)
    assert metrics.net_return == pytest.approx(1.008**4 - 1.0)
    assert metrics.max_drawdown == 0.0
    assert metrics.cash_bar_rate == pytest.approx(0.2)
    assert metrics.trades_per_day == pytest.approx(4 / (10 / 288))


def test_cost_aware_policy_can_hold_cash_and_enforces_entry_spacing() -> None:
    labels = np.ones((20, 3), dtype=np.uint8)
    returns = np.full((20, 3), 0.01, dtype=np.float64)
    expected = np.zeros((20, 3), dtype=np.float64)
    expected[0, 1] = 0.005
    expected[8, 2] = 0.006

    metrics = evaluate_cost_aware_portfolio(
        labels,
        returns,
        expected,
        cost_bps=20.0,
        edge_buffer_bps=10.0,
        holding_period_bars=2,
        min_entry_spacing_bars=8,
    )

    assert metrics.trades == 2
    assert metrics.trades_by_instrument == (0, 1, 1)
    assert metrics.exposure_bars == 4
    assert metrics.cash_bar_rate == pytest.approx(0.8)
    assert metrics.average_gross_return == pytest.approx(0.01)
    assert metrics.average_net_return == pytest.approx(0.008)


def test_cost_aware_policy_stays_in_cash_without_net_edge() -> None:
    metrics = evaluate_cost_aware_portfolio(
        np.ones((20, 3), dtype=np.uint8),
        np.full((20, 3), 0.01, dtype=np.float64),
        np.full((20, 3), 0.003, dtype=np.float64),
        cost_bps=20.0,
        edge_buffer_bps=10.0,
        holding_period_bars=2,
        min_entry_spacing_bars=8,
    )

    assert metrics.trades == 0
    assert metrics.cash_bar_rate == 1.0
    assert metrics.gross_return == 0.0
    assert metrics.net_return == 0.0


def test_portfolio_aggregate_and_research_gate_remain_evidence_only() -> None:
    labels = np.ones((8, 3), dtype=np.uint8)
    returns = np.full((8, 3), 0.01, dtype=np.float64)
    scores = np.full((8, 3), 0.9, dtype=np.float64)
    fold = evaluate_portfolio_scores(
        labels,
        returns,
        scores,
        buy_threshold=0.6,
        cost_bps=24.0,
        holding_period_bars=2,
    )

    aggregate = aggregate_portfolio_folds((fold, fold))

    assert aggregate.folds == 2
    assert aggregate.trades == 6
    assert aggregate.gross_return == pytest.approx((1.0 + fold.gross_return) ** 2 - 1.0)
    assert aggregate.net_return == pytest.approx((1.0 + fold.net_return) ** 2 - 1.0)
    assert portfolio_gate_failures(
        aggregate,
        min_folds=2,
        min_trades=6,
        min_profitable_trade_rate=0.5,
        min_net_return=0.0,
        min_worst_fold_net_return=-0.03,
        max_drawdown=0.10,
    ) == ()


def test_portfolio_evaluation_rejects_misaligned_scores() -> None:
    with pytest.raises(ValidationError, match="aligned"):
        evaluate_portfolio_scores(
            np.zeros((8, 3), dtype=np.uint8),
            np.zeros((8, 3), dtype=np.float64),
            np.zeros((8, 2), dtype=np.float64),
            buy_threshold=0.6,
            cost_bps=24.0,
            holding_period_bars=2,
        )
