from __future__ import annotations

import numpy as np
import pytest

from okx_demo_lab.ml.multi_asset_research import PreparedMultiAssetDataset
from okx_demo_lab.ml.walk_forward import WalkForwardSpec, plan_walk_forward
import research.multi_asset_benchmark as benchmark_module
from research.multi_asset_benchmark import (
    MultiAssetBenchmarkError,
    _evaluate_scores,
    _inside_research_subdir,
    _write_report,
)


def test_benchmark_result_is_always_research_only_and_shadow_blocked() -> None:
    time_rows = 70
    instruments = ("BTC-USDT", "ETH-USDT", "SOL-USDT")
    dataset = PreparedMultiAssetDataset(
        timestamps_ms=np.arange(time_rows, dtype=np.int64) * 300_000 + 300_000,
        features=np.zeros((time_rows, 3, 2), dtype=np.float32),
        labels=np.ones((time_rows, 3), dtype=np.uint8),
        forward_returns=np.full((time_rows, 3), 0.01, dtype=np.float64),
        feature_names=("signal", "asset"),
        instruments=instruments,
        cohort_id="cohort_" + "a" * 24,
        cohort_sha256="a" * 64,
        label_contract_sha256="b" * 64,
    )
    folds = plan_walk_forward(
        time_rows,
        WalkForwardSpec(
            train_size=20,
            test_size=16,
            step_size=16,
            label_horizon=12,
            embargo_size=1,
            expanding=False,
        ),
    )
    scores = [
        np.full((fold.test_stop - fold.test_start, 3), 0.9, dtype=np.float32)
        for fold in folds
    ]

    result = _evaluate_scores(dataset, folds, scores)

    assert result["decision"] == "research_only"
    assert result["sealed"]["exploratoryGatePassed"] is False
    assert "sealed_folds_unavailable" in result["sealed"]["failures"]
    assert "fixed_current_survivor_cohort" in result["promotionBlockers"]
    assert "requires_90_day_forward_public_shadow" in result["promotionBlockers"]
    assert "manual_model_review_required" in result["promotionBlockers"]


def test_benchmark_paths_are_confined_to_their_research_subdirectories(
    tmp_path, monkeypatch
) -> None:
    root = tmp_path / ".research-data"
    monkeypatch.setattr(benchmark_module, "RESEARCH_DATA_ROOT", root)
    allowed = root / "benchmarks" / "result.json"

    assert _inside_research_subdir(allowed, "benchmarks") == allowed.resolve()
    with pytest.raises(MultiAssetBenchmarkError, match="benchmarks"):
        _inside_research_subdir(root / "multi-asset-market.sqlite3", "benchmarks")


def test_benchmark_evidence_cannot_be_overwritten(tmp_path) -> None:
    output = tmp_path / "result.json"
    output.write_text("existing evidence\n", encoding="utf-8")

    with pytest.raises(MultiAssetBenchmarkError, match="already exists"):
        _write_report(output, {"reportSha256": "a" * 64})

    assert output.read_text(encoding="utf-8") == "existing evidence\n"
