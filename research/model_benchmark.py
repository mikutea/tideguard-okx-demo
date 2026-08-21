from __future__ import annotations

import argparse
import importlib.metadata
import json
import math
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")

import numpy as np

from okx_demo_lab.config import app_data_dir
from okx_demo_lab.ml.market_data import MarketDataError, MarketDataStore, MarketSnapshot
from okx_demo_lab.ml.pipeline import (
    DEFAULT_LABEL_HORIZON,
    FEATURE_NAMES,
    feature_contract_sha256,
    prepare_training_dataset,
)
from okx_demo_lab.ml.research import (
    RESEARCH_SCHEMA_VERSION,
    ResearchFold,
    ResearchModelSpec,
    aggregate_research_folds,
    research_gate_failures,
)
from okx_demo_lab.ml.strategy import canonical_json, sha256_hex
from okx_demo_lab.ml.walk_forward import (
    FoldMetrics,
    TrainingConfig,
    WalkForwardSpec,
    evaluate_score_vector,
    plan_walk_forward,
)


EXPECTED_VERSIONS = {
    "catboost": "1.2.10",
    "cryptofeed": "2.4.1",
    "lightgbm": "4.7.0",
    "quantstats": "0.0.81",
    "scikit-learn": "1.9.0",
    "xgboost": "3.2.0",
}
MODEL_FAMILIES = (
    "hist_gradient_boosting",
    "extra_trees",
    "mlp",
    "lightgbm",
    "xgboost",
    "catboost",
)
THRESHOLDS = (0.52, 0.56, 0.60)
STANDARD_COST_BPS = 24.0
STRESS_COST_BPS = 48.0
SEALED_FOLDS = 4
V4_TRAIN_BARS = 365 * 24 * 12
V4_TEST_BARS = 90 * 24 * 12


class BenchmarkError(RuntimeError):
    pass


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _emit(event: str, **fields: Any) -> None:
    print(canonical_json({"event": event, "at": _iso(_utc_now()), **fields}), flush=True)


def _installed_versions() -> dict[str, str]:
    found: dict[str, str] = {}
    for package, expected in EXPECTED_VERSIONS.items():
        try:
            actual = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError as exc:
            raise BenchmarkError(f"research dependency is missing: {package}=={expected}") from exc
        if actual != expected:
            raise BenchmarkError(
                f"research dependency drift: {package} expected {expected}, found {actual}"
            )
        found[package] = actual
    return found


def _balanced_weights(labels: np.ndarray) -> np.ndarray:
    labels = np.asarray(labels, dtype=np.uint8)
    positives = int(np.count_nonzero(labels == 1))
    negatives = int(labels.size - positives)
    if positives == 0 or negatives == 0:
        raise BenchmarkError("training fold contains only one class")
    return np.where(
        labels == 1,
        labels.size / (2.0 * positives),
        labels.size / (2.0 * negatives),
    ).astype(np.float64, copy=False)


@dataclass
class _StandardizedMlp:
    scaler: Any
    classifier: Any

    def fit(self, features: np.ndarray, labels: np.ndarray, weights: np.ndarray) -> None:
        scaled = self.scaler.fit_transform(features)
        self.classifier.fit(scaled, labels, sample_weight=weights)

    def predict_proba(self, features: np.ndarray) -> np.ndarray:
        return np.asarray(self.classifier.predict_proba(self.scaler.transform(features)))


def _model_factory(family: str) -> tuple[ResearchModelSpec, Callable[[], Any]]:
    if family == "hist_gradient_boosting":
        from sklearn.ensemble import HistGradientBoostingClassifier

        parameters = {
            "class_weight": "balanced",
            "early_stopping": False,
            "l2_regularization": 1.0,
            "learning_rate": 0.04,
            "max_iter": 240,
            "max_leaf_nodes": 31,
            "min_samples_leaf": 100,
            "random_state": 0,
        }
        return ResearchModelSpec(family, "scikit-learn", EXPECTED_VERSIONS["scikit-learn"], parameters), lambda: HistGradientBoostingClassifier(**parameters)
    if family == "extra_trees":
        from sklearn.ensemble import ExtraTreesClassifier

        parameters = {
            "class_weight": "balanced",
            "max_depth": 12,
            "min_samples_leaf": 64,
            "n_estimators": 240,
            "n_jobs": 1,
            "random_state": 0,
        }
        return ResearchModelSpec(family, "scikit-learn", EXPECTED_VERSIONS["scikit-learn"], parameters), lambda: ExtraTreesClassifier(**parameters)
    if family == "mlp":
        from sklearn.neural_network import MLPClassifier
        from sklearn.preprocessing import StandardScaler

        parameters = {
            "activation": "relu",
            "alpha": 0.001,
            "batch_size": 2048,
            "early_stopping": False,
            "hidden_layer_sizes": [64, 32],
            "learning_rate_init": 0.001,
            "max_iter": 60,
            "random_state": 0,
            "shuffle": False,
        }
        return ResearchModelSpec(family, "scikit-learn", EXPECTED_VERSIONS["scikit-learn"], parameters), lambda: _StandardizedMlp(StandardScaler(), MLPClassifier(**parameters))
    if family == "lightgbm":
        from lightgbm import LGBMClassifier

        parameters = {
            "colsample_bytree": 0.8,
            "deterministic": True,
            "force_col_wise": True,
            "learning_rate": 0.04,
            "min_child_samples": 100,
            "n_estimators": 240,
            "n_jobs": 1,
            "num_leaves": 31,
            "random_state": 0,
            "reg_lambda": 1.0,
            "subsample": 0.8,
            "verbosity": -1,
        }
        return ResearchModelSpec(family, "lightgbm", EXPECTED_VERSIONS["lightgbm"], parameters), lambda: LGBMClassifier(**parameters)
    if family == "xgboost":
        from xgboost import XGBClassifier

        parameters = {
            "colsample_bytree": 0.8,
            "learning_rate": 0.04,
            "max_depth": 5,
            "min_child_weight": 20,
            "n_estimators": 240,
            "n_jobs": 1,
            "random_state": 0,
            "reg_alpha": 0.1,
            "reg_lambda": 1.0,
            "subsample": 0.8,
            "tree_method": "hist",
        }
        return ResearchModelSpec(family, "xgboost", EXPECTED_VERSIONS["xgboost"], parameters), lambda: XGBClassifier(**parameters)
    if family == "catboost":
        from catboost import CatBoostClassifier

        parameters = {
            "allow_writing_files": False,
            "depth": 6,
            "iterations": 240,
            "l2_leaf_reg": 3.0,
            "learning_rate": 0.04,
            "loss_function": "Logloss",
            "random_seed": 0,
            "thread_count": 1,
            "verbose": False,
        }
        return ResearchModelSpec(family, "catboost", EXPECTED_VERSIONS["catboost"], parameters), lambda: CatBoostClassifier(**parameters)
    raise BenchmarkError(f"unsupported model family: {family}")


def _fit_model(model: Any, features: np.ndarray, labels: np.ndarray) -> None:
    weights = _balanced_weights(labels)
    if isinstance(model, _StandardizedMlp):
        model.fit(features, labels, weights)
    else:
        model.fit(features, labels, sample_weight=weights)


def _positive_scores(model: Any, features: np.ndarray) -> np.ndarray:
    probabilities = np.asarray(model.predict_proba(features), dtype=np.float64)
    if probabilities.ndim != 2 or probabilities.shape != (len(features), 2):
        raise BenchmarkError("model probability output does not have two classes")
    scores = probabilities[:, 1]
    if not np.all(np.isfinite(scores)) or np.any(scores < 0.0) or np.any(scores > 1.0):
        raise BenchmarkError("model probability output is invalid")
    return np.ascontiguousarray(scores)


def _fold_result(
    observations: Any,
    fold: Any,
    scores: np.ndarray,
    *,
    threshold: float,
    cost_bps: float,
) -> ResearchFold:
    test = observations.window(fold.test_start, fold.test_stop)
    trades, accuracy, gross, net, drawdown, evaluated = evaluate_score_vector(
        test.labels,
        test.forward_returns,
        scores,
        buy_threshold=threshold,
        cost_bps=cost_bps,
        holding_period_bars=DEFAULT_LABEL_HORIZON,
    )
    return ResearchFold(
        metrics=FoldMetrics(
            fold=fold.fold,
            train_start_at=observations.observed_at(fold.train_start),
            train_stop_at=observations.observed_at(fold.train_stop - 1),
            test_start_at=observations.observed_at(fold.test_start),
            test_stop_at=observations.observed_at(fold.test_stop - 1),
            train_rows=fold.train_stop - fold.train_start,
            test_rows=fold.test_stop - fold.test_start,
            trades=trades,
            accuracy=accuracy,
            gross_return=gross,
            net_return=net,
            max_drawdown=drawdown,
        ),
        evaluated_rows=evaluated,
    )


def _aggregate_payload(folds: Sequence[ResearchFold], *, sealed: bool) -> dict[str, Any]:
    aggregate = aggregate_research_folds(folds)
    failures = research_gate_failures(
        aggregate,
        min_folds=SEALED_FOLDS if sealed else 5,
        min_trades=20,
        min_accuracy=0.52,
        min_net_return=0.005,
        min_worst_fold_net_return=-0.03,
        max_drawdown=0.10,
    )
    return {**aggregate.to_dict(), "failures": list(failures), "passed": not failures}


def _stress_payload(folds: Sequence[ResearchFold]) -> dict[str, Any]:
    aggregate = aggregate_research_folds(folds)
    failures: list[str] = []
    if aggregate.net_return < 0.0:
        failures.append("stress_net_return_below_zero")
    if aggregate.max_drawdown > 0.15:
        failures.append("stress_drawdown_above_gate")
    return {**aggregate.to_dict(), "failures": failures, "passed": not failures}


def _choose_threshold(dev_by_threshold: dict[float, list[ResearchFold]]) -> float:
    ranked = []
    for threshold, folds in dev_by_threshold.items():
        aggregate = aggregate_research_folds(folds)
        ranked.append(
            (
                not research_gate_failures(
                    aggregate,
                    min_folds=5,
                    min_trades=20,
                    min_accuracy=0.52,
                    min_net_return=0.005,
                    min_worst_fold_net_return=-0.03,
                    max_drawdown=0.10,
                ),
                aggregate.net_return,
                -aggregate.max_drawdown,
                threshold,
            )
        )
    return max(ranked)[-1]


def _evaluate_family(
    family: str,
    spec: ResearchModelSpec,
    fold_scores: Sequence[np.ndarray],
    observations: Any,
    folds: Sequence[Any],
) -> dict[str, Any]:
    if len(folds) <= SEALED_FOLDS:
        dev_indices = tuple(range(len(folds)))
        sealed_indices: tuple[int, ...] = ()
    else:
        dev_indices = tuple(range(len(folds) - SEALED_FOLDS))
        sealed_indices = tuple(range(len(folds) - SEALED_FOLDS, len(folds)))
    threshold_results: dict[float, list[ResearchFold]] = {}
    for threshold in THRESHOLDS:
        threshold_results[threshold] = [
            _fold_result(
                observations,
                folds[index],
                fold_scores[index],
                threshold=threshold,
                cost_bps=STANDARD_COST_BPS,
            )
            for index in range(len(folds))
        ]
    chosen = _choose_threshold(
        {threshold: [items[index] for index in dev_indices] for threshold, items in threshold_results.items()}
    )
    selected = threshold_results[chosen]
    dev = [selected[index] for index in dev_indices]
    sealed = [selected[index] for index in sealed_indices]
    stress = [
        _fold_result(
            observations,
            folds[index],
            fold_scores[index],
            threshold=chosen,
            cost_bps=STRESS_COST_BPS,
        )
        for index in range(len(folds))
    ]
    all_payload = _aggregate_payload(selected, sealed=False)
    dev_payload = _aggregate_payload(dev, sealed=False)
    sealed_payload = (
        _aggregate_payload(sealed, sealed=True)
        if sealed
        else {"passed": False, "failures": ["sealed_folds_unavailable"]}
    )
    stress_payload = _stress_payload(stress)
    failures = []
    for scope, payload in (("all", all_payload), ("sealed", sealed_payload), ("stress", stress_payload)):
        failures.extend(f"{scope}:{item}" for item in payload["failures"])
    return {
        "chosenThreshold": chosen,
        "decision": "eligible_for_native_adapter_review" if not failures else "rejected",
        "development": dev_payload,
        "failures": failures,
        "family": family,
        "folds": [item.to_dict() for item in selected],
        "modelSpec": {**spec.to_dict(), "sha256": spec.sha256},
        "ordinary": all_payload,
        "sealed": sealed_payload,
        "stress48Bps": stress_payload,
        "thresholdDevelopment": {
            str(threshold): _aggregate_payload(
                [items[index] for index in dev_indices], sealed=False
            )
            for threshold, items in threshold_results.items()
        },
    }


def _latest_snapshot(store: MarketDataStore) -> MarketSnapshot:
    status = store.status()
    if (
        not status["backfillComplete"]
        or status["syncStatus"] != "idle"
        or status["missingBars"] != 0
        or status["unresolvedConflicts"] != 0
    ):
        raise MarketDataError("market warehouse is not complete and clean")
    latest = status["latestSnapshot"]
    if not isinstance(latest, dict) or not isinstance(latest.get("snapshotId"), str):
        raise MarketDataError("latest immutable market snapshot is unavailable")
    snapshot = store.get_snapshot(latest["snapshotId"])
    if snapshot is None or not store.snapshot_is_current(snapshot.content_sha256):
        raise MarketDataError("latest immutable market snapshot is not current")
    if snapshot.feature_contract_sha256 != feature_contract_sha256():
        raise MarketDataError("snapshot feature contract differs from current code")
    return snapshot


def _walk_forward_spec(snapshot: MarketSnapshot) -> WalkForwardSpec:
    protocol = WalkForwardSpec(
        train_size=V4_TRAIN_BARS,
        test_size=V4_TEST_BARS,
        step_size=V4_TEST_BARS,
        label_horizon=DEFAULT_LABEL_HORIZON,
        embargo_size=1,
        expanding=False,
    )
    cohort_id = "cohort_" + sha256_hex(
        canonical_json(
            {
                "market_snapshot_sha256": snapshot.content_sha256,
                "split_protocol_sha256": protocol.split_protocol_sha256,
            }
        )
    )[:24]
    return WalkForwardSpec(
        train_size=protocol.train_size,
        test_size=protocol.test_size,
        step_size=protocol.step_size,
        label_horizon=protocol.label_horizon,
        embargo_size=protocol.embargo_size,
        expanding=False,
        benchmark_cohort_id=cohort_id,
        market_snapshot_sha256=snapshot.content_sha256,
    )


def run_benchmark(
    *,
    data_path: Path,
    families: Sequence[str],
    max_folds: int | None,
) -> dict[str, Any]:
    started = _utc_now()
    versions = _installed_versions()
    store = MarketDataStore(data_path)
    snapshot = _latest_snapshot(store)
    _emit("dataset.load_started", snapshotId=snapshot.snapshot_id, rows=snapshot.row_count)
    training_config = TrainingConfig(round_trip_cost_bps=STANDARD_COST_BPS)
    prepared = prepare_training_dataset(
        store.snapshot_rows(snapshot.snapshot_id),
        now=_utc_now(),
        training_config=training_config,
    )
    observations = prepared.observations
    walk_spec = _walk_forward_spec(snapshot)
    planned = list(plan_walk_forward(len(observations), walk_spec))
    if max_folds is not None:
        if max_folds < 1:
            raise BenchmarkError("max_folds must be positive")
        planned = planned[:max_folds]
    _emit("dataset.ready", observations=len(observations), folds=len(planned))

    prediction_bank: dict[str, list[np.ndarray]] = {}
    specs: dict[str, ResearchModelSpec] = {}
    family_seconds: dict[str, float] = {}
    for family in families:
        if family not in MODEL_FAMILIES:
            raise BenchmarkError(f"unsupported model family: {family}")
        spec, factory = _model_factory(family)
        specs[family] = spec
        scores_by_fold: list[np.ndarray] = []
        family_started = time.perf_counter()
        for position, fold in enumerate(planned, start=1):
            fold_started = time.perf_counter()
            train = observations.window(fold.train_start, fold.train_stop)
            test = observations.window(fold.test_start, fold.test_stop)
            model = factory()
            _fit_model(model, np.asarray(train.features), np.asarray(train.labels))
            scores_by_fold.append(_positive_scores(model, np.asarray(test.features)))
            elapsed = time.perf_counter() - fold_started
            _emit(
                "model.fold_completed",
                family=family,
                fold=fold.fold,
                position=position,
                total=len(planned),
                seconds=round(elapsed, 3),
            )
        prediction_bank[family] = scores_by_fold
        family_seconds[family] = time.perf_counter() - family_started

    if len(families) >= 2:
        ensemble_family = "probability_ensemble"
        prediction_bank[ensemble_family] = [
            np.mean(
                np.vstack([prediction_bank[family][index] for family in families]),
                axis=0,
            )
            for index in range(len(planned))
        ]
        specs[ensemble_family] = ResearchModelSpec(
            ensemble_family,
            "moheng-local",
            "0.4.0",
            {"aggregation": "unweighted_probability_mean", "members": list(families)},
        )
        family_seconds[ensemble_family] = 0.0

    results = []
    for family, fold_scores in prediction_bank.items():
        result = _evaluate_family(family, specs[family], fold_scores, observations, planned)
        result["trainingSeconds"] = round(family_seconds[family], 3)
        results.append(result)
        _emit(
            "model.evaluated",
            family=family,
            decision=result["decision"],
            failures=result["failures"],
        )

    if not store.snapshot_is_current(snapshot.content_sha256):
        raise MarketDataError("market snapshot became stale during benchmark")
    report = {
        "benchmarkId": "bench_" + sha256_hex(
            canonical_json(
                {
                    "at": _iso(started),
                    "families": list(families),
                    "snapshot": snapshot.content_sha256,
                }
            )
        )[:24],
        "completedAt": _iso(_utc_now()),
        "costProtocol": {
            "ordinaryBps": STANDARD_COST_BPS,
            "stressBps": STRESS_COST_BPS,
        },
        "dataset": {
            **snapshot.to_dict(),
            "labelContractSha256": prepared.label_contract_sha256,
            "observations": len(observations),
        },
        "evaluation": {
            "capital": "cash_spot_long_flat_non_overlapping",
            "embargoBars": walk_spec.embargo_size,
            "holdingBars": DEFAULT_LABEL_HORIZON,
            "sealedFolds": SEALED_FOLDS,
            "testBars": V4_TEST_BARS,
            "thresholdsPredeclared": list(THRESHOLDS),
            "trainBars": V4_TRAIN_BARS,
        },
        "packages": versions,
        "results": results,
        "schemaVersion": RESEARCH_SCHEMA_VERSION,
        "startedAt": _iso(started),
        "walkForward": walk_spec.to_dict(),
    }
    report["reportSha256"] = sha256_hex(canonical_json(report))
    return report


def _write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run isolated third-party models against the immutable MOHENG OOS protocol."
    )
    parser.add_argument(
        "--data-path",
        type=Path,
        default=app_data_dir() / "market-data.sqlite3",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("research/results/latest-benchmark.json"),
    )
    parser.add_argument(
        "--families",
        default=",".join(MODEL_FAMILIES),
        help="Comma-separated fixed family names.",
    )
    parser.add_argument("--max-folds", type=int)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    families = tuple(item.strip() for item in args.families.split(",") if item.strip())
    if not families:
        raise BenchmarkError("at least one model family is required")
    report = run_benchmark(
        data_path=args.data_path,
        families=families,
        max_folds=args.max_folds,
    )
    _write_report(args.output, report)
    _emit(
        "benchmark.completed",
        output=str(args.output.resolve()),
        reportSha256=report["reportSha256"],
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (BenchmarkError, MarketDataError) as exc:
        _emit("benchmark.failed", errorType=type(exc).__name__, message=str(exc))
        raise SystemExit(2) from exc
