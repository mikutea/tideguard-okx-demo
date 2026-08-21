from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Sequence

import numpy as np

from .multi_asset_cohort import MultiAssetCohortError, ValidatedCohort
from .pipeline import (
    DEFAULT_LABEL_HORIZON,
    FEATURE_NAMES,
    DatasetError,
    prepare_training_arrays,
)
from .walk_forward import TrainingConfig, ValidationError


MULTI_ASSET_RESEARCH_SCHEMA_VERSION = "moheng.multi-asset-research.v2"
MARKET_FEATURES = (
    "market_return_1",
    "market_return_12",
    "market_return_48",
    "relative_return_1",
    "relative_return_12",
    "relative_return_48",
)
_RETURN_FEATURE_INDICES = (0, 2, 4)


def _asset_feature_name(instrument: str) -> str:
    return "asset_" + re.sub(r"[^a-z0-9]+", "_", instrument.lower()).strip("_")


@dataclass(frozen=True)
class PreparedMultiAssetDataset:
    timestamps_ms: np.ndarray
    features: np.ndarray
    labels: np.ndarray
    forward_returns: np.ndarray
    feature_names: tuple[str, ...]
    instruments: tuple[str, ...]
    cohort_id: str
    cohort_sha256: str
    label_contract_sha256: str

    def __post_init__(self) -> None:
        time_rows = int(self.timestamps_ms.size)
        asset_rows = len(self.instruments)
        if (
            self.timestamps_ms.dtype != np.int64
            or self.timestamps_ms.shape != (time_rows,)
            or self.features.shape != (time_rows, asset_rows, len(self.feature_names))
            or self.labels.shape != (time_rows, asset_rows)
            or self.forward_returns.shape != (time_rows, asset_rows)
        ):
            raise ValidationError("multi-asset research arrays are not aligned")
        if (
            time_rows < 2
            or asset_rows < 3
            or len(self.instruments) != len(set(self.instruments))
            or not np.all(np.diff(self.timestamps_ms) == 300_000)
            or not np.all(np.isfinite(self.features))
            or not np.all(np.isfinite(self.forward_returns))
            or np.any((self.labels != 0) & (self.labels != 1))
        ):
            raise ValidationError("multi-asset research arrays contain invalid values")

    @property
    def time_rows(self) -> int:
        return int(self.timestamps_ms.size)

    @property
    def asset_rows(self) -> int:
        return len(self.instruments)

    def flat_window(self, start: int, stop: int) -> tuple[np.ndarray, np.ndarray]:
        if not 0 <= start < stop <= self.time_rows:
            raise ValidationError("multi-asset training window is invalid")
        return (
            self.features[start:stop].reshape(-1, len(self.feature_names)),
            self.labels[start:stop].reshape(-1),
        )


@dataclass(frozen=True)
class PortfolioFoldMetrics:
    time_rows: int
    evaluated_decisions: int
    trades: int
    label_hits: int
    profitable_trades: int
    exposure_bars: int
    gross_return: float
    net_return: float
    max_drawdown: float
    sum_gross_returns: float
    sum_net_returns: float
    round_trip_cost_bps: float
    trades_by_instrument: tuple[int, ...]

    def __post_init__(self) -> None:
        if (
            self.time_rows < 1
            or self.evaluated_decisions < 1
            or self.trades < 0
            or self.trades > self.evaluated_decisions
            or not 0 <= self.label_hits <= self.trades
            or not 0 <= self.profitable_trades <= self.trades
            or not 0 <= self.exposure_bars <= self.time_rows
            or len(self.trades_by_instrument) < 3
            or any(item < 0 for item in self.trades_by_instrument)
            or sum(self.trades_by_instrument) != self.trades
            or any(
                not math.isfinite(value)
                for value in (
                    self.gross_return,
                    self.net_return,
                    self.max_drawdown,
                    self.sum_gross_returns,
                    self.sum_net_returns,
                    self.round_trip_cost_bps,
                )
            )
            or not 0.0 <= self.max_drawdown <= 1.0
            or not 0.0 < self.round_trip_cost_bps <= 1_000.0
        ):
            raise ValidationError("portfolio fold metrics are invalid")

    @property
    def label_precision(self) -> float:
        return self.label_hits / self.trades if self.trades else 0.0

    @property
    def profitable_trade_rate(self) -> float:
        return self.profitable_trades / self.trades if self.trades else 0.0

    @property
    def entry_rate(self) -> float:
        return self.trades / self.evaluated_decisions

    @property
    def cash_bar_rate(self) -> float:
        return 1.0 - self.exposure_bars / self.time_rows

    @property
    def average_gross_return(self) -> float:
        return self.sum_gross_returns / self.trades if self.trades else 0.0

    @property
    def average_net_return(self) -> float:
        return self.sum_net_returns / self.trades if self.trades else 0.0

    @property
    def trades_per_day(self) -> float:
        return self.trades / (self.time_rows / 288.0)

    def to_dict(self, instruments: Sequence[str]) -> dict[str, Any]:
        if len(instruments) != len(self.trades_by_instrument):
            raise ValidationError("portfolio instrument metrics are not aligned")
        return {
            "averageGrossReturn": self.average_gross_return,
            "averageNetReturn": self.average_net_return,
            "cashBarRate": self.cash_bar_rate,
            "entryRate": self.entry_rate,
            "evaluatedDecisions": self.evaluated_decisions,
            "exposureBars": self.exposure_bars,
            "grossReturn": self.gross_return,
            "labelPrecision": self.label_precision,
            "maxDrawdown": self.max_drawdown,
            "netReturn": self.net_return,
            "profitableTradeRate": self.profitable_trade_rate,
            "timeRows": self.time_rows,
            "trades": self.trades,
            "tradesPerDay": self.trades_per_day,
            "tradesByInstrument": dict(
                zip(instruments, self.trades_by_instrument, strict=True)
            ),
        }


@dataclass(frozen=True)
class PortfolioAggregate:
    folds: int
    time_rows: int
    evaluated_decisions: int
    trades: int
    label_hits: int
    profitable_trades: int
    exposure_bars: int
    gross_return: float
    net_return: float
    max_drawdown: float
    sum_gross_returns: float
    sum_net_returns: float
    round_trip_cost_bps: float
    worst_fold_gross_return: float
    worst_fold_net_return: float
    trades_by_instrument: tuple[int, ...]

    def __post_init__(self) -> None:
        if (
            self.folds < 1
            or self.time_rows < 1
            or self.evaluated_decisions < 1
            or self.trades < 0
            or self.trades > self.evaluated_decisions
            or not 0 <= self.label_hits <= self.trades
            or not 0 <= self.profitable_trades <= self.trades
            or not 0 <= self.exposure_bars <= self.time_rows
            or len(self.trades_by_instrument) < 3
            or any(item < 0 for item in self.trades_by_instrument)
            or sum(self.trades_by_instrument) != self.trades
            or any(
                not math.isfinite(value)
                for value in (
                    self.gross_return,
                    self.net_return,
                    self.max_drawdown,
                    self.sum_gross_returns,
                    self.sum_net_returns,
                    self.round_trip_cost_bps,
                    self.worst_fold_gross_return,
                    self.worst_fold_net_return,
                )
            )
            or not 0.0 <= self.max_drawdown <= 1.0
            or not 0.0 < self.round_trip_cost_bps <= 1_000.0
        ):
            raise ValidationError("portfolio aggregate metrics are invalid")

    @property
    def label_precision(self) -> float:
        return self.label_hits / self.trades if self.trades else 0.0

    @property
    def profitable_trade_rate(self) -> float:
        return self.profitable_trades / self.trades if self.trades else 0.0

    @property
    def entry_rate(self) -> float:
        return self.trades / self.evaluated_decisions

    @property
    def cash_bar_rate(self) -> float:
        return 1.0 - self.exposure_bars / self.time_rows

    @property
    def average_gross_return(self) -> float:
        return self.sum_gross_returns / self.trades if self.trades else 0.0

    @property
    def average_net_return(self) -> float:
        return self.sum_net_returns / self.trades if self.trades else 0.0

    @property
    def trades_per_day(self) -> float:
        return self.trades / (self.time_rows / 288.0)

    @property
    def max_instrument_trade_share(self) -> float:
        return max(self.trades_by_instrument, default=0) / self.trades if self.trades else 0.0

    def to_dict(self, instruments: Sequence[str]) -> dict[str, Any]:
        return {
            "averageGrossReturn": self.average_gross_return,
            "averageNetReturn": self.average_net_return,
            "cashBarRate": self.cash_bar_rate,
            "entryRate": self.entry_rate,
            "evaluatedDecisions": self.evaluated_decisions,
            "exposureBars": self.exposure_bars,
            "folds": self.folds,
            "grossReturn": self.gross_return,
            "labelPrecision": self.label_precision,
            "maxDrawdown": self.max_drawdown,
            "netReturn": self.net_return,
            "profitableTradeRate": self.profitable_trade_rate,
            "roundTripCostBps": self.round_trip_cost_bps,
            "timeRows": self.time_rows,
            "trades": self.trades,
            "tradesPerDay": self.trades_per_day,
            "tradesByInstrument": dict(
                zip(instruments, self.trades_by_instrument, strict=True)
            ),
            "maxInstrumentTradeShare": self.max_instrument_trade_share,
            "worstFoldGrossReturn": self.worst_fold_gross_return,
            "worstFoldNetReturn": self.worst_fold_net_return,
        }


def prepare_multi_asset_dataset(
    cohort: ValidatedCohort,
    *,
    now: datetime,
    training_config: TrainingConfig | None = None,
) -> PreparedMultiAssetDataset:
    """Prepare causal per-asset and cross-sectional features from one cohort."""

    if now.tzinfo is None or now.utcoffset() is None:
        raise DatasetError("now must be timezone-aware")
    config = training_config or TrainingConfig(round_trip_cost_bps=24.0)
    manifest = cohort.manifest
    instruments_value = manifest.get("instruments")
    cohort_id = manifest.get("cohortId")
    cohort_sha256 = manifest.get("contentSha256")
    if (
        not isinstance(instruments_value, list)
        or tuple(instruments_value) == ()
        or not isinstance(cohort_id, str)
        or not isinstance(cohort_sha256, str)
        or manifest.get("promotable") is not False
    ):
        raise MultiAssetCohortError("validated cohort metadata is incomplete")
    instruments = tuple(str(item) for item in instruments_value)
    timestamps = np.asarray(cohort.timestamps, dtype=np.int64)
    candles = np.asarray(cohort.candles)
    asset_count = len(instruments)
    core_features: np.ndarray | None = None
    labels: np.ndarray | None = None
    forward_returns: np.ndarray | None = None
    observed_at_ms: np.ndarray | None = None
    label_contract: str | None = None
    for asset_index in range(asset_count):
        prepared = prepare_training_arrays(
            timestamps,
            np.asarray(candles[:, asset_index, :5], dtype=np.float64),
            now=now.astimezone(timezone.utc),
            training_config=config,
        )
        observations = prepared.observations
        if core_features is None:
            time_rows = len(observations)
            core_features = np.empty(
                (time_rows, asset_count, len(FEATURE_NAMES)), dtype=np.float32
            )
            labels = np.empty((time_rows, asset_count), dtype=np.uint8)
            forward_returns = np.empty(
                (time_rows, asset_count), dtype=np.float64
            )
            observed_at_ms = np.asarray(observations.observed_at_ms, dtype=np.int64)
            label_contract = prepared.label_contract_sha256
        elif (
            label_contract != prepared.label_contract_sha256
            or not np.array_equal(observed_at_ms, observations.observed_at_ms)
        ):
            raise DatasetError("asset feature timelines or label contracts differ")
        core_features[:, asset_index, :] = observations.features
        labels[:, asset_index] = observations.labels
        forward_returns[:, asset_index] = observations.forward_returns
    if (
        core_features is None
        or labels is None
        or forward_returns is None
        or observed_at_ms is None
        or label_contract is None
    ):
        raise DatasetError("multi-asset cohort contains no usable instruments")

    selected = core_features[:, :, _RETURN_FEATURE_INDICES]
    market_returns = np.mean(selected, axis=1, dtype=np.float64).astype(np.float32)
    feature_names = (
        *FEATURE_NAMES,
        *MARKET_FEATURES,
        *tuple(_asset_feature_name(item) for item in instruments),
    )
    features = np.empty(
        (core_features.shape[0], asset_count, len(feature_names)), dtype=np.float32
    )
    features[:, :, : len(FEATURE_NAMES)] = core_features
    market_start = len(FEATURE_NAMES)
    features[:, :, market_start : market_start + 3] = market_returns[:, None, :]
    features[:, :, market_start + 3 : market_start + 6] = (
        selected - market_returns[:, None, :]
    )
    identity_start = market_start + len(MARKET_FEATURES)
    features[:, :, identity_start:] = np.eye(asset_count, dtype=np.float32)[None, :, :]
    if not np.all(np.isfinite(features)):
        raise DatasetError("multi-asset feature calculation produced non-finite values")
    return PreparedMultiAssetDataset(
        timestamps_ms=np.ascontiguousarray(observed_at_ms, dtype=np.int64),
        features=np.ascontiguousarray(features, dtype=np.float32),
        labels=np.ascontiguousarray(labels, dtype=np.uint8),
        forward_returns=np.ascontiguousarray(forward_returns, dtype=np.float64),
        feature_names=tuple(feature_names),
        instruments=instruments,
        cohort_id=cohort_id,
        cohort_sha256=cohort_sha256,
        label_contract_sha256=label_contract,
    )


def evaluate_portfolio_scores(
    labels: np.ndarray,
    forward_returns: np.ndarray,
    scores: np.ndarray,
    *,
    buy_threshold: float,
    cost_bps: float,
    holding_period_bars: int = DEFAULT_LABEL_HORIZON,
) -> PortfolioFoldMetrics:
    """Evaluate one cash SPOT position, selecting the highest score at each entry."""

    label_values = np.asarray(labels)
    return_values = np.asarray(forward_returns, dtype=np.float64)
    score_values = np.asarray(scores, dtype=np.float64)
    if (
        label_values.ndim != 2
        or label_values.shape != return_values.shape
        or label_values.shape != score_values.shape
        or label_values.shape[1] < 3
        or label_values.shape[0] <= holding_period_bars
    ):
        raise ValidationError("portfolio score matrices are not aligned")
    if (
        isinstance(holding_period_bars, bool)
        or holding_period_bars < 1
        or isinstance(buy_threshold, bool)
        or not math.isfinite(float(buy_threshold))
        or not 0.5 < float(buy_threshold) < 1.0
        or isinstance(cost_bps, bool)
        or not math.isfinite(float(cost_bps))
        or not 0.0 < float(cost_bps) <= 1_000.0
        or np.any((label_values != 0) & (label_values != 1))
        or not np.all(np.isfinite(return_values))
        or np.any(return_values <= -1.0)
        or np.any(return_values > 1.0)
        or not np.all(np.isfinite(score_values))
        or np.any(score_values < 0.0)
        or np.any(score_values > 1.0)
    ):
        raise ValidationError("portfolio score matrices contain invalid values")

    asset_count = label_values.shape[1]
    trades_by_instrument = np.zeros(asset_count, dtype=np.int64)
    cost = float(cost_bps) / 10_000.0
    cursor = 0
    entry_stop = label_values.shape[0] - holding_period_bars
    evaluated = 0
    trades = 0
    label_hits = 0
    profitable = 0
    sum_gross_returns = 0.0
    sum_net_returns = 0.0
    equity = 1.0
    gross_equity = 1.0
    peak = 1.0
    max_drawdown = 0.0
    while cursor < entry_stop:
        evaluated += 1
        asset_index = int(np.argmax(score_values[cursor]))
        if float(score_values[cursor, asset_index]) < float(buy_threshold):
            cursor += 1
            continue
        gross = float(return_values[cursor, asset_index])
        net = gross - cost
        trades += 1
        label_hits += int(label_values[cursor, asset_index] == 1)
        profitable += int(net > 0.0)
        sum_gross_returns += gross
        sum_net_returns += net
        trades_by_instrument[asset_index] += 1
        gross_equity *= max(0.0, 1.0 + gross)
        equity *= max(0.0, 1.0 + net)
        peak = max(peak, equity)
        max_drawdown = max(
            max_drawdown, (peak - equity) / peak if peak else 1.0
        )
        cursor += holding_period_bars
    return PortfolioFoldMetrics(
        time_rows=int(label_values.shape[0]),
        evaluated_decisions=evaluated,
        trades=trades,
        label_hits=label_hits,
        profitable_trades=profitable,
        exposure_bars=trades * holding_period_bars,
        gross_return=gross_equity - 1.0,
        net_return=equity - 1.0,
        max_drawdown=max_drawdown,
        sum_gross_returns=sum_gross_returns,
        sum_net_returns=sum_net_returns,
        round_trip_cost_bps=float(cost_bps),
        trades_by_instrument=tuple(int(item) for item in trades_by_instrument),
    )


def evaluate_cost_aware_portfolio(
    labels: np.ndarray,
    forward_returns: np.ndarray,
    expected_returns: np.ndarray,
    *,
    cost_bps: float,
    edge_buffer_bps: float,
    holding_period_bars: int = DEFAULT_LABEL_HORIZON,
    min_entry_spacing_bars: int = 96,
) -> PortfolioFoldMetrics:
    """Evaluate a low-turnover cash/long policy from calibrated gross returns."""

    label_values = np.asarray(labels)
    return_values = np.asarray(forward_returns, dtype=np.float64)
    expected_values = np.asarray(expected_returns, dtype=np.float64)
    if (
        label_values.ndim != 2
        or label_values.shape != return_values.shape
        or label_values.shape != expected_values.shape
        or label_values.shape[1] < 3
        or label_values.shape[0] <= holding_period_bars
    ):
        raise ValidationError("cost-aware portfolio matrices are not aligned")
    if (
        isinstance(holding_period_bars, bool)
        or holding_period_bars < 1
        or isinstance(min_entry_spacing_bars, bool)
        or min_entry_spacing_bars < holding_period_bars
        or isinstance(cost_bps, bool)
        or not math.isfinite(float(cost_bps))
        or not 0.0 < float(cost_bps) <= 1_000.0
        or isinstance(edge_buffer_bps, bool)
        or not math.isfinite(float(edge_buffer_bps))
        or not 0.0 <= float(edge_buffer_bps) <= 1_000.0
        or np.any((label_values != 0) & (label_values != 1))
        or not np.all(np.isfinite(return_values))
        or np.any(return_values <= -1.0)
        or np.any(return_values > 1.0)
        or not np.all(np.isfinite(expected_values))
        or np.any(np.abs(expected_values) > 1.0)
    ):
        raise ValidationError("cost-aware portfolio matrices contain invalid values")

    asset_count = label_values.shape[1]
    trades_by_instrument = np.zeros(asset_count, dtype=np.int64)
    cost = float(cost_bps) / 10_000.0
    required_gross_return = cost + float(edge_buffer_bps) / 10_000.0
    cursor = 0
    entry_stop = label_values.shape[0] - holding_period_bars
    evaluated = 0
    trades = 0
    label_hits = 0
    profitable = 0
    sum_gross_returns = 0.0
    sum_net_returns = 0.0
    equity = 1.0
    gross_equity = 1.0
    peak = 1.0
    max_drawdown = 0.0
    while cursor < entry_stop:
        evaluated += 1
        asset_index = int(np.argmax(expected_values[cursor]))
        if float(expected_values[cursor, asset_index]) <= required_gross_return:
            cursor += 1
            continue
        gross = float(return_values[cursor, asset_index])
        net = gross - cost
        trades += 1
        label_hits += int(label_values[cursor, asset_index] == 1)
        profitable += int(net > 0.0)
        sum_gross_returns += gross
        sum_net_returns += net
        trades_by_instrument[asset_index] += 1
        gross_equity *= max(0.0, 1.0 + gross)
        equity *= max(0.0, 1.0 + net)
        peak = max(peak, equity)
        max_drawdown = max(
            max_drawdown, (peak - equity) / peak if peak else 1.0
        )
        cursor += min_entry_spacing_bars
    return PortfolioFoldMetrics(
        time_rows=int(label_values.shape[0]),
        evaluated_decisions=evaluated,
        trades=trades,
        label_hits=label_hits,
        profitable_trades=profitable,
        exposure_bars=trades * holding_period_bars,
        gross_return=gross_equity - 1.0,
        net_return=equity - 1.0,
        max_drawdown=max_drawdown,
        sum_gross_returns=sum_gross_returns,
        sum_net_returns=sum_net_returns,
        round_trip_cost_bps=float(cost_bps),
        trades_by_instrument=tuple(int(item) for item in trades_by_instrument),
    )


def aggregate_portfolio_folds(
    folds: Sequence[PortfolioFoldMetrics],
) -> PortfolioAggregate:
    if not folds:
        raise ValidationError("portfolio aggregate requires at least one fold")
    asset_count = len(folds[0].trades_by_instrument)
    if asset_count < 3 or any(
        len(item.trades_by_instrument) != asset_count for item in folds
    ):
        raise ValidationError("portfolio fold instruments are not aligned")
    cost_bps = folds[0].round_trip_cost_bps
    if any(item.round_trip_cost_bps != cost_bps for item in folds):
        raise ValidationError("portfolio fold costs are not aligned")
    gross_equity = 1.0
    equity = 1.0
    peak = 1.0
    max_drawdown = 0.0
    by_instrument = np.zeros(asset_count, dtype=np.int64)
    for item in folds:
        gross_equity *= max(0.0, 1.0 + item.gross_return)
        equity *= max(0.0, 1.0 + item.net_return)
        peak = max(peak, equity)
        max_drawdown = max(
            max_drawdown,
            item.max_drawdown,
            (peak - equity) / peak if peak else 1.0,
        )
        by_instrument += np.asarray(item.trades_by_instrument, dtype=np.int64)
    return PortfolioAggregate(
        folds=len(folds),
        time_rows=sum(item.time_rows for item in folds),
        evaluated_decisions=sum(item.evaluated_decisions for item in folds),
        trades=sum(item.trades for item in folds),
        label_hits=sum(item.label_hits for item in folds),
        profitable_trades=sum(item.profitable_trades for item in folds),
        exposure_bars=sum(item.exposure_bars for item in folds),
        gross_return=gross_equity - 1.0,
        net_return=equity - 1.0,
        max_drawdown=max_drawdown,
        sum_gross_returns=sum(item.sum_gross_returns for item in folds),
        sum_net_returns=sum(item.sum_net_returns for item in folds),
        round_trip_cost_bps=cost_bps,
        worst_fold_gross_return=min(item.gross_return for item in folds),
        worst_fold_net_return=min(item.net_return for item in folds),
        trades_by_instrument=tuple(int(item) for item in by_instrument),
    )


def portfolio_gate_failures(
    aggregate: PortfolioAggregate,
    *,
    min_folds: int,
    min_trades: int,
    min_profitable_trade_rate: float,
    min_net_return: float,
    min_worst_fold_net_return: float,
    max_drawdown: float,
    min_gross_return: float | None = None,
    max_trades_per_day: float | None = None,
    max_instrument_trade_share: float | None = None,
) -> tuple[str, ...]:
    failures: list[str] = []
    if aggregate.folds < min_folds:
        failures.append("folds_insufficient")
    if aggregate.trades < min_trades:
        failures.append("trades_insufficient")
    if aggregate.profitable_trade_rate < min_profitable_trade_rate:
        failures.append("profitable_trade_rate_below_gate")
    if min_gross_return is not None and aggregate.gross_return < min_gross_return:
        failures.append("gross_return_below_gate")
    if aggregate.net_return < min_net_return:
        failures.append("net_return_below_gate")
    if aggregate.worst_fold_net_return < min_worst_fold_net_return:
        failures.append("worst_fold_below_gate")
    if aggregate.max_drawdown > max_drawdown:
        failures.append("drawdown_above_gate")
    if max_trades_per_day is not None and aggregate.trades_per_day > max_trades_per_day:
        failures.append("turnover_above_gate")
    if (
        max_instrument_trade_share is not None
        and aggregate.max_instrument_trade_share > max_instrument_trade_share
    ):
        failures.append("instrument_concentration_above_gate")
    return tuple(failures)


__all__ = [
    "MARKET_FEATURES",
    "MULTI_ASSET_RESEARCH_SCHEMA_VERSION",
    "PortfolioAggregate",
    "PortfolioFoldMetrics",
    "PreparedMultiAssetDataset",
    "aggregate_portfolio_folds",
    "evaluate_cost_aware_portfolio",
    "evaluate_portfolio_scores",
    "portfolio_gate_failures",
    "prepare_multi_asset_dataset",
]
