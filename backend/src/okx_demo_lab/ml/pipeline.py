from __future__ import annotations

import hmac
import math
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Sequence

os.environ["OPENBLAS_NUM_THREADS"] = "1"
os.environ["OMP_NUM_THREADS"] = "1"
os.environ["MKL_NUM_THREADS"] = "1"

import numpy as np

from .registry import ModelRegistry, PromotionPolicy
from .strategy import (
    FrozenModelBundle,
    ModelManifest,
    feature_schema_hash,
    sha256_hex,
    canonical_json,
)
from .walk_forward import (
    LONG_ONLY_EVALUATION_MODE,
    Observation,
    ObservationMatrix,
    TrainingConfig,
    ValidationReport,
    WalkForwardSpec,
    dataset_sha256,
    fit_linear_model,
    run_walk_forward,
)


BAR_MILLISECONDS = 5 * 60 * 1_000
WARMUP_BARS = 48
DEFAULT_LABEL_HORIZON = 12
FEATURE_NAMES = (
    "return_1",
    "return_3",
    "return_12",
    "return_24",
    "return_48",
    "volatility_12",
    "volatility_48",
    "range_1",
    "relative_volume_24",
    "ema_distance_12",
    "ema_distance_48",
    "rsi_14",
    "hour_sin",
    "hour_cos",
    "weekday_sin",
    "weekday_cos",
)


class DatasetError(ValueError):
    pass


@dataclass(frozen=True)
class ParsedCandle:
    opened_at: datetime
    closed_at: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class CandidateTrainingResult:
    model_id: str
    artifact_sha256: str
    validation_run_id: str
    report: ValidationReport
    gate_failures: tuple[str, ...]
    candle_rows: int
    observation_rows: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifactSha256": self.artifact_sha256,
            "candleRows": self.candle_rows,
            "gateFailures": list(self.gate_failures),
            "modelId": self.model_id,
            "observationRows": self.observation_rows,
            "validation": self.report.to_dict(),
            "validationRunId": self.validation_run_id,
        }


@dataclass(frozen=True)
class PreparedTrainingDataset:
    observations: ObservationMatrix
    candle_rows: int
    label_contract_sha256: str


def _label_contract_sha256(config: TrainingConfig) -> str:
    return sha256_hex(
        canonical_json(
            {
                "label_horizon": DEFAULT_LABEL_HORIZON,
                "round_trip_cost_bps": float(config.round_trip_cost_bps),
                "stop_loss_fraction": float(config.stop_loss_fraction),
                "take_profit_fraction": float(config.take_profit_fraction),
            }
        )
    )


def _finite_decimal(value: Any, name: str, *, allow_zero: bool = False) -> float:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise DatasetError(f"{name} is not a decimal") from exc
    if not parsed.is_finite() or parsed < 0 or (parsed == 0 and not allow_zero):
        raise DatasetError(f"{name} must be finite and positive")
    return float(parsed)


def parse_completed_candles(
    rows: Sequence[Sequence[Any]], *, now: datetime
) -> tuple[ParsedCandle, ...]:
    if now.tzinfo is None or now.utcoffset() is None:
        raise DatasetError("now must be timezone-aware")
    now_utc = now.astimezone(timezone.utc)
    parsed: list[ParsedCandle] = []
    previous_ms: int | None = None
    for index, row in enumerate(rows):
        if not isinstance(row, (list, tuple)) or len(row) != 9:
            raise DatasetError(f"candle {index} does not match the OKX 9-field schema")
        timestamp_text = str(row[0]).strip()
        if not timestamp_text.isdigit():
            raise DatasetError(f"candle {index} has an invalid timestamp")
        timestamp_ms = int(timestamp_text)
        if timestamp_ms <= 0:
            raise DatasetError(f"candle {index} has an invalid timestamp")
        if previous_ms is not None and timestamp_ms - previous_ms != BAR_MILLISECONDS:
            raise DatasetError("candles must be unique, chronological, and exactly 5 minutes apart")
        previous_ms = timestamp_ms
        if str(row[8]).strip() != "1":
            raise DatasetError("training and inference require exchange-confirmed candles")
        opened_at = datetime.fromtimestamp(timestamp_ms / 1_000, tz=timezone.utc)
        closed_at = opened_at + timedelta(milliseconds=BAR_MILLISECONDS)
        if closed_at > now_utc + timedelta(seconds=2):
            raise DatasetError("candle close is in the future")
        open_price = _finite_decimal(row[1], f"candle {index} open")
        high = _finite_decimal(row[2], f"candle {index} high")
        low = _finite_decimal(row[3], f"candle {index} low")
        close = _finite_decimal(row[4], f"candle {index} close")
        volume = _finite_decimal(row[5], f"candle {index} volume", allow_zero=True)
        if high < max(open_price, close, low) or low > min(open_price, close, high):
            raise DatasetError(f"candle {index} OHLC values are inconsistent")
        parsed.append(
            ParsedCandle(
                opened_at=opened_at,
                closed_at=closed_at,
                open=open_price,
                high=high,
                low=low,
                close=close,
                volume=volume,
            )
        )
    if len(parsed) <= WARMUP_BARS:
        raise DatasetError(f"at least {WARMUP_BARS + 1} completed candles are required")
    return tuple(parsed)


def _return(candles: Sequence[ParsedCandle], index: int, periods: int) -> float:
    return candles[index].close / candles[index - periods].close - 1.0


def _volatility(candles: Sequence[ParsedCandle], index: int, periods: int) -> float:
    values = [_return(candles, cursor, 1) for cursor in range(index - periods + 1, index + 1)]
    mean = sum(values) / len(values)
    return math.sqrt(sum((value - mean) ** 2 for value in values) / len(values))


def _ema(values: Sequence[float]) -> float:
    alpha = 2.0 / (len(values) + 1.0)
    current = float(values[0])
    for value in values[1:]:
        current = alpha * float(value) + (1.0 - alpha) * current
    return current


def _features(candles: Sequence[ParsedCandle], index: int) -> tuple[float, ...]:
    current = candles[index]
    volumes = [row.volume for row in candles[index - 23 : index + 1]]
    average_volume = sum(volumes) / len(volumes)
    relative_volume = current.volume / average_volume if average_volume > 0 else 1.0
    deltas = [
        candles[cursor].close - candles[cursor - 1].close
        for cursor in range(index - 13, index + 1)
    ]
    gains = sum(max(delta, 0.0) for delta in deltas)
    losses = sum(max(-delta, 0.0) for delta in deltas)
    rsi = gains / (gains + losses) if gains + losses > 0 else 0.5
    ema_12 = _ema([row.close for row in candles[index - 11 : index + 1]])
    ema_48 = _ema([row.close for row in candles[index - 47 : index + 1]])
    hour = current.closed_at.hour + current.closed_at.minute / 60.0
    weekday = current.closed_at.weekday()
    values = (
        _return(candles, index, 1),
        _return(candles, index, 3),
        _return(candles, index, 12),
        _return(candles, index, 24),
        _return(candles, index, 48),
        _volatility(candles, index, 12),
        _volatility(candles, index, 48),
        (current.high - current.low) / current.close,
        relative_volume,
        current.close / ema_12 - 1.0,
        current.close / ema_48 - 1.0,
        rsi,
        math.sin(2.0 * math.pi * hour / 24.0),
        math.cos(2.0 * math.pi * hour / 24.0),
        math.sin(2.0 * math.pi * weekday / 7.0),
        math.cos(2.0 * math.pi * weekday / 7.0),
    )
    if any(not math.isfinite(value) for value in values):
        raise DatasetError("feature calculation produced a non-finite value")
    return values


def build_observations(
    candles: Sequence[ParsedCandle],
    *,
    label_horizon: int = DEFAULT_LABEL_HORIZON,
    round_trip_cost_bps: float = 12.0,
    stop_loss_fraction: float = 0.015,
    take_profit_fraction: float = 0.025,
) -> tuple[Observation, ...]:
    if not 1 <= label_horizon <= 96:
        raise DatasetError("label horizon is outside the supported range")
    if not math.isfinite(round_trip_cost_bps) or round_trip_cost_bps <= 0:
        raise DatasetError("round-trip cost must be positive")
    if not math.isfinite(stop_loss_fraction) or not 0 < stop_loss_fraction <= 0.05:
        raise DatasetError("stop-loss fraction is invalid")
    if not math.isfinite(take_profit_fraction) or not 0 < take_profit_fraction <= 0.10:
        raise DatasetError("take-profit fraction is invalid")
    if len(candles) <= WARMUP_BARS + label_horizon:
        raise DatasetError("dataset is too short after warmup and label horizon")
    required_return = round_trip_cost_bps / 10_000.0
    observations: list[Observation] = []
    for index in range(WARMUP_BARS, len(candles) - label_horizon):
        entry_close = candles[index].close
        stop_price = entry_close * (1.0 - stop_loss_fraction)
        take_price = entry_close * (1.0 + take_profit_fraction)
        forward_return: float | None = None
        for future in candles[index + 1 : index + label_horizon + 1]:
            # OHLC cannot reveal which barrier was first inside one candle.  Use
            # the adverse outcome to keep validation conservative and stable.
            if future.low <= stop_price:
                forward_return = -stop_loss_fraction
                break
            if future.high >= take_price:
                forward_return = take_profit_fraction
                break
        if forward_return is None:
            forward_return = candles[index + label_horizon].close / entry_close - 1.0
        observations.append(
            Observation(
                observed_at=candles[index].closed_at,
                features=_features(candles, index),
                label=1 if forward_return > required_return else 0,
                forward_return=forward_return,
            )
        )
    return tuple(observations)


def _rolling_sum(values: np.ndarray, indices: np.ndarray, periods: int) -> np.ndarray:
    prefix = np.empty(values.shape[0] + 1, dtype=np.float64)
    prefix[0] = 0.0
    np.cumsum(values, dtype=np.float64, out=prefix[1:])
    starts = indices - periods + 1
    return prefix[indices + 1] - prefix[starts]


def _window_ema(close: np.ndarray, indices: np.ndarray, periods: int) -> np.ndarray:
    alpha = 2.0 / (periods + 1.0)
    weights = np.empty(periods, dtype=np.float64)
    weights[0] = (1.0 - alpha) ** (periods - 1)
    if periods > 1:
        offsets = np.arange(periods - 2, -1, -1, dtype=np.float64)
        weights[1:] = alpha * np.power(1.0 - alpha, offsets)
    convolved = np.convolve(close, weights[::-1], mode="valid")
    return convolved[indices - periods + 1]


def prepare_training_dataset(
    raw_candles: Sequence[Sequence[Any]],
    *,
    now: datetime,
    training_config: TrainingConfig,
) -> PreparedTrainingDataset:
    """Build one compact feature/label matrix shared by every candidate config."""

    if now.tzinfo is None or now.utcoffset() is None:
        raise DatasetError("now must be timezone-aware")
    candle_count = len(raw_candles)
    if candle_count <= WARMUP_BARS + DEFAULT_LABEL_HORIZON:
        raise DatasetError("dataset is too short after warmup and label horizon")
    timestamps = np.empty(candle_count, dtype=np.int64)
    highs = np.empty(candle_count, dtype=np.float64)
    lows = np.empty(candle_count, dtype=np.float64)
    closes = np.empty(candle_count, dtype=np.float64)
    volumes = np.empty(candle_count, dtype=np.float64)
    previous_ms: int | None = None
    now_ms = round(now.astimezone(timezone.utc).timestamp() * 1_000)
    seen = 0
    for index, row in enumerate(raw_candles):
        if index >= candle_count:
            raise DatasetError("candle source yielded more rows than declared")
        if not isinstance(row, (list, tuple)) or len(row) != 9:
            raise DatasetError(f"candle {index} does not match the OKX 9-field schema")
        timestamp_text = str(row[0]).strip()
        if not timestamp_text.isdigit():
            raise DatasetError(f"candle {index} has an invalid timestamp")
        timestamp_ms = int(timestamp_text)
        if timestamp_ms <= 0:
            raise DatasetError(f"candle {index} has an invalid timestamp")
        if previous_ms is not None and timestamp_ms - previous_ms != BAR_MILLISECONDS:
            raise DatasetError("candles must be unique, chronological, and exactly 5 minutes apart")
        previous_ms = timestamp_ms
        if str(row[8]).strip() != "1":
            raise DatasetError("training and inference require exchange-confirmed candles")
        if timestamp_ms + BAR_MILLISECONDS > now_ms + 2_000:
            raise DatasetError("candle close is in the future")
        open_price = _finite_decimal(row[1], f"candle {index} open")
        high = _finite_decimal(row[2], f"candle {index} high")
        low = _finite_decimal(row[3], f"candle {index} low")
        close = _finite_decimal(row[4], f"candle {index} close")
        volume = _finite_decimal(
            row[5], f"candle {index} volume", allow_zero=True
        )
        if high < max(open_price, close, low) or low > min(open_price, close, high):
            raise DatasetError(f"candle {index} OHLC values are inconsistent")
        timestamps[index] = timestamp_ms
        highs[index] = high
        lows[index] = low
        closes[index] = close
        volumes[index] = volume
        seen += 1
    if seen != candle_count:
        raise DatasetError("candle source yielded fewer rows than declared")

    indices = np.arange(
        WARMUP_BARS,
        candle_count - DEFAULT_LABEL_HORIZON,
        dtype=np.int64,
    )
    one_step_returns = np.zeros(candle_count, dtype=np.float64)
    one_step_returns[1:] = closes[1:] / closes[:-1] - 1.0
    deltas = np.zeros(candle_count, dtype=np.float64)
    deltas[1:] = closes[1:] - closes[:-1]

    volatility_values: dict[int, np.ndarray] = {}
    for periods in (12, 48):
        sums = _rolling_sum(one_step_returns, indices, periods)
        squares = _rolling_sum(np.square(one_step_returns), indices, periods)
        means = sums / periods
        variance = np.maximum(squares / periods - np.square(means), 0.0)
        volatility_values[periods] = np.sqrt(variance)

    volume_average = _rolling_sum(volumes, indices, 24) / 24.0
    relative_volume = np.divide(
        volumes[indices],
        volume_average,
        out=np.ones(indices.shape[0], dtype=np.float64),
        where=volume_average > 0,
    )
    gains = np.maximum(deltas, 0.0)
    losses = np.maximum(-deltas, 0.0)
    gain_sum = _rolling_sum(gains, indices, 14)
    loss_sum = _rolling_sum(losses, indices, 14)
    rsi_denominator = gain_sum + loss_sum
    rsi = np.divide(
        gain_sum,
        rsi_denominator,
        out=np.full(indices.shape[0], 0.5, dtype=np.float64),
        where=rsi_denominator > 0,
    )
    ema_12 = _window_ema(closes, indices, 12)
    ema_48 = _window_ema(closes, indices, 48)
    closed_ms = timestamps[indices] + BAR_MILLISECONDS
    hour = (closed_ms % 86_400_000) / 3_600_000.0
    weekday = ((closed_ms // 86_400_000) + 3) % 7

    features = np.column_stack(
        (
            closes[indices] / closes[indices - 1] - 1.0,
            closes[indices] / closes[indices - 3] - 1.0,
            closes[indices] / closes[indices - 12] - 1.0,
            closes[indices] / closes[indices - 24] - 1.0,
            closes[indices] / closes[indices - 48] - 1.0,
            volatility_values[12],
            volatility_values[48],
            (highs[indices] - lows[indices]) / closes[indices],
            relative_volume,
            closes[indices] / ema_12 - 1.0,
            closes[indices] / ema_48 - 1.0,
            rsi,
            np.sin(2.0 * math.pi * hour / 24.0),
            np.cos(2.0 * math.pi * hour / 24.0),
            np.sin(2.0 * math.pi * weekday / 7.0),
            np.cos(2.0 * math.pi * weekday / 7.0),
        )
    ).astype(np.float64, copy=False)
    if not np.all(np.isfinite(features)):
        raise DatasetError("feature calculation produced a non-finite value")

    entry_close = closes[indices]
    stop_prices = entry_close * (1.0 - training_config.stop_loss_fraction)
    take_prices = entry_close * (1.0 + training_config.take_profit_fraction)
    forward_returns = (
        closes[indices + DEFAULT_LABEL_HORIZON] / entry_close - 1.0
    )
    unresolved = np.ones(indices.shape[0], dtype=bool)
    for offset in range(1, DEFAULT_LABEL_HORIZON + 1):
        stop_hits = unresolved & (lows[indices + offset] <= stop_prices)
        forward_returns[stop_hits] = -training_config.stop_loss_fraction
        unresolved[stop_hits] = False
        take_hits = unresolved & (highs[indices + offset] >= take_prices)
        forward_returns[take_hits] = training_config.take_profit_fraction
        unresolved[take_hits] = False
    required_return = training_config.round_trip_cost_bps / 10_000.0
    labels = (forward_returns > required_return).astype(np.uint8)
    matrix = ObservationMatrix(
        observed_at_ms=np.ascontiguousarray(closed_ms, dtype=np.int64),
        features=np.ascontiguousarray(features, dtype=np.float64),
        labels=np.ascontiguousarray(labels, dtype=np.uint8),
        forward_returns=np.ascontiguousarray(forward_returns, dtype=np.float64),
    )
    return PreparedTrainingDataset(
        observations=matrix,
        candle_rows=candle_count,
        label_contract_sha256=_label_contract_sha256(training_config),
    )


def latest_features(candles: Sequence[ParsedCandle]) -> tuple[dict[str, float], datetime]:
    index = len(candles) - 1
    if index < WARMUP_BARS:
        raise DatasetError("not enough completed candles for inference")
    values = _features(candles, index)
    return dict(zip(FEATURE_NAMES, values, strict=True)), candles[index].closed_at


def train_and_register_candidate(
    raw_candles: Sequence[Sequence[Any]],
    registry: ModelRegistry,
    *,
    now: datetime,
    code_revision: str,
    promotion_policy: PromotionPolicy | None = None,
    training_config: TrainingConfig | None = None,
    walk_forward_spec: WalkForwardSpec | None = None,
    prepared_dataset: PreparedTrainingDataset | None = None,
    final_fit_rows: int | None = None,
) -> CandidateTrainingResult:
    """Train an offline, data-only candidate; never promote or trade automatically."""

    config = training_config or TrainingConfig(round_trip_cost_bps=12.0)
    if prepared_dataset is None:
        candles = parse_completed_candles(raw_candles, now=now)
        observations: Sequence[Observation] | ObservationMatrix = build_observations(
            candles,
            label_horizon=DEFAULT_LABEL_HORIZON,
            round_trip_cost_bps=config.round_trip_cost_bps,
            stop_loss_fraction=config.stop_loss_fraction,
            take_profit_fraction=config.take_profit_fraction,
        )
        candle_rows = len(candles)
    else:
        if not hmac.compare_digest(
            prepared_dataset.label_contract_sha256,
            _label_contract_sha256(config),
        ):
            raise DatasetError("prepared labels do not match the training configuration")
        observations = prepared_dataset.observations
        candle_rows = prepared_dataset.candle_rows
    spec = walk_forward_spec or WalkForwardSpec(
        train_size=800,
        test_size=200,
        step_size=200,
        label_horizon=DEFAULT_LABEL_HORIZON,
        embargo_size=1,
        expanding=True,
    )
    if spec.label_horizon != DEFAULT_LABEL_HORIZON:
        raise DatasetError("walk-forward label horizon does not match the frozen label contract")
    report = run_walk_forward(observations, FEATURE_NAMES, spec, config, created_at=now)
    if final_fit_rows is None:
        fit_observations = observations
    else:
        if not 2 <= final_fit_rows <= len(observations):
            raise DatasetError("final fit window is outside the prepared dataset")
        if isinstance(observations, ObservationMatrix):
            fit_observations = observations.window(
                len(observations) - final_fit_rows,
                len(observations),
            )
        else:
            fit_observations = observations[-final_fit_rows:]
    model = fit_linear_model(fit_observations, FEATURE_NAMES, config)
    fit_dataset_sha256 = dataset_sha256(fit_observations, FEATURE_NAMES)
    if isinstance(fit_observations, ObservationMatrix):
        trained_from = fit_observations.observed_at(0)
        trained_through = fit_observations.observed_at(len(fit_observations) - 1)
    else:
        trained_from = fit_observations[0].observed_at
        trained_through = fit_observations[-1].observed_at
    manifest = ModelManifest(
        dataset_sha256=report.dataset_sha256,
        fit_dataset_sha256=fit_dataset_sha256,
        fit_rows=len(fit_observations),
        training_config_sha256=report.training_config_sha256,
        validation_run_id=report.validation_run_id,
        code_revision=code_revision[:128] or "unknown",
        trained_from=trained_from,
        trained_through=trained_through,
        created_at=now.astimezone(timezone.utc),
        trainer=(
            f"tideguard-native-numpy-{np.__version__}-linear-logit-v4-"
            + config.sha256[:12]
        ),
        random_seed=0,
        feature_schema_sha256=feature_schema_hash(FEATURE_NAMES),
        benchmark_cohort_id=report.benchmark_cohort_id,
        market_snapshot_sha256=report.market_snapshot_sha256,
        split_protocol_sha256=report.split_protocol_sha256,
    )
    bundle = FrozenModelBundle(manifest=manifest, model=model)
    model_id = registry.register_candidate(bundle.to_bytes())
    validation_run_id = registry.record_validation(model_id, report, recorded_at=now)
    policy = promotion_policy or PromotionPolicy()
    return CandidateTrainingResult(
        model_id=model_id,
        artifact_sha256=bundle.artifact_sha256,
        validation_run_id=validation_run_id,
        report=report,
        gate_failures=policy.failures(report),
        candle_rows=candle_rows,
        observation_rows=len(observations),
    )


def feature_contract_sha256() -> str:
    return sha256_hex(
        canonical_json(
            {
                "bar": "5m",
                "feature_names": list(FEATURE_NAMES),
                "label_horizon": DEFAULT_LABEL_HORIZON,
                "evaluation_mode": LONG_ONLY_EVALUATION_MODE,
                "stop_loss_fraction": TrainingConfig().stop_loss_fraction,
                "take_profit_fraction": TrainingConfig().take_profit_fraction,
                "warmup_bars": WARMUP_BARS,
            }
        )
    )
