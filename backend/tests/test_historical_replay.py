from __future__ import annotations

import numpy as np
import pytest

from okx_demo_lab.ml.historical_replay import (
    HistoricalReplayError,
    ReplayBrokerConfig,
    ReplayEpisodeBinding,
    ReplayPolicy,
    run_historical_replay,
)


def _market(rows: int = 80) -> tuple[np.ndarray, np.ndarray]:
    timestamps = np.arange(rows, dtype=np.int64) * 300_000 + 300_000
    candles = np.zeros((rows, 3, 7), dtype=np.float64)
    for asset in range(3):
        opens = 100.0 + asset * 10.0 + np.arange(rows) * (0.08 + asset * 0.01)
        closes = opens + 0.04
        candles[:, asset, 0] = opens
        candles[:, asset, 1] = closes + 0.10
        candles[:, asset, 2] = opens - 0.10
        candles[:, asset, 3] = closes
        candles[:, asset, 4] = 1_000.0
        candles[:, asset, 5] = 100_000.0
        candles[:, asset, 6] = 1_000_000.0
    return timestamps, candles


def _run(expected: np.ndarray, **overrides: object) -> dict[str, object]:
    timestamps, candles = _market(expected.shape[0])
    broker = ReplayBrokerConfig(
        checkpoint_stride_bars=12,
        **overrides,
    )
    return run_historical_replay(
        ("BTC-USDT", "ETH-USDT", "SOL-USDT"),
        timestamps,
        candles,
        expected,
        np.zeros(expected.shape[0], dtype=np.int64),
        (ReplayEpisodeBinding("replay_episode_000", int(timestamps[0])),),
        policy=ReplayPolicy(edge_buffer_bps=12.0, min_entry_spacing_bars=24),
        broker=broker,
    )


def test_replay_uses_next_bar_fill_one_cash_ledger_and_costs() -> None:
    expected = np.zeros((80, 3), dtype=np.float64)
    expected[2, 0] = 0.02

    result = _run(expected)

    trades = result["trades"]
    assert isinstance(trades, list) and len(trades) == 1
    trade = trades[0]
    assert trade["signalAt"] == "1970-01-01T00:15:00.000Z"
    assert trade["enteredAt"] == "1970-01-01T00:20:00.000Z"
    assert trade["exitedAt"] == "1970-01-01T01:20:00.000Z"
    assert trade["entryFillPrice"] > 100.0
    assert result["ordersSubmitted"] == 1
    assert result["ordersRejected"] == 0
    assert result["exposureBars"] == 12
    assert result["totalFees"] > 0.0
    assert result["totalEstimatedSlippageCost"] > 0.0
    assert result["leakageGuard"] == {
        "causalEpisodeBinding": True,
        "nextBarExecution": True,
        "predictionRowsAvailableBeforeDecision": True,
        "sameBarFillAllowed": False,
    }


def test_replay_rejects_order_above_historical_volume_capacity() -> None:
    expected = np.zeros((80, 3), dtype=np.float64)
    expected[2, 0] = 0.02

    result = _run(expected, max_quote_volume_participation=0.000001)

    assert result["ordersSubmitted"] == 1
    assert result["ordersRejected"] == 1
    assert result["trades"] == []
    assert result["rejectionReasons"] == {"quote_volume_capacity_exceeded": 1}


def test_replay_can_clip_notional_to_historical_volume_capacity() -> None:
    expected = np.zeros((80, 3), dtype=np.float64)
    expected[2, 0] = 0.02

    result = _run(
        expected,
        capacity_handling="clip",
        max_quote_volume_participation=0.0001,
    )

    assert result["ordersSubmitted"] == 1
    assert result["ordersRejected"] == 0
    assert result["ordersClipped"] == 1
    assert result["totalCapacityClipNotional"] > 0.0
    assert len(result["trades"]) == 1
    assert result["broker"]["capacityHandling"] == "clip"
    assert result["broker"]["executionLabelHorizonBars"] == 13


@pytest.mark.parametrize("capacity_handling", ["scale", None, []])
def test_replay_rejects_invalid_capacity_handling(capacity_handling: object) -> None:
    with pytest.raises(HistoricalReplayError, match="configuration is invalid"):
        ReplayBrokerConfig(capacity_handling=capacity_handling)  # type: ignore[arg-type]


def test_future_signal_mutation_cannot_change_earlier_replay_trade() -> None:
    baseline = np.zeros((80, 3), dtype=np.float64)
    baseline[2, 0] = 0.02
    changed = baseline.copy()
    changed[50:, 1] = 0.50

    first = _run(baseline)
    second = _run(changed)

    first_trade = first["trades"][0]
    second_trade = second["trades"][0]
    assert first_trade == second_trade
    assert first_trade["signalAt"] < "1970-01-01T04:15:00.000Z"


def test_prediction_episode_must_be_available_before_decision() -> None:
    timestamps, candles = _market()
    expected = np.zeros((80, 3), dtype=np.float64)

    with pytest.raises(HistoricalReplayError, match="available only after"):
        run_historical_replay(
            ("BTC-USDT", "ETH-USDT", "SOL-USDT"),
            timestamps,
            candles,
            expected,
            np.zeros(80, dtype=np.int64),
            (
                ReplayEpisodeBinding(
                    "replay_episode_future",
                    int(timestamps[1]),
                ),
            ),
            policy=ReplayPolicy(edge_buffer_bps=12.0, min_entry_spacing_bars=24),
            broker=ReplayBrokerConfig(),
        )


def test_policy_uses_actual_round_trip_cost_plus_edge_buffer() -> None:
    expected = np.zeros((80, 3), dtype=np.float64)
    expected[2, 0] = 0.0040

    ordinary = _run(expected)
    stress = _run(expected, slippage_bps_per_side=16.0)

    assert ordinary["policy"]["requiredGrossReturnBps"] == 36.0
    assert stress["policy"]["requiredGrossReturnBps"] == 60.0
    assert len(ordinary["trades"]) == 1
    assert stress["trades"] == []
