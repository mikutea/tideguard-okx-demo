from __future__ import annotations

import math
import json
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from .strategy import canonical_json, sha256_hex
from .walk_forward import FoldMetrics, ValidationError


RESEARCH_SCHEMA_VERSION = "moheng.third-party-benchmark.v1"


@dataclass(frozen=True)
class ResearchModelSpec:
    family: str
    library: str
    library_version: str
    parameters: Mapping[str, Any]
    _parameters_json: str = field(init=False, repr=False, compare=True)

    def __post_init__(self) -> None:
        for name, value in (
            ("family", self.family),
            ("library", self.library),
            ("library_version", self.library_version),
        ):
            if not isinstance(value, str) or not value.strip() or len(value) > 128:
                raise ValidationError(f"research model {name} is invalid")
        try:
            encoded = canonical_json(dict(self.parameters))
        except (TypeError, ValueError) as exc:
            raise ValidationError("research model parameters are not canonical JSON") from exc
        if len(encoded) > 16_384:
            raise ValidationError("research model parameters are too large")
        object.__setattr__(self, "_parameters_json", encoded)

    def to_dict(self) -> dict[str, Any]:
        return {
            "family": self.family,
            "library": self.library,
            "libraryVersion": self.library_version,
            "parameters": json.loads(self._parameters_json),
        }

    @property
    def sha256(self) -> str:
        return sha256_hex(canonical_json(self.to_dict()))


@dataclass(frozen=True)
class ResearchFold:
    metrics: FoldMetrics
    evaluated_rows: int

    def __post_init__(self) -> None:
        if self.evaluated_rows < 1 or self.evaluated_rows > self.metrics.test_rows:
            raise ValidationError("research fold evaluated row count is invalid")

    def to_dict(self) -> dict[str, Any]:
        return {**self.metrics.to_dict(), "evaluated_rows": self.evaluated_rows}


@dataclass(frozen=True)
class ResearchAggregate:
    folds: int
    oos_rows: int
    evaluated_rows: int
    trades: int
    accuracy: float
    net_return: float
    max_drawdown: float
    worst_fold_net_return: float

    def __post_init__(self) -> None:
        if min(self.folds, self.oos_rows, self.evaluated_rows) < 1 or self.trades < 0:
            raise ValidationError("research aggregate counts are invalid")
        values = (
            self.accuracy,
            self.net_return,
            self.max_drawdown,
            self.worst_fold_net_return,
        )
        if any(not math.isfinite(float(value)) for value in values):
            raise ValidationError("research aggregate contains non-finite metrics")
        if not 0.0 <= self.accuracy <= 1.0 or not 0.0 <= self.max_drawdown <= 1.0:
            raise ValidationError("research aggregate metrics are outside valid ranges")

    def to_dict(self) -> dict[str, Any]:
        return {
            "accuracy": self.accuracy,
            "evaluatedRows": self.evaluated_rows,
            "folds": self.folds,
            "maxDrawdown": self.max_drawdown,
            "netReturn": self.net_return,
            "oosRows": self.oos_rows,
            "trades": self.trades,
            "worstFoldNetReturn": self.worst_fold_net_return,
        }


def aggregate_research_folds(folds: Sequence[ResearchFold]) -> ResearchAggregate:
    if not folds:
        raise ValidationError("research aggregate requires at least one fold")
    equity = 1.0
    peak = 1.0
    max_drawdown = 0.0
    evaluated = 0
    weighted_correct = 0.0
    for item in folds:
        metric = item.metrics
        evaluated += item.evaluated_rows
        weighted_correct += metric.accuracy * item.evaluated_rows
        equity *= max(0.0, 1.0 + metric.net_return)
        peak = max(peak, equity)
        cross_fold_drawdown = (peak - equity) / peak if peak else 1.0
        max_drawdown = max(
            max_drawdown,
            metric.max_drawdown,
            cross_fold_drawdown,
        )
    return ResearchAggregate(
        folds=len(folds),
        oos_rows=sum(item.metrics.test_rows for item in folds),
        evaluated_rows=evaluated,
        trades=sum(item.metrics.trades for item in folds),
        accuracy=weighted_correct / evaluated,
        net_return=equity - 1.0,
        max_drawdown=max_drawdown,
        worst_fold_net_return=min(item.metrics.net_return for item in folds),
    )


def research_gate_failures(
    aggregate: ResearchAggregate,
    *,
    min_folds: int,
    min_trades: int,
    min_accuracy: float,
    min_net_return: float,
    min_worst_fold_net_return: float,
    max_drawdown: float,
) -> tuple[str, ...]:
    failures: list[str] = []
    if aggregate.folds < min_folds:
        failures.append("folds_insufficient")
    if aggregate.trades < min_trades:
        failures.append("trades_insufficient")
    if aggregate.accuracy < min_accuracy:
        failures.append("accuracy_below_gate")
    if aggregate.net_return < min_net_return:
        failures.append("net_return_below_gate")
    if aggregate.worst_fold_net_return < min_worst_fold_net_return:
        failures.append("worst_fold_below_gate")
    if aggregate.max_drawdown > max_drawdown:
        failures.append("drawdown_above_gate")
    return tuple(failures)


__all__ = [
    "RESEARCH_SCHEMA_VERSION",
    "ResearchAggregate",
    "ResearchFold",
    "ResearchModelSpec",
    "aggregate_research_folds",
    "research_gate_failures",
]
