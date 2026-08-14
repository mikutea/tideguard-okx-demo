from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Sequence

from .strategy import (
    FrozenLinearModel,
    ModelArtifactError,
    canonical_json,
    feature_schema_hash,
    sha256_hex,
)


LEGACY_VALIDATION_SCHEMA_VERSION = "tideguard.walk-forward.v1"
VALIDATION_SCHEMA_VERSION = "tideguard.walk-forward.v2"
LEGACY_EVALUATION_MODE = "directional-overlapping-long-short"
LONG_ONLY_EVALUATION_MODE = "long-only-fixed-horizon-non-overlapping"


class ValidationError(ValueError):
    pass


def _utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return _utc(value, "timestamp").isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_iso(value: Any, name: str) -> datetime:
    if not isinstance(value, str):
        raise ValidationError(f"{name} must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError(f"{name} must be an ISO timestamp") from exc
    return _utc(parsed, name)


@dataclass(frozen=True)
class Observation:
    observed_at: datetime
    features: tuple[float, ...]
    label: int
    forward_return: float

    def __post_init__(self) -> None:
        _utc(self.observed_at, "observed_at")
        if not self.features or any(
            isinstance(value, bool) or not math.isfinite(float(value))
            for value in self.features
        ):
            raise ValidationError("observation features must be finite and non-empty")
        if isinstance(self.label, bool) or self.label not in {0, 1}:
            raise ValidationError("observation label must be 0 or 1")
        if not math.isfinite(float(self.forward_return)) or not -1.0 < self.forward_return <= 1.0:
            raise ValidationError("forward_return must be finite and in (-1, 1]")


@dataclass(frozen=True)
class WalkForwardSpec:
    train_size: int
    test_size: int
    step_size: int
    embargo_size: int = 1
    label_horizon: int = 1
    expanding: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.expanding, bool):
            raise ValidationError("expanding must be boolean")
        if self.train_size < 2:
            raise ValidationError("train_size must be at least 2")
        if self.test_size < 1 or self.step_size < 1:
            raise ValidationError("test_size and step_size must be positive")
        if self.embargo_size < 0 or self.label_horizon < 1:
            raise ValidationError("embargo_size and label_horizon are invalid")
        if self.test_size <= self.label_horizon:
            raise ValidationError("test_size must exceed label_horizon")
        if self.step_size != self.test_size:
            raise ValidationError("outer test windows must be contiguous and non-overlapping")

    def to_dict(self) -> dict[str, Any]:
        return {
            "embargo_size": self.embargo_size,
            "expanding": self.expanding,
            "label_horizon": self.label_horizon,
            "step_size": self.step_size,
            "test_size": self.test_size,
            "train_size": self.train_size,
        }


@dataclass(frozen=True)
class WalkForwardFold:
    fold: int
    train_start: int
    train_stop: int
    test_start: int
    test_stop: int

    @property
    def train_indices(self) -> range:
        return range(self.train_start, self.train_stop)

    @property
    def test_indices(self) -> range:
        return range(self.test_start, self.test_stop)


def plan_walk_forward(row_count: int, spec: WalkForwardSpec) -> tuple[WalkForwardFold, ...]:
    if row_count < 1:
        raise ValidationError("row_count must be positive")
    gap = spec.label_horizon + spec.embargo_size
    folds: list[WalkForwardFold] = []
    test_start = spec.train_size + gap
    fold_number = 0
    while test_start + spec.test_size <= row_count:
        train_stop = test_start - gap
        train_start = 0 if spec.expanding else train_stop - spec.train_size
        if train_start < 0 or train_stop - train_start < spec.train_size:
            raise ValidationError("walk-forward train window is invalid")
        folds.append(
            WalkForwardFold(
                fold=fold_number,
                train_start=train_start,
                train_stop=train_stop,
                test_start=test_start,
                test_stop=test_start + spec.test_size,
            )
        )
        fold_number += 1
        test_start += spec.step_size
    if not folds:
        raise ValidationError("dataset is too short for one walk-forward fold")
    return tuple(folds)


@dataclass(frozen=True)
class TrainingConfig:
    learning_rate: float = 0.05
    epochs: int = 250
    l2: float = 0.001
    buy_threshold: float = 0.60
    sell_threshold: float = 0.40
    round_trip_cost_bps: float = 12.0

    def __post_init__(self) -> None:
        values = (
            self.learning_rate,
            self.l2,
            self.buy_threshold,
            self.sell_threshold,
            self.round_trip_cost_bps,
        )
        if any(not math.isfinite(float(value)) for value in values):
            raise ValidationError("training configuration must be finite")
        if not 0 < self.learning_rate <= 1:
            raise ValidationError("learning_rate must be in (0, 1]")
        if not 1 <= self.epochs <= 100_000:
            raise ValidationError("epochs are outside the supported range")
        if not 0 <= self.l2 <= 100:
            raise ValidationError("l2 is outside the supported range")
        if not 0 < self.sell_threshold < 0.5 < self.buy_threshold < 1:
            raise ValidationError("signal thresholds must straddle 0.5")
        if not 0 < self.round_trip_cost_bps <= 1_000:
            raise ValidationError("validation must include positive bounded costs")

    def to_dict(self) -> dict[str, Any]:
        return {
            "buy_threshold": float(self.buy_threshold),
            "epochs": self.epochs,
            "l2": float(self.l2),
            "learning_rate": float(self.learning_rate),
            "round_trip_cost_bps": float(self.round_trip_cost_bps),
            "sell_threshold": float(self.sell_threshold),
        }

    @property
    def sha256(self) -> str:
        return sha256_hex(canonical_json(self.to_dict()))


def _validate_dataset(
    observations: Sequence[Observation], feature_names: tuple[str, ...]
) -> None:
    if len(observations) < 2:
        raise ValidationError("at least two observations are required")
    if len(feature_names) != len(set(feature_names)) or not feature_names:
        raise ValidationError("feature_names must be unique and non-empty")
    width = len(feature_names)
    previous: datetime | None = None
    for observation in observations:
        if len(observation.features) != width:
            raise ValidationError("observation width does not match feature_names")
        current = _utc(observation.observed_at, "observed_at")
        if previous is not None and current <= previous:
            raise ValidationError("observations must have strictly increasing timestamps")
        previous = current
    # Reuse the artifact format's stricter feature-name validation.
    try:
        FrozenLinearModel(
            feature_names=feature_names,
            means=tuple(0.0 for _ in feature_names),
            scales=tuple(1.0 for _ in feature_names),
            coefficients=tuple(0.0 for _ in feature_names),
            intercept=0.0,
        )
    except ModelArtifactError as exc:
        raise ValidationError(str(exc)) from exc


def dataset_sha256(
    observations: Sequence[Observation], feature_names: tuple[str, ...]
) -> str:
    _validate_dataset(observations, feature_names)
    return sha256_hex(
        canonical_json(
            {
                "feature_names": list(feature_names),
                "observations": [
                    {
                        "features": list(row.features),
                        "forward_return": row.forward_return,
                        "label": row.label,
                        "observed_at": _iso(row.observed_at),
                    }
                    for row in observations
                ],
            }
        )
    )


def fit_linear_model(
    observations: Sequence[Observation],
    feature_names: tuple[str, ...],
    config: TrainingConfig,
) -> FrozenLinearModel:
    """Deterministic full-batch logistic regression for an offline candidate."""

    _validate_dataset(observations, feature_names)
    row_count = len(observations)
    width = len(feature_names)
    means = tuple(
        sum(float(row.features[index]) for row in observations) / row_count
        for index in range(width)
    )
    variances = tuple(
        sum((float(row.features[index]) - means[index]) ** 2 for row in observations)
        / row_count
        for index in range(width)
    )
    scales = tuple(math.sqrt(value) if value > 1e-18 else 1.0 for value in variances)
    normalized = [
        tuple(
            (float(row.features[index]) - means[index]) / scales[index]
            for index in range(width)
        )
        for row in observations
    ]
    weights = [0.0] * width
    intercept = 0.0
    for _ in range(config.epochs):
        gradient = [0.0] * width
        intercept_gradient = 0.0
        for row, values in zip(observations, normalized, strict=True):
            linear = intercept + sum(
                weight * value for weight, value in zip(weights, values, strict=True)
            )
            if linear >= 0:
                probability = 1.0 / (1.0 + math.exp(-min(linear, 700.0)))
            else:
                exp_value = math.exp(max(linear, -700.0))
                probability = exp_value / (1.0 + exp_value)
            error = probability - row.label
            intercept_gradient += error
            for index, value in enumerate(values):
                gradient[index] += error * value
        intercept -= config.learning_rate * (intercept_gradient / row_count)
        for index in range(width):
            regularized = gradient[index] / row_count + config.l2 * weights[index]
            weights[index] -= config.learning_rate * regularized
    return FrozenLinearModel(
        feature_names=feature_names,
        means=means,
        scales=scales,
        coefficients=tuple(weights),
        intercept=intercept,
        buy_threshold=config.buy_threshold,
        sell_threshold=config.sell_threshold,
    )


@dataclass(frozen=True)
class FoldMetrics:
    fold: int
    train_start_at: datetime
    train_stop_at: datetime
    test_start_at: datetime
    test_stop_at: datetime
    train_rows: int
    test_rows: int
    trades: int
    accuracy: float
    gross_return: float
    net_return: float
    max_drawdown: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "accuracy": self.accuracy,
            "fold": self.fold,
            "gross_return": self.gross_return,
            "max_drawdown": self.max_drawdown,
            "net_return": self.net_return,
            "test_rows": self.test_rows,
            "test_start_at": _iso(self.test_start_at),
            "test_stop_at": _iso(self.test_stop_at),
            "trades": self.trades,
            "train_rows": self.train_rows,
            "train_start_at": _iso(self.train_start_at),
            "train_stop_at": _iso(self.train_stop_at),
        }

    @classmethod
    def from_dict(cls, value: Any) -> FoldMetrics:
        expected = {
            "accuracy",
            "fold",
            "gross_return",
            "max_drawdown",
            "net_return",
            "test_rows",
            "test_start_at",
            "test_stop_at",
            "trades",
            "train_rows",
            "train_start_at",
            "train_stop_at",
        }
        if not isinstance(value, dict) or set(value) != expected:
            raise ValidationError("fold metrics have missing or unexpected fields")
        try:
            metric = cls(
                fold=int(value["fold"]),
                train_start_at=_parse_iso(value["train_start_at"], "train_start_at"),
                train_stop_at=_parse_iso(value["train_stop_at"], "train_stop_at"),
                test_start_at=_parse_iso(value["test_start_at"], "test_start_at"),
                test_stop_at=_parse_iso(value["test_stop_at"], "test_stop_at"),
                train_rows=int(value["train_rows"]),
                test_rows=int(value["test_rows"]),
                trades=int(value["trades"]),
                accuracy=float(value["accuracy"]),
                gross_return=float(value["gross_return"]),
                net_return=float(value["net_return"]),
                max_drawdown=float(value["max_drawdown"]),
            )
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValidationError("fold metrics contain invalid values") from exc
        numbers = (metric.accuracy, metric.gross_return, metric.net_return, metric.max_drawdown)
        if any(not math.isfinite(value) for value in numbers):
            raise ValidationError("fold metrics contain non-finite values")
        if metric.fold < 0 or metric.train_rows < 2 or metric.test_rows < 1 or metric.trades < 0:
            raise ValidationError("fold metrics contain invalid counts")
        if not 0 <= metric.accuracy <= 1 or not 0 <= metric.max_drawdown <= 1:
            raise ValidationError("fold metrics are outside valid ranges")
        return metric


@dataclass(frozen=True)
class ValidationReport:
    created_at: datetime
    dataset_sha256: str
    feature_schema_sha256: str
    training_config_sha256: str
    walk_forward_spec: WalkForwardSpec
    round_trip_cost_bps: float
    folds: tuple[FoldMetrics, ...]
    oos_rows: int
    trades: int
    aggregate_accuracy: float
    aggregate_net_return: float
    max_drawdown: float
    worst_fold_net_return: float
    evaluation_mode: str = LONG_ONLY_EVALUATION_MODE
    schema_version: str = VALIDATION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _utc(self.created_at, "created_at")
        for value in (
            self.dataset_sha256,
            self.feature_schema_sha256,
            self.training_config_sha256,
        ):
            if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
                raise ValidationError("validation hashes must be lowercase sha256")
        if self.schema_version not in {
            LEGACY_VALIDATION_SCHEMA_VERSION,
            VALIDATION_SCHEMA_VERSION,
        } or not self.folds:
            raise ValidationError("unsupported or empty validation report")
        expected_mode = (
            LONG_ONLY_EVALUATION_MODE
            if self.schema_version == VALIDATION_SCHEMA_VERSION
            else LEGACY_EVALUATION_MODE
        )
        if self.evaluation_mode != expected_mode:
            raise ValidationError("validation evaluation semantics do not match the schema")
        if self.oos_rows != sum(fold.test_rows for fold in self.folds):
            raise ValidationError("aggregate OOS row count is inconsistent")
        if self.trades != sum(fold.trades for fold in self.folds):
            raise ValidationError("aggregate trade count is inconsistent")
        numbers = (
            self.round_trip_cost_bps,
            self.aggregate_accuracy,
            self.aggregate_net_return,
            self.max_drawdown,
            self.worst_fold_net_return,
        )
        if any(not math.isfinite(float(value)) for value in numbers):
            raise ValidationError("validation report contains non-finite values")
        if self.round_trip_cost_bps <= 0:
            raise ValidationError("validation report must include positive costs")
        if not 0 <= self.aggregate_accuracy <= 1 or not 0 <= self.max_drawdown <= 1:
            raise ValidationError("validation aggregates are outside valid ranges")

    def to_dict(self) -> dict[str, Any]:
        value = {
            "aggregate_accuracy": self.aggregate_accuracy,
            "aggregate_net_return": self.aggregate_net_return,
            "created_at": _iso(self.created_at),
            "dataset_sha256": self.dataset_sha256,
            "feature_schema_sha256": self.feature_schema_sha256,
            "folds": [fold.to_dict() for fold in self.folds],
            "max_drawdown": self.max_drawdown,
            "oos_rows": self.oos_rows,
            "round_trip_cost_bps": float(self.round_trip_cost_bps),
            "schema_version": self.schema_version,
            "trades": self.trades,
            "training_config_sha256": self.training_config_sha256,
            "walk_forward_spec": self.walk_forward_spec.to_dict(),
            "worst_fold_net_return": self.worst_fold_net_return,
        }
        if self.schema_version == VALIDATION_SCHEMA_VERSION:
            value["evaluation_mode"] = self.evaluation_mode
        return value

    @property
    def report_sha256(self) -> str:
        return sha256_hex(canonical_json(self.to_dict()))

    @property
    def validation_run_id(self) -> str:
        return f"val_{self.report_sha256[:24]}"

    @classmethod
    def from_dict(cls, value: Any) -> ValidationReport:
        base_expected = {
            "aggregate_accuracy",
            "aggregate_net_return",
            "created_at",
            "dataset_sha256",
            "feature_schema_sha256",
            "folds",
            "max_drawdown",
            "oos_rows",
            "round_trip_cost_bps",
            "schema_version",
            "trades",
            "training_config_sha256",
            "walk_forward_spec",
            "worst_fold_net_return",
        }
        if not isinstance(value, dict):
            raise ValidationError("validation report has missing or unexpected fields")
        schema_version = str(value.get("schema_version", ""))
        if schema_version == VALIDATION_SCHEMA_VERSION:
            expected = base_expected | {"evaluation_mode"}
            evaluation_mode = str(value.get("evaluation_mode", ""))
        elif schema_version == LEGACY_VALIDATION_SCHEMA_VERSION:
            expected = base_expected
            evaluation_mode = LEGACY_EVALUATION_MODE
        else:
            raise ValidationError("unsupported validation report schema")
        if set(value) != expected:
            raise ValidationError("validation report has missing or unexpected fields")
        spec_value = value["walk_forward_spec"]
        if not isinstance(spec_value, dict) or set(spec_value) != {
            "embargo_size",
            "expanding",
            "label_horizon",
            "step_size",
            "test_size",
            "train_size",
        }:
            raise ValidationError("walk-forward specification is invalid")
        if not isinstance(spec_value["expanding"], bool):
            raise ValidationError("walk-forward expanding must be boolean")
        try:
            return cls(
                created_at=_parse_iso(value["created_at"], "created_at"),
                dataset_sha256=str(value["dataset_sha256"]),
                feature_schema_sha256=str(value["feature_schema_sha256"]),
                training_config_sha256=str(value["training_config_sha256"]),
                walk_forward_spec=WalkForwardSpec(
                    train_size=int(spec_value["train_size"]),
                    test_size=int(spec_value["test_size"]),
                    step_size=int(spec_value["step_size"]),
                    embargo_size=int(spec_value["embargo_size"]),
                    label_horizon=int(spec_value["label_horizon"]),
                    expanding=spec_value["expanding"],
                ),
                round_trip_cost_bps=float(value["round_trip_cost_bps"]),
                folds=tuple(FoldMetrics.from_dict(item) for item in value["folds"]),
                oos_rows=int(value["oos_rows"]),
                trades=int(value["trades"]),
                aggregate_accuracy=float(value["aggregate_accuracy"]),
                aggregate_net_return=float(value["aggregate_net_return"]),
                max_drawdown=float(value["max_drawdown"]),
                worst_fold_net_return=float(value["worst_fold_net_return"]),
                evaluation_mode=evaluation_mode,
                schema_version=schema_version,
            )
        except (TypeError, ValueError, OverflowError) as exc:
            if isinstance(exc, ValidationError):
                raise
            raise ValidationError("validation report contains invalid values") from exc


def _evaluate_fold(
    model: FrozenLinearModel,
    rows: Sequence[Observation],
    feature_names: tuple[str, ...],
    cost_bps: float,
    holding_period_bars: int,
) -> tuple[int, float, float, float, float, int]:
    """Evaluate a cash-SPOT-compatible long/flat diagnostic.

    A BUY uses one unit of diagnostic capital for exactly ``holding_period_bars``.
    Signals inside that holding interval are ignored. SELL is always flat and can
    never earn short-side PnL. Entries that could not exit inside the same outer
    test fold are not evaluated.
    """

    if holding_period_bars < 1 or len(rows) <= holding_period_bars:
        raise ValidationError("test fold is too short for the fixed holding period")
    trades = 0
    correct = 0
    evaluated_rows = 0
    equity = 1.0
    gross_equity = 1.0
    peak = 1.0
    max_drawdown = 0.0
    cost = cost_bps / 10_000.0
    cursor = 0
    entry_stop = len(rows) - holding_period_bars
    while cursor < entry_stop:
        row = rows[cursor]
        features = dict(zip(feature_names, row.features, strict=True))
        action, _score = model.action(features)
        predicted_label = 1 if action == "buy" else 0
        correct += int(predicted_label == row.label)
        evaluated_rows += 1
        if action != "buy":
            cursor += 1
            continue

        gross = row.forward_return
        net = gross - cost
        trades += 1
        gross_equity *= max(0.0, 1.0 + gross)
        equity *= max(0.0, 1.0 + net)
        peak = max(peak, equity)
        drawdown = (peak - equity) / peak if peak else 1.0
        max_drawdown = max(max_drawdown, drawdown)
        cursor += holding_period_bars
    return (
        trades,
        correct / evaluated_rows,
        gross_equity - 1.0,
        equity - 1.0,
        max_drawdown,
        evaluated_rows,
    )


def run_walk_forward(
    observations: Sequence[Observation],
    feature_names: tuple[str, ...],
    spec: WalkForwardSpec,
    config: TrainingConfig,
    *,
    created_at: datetime,
) -> ValidationReport:
    """Train only on each past window and score every outer window exactly once."""

    _validate_dataset(observations, feature_names)
    folds = plan_walk_forward(len(observations), spec)
    fold_metrics: list[FoldMetrics] = []
    total_correct_weighted = 0.0
    total_accuracy_rows = 0
    total_oos_rows = 0
    total_trades = 0
    aggregate_equity = 1.0
    aggregate_peak = 1.0
    aggregate_max_drawdown = 0.0
    for fold in folds:
        train_rows = observations[fold.train_start : fold.train_stop]
        test_rows = observations[fold.test_start : fold.test_stop]
        model = fit_linear_model(train_rows, feature_names, config)
        trades, accuracy, gross_return, net_return, max_drawdown, evaluated_rows = _evaluate_fold(
            model,
            test_rows,
            feature_names,
            config.round_trip_cost_bps,
            spec.label_horizon,
        )
        fold_metrics.append(
            FoldMetrics(
                fold=fold.fold,
                train_start_at=train_rows[0].observed_at,
                train_stop_at=train_rows[-1].observed_at,
                test_start_at=test_rows[0].observed_at,
                test_stop_at=test_rows[-1].observed_at,
                train_rows=len(train_rows),
                test_rows=len(test_rows),
                trades=trades,
                accuracy=accuracy,
                gross_return=gross_return,
                net_return=net_return,
                max_drawdown=max_drawdown,
            )
        )
        total_correct_weighted += accuracy * evaluated_rows
        total_accuracy_rows += evaluated_rows
        total_oos_rows += len(test_rows)
        total_trades += trades
        aggregate_equity *= max(0.0, 1.0 + net_return)
        aggregate_peak = max(aggregate_peak, aggregate_equity)
        drawdown = (
            (aggregate_peak - aggregate_equity) / aggregate_peak
            if aggregate_peak
            else 1.0
        )
        aggregate_max_drawdown = max(aggregate_max_drawdown, max_drawdown, drawdown)
    return ValidationReport(
        created_at=_utc(created_at, "created_at"),
        dataset_sha256=dataset_sha256(observations, feature_names),
        feature_schema_sha256=feature_schema_hash(feature_names),
        training_config_sha256=config.sha256,
        walk_forward_spec=spec,
        round_trip_cost_bps=config.round_trip_cost_bps,
        folds=tuple(fold_metrics),
        oos_rows=total_oos_rows,
        trades=total_trades,
        aggregate_accuracy=total_correct_weighted / total_accuracy_rows,
        aggregate_net_return=aggregate_equity - 1.0,
        max_drawdown=aggregate_max_drawdown,
        worst_fold_net_return=min(fold.net_return for fold in fold_metrics),
        evaluation_mode=LONG_ONLY_EVALUATION_MODE,
    )
