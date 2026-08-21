from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Sequence

import numpy as np

from .multi_asset_market import FIVE_MINUTES_MS
from .pipeline import DEFAULT_LABEL_HORIZON


HISTORICAL_REPLAY_SCHEMA_VERSION = "moheng.historical-replay.v2"


class HistoricalReplayError(ValueError):
    pass


def _finite(value: float, name: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise HistoricalReplayError(f"{name} must be finite")
    return parsed


@dataclass(frozen=True)
class ReplayBrokerConfig:
    starting_cash: float = 10_000.0
    allocation_fraction: float = 0.25
    fee_bps_per_side: float = 8.0
    slippage_bps_per_side: float = 4.0
    max_quote_volume_participation: float = 0.005
    minimum_notional: float = 10.0
    latency_bars: int = 1
    holding_period_bars: int = DEFAULT_LABEL_HORIZON
    checkpoint_stride_bars: int = 288
    capacity_handling: str = "reject"

    def __post_init__(self) -> None:
        numeric = {
            "starting_cash": self.starting_cash,
            "allocation_fraction": self.allocation_fraction,
            "fee_bps_per_side": self.fee_bps_per_side,
            "slippage_bps_per_side": self.slippage_bps_per_side,
            "max_quote_volume_participation": self.max_quote_volume_participation,
            "minimum_notional": self.minimum_notional,
        }
        parsed = {name: _finite(value, name) for name, value in numeric.items()}
        if (
            parsed["starting_cash"] <= 0.0
            or not 0.0 < parsed["allocation_fraction"] <= 1.0
            or not 0.0 <= parsed["fee_bps_per_side"] <= 500.0
            or not 0.0 <= parsed["slippage_bps_per_side"] <= 500.0
            or not 0.0 < parsed["max_quote_volume_participation"] <= 0.05
            or not 0.0 < parsed["minimum_notional"] <= parsed["starting_cash"]
            or isinstance(self.latency_bars, bool)
            or self.latency_bars < 1
            or isinstance(self.holding_period_bars, bool)
            or self.holding_period_bars < 1
            or isinstance(self.checkpoint_stride_bars, bool)
            or self.checkpoint_stride_bars < 1
            or not isinstance(self.capacity_handling, str)
            or self.capacity_handling not in {"reject", "clip"}
        ):
            raise HistoricalReplayError("replay broker configuration is invalid")

    @property
    def round_trip_cost_bps(self) -> float:
        return 2.0 * (self.fee_bps_per_side + self.slippage_bps_per_side)

    @property
    def execution_label_horizon_bars(self) -> int:
        return self.latency_bars + self.holding_period_bars

    @property
    def break_even_gross_return_bps(self) -> float:
        fee = self.fee_bps_per_side / 10_000.0
        slippage = self.slippage_bps_per_side / 10_000.0
        gross_multiplier = ((1.0 + slippage) * (1.0 + fee)) / (
            (1.0 - slippage) * (1.0 - fee)
        )
        return (gross_multiplier - 1.0) * 10_000.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "allocationFraction": self.allocation_fraction,
            "breakEvenGrossReturnBps": self.break_even_gross_return_bps,
            "capacityHandling": self.capacity_handling,
            "checkpointStrideBars": self.checkpoint_stride_bars,
            "executionLabelHorizonBars": self.execution_label_horizon_bars,
            "feeBpsPerSide": self.fee_bps_per_side,
            "holdingPeriodBars": self.holding_period_bars,
            "latencyBars": self.latency_bars,
            "maxQuoteVolumeParticipation": self.max_quote_volume_participation,
            "minimumNotional": self.minimum_notional,
            "roundTripCostBps": self.round_trip_cost_bps,
            "slippageBpsPerSide": self.slippage_bps_per_side,
            "startingCash": self.starting_cash,
        }


@dataclass(frozen=True)
class ReplayPolicy:
    edge_buffer_bps: float
    min_entry_spacing_bars: int

    def __post_init__(self) -> None:
        edge = _finite(self.edge_buffer_bps, "edge_buffer_bps")
        if (
            not 0.0 <= edge <= 1_000.0
            or isinstance(self.min_entry_spacing_bars, bool)
            or self.min_entry_spacing_bars < DEFAULT_LABEL_HORIZON
        ):
            raise HistoricalReplayError("replay policy is invalid")

    def to_dict(self, broker: ReplayBrokerConfig) -> dict[str, Any]:
        return {
            "edgeBufferBps": self.edge_buffer_bps,
            "minEntrySpacingBars": self.min_entry_spacing_bars,
            "requiredGrossReturnBps": (
                broker.round_trip_cost_bps + self.edge_buffer_bps
            ),
        }


@dataclass(frozen=True)
class ReplayEpisodeBinding:
    episode_id: str
    available_at_ms: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.episode_id, str)
            or not self.episode_id.startswith("replay_episode_")
            or isinstance(self.available_at_ms, bool)
            or self.available_at_ms < 0
        ):
            raise HistoricalReplayError("replay episode binding is invalid")


@dataclass
class _OpenPosition:
    trade_id: str
    instrument_index: int
    signal_index: int
    entry_index: int
    exit_index: int
    episode_index: int
    expected_gross_return: float
    quantity: float
    raw_entry_price: float
    entry_fill_price: float
    entry_notional: float
    entry_fee: float
    cash_out: float


def _iso_from_ms(value: int) -> str:
    from datetime import datetime, timezone

    return (
        datetime.fromtimestamp(value / 1_000, tz=timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _validated_inputs(
    instruments: Sequence[str],
    timestamps_ms: np.ndarray,
    candles: np.ndarray,
    expected_returns: np.ndarray,
    episode_indices: np.ndarray,
    episodes: Sequence[ReplayEpisodeBinding],
) -> tuple[tuple[str, ...], np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    names = tuple(instruments)
    timestamps = np.asarray(timestamps_ms)
    candle_values = np.asarray(candles, dtype=np.float64)
    expected = np.asarray(expected_returns, dtype=np.float64)
    episode_values = np.asarray(episode_indices)
    time_rows = int(timestamps.size)
    if (
        not 1 <= len(names) <= 8
        or len(names) != len(set(names))
        or any(not name.endswith("-USDT") for name in names)
        or timestamps.dtype != np.int64
        or timestamps.shape != (time_rows,)
        or time_rows <= DEFAULT_LABEL_HORIZON + 2
        or candle_values.shape != (time_rows, len(names), 7)
        or expected.shape != (time_rows, len(names))
        or episode_values.shape != (time_rows,)
        or not np.issubdtype(episode_values.dtype, np.integer)
        or not episodes
    ):
        raise HistoricalReplayError("historical replay arrays are not aligned")
    if (
        not np.all(np.diff(timestamps) == FIVE_MINUTES_MS)
        or not np.all(np.isfinite(candle_values))
        or not np.all(np.isfinite(expected))
        or np.any(np.abs(expected) > 1.0)
        or np.any(episode_values < 0)
        or np.any(episode_values >= len(episodes))
    ):
        raise HistoricalReplayError("historical replay arrays contain invalid values")
    opens = candle_values[:, :, 0]
    highs = candle_values[:, :, 1]
    lows = candle_values[:, :, 2]
    closes = candle_values[:, :, 3]
    volumes = candle_values[:, :, 4:7]
    if (
        np.any(opens <= 0.0)
        or np.any(highs < np.maximum(np.maximum(opens, closes), lows))
        or np.any(lows > np.minimum(np.minimum(opens, closes), highs))
        or np.any(volumes < 0.0)
    ):
        raise HistoricalReplayError("historical replay candle domain rules failed")
    for row_index, episode_index in enumerate(episode_values):
        if int(timestamps[row_index]) < episodes[int(episode_index)].available_at_ms:
            raise HistoricalReplayError(
                "replay prediction was available only after its decision timestamp"
            )
    for array in (timestamps, candle_values, expected, episode_values):
        array.flags.writeable = False
    return names, timestamps, candle_values, expected, episode_values


def run_historical_replay(
    instruments: Sequence[str],
    timestamps_ms: np.ndarray,
    candles: np.ndarray,
    expected_returns: np.ndarray,
    episode_indices: np.ndarray,
    episodes: Sequence[ReplayEpisodeBinding],
    *,
    policy: ReplayPolicy,
    broker: ReplayBrokerConfig,
) -> dict[str, Any]:
    """Replay frozen predictions through a causal next-bar SPOT cash ledger."""

    names, timestamps, candle_values, expected, episode_values = _validated_inputs(
        instruments,
        timestamps_ms,
        candles,
        expected_returns,
        episode_indices,
        episodes,
    )
    fee_rate = broker.fee_bps_per_side / 10_000.0
    slippage_rate = broker.slippage_bps_per_side / 10_000.0
    required_expected = (
        broker.round_trip_cost_bps + policy.edge_buffer_bps
    ) / 10_000.0
    time_rows = int(timestamps.size)
    last_signal_index = (
        time_rows - broker.latency_bars - broker.holding_period_bars - 1
    )
    cash = broker.starting_cash
    peak_equity = broker.starting_cash
    max_drawdown = 0.0
    exposure_bars = 0
    evaluated_decisions = 0
    qualifying_signals = 0
    orders_submitted = 0
    orders_rejected = 0
    orders_clipped = 0
    next_entry_signal_index = 0
    open_position: _OpenPosition | None = None
    pending_entry: tuple[int, int, int, float, int] | None = None
    trades: list[dict[str, Any]] = []
    checkpoints: list[dict[str, Any]] = []
    trades_by_instrument = {name: 0 for name in names}
    net_pnl_by_instrument = {name: 0.0 for name in names}
    rejection_reasons: dict[str, int] = {}
    gross_pnl = 0.0
    total_fees = 0.0
    estimated_slippage_cost = 0.0
    turnover_notional = 0.0
    total_capacity_clip_notional = 0.0

    for index in range(time_rows):
        if open_position is not None and index == open_position.exit_index:
            position = open_position
            raw_exit_price = float(
                candle_values[index, position.instrument_index, 0]
            )
            exit_fill_price = raw_exit_price * (1.0 - slippage_rate)
            exit_notional = position.quantity * exit_fill_price
            exit_fee = exit_notional * fee_rate
            cash += exit_notional - exit_fee
            raw_gross_pnl = position.quantity * (
                raw_exit_price - position.raw_entry_price
            )
            net_pnl = exit_notional - exit_fee - position.cash_out
            gross_pnl += raw_gross_pnl
            total_fees += exit_fee
            estimated_slippage_cost += position.quantity * (
                (position.entry_fill_price - position.raw_entry_price)
                + (raw_exit_price - exit_fill_price)
            )
            turnover_notional += exit_notional
            instrument = names[position.instrument_index]
            trades_by_instrument[instrument] += 1
            net_pnl_by_instrument[instrument] += net_pnl
            trades.append(
                {
                    "entryFillPrice": position.entry_fill_price,
                    "enteredAt": _iso_from_ms(int(timestamps[position.entry_index])),
                    "episodeId": episodes[position.episode_index].episode_id,
                    "exitFillPrice": exit_fill_price,
                    "exitedAt": _iso_from_ms(int(timestamps[index])),
                    "expectedGrossReturnBps": (
                        position.expected_gross_return * 10_000.0
                    ),
                    "fees": position.entry_fee + exit_fee,
                    "grossReturn": (
                        raw_exit_price / position.raw_entry_price - 1.0
                    ),
                    "instrument": instrument,
                    "netPnl": net_pnl,
                    "netReturnOnCommittedCash": net_pnl / position.cash_out,
                    "quantity": position.quantity,
                    "signalAt": _iso_from_ms(
                        int(timestamps[position.signal_index])
                    ),
                    "tradeId": position.trade_id,
                }
            )
            open_position = None

        if pending_entry is not None and index == pending_entry[0]:
            (
                _entry_index,
                signal_index,
                asset_index,
                expected_gross,
                episode_index,
            ) = pending_entry
            orders_submitted += 1
            raw_entry_price = float(candle_values[index, asset_index, 0])
            entry_fill_price = raw_entry_price * (1.0 + slippage_rate)
            available_cash = cash * broker.allocation_fraction
            desired_entry_notional = available_cash / (1.0 + fee_rate)
            entry_notional = desired_entry_notional
            quote_volume = float(candle_values[index, asset_index, 6])
            capacity = quote_volume * broker.max_quote_volume_participation
            rejection: str | None = None
            if entry_notional < broker.minimum_notional:
                rejection = "minimum_notional_unavailable"
            elif entry_notional > capacity:
                if (
                    broker.capacity_handling == "clip"
                    and capacity >= broker.minimum_notional
                ):
                    entry_notional = capacity
                    orders_clipped += 1
                    total_capacity_clip_notional += (
                        desired_entry_notional - entry_notional
                    )
                else:
                    rejection = "quote_volume_capacity_exceeded"
            if rejection is not None:
                orders_rejected += 1
                rejection_reasons[rejection] = rejection_reasons.get(rejection, 0) + 1
            else:
                quantity = entry_notional / entry_fill_price
                entry_fee = entry_notional * fee_rate
                cash_out = entry_notional + entry_fee
                cash -= cash_out
                total_fees += entry_fee
                estimated_slippage_cost += quantity * (
                    entry_fill_price - raw_entry_price
                )
                turnover_notional += entry_notional
                open_position = _OpenPosition(
                    trade_id=f"replay_trade_{signal_index:09d}_{asset_index:02d}",
                    instrument_index=asset_index,
                    signal_index=signal_index,
                    entry_index=index,
                    exit_index=index + broker.holding_period_bars,
                    episode_index=episode_index,
                    expected_gross_return=expected_gross,
                    quantity=quantity,
                    raw_entry_price=raw_entry_price,
                    entry_fill_price=entry_fill_price,
                    entry_notional=entry_notional,
                    entry_fee=entry_fee,
                    cash_out=cash_out,
                )
            pending_entry = None

        position_market_value = 0.0
        if open_position is not None:
            exposure_bars += 1
            close_price = float(
                candle_values[index, open_position.instrument_index, 3]
            )
            liquidation_notional = (
                open_position.quantity * close_price * (1.0 - slippage_rate)
            )
            position_market_value = liquidation_notional * (1.0 - fee_rate)
        equity = cash + position_market_value
        peak_equity = max(peak_equity, equity)
        drawdown = (peak_equity - equity) / peak_equity if peak_equity else 1.0
        max_drawdown = max(max_drawdown, drawdown)
        if (
            index == 0
            or index == time_rows - 1
            or index % broker.checkpoint_stride_bars == 0
        ):
            checkpoints.append(
                {
                    "at": _iso_from_ms(int(timestamps[index])),
                    "cash": cash,
                    "drawdown": drawdown,
                    "equity": equity,
                    "positionInstrument": (
                        names[open_position.instrument_index]
                        if open_position is not None
                        else None
                    ),
                    "positionMarketValue": position_market_value,
                }
            )

        if (
            index <= last_signal_index
            and open_position is None
            and pending_entry is None
            and index >= next_entry_signal_index
        ):
            evaluated_decisions += 1
            asset_index = int(np.argmax(expected[index]))
            expected_gross = float(expected[index, asset_index])
            if expected_gross > required_expected:
                qualifying_signals += 1
                pending_entry = (
                    index + broker.latency_bars,
                    index,
                    asset_index,
                    expected_gross,
                    int(episode_values[index]),
                )
                next_entry_signal_index = index + policy.min_entry_spacing_bars

    if open_position is not None or pending_entry is not None:
        raise HistoricalReplayError("historical replay ended with unsettled state")
    final_equity = cash
    total_days = time_rows / 288.0
    profitable_trades = sum(item["netPnl"] > 0.0 for item in trades)
    return {
        "broker": broker.to_dict(),
        "cashBarRate": 1.0 - exposure_bars / time_rows,
        "checkpoints": checkpoints,
        "evaluatedDecisions": evaluated_decisions,
        "exposureBars": exposure_bars,
        "finalCash": final_equity,
        "grossPnlReturn": gross_pnl / broker.starting_cash,
        "leakageGuard": {
            "causalEpisodeBinding": True,
            "nextBarExecution": True,
            "predictionRowsAvailableBeforeDecision": True,
            "sameBarFillAllowed": False,
        },
        "maxDrawdown": max_drawdown,
        "netPnlByInstrument": net_pnl_by_instrument,
        "netReturn": final_equity / broker.starting_cash - 1.0,
        "ordersRejected": orders_rejected,
        "ordersClipped": orders_clipped,
        "ordersSubmitted": orders_submitted,
        "policy": policy.to_dict(broker),
        "profitableTradeRate": (
            profitable_trades / len(trades) if trades else 0.0
        ),
        "qualifyingSignals": qualifying_signals,
        "rejectionReasons": rejection_reasons,
        "simulatedDays": total_days,
        "timeRows": time_rows,
        "totalEstimatedSlippageCost": estimated_slippage_cost,
        "totalCapacityClipNotional": total_capacity_clip_notional,
        "totalFees": total_fees,
        "trades": trades,
        "tradesByInstrument": trades_by_instrument,
        "tradesPerDay": len(trades) / total_days,
        "turnoverMultiple": turnover_notional / broker.starting_cash,
    }


__all__ = [
    "HISTORICAL_REPLAY_SCHEMA_VERSION",
    "HistoricalReplayError",
    "ReplayBrokerConfig",
    "ReplayEpisodeBinding",
    "ReplayPolicy",
    "run_historical_replay",
]
