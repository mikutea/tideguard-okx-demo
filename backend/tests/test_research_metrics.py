from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from okx_demo_lab.ml.research import (
    ResearchFold,
    ResearchModelSpec,
    aggregate_research_folds,
    research_gate_failures,
)
from okx_demo_lab.ml.walk_forward import FoldMetrics, ValidationError


def _fold(index: int, net: float, drawdown: float, accuracy: float) -> ResearchFold:
    start = datetime(2025, 1, 1, tzinfo=timezone.utc) + timedelta(days=index)
    return ResearchFold(
        metrics=FoldMetrics(
            fold=index,
            train_start_at=start - timedelta(days=365),
            train_stop_at=start - timedelta(days=1),
            test_start_at=start,
            test_stop_at=start + timedelta(hours=1),
            train_rows=100,
            test_rows=20,
            trades=2,
            accuracy=accuracy,
            gross_return=net + 0.01,
            net_return=net,
            max_drawdown=drawdown,
        ),
        evaluated_rows=10,
    )


def test_research_aggregate_compounds_folds_and_applies_gates() -> None:
    aggregate = aggregate_research_folds(
        (_fold(0, 0.02, 0.01, 0.6), _fold(1, -0.01, 0.02, 0.5))
    )

    assert aggregate.net_return == pytest.approx(1.02 * 0.99 - 1.0)
    assert aggregate.accuracy == pytest.approx(0.55)
    assert aggregate.trades == 4
    assert aggregate.worst_fold_net_return == -0.01
    assert research_gate_failures(
        aggregate,
        min_folds=2,
        min_trades=4,
        min_accuracy=0.52,
        min_net_return=0.005,
        min_worst_fold_net_return=-0.03,
        max_drawdown=0.10,
    ) == ()


def test_research_model_spec_is_content_addressed_and_data_only() -> None:
    mutable = {"depth": 4, "seed": 0, "layers": [8, 4]}
    left = ResearchModelSpec("tree", "example", "1.0", mutable)
    right = ResearchModelSpec(
        "tree", "example", "1.0", {"layers": [8, 4], "seed": 0, "depth": 4}
    )

    frozen_sha = left.sha256
    mutable["depth"] = 99
    mutable["layers"].append(2)
    assert left.sha256 == frozen_sha
    assert left.to_dict()["parameters"] == {"depth": 4, "layers": [8, 4], "seed": 0}
    assert left.sha256 == right.sha256
    assert len(left.sha256) == 64
    with pytest.raises(ValidationError, match="canonical JSON"):
        ResearchModelSpec("tree", "example", "1.0", {"payload": object()})
