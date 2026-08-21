from __future__ import annotations

import numpy as np
import pytest

from okx_demo_lab.ml.walk_forward import ValidationError, evaluate_score_vector


def test_score_vector_uses_non_overlapping_cash_spot_capital() -> None:
    labels = np.array([1, 1, 1, 1, 0, 0, 0], dtype=np.uint8)
    returns = np.array([0.02, 0.50, 0.50, 0.03, -0.01, 0.0, 0.0])
    scores = np.array([0.9, 0.9, 0.9, 0.9, 0.1, 0.1, 0.1])

    trades, accuracy, gross, net, drawdown, evaluated = evaluate_score_vector(
        labels,
        returns,
        scores,
        buy_threshold=0.6,
        cost_bps=20.0,
        holding_period_bars=3,
    )

    assert trades == 2
    assert evaluated == 2
    assert accuracy == 1.0
    assert gross == pytest.approx((1.02 * 1.03) - 1.0)
    assert net == pytest.approx((1.018 * 1.028) - 1.0)
    assert drawdown == 0.0


@pytest.mark.parametrize(
    ("labels", "returns", "scores", "message"),
    [
        ([0, 1], [0.0], [0.1, 0.9], "aligned"),
        ([0, 2], [0.0, 0.0], [0.1, 0.9], "invalid values"),
        ([0, 1], [0.0, 0.0], [0.1, 1.1], "invalid values"),
    ],
)
def test_score_vector_rejects_malformed_external_predictions(
    labels: list[int],
    returns: list[float],
    scores: list[float],
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        evaluate_score_vector(
            labels,
            returns,
            scores,
            buy_threshold=0.6,
            cost_bps=24.0,
            holding_period_bars=1,
        )
