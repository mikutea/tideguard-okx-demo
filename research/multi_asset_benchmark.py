from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")

import numpy as np

try:
    from .model_benchmark import (
        MODEL_FAMILIES,
        _fit_model,
        _installed_versions,
        _model_factory,
        _positive_scores,
    )
except ImportError:  # pragma: no cover - direct script execution
    from model_benchmark import (
        MODEL_FAMILIES,
        _fit_model,
        _installed_versions,
        _model_factory,
        _positive_scores,
    )
from okx_demo_lab.ml.multi_asset_cohort import (
    MultiAssetCohortError,
    load_validated_cohort,
)
from okx_demo_lab.ml.multi_asset_research import (
    MULTI_ASSET_RESEARCH_SCHEMA_VERSION,
    PortfolioFoldMetrics,
    PreparedMultiAssetDataset,
    aggregate_portfolio_folds,
    evaluate_portfolio_scores,
    portfolio_gate_failures,
    prepare_multi_asset_dataset,
)
from okx_demo_lab.ml.pipeline import DEFAULT_LABEL_HORIZON
from okx_demo_lab.ml.strategy import canonical_json, sha256_hex
from okx_demo_lab.ml.walk_forward import (
    TrainingConfig,
    ValidationError,
    WalkForwardSpec,
    plan_walk_forward,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RESEARCH_DATA_ROOT = PROJECT_ROOT / ".research-data"
THRESHOLDS = (0.52, 0.56, 0.60)
STANDARD_COST_BPS = 24.0
STRESS_COST_BPS = 48.0
SEALED_FOLDS = 4
TRAIN_BARS = 365 * 24 * 12
TEST_BARS = 90 * 24 * 12


class MultiAssetBenchmarkError(RuntimeError):
    pass


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso_from_ms(value: int) -> str:
    return (
        datetime.fromtimestamp(value / 1_000, tz=timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _emit(event: str, **fields: Any) -> None:
    print(canonical_json({"event": event, "at": _iso(_utc_now()), **fields}), flush=True)


def _inside_research_subdir(path: Path, subdir: str) -> Path:
    resolved = path.expanduser().resolve()
    try:
        resolved.relative_to((RESEARCH_DATA_ROOT / subdir).resolve())
    except ValueError as exc:
        raise MultiAssetBenchmarkError(
            f"benchmark path must stay under project .research-data/{subdir}"
        ) from exc
    return resolved


def _fold_payload(
    dataset: PreparedMultiAssetDataset,
    fold: Any,
    metrics: PortfolioFoldMetrics,
) -> dict[str, Any]:
    return {
        "fold": int(fold.fold),
        "testStartAt": _iso_from_ms(int(dataset.timestamps_ms[fold.test_start])),
        "testStopAt": _iso_from_ms(int(dataset.timestamps_ms[fold.test_stop - 1])),
        "trainRows": int((fold.train_stop - fold.train_start) * dataset.asset_rows),
        "trainStartAt": _iso_from_ms(int(dataset.timestamps_ms[fold.train_start])),
        "trainStopAt": _iso_from_ms(int(dataset.timestamps_ms[fold.train_stop - 1])),
        **metrics.to_dict(dataset.instruments),
    }


def _aggregate_payload(
    dataset: PreparedMultiAssetDataset,
    folds: Sequence[PortfolioFoldMetrics],
    *,
    min_folds: int,
) -> dict[str, Any]:
    aggregate = aggregate_portfolio_folds(folds)
    failures = portfolio_gate_failures(
        aggregate,
        min_folds=min_folds,
        min_trades=20,
        min_profitable_trade_rate=0.50,
        min_net_return=0.005,
        min_worst_fold_net_return=-0.03,
        max_drawdown=0.10,
    )
    return {
        **aggregate.to_dict(dataset.instruments),
        "exploratoryGatePassed": not failures,
        "failures": list(failures),
    }


def _stress_payload(
    dataset: PreparedMultiAssetDataset,
    folds: Sequence[PortfolioFoldMetrics],
) -> dict[str, Any]:
    aggregate = aggregate_portfolio_folds(folds)
    failures: list[str] = []
    if aggregate.net_return < 0.0:
        failures.append("stress_net_return_below_zero")
    if aggregate.max_drawdown > 0.15:
        failures.append("stress_drawdown_above_gate")
    return {
        **aggregate.to_dict(dataset.instruments),
        "exploratoryGatePassed": not failures,
        "failures": failures,
    }


def _evaluate_scores(
    dataset: PreparedMultiAssetDataset,
    folds: Sequence[Any],
    score_matrices: Sequence[np.ndarray],
) -> dict[str, Any]:
    if len(folds) != len(score_matrices):
        raise MultiAssetBenchmarkError("fold predictions are incomplete")
    if len(folds) <= SEALED_FOLDS:
        development_indices = tuple(range(len(folds)))
        sealed_indices: tuple[int, ...] = ()
    else:
        development_indices = tuple(range(len(folds) - SEALED_FOLDS))
        sealed_indices = tuple(range(len(folds) - SEALED_FOLDS, len(folds)))

    ordinary_by_threshold: dict[float, list[PortfolioFoldMetrics]] = {}
    for threshold in THRESHOLDS:
        ordinary_by_threshold[threshold] = [
            evaluate_portfolio_scores(
                dataset.labels[fold.test_start : fold.test_stop],
                dataset.forward_returns[fold.test_start : fold.test_stop],
                scores,
                buy_threshold=threshold,
                cost_bps=STANDARD_COST_BPS,
            )
            for fold, scores in zip(folds, score_matrices, strict=True)
        ]
    ranked: list[tuple[bool, float, float, float]] = []
    threshold_development: dict[str, Any] = {}
    for threshold, metrics in ordinary_by_threshold.items():
        selected = [metrics[index] for index in development_indices]
        payload = _aggregate_payload(
            dataset, selected, min_folds=min(5, len(development_indices))
        )
        threshold_development[str(threshold)] = payload
        ranked.append(
            (
                bool(payload["exploratoryGatePassed"]),
                float(payload["netReturn"]),
                -float(payload["maxDrawdown"]),
                threshold,
            )
        )
    chosen = max(ranked)[-1]
    ordinary = ordinary_by_threshold[chosen]
    development = [ordinary[index] for index in development_indices]
    sealed = [ordinary[index] for index in sealed_indices]
    stress = [
        evaluate_portfolio_scores(
            dataset.labels[fold.test_start : fold.test_stop],
            dataset.forward_returns[fold.test_start : fold.test_stop],
            scores,
            buy_threshold=chosen,
            cost_bps=STRESS_COST_BPS,
        )
        for fold, scores in zip(folds, score_matrices, strict=True)
    ]
    ordinary_payload = _aggregate_payload(
        dataset, ordinary, min_folds=min(5, len(ordinary))
    )
    development_payload = _aggregate_payload(
        dataset, development, min_folds=min(5, len(development))
    )
    sealed_payload = (
        _aggregate_payload(dataset, sealed, min_folds=SEALED_FOLDS)
        if sealed
        else {
            "exploratoryGatePassed": False,
            "failures": ["sealed_folds_unavailable"],
        }
    )
    stress_payload = _stress_payload(dataset, stress)
    return {
        "chosenThreshold": chosen,
        "decision": "research_only",
        "development": development_payload,
        "exploratoryGatePassed": bool(
            development_payload["exploratoryGatePassed"]
            and ordinary_payload["exploratoryGatePassed"]
            and sealed_payload["exploratoryGatePassed"]
            and stress_payload["exploratoryGatePassed"]
        ),
        "folds": [
            _fold_payload(dataset, fold, metrics)
            for fold, metrics in zip(folds, ordinary, strict=True)
        ],
        "ordinary": ordinary_payload,
        "promotionBlockers": [
            "fixed_current_survivor_cohort",
            "requires_90_day_forward_public_shadow",
            "static_cost_only",
            "manual_model_review_required",
        ],
        "sealed": sealed_payload,
        "stress48Bps": stress_payload,
        "thresholdDevelopment": threshold_development,
    }


def run_benchmark(
    *,
    cohort_manifest: Path,
    families: Sequence[str],
    max_folds: int | None,
) -> dict[str, Any]:
    started = _utc_now()
    if not families or len(families) != len(set(families)):
        raise MultiAssetBenchmarkError("model families must be non-empty and unique")
    versions = _installed_versions()
    cohort = load_validated_cohort(cohort_manifest)
    dataset = prepare_multi_asset_dataset(
        cohort,
        now=started,
        training_config=TrainingConfig(round_trip_cost_bps=STANDARD_COST_BPS),
    )
    protocol = WalkForwardSpec(
        train_size=TRAIN_BARS,
        test_size=TEST_BARS,
        step_size=TEST_BARS,
        label_horizon=DEFAULT_LABEL_HORIZON,
        embargo_size=1,
        expanding=False,
    )
    folds = list(plan_walk_forward(dataset.time_rows, protocol))
    if max_folds is not None:
        if max_folds < 1:
            raise MultiAssetBenchmarkError("max-folds must be positive")
        folds = folds[:max_folds]
    if not folds:
        raise MultiAssetBenchmarkError("cohort is too short for a benchmark fold")
    _emit(
        "multi_asset.dataset_ready",
        assets=dataset.asset_rows,
        cohortId=dataset.cohort_id,
        folds=len(folds),
        timeRows=dataset.time_rows,
    )

    predictions: dict[str, list[np.ndarray]] = {}
    specs: dict[str, Any] = {}
    training_seconds: dict[str, float] = {}
    for family in families:
        if family not in MODEL_FAMILIES:
            raise MultiAssetBenchmarkError(f"unsupported model family: {family}")
        spec, factory = _model_factory(family)
        specs[family] = spec
        family_scores: list[np.ndarray] = []
        family_started = time.perf_counter()
        for position, fold in enumerate(folds, start=1):
            fold_started = time.perf_counter()
            train_features, train_labels = dataset.flat_window(
                fold.train_start, fold.train_stop
            )
            test_features = dataset.features[
                fold.test_start : fold.test_stop
            ].reshape(-1, len(dataset.feature_names))
            model = factory()
            _fit_model(model, train_features, train_labels)
            scores = _positive_scores(model, test_features).reshape(
                fold.test_stop - fold.test_start, dataset.asset_rows
            )
            family_scores.append(np.asarray(scores, dtype=np.float32))
            _emit(
                "multi_asset.fold_completed",
                family=family,
                fold=fold.fold,
                position=position,
                seconds=round(time.perf_counter() - fold_started, 3),
                total=len(folds),
            )
        predictions[family] = family_scores
        training_seconds[family] = time.perf_counter() - family_started

    if len(families) >= 2:
        ensemble = "probability_ensemble"
        predictions[ensemble] = [
            np.mean(
                np.stack([predictions[family][index] for family in families]),
                axis=0,
                dtype=np.float64,
            ).astype(np.float32)
            for index in range(len(folds))
        ]
        specs[ensemble] = None
        training_seconds[ensemble] = 0.0

    results: list[dict[str, Any]] = []
    for family, score_matrices in predictions.items():
        result = _evaluate_scores(dataset, folds, score_matrices)
        result.update(
            {
                "family": family,
                "modelSpec": (
                    {
                        **specs[family].to_dict(),
                        "sha256": specs[family].sha256,
                    }
                    if specs[family] is not None
                    else {
                        "aggregation": "unweighted_probability_mean",
                        "members": list(families),
                    }
                ),
                "trainingSeconds": round(training_seconds[family], 3),
            }
        )
        results.append(result)
        _emit(
            "multi_asset.model_evaluated",
            exploratoryGatePassed=result["exploratoryGatePassed"],
            family=family,
        )

    ending_cohort = load_validated_cohort(cohort_manifest)
    if ending_cohort.manifest.get("contentSha256") != dataset.cohort_sha256:
        raise MultiAssetBenchmarkError("cohort changed during benchmark")
    feature_contract = {
        "dtype": "float32",
        "featureNames": list(dataset.feature_names),
        "labelHorizonBars": DEFAULT_LABEL_HORIZON,
        "portfolio": "cash-spot-long-flat-top-score-non-overlapping",
        "schemaVersion": MULTI_ASSET_RESEARCH_SCHEMA_VERSION,
    }
    report: dict[str, Any] = {
        "benchmarkId": "mabench_"
        + sha256_hex(
            canonical_json(
                {
                    "at": _iso(started),
                    "cohort": dataset.cohort_sha256,
                    "families": list(families),
                }
            )
        )[:24],
        "completedAt": _iso(_utc_now()),
        "costProtocol": {
            "ordinaryBps": STANDARD_COST_BPS,
            "stressBps": STRESS_COST_BPS,
        },
        "dataset": {
            "assetRows": dataset.asset_rows,
            "cohortId": dataset.cohort_id,
            "cohortSha256": dataset.cohort_sha256,
            "featureContract": feature_contract,
            "featureContractSha256": sha256_hex(canonical_json(feature_contract)),
            "instruments": list(dataset.instruments),
            "labelContractSha256": dataset.label_contract_sha256,
            "timeRows": dataset.time_rows,
        },
        "evaluation": {
            "embargoBars": protocol.embargo_size,
            "holdingBars": DEFAULT_LABEL_HORIZON,
            "sealedFolds": SEALED_FOLDS,
            "testBars": TEST_BARS,
            "thresholdsPredeclared": list(THRESHOLDS),
            "trainBars": TRAIN_BARS,
        },
        "packages": versions,
        "promotable": False,
        "results": results,
        "schemaVersion": MULTI_ASSET_RESEARCH_SCHEMA_VERSION,
        "startedAt": _iso(started),
        "walkForward": {
            **protocol.to_dict(),
            "splitProtocolSha256": protocol.split_protocol_sha256,
        },
    }
    report["reportSha256"] = sha256_hex(canonical_json(report))
    return report


def _write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise MultiAssetBenchmarkError("benchmark evidence already exists")
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a research-only multi-asset portfolio benchmark."
    )
    parser.add_argument("--cohort", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--families", default=",".join(MODEL_FAMILIES))
    parser.add_argument("--max-folds", type=int)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    families = tuple(item.strip() for item in args.families.split(",") if item.strip())
    if not families:
        raise MultiAssetBenchmarkError("at least one model family is required")
    cohort = _inside_research_subdir(args.cohort, "cohorts")
    output = _inside_research_subdir(args.output, "benchmarks")
    if cohort.name != "manifest.json" or output.suffix.lower() != ".json":
        raise MultiAssetBenchmarkError("benchmark cohort or output filename is invalid")
    report = run_benchmark(
        cohort_manifest=cohort,
        families=families,
        max_folds=args.max_folds,
    )
    _write_report(output, report)
    _emit(
        "multi_asset.benchmark_completed",
        output=str(output),
        reportSha256=report["reportSha256"],
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (
        MultiAssetBenchmarkError,
        MultiAssetCohortError,
        ValidationError,
        ValueError,
    ) as exc:
        _emit(
            "multi_asset.benchmark_failed",
            errorType=type(exc).__name__,
            message=str(exc),
        )
        raise SystemExit(2) from exc
