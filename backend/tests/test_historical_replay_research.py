from __future__ import annotations

import numpy as np
import pytest

from okx_demo_lab.ml.historical_replay import ReplayEpisodeBinding
import research.historical_replay as replay_module
from research.historical_replay import (
    EXECUTION_MODEL_FAMILY,
    ReplayResearchError,
    _execution_aligned_targets,
    _evaluate_policies,
    _inside_research_subdir,
    _write_report,
    replay_walk_forward_spec,
)


def _replay_arrays(rows: int = 500) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    timestamps = np.arange(rows, dtype=np.int64) * 300_000 + 300_000
    candles = np.zeros((rows, 3, 7), dtype=np.float64)
    for asset in range(3):
        prices = 100.0 + asset * 10.0 + np.arange(rows) * 0.03
        candles[:, asset, 0] = prices
        candles[:, asset, 1] = prices + 0.10
        candles[:, asset, 2] = prices - 0.10
        candles[:, asset, 3] = prices + 0.04
        candles[:, asset, 4] = 10_000.0
        candles[:, asset, 5] = 1_000_000.0
        candles[:, asset, 6] = 10_000_000.0
    expected = np.zeros((rows, 3), dtype=np.float64)
    expected[::30, 0] = 0.02
    return timestamps, candles, expected


def test_replay_protocol_is_rolling_365_day_training_and_30_day_retraining() -> None:
    spec = replay_walk_forward_spec()

    assert spec.train_size == 365 * 288
    assert spec.test_size == 30 * 288
    assert spec.step_size == 30 * 288
    assert spec.label_horizon == 13
    assert spec.embargo_size == 1
    assert spec.expanding is False


def test_policy_replay_is_always_research_only_and_shadow_days_stay_zero() -> None:
    timestamps, candles, expected = _replay_arrays()

    result = _evaluate_policies(
        ("BTC-USDT", "ETH-USDT", "SOL-USDT"),
        timestamps,
        candles,
        expected,
        np.zeros(timestamps.size, dtype=np.int32),
        (ReplayEpisodeBinding("replay_episode_test", int(timestamps[0])),),
    )

    assert result["decision"] == "research_only"
    assert result["shadowDaysCredited"] == 0
    assert "historical_replay_development_only" in result["promotionBlockers"]
    assert "requires_90_day_forward_public_shadow" in result["promotionBlockers"]
    assert len(result["policySensitivity"]) == 6
    assert result["chosenPolicy"]["edgeBufferBps"] == 72.0
    assert result["chosenPolicy"]["minEntrySpacingBars"] == 12
    assert result["historicalSelectionBias"]["resultMayBeOptimistic"] is True
    assert result["ordinary"]["leakageGuard"]["sameBarFillAllowed"] is False
    assert result["stress48Bps"]["broker"]["roundTripCostBps"] == 48.0
    assert result["ordinary"]["broker"]["capacityHandling"] == "clip"


def test_execution_targets_match_next_open_and_fixed_horizon_exit() -> None:
    _, candles, _ = _replay_arrays(rows=80)
    candles[:, :, 0] = np.arange(80, dtype=np.float64)[:, None] + np.asarray(
        [100.0, 200.0, 300.0]
    )

    labels, returns = _execution_aligned_targets(
        candles,
        raw_offset=2,
        time_rows=20,
    )

    expected = candles[15:35, :, 0] / candles[3:23, :, 0] - 1.0
    assert np.allclose(returns, expected)
    assert np.array_equal(
        labels,
        expected
        > replay_module.STANDARD_BROKER.break_even_gross_return_bps / 10_000.0,
    )
    assert EXECUTION_MODEL_FAMILY == "execution_hist_gradient_boosting"


def test_replay_paths_are_confined_to_project_research_subdirectories(
    tmp_path, monkeypatch
) -> None:
    root = tmp_path / ".research-data"
    monkeypatch.setattr(replay_module, "RESEARCH_DATA_ROOT", root)
    allowed = root / "replays" / "report.json"

    assert _inside_research_subdir(allowed, "replays") == allowed.resolve()
    with pytest.raises(ReplayResearchError, match="replays"):
        _inside_research_subdir(root / "credentials.json", "replays")


def test_replay_evidence_cannot_be_overwritten(tmp_path) -> None:
    output = tmp_path / "replay.json"
    output.write_text("existing evidence\n", encoding="utf-8")

    with pytest.raises(ReplayResearchError, match="already exists"):
        _write_report(output, {"reportSha256": "a" * 64})

    assert output.read_text(encoding="utf-8") == "existing evidence\n"
