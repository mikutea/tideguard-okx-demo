from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import dataclass
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
    evaluate_cost_aware_portfolio,
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
STANDARD_COST_BPS = 24.0
STRESS_COST_BPS = 48.0
TRAIN_BARS = 365 * 24 * 12
TEST_BARS = 90 * 24 * 12
DEVELOPMENT_FOLDS = 5
PRIOR_SEALED_FOLDS = 4
CALIBRATION_BARS = 30 * 24 * 12
CALIBRATION_PURGE_BARS = DEFAULT_LABEL_HORIZON + 1
POLICY_CANDIDATES = tuple(
    (edge_buffer_bps, min_entry_spacing_bars)
    for min_entry_spacing_bars in (48, 96)
    for edge_buffer_bps in (12.0, 24.0, 48.0)
)


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


@dataclass(frozen=True)
class FoldCalibration:
    probability_model: Any | None
    expected_return_model: Any | None
    constant_probability: float
    constant_expected_return: float

    def calibrated_probabilities(self, raw_scores: np.ndarray) -> np.ndarray:
        scores = np.asarray(raw_scores, dtype=np.float64).reshape(-1)
        if not np.all(np.isfinite(scores)) or np.any(scores < 0.0) or np.any(scores > 1.0):
            raise MultiAssetBenchmarkError("raw calibration scores are invalid")
        if self.probability_model is None:
            return np.full(scores.shape, self.constant_probability, dtype=np.float64)
        probabilities = np.asarray(
            self.probability_model.predict_proba(_score_logits(scores).reshape(-1, 1)),
            dtype=np.float64,
        )[:, 1]
        if not np.all(np.isfinite(probabilities)):
            raise MultiAssetBenchmarkError("calibrated probabilities are invalid")
        return np.clip(probabilities, 0.0, 1.0)

    def expected_returns(self, raw_scores: np.ndarray) -> np.ndarray:
        probabilities = self.calibrated_probabilities(raw_scores)
        if self.expected_return_model is None:
            return np.full(
                probabilities.shape, self.constant_expected_return, dtype=np.float64
            )
        expected = np.asarray(
            self.expected_return_model.predict(probabilities), dtype=np.float64
        )
        if not np.all(np.isfinite(expected)) or np.any(np.abs(expected) > 1.0):
            raise MultiAssetBenchmarkError("calibrated expected returns are invalid")
        return expected


def _score_logits(scores: np.ndarray) -> np.ndarray:
    clipped = np.clip(np.asarray(scores, dtype=np.float64), 1e-6, 1.0 - 1e-6)
    return np.log(clipped / (1.0 - clipped))


def _fit_fold_calibration(
    raw_scores: np.ndarray,
    labels: np.ndarray,
    forward_returns: np.ndarray,
) -> FoldCalibration:
    from sklearn.isotonic import IsotonicRegression
    from sklearn.linear_model import LogisticRegression

    scores = np.asarray(raw_scores, dtype=np.float64).reshape(-1)
    label_values = np.asarray(labels, dtype=np.uint8).reshape(-1)
    return_values = np.asarray(forward_returns, dtype=np.float64).reshape(-1)
    if (
        scores.size < 100
        or scores.shape != label_values.shape
        or scores.shape != return_values.shape
        or not np.all(np.isfinite(scores))
        or not np.all(np.isfinite(return_values))
        or np.any(scores < 0.0)
        or np.any(scores > 1.0)
        or np.any((label_values != 0) & (label_values != 1))
    ):
        raise MultiAssetBenchmarkError("calibration arrays are invalid")

    constant_probability = float(np.mean(label_values))
    probability_model: Any | None = None
    if np.unique(label_values).size == 2 and np.unique(scores).size >= 2:
        probability_model = LogisticRegression(
            C=1.0,
            max_iter=200,
            random_state=0,
            solver="lbfgs",
        )
        probability_model.fit(_score_logits(scores).reshape(-1, 1), label_values)
    provisional = FoldCalibration(
        probability_model=probability_model,
        expected_return_model=None,
        constant_probability=constant_probability,
        constant_expected_return=float(np.mean(return_values)),
    )
    probabilities = provisional.calibrated_probabilities(scores)
    expected_return_model: Any | None = None
    if np.unique(probabilities).size >= 2 and np.ptp(return_values) > 0.0:
        expected_return_model = IsotonicRegression(
            increasing=True,
            out_of_bounds="clip",
        )
        expected_return_model.fit(probabilities, return_values)
    return FoldCalibration(
        probability_model=probability_model,
        expected_return_model=expected_return_model,
        constant_probability=constant_probability,
        constant_expected_return=float(np.mean(return_values)),
    )


def _score_diagnostics(
    raw_scores: np.ndarray,
    labels: np.ndarray,
    forward_returns: np.ndarray,
    calibration: FoldCalibration,
) -> dict[str, Any]:
    raw = np.asarray(raw_scores, dtype=np.float64).reshape(-1)
    label_values = np.asarray(labels, dtype=np.float64).reshape(-1)
    return_values = np.asarray(forward_returns, dtype=np.float64).reshape(-1)
    calibrated = calibration.calibrated_probabilities(raw)
    expected = calibration.expected_returns(raw)
    quantiles = np.quantile(expected, (0.10, 0.50, 0.90)) * 10_000.0
    return {
        "actualMeanReturnBps": float(np.mean(return_values) * 10_000.0),
        "calibratedBrier": float(np.mean(np.square(calibrated - label_values))),
        "calibratedProbabilityMean": float(np.mean(calibrated)),
        "expectedReturnBpsP10": float(quantiles[0]),
        "expectedReturnBpsP50": float(quantiles[1]),
        "expectedReturnBpsP90": float(quantiles[2]),
        "positiveRate": float(np.mean(label_values)),
        "rawBrier": float(np.mean(np.square(raw - label_values))),
        "rows": int(raw.size),
    }


def _fold_training_windows(
    fold: Any,
    *,
    calibration_bars: int = CALIBRATION_BARS,
    calibration_purge_bars: int = CALIBRATION_PURGE_BARS,
) -> tuple[int, int, int, int]:
    calibration_start = int(fold.train_stop) - calibration_bars
    model_fit_stop = calibration_start - calibration_purge_bars
    if (
        calibration_bars < 1
        or calibration_purge_bars < DEFAULT_LABEL_HORIZON
        or model_fit_stop <= int(fold.train_start)
        or calibration_start >= int(fold.train_stop)
    ):
        raise MultiAssetBenchmarkError("fold is too short for isolated calibration")
    return (
        int(fold.train_start),
        model_fit_stop,
        calibration_start,
        int(fold.train_stop),
    )


def _fold_payload(
    dataset: PreparedMultiAssetDataset,
    fold: Any,
    metrics: PortfolioFoldMetrics,
    calibration: dict[str, Any],
) -> dict[str, Any]:
    return {
        "fold": int(fold.fold),
        "testStartAt": _iso_from_ms(int(dataset.timestamps_ms[fold.test_start])),
        "testStopAt": _iso_from_ms(int(dataset.timestamps_ms[fold.test_stop - 1])),
        **calibration,
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
        min_gross_return=0.0,
        min_net_return=0.005,
        min_worst_fold_net_return=-0.03,
        max_drawdown=0.10,
        max_trades_per_day=3.0,
        max_instrument_trade_share=0.60,
    )
    return {
        **aggregate.to_dict(dataset.instruments),
        "developmentGatePassed": not failures,
        "failures": list(failures),
    }


def _stress_payload(
    dataset: PreparedMultiAssetDataset,
    folds: Sequence[PortfolioFoldMetrics],
) -> dict[str, Any]:
    aggregate = aggregate_portfolio_folds(folds)
    failures: list[str] = []
    if aggregate.gross_return < 0.0:
        failures.append("stress_gross_return_below_zero")
    if aggregate.net_return < 0.0:
        failures.append("stress_net_return_below_zero")
    if aggregate.max_drawdown > 0.15:
        failures.append("stress_drawdown_above_gate")
    return {
        **aggregate.to_dict(dataset.instruments),
        "developmentGatePassed": not failures,
        "failures": failures,
    }


def _policy_key(edge_buffer_bps: float, min_entry_spacing_bars: int) -> str:
    return f"buffer-{edge_buffer_bps:g}bps_spacing-{min_entry_spacing_bars}bars"


def _evaluate_expected_returns(
    dataset: PreparedMultiAssetDataset,
    folds: Sequence[Any],
    expected_return_matrices: Sequence[np.ndarray],
    calibration_details: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    if (
        len(folds) != len(expected_return_matrices)
        or len(folds) != len(calibration_details)
    ):
        raise MultiAssetBenchmarkError("fold predictions are incomplete")
    metrics_by_policy: dict[tuple[float, int], list[PortfolioFoldMetrics]] = {}
    policy_development: dict[str, Any] = {}
    ranked: list[tuple[bool, float, float, float, int, float]] = []
    for edge_buffer_bps, min_entry_spacing_bars in POLICY_CANDIDATES:
        metrics = [
            evaluate_cost_aware_portfolio(
                dataset.labels[fold.test_start : fold.test_stop],
                dataset.forward_returns[fold.test_start : fold.test_stop],
                expected,
                cost_bps=STANDARD_COST_BPS,
                edge_buffer_bps=edge_buffer_bps,
                holding_period_bars=DEFAULT_LABEL_HORIZON,
                min_entry_spacing_bars=min_entry_spacing_bars,
            )
            for fold, expected in zip(folds, expected_return_matrices, strict=True)
        ]
        metrics_by_policy[(edge_buffer_bps, min_entry_spacing_bars)] = metrics
        payload = _aggregate_payload(
            dataset, metrics, min_folds=min(DEVELOPMENT_FOLDS, len(folds))
        )
        policy_development[
            _policy_key(edge_buffer_bps, min_entry_spacing_bars)
        ] = payload
        ranked.append(
            (
                bool(payload["developmentGatePassed"]),
                float(payload["netReturn"]),
                -float(payload["maxDrawdown"]),
                -float(payload["tradesPerDay"]),
                min_entry_spacing_bars,
                edge_buffer_bps,
            )
        )
    chosen_rank = max(ranked)
    chosen = (float(chosen_rank[-1]), int(chosen_rank[-2]))
    ordinary = metrics_by_policy[chosen]
    ordinary_payload = _aggregate_payload(
        dataset, ordinary, min_folds=min(DEVELOPMENT_FOLDS, len(folds))
    )
    stress = [
        evaluate_cost_aware_portfolio(
            dataset.labels[fold.test_start : fold.test_stop],
            dataset.forward_returns[fold.test_start : fold.test_stop],
            expected,
            cost_bps=STRESS_COST_BPS,
            edge_buffer_bps=chosen[0],
            holding_period_bars=DEFAULT_LABEL_HORIZON,
            min_entry_spacing_bars=chosen[1],
        )
        for fold, expected in zip(folds, expected_return_matrices, strict=True)
    ]
    stress_payload = _stress_payload(dataset, stress)
    total_diagnostic_rows = sum(
        int(item["developmentTest"]["rows"]) for item in calibration_details
    )
    raw_brier = sum(
        float(item["developmentTest"]["rawBrier"])
        * int(item["developmentTest"]["rows"])
        for item in calibration_details
    ) / total_diagnostic_rows
    calibrated_brier = sum(
        float(item["developmentTest"]["calibratedBrier"])
        * int(item["developmentTest"]["rows"])
        for item in calibration_details
    ) / total_diagnostic_rows
    promotion_blockers = [
        "fixed_current_survivor_cohort",
        "prior_sealed_folds_already_observed",
        "fresh_sealed_oos_unavailable",
        "requires_90_day_forward_public_shadow",
        "actual_account_fee_schedule_unbound",
        "static_cost_only",
        "manual_model_review_required",
    ]
    if calibrated_brier > raw_brier:
        promotion_blockers.append("probability_calibration_not_improved")
    return {
        "calibration": {
            "calibratedBrier": calibrated_brier,
            "folds": list(calibration_details),
            "improved": calibrated_brier <= raw_brier,
            "rawBrier": raw_brier,
        },
        "chosenPolicy": {
            "edgeBufferBps": chosen[0],
            "holdingBars": DEFAULT_LABEL_HORIZON,
            "minEntrySpacingBars": chosen[1],
            "requiredGrossReturnBps": STANDARD_COST_BPS + chosen[0],
        },
        "decision": "research_only",
        "development": ordinary_payload,
        "developmentGatePassed": bool(
            ordinary_payload["developmentGatePassed"]
            and stress_payload["developmentGatePassed"]
        ),
        "exploratoryGatePassed": False,
        "folds": [
            _fold_payload(dataset, fold, metrics, calibration)
            for fold, metrics, calibration in zip(
                folds, ordinary, calibration_details, strict=True
            )
        ],
        "ordinary": ordinary_payload,
        "policyDevelopment": policy_development,
        "promotionBlockers": promotion_blockers,
        "sealed": {
            "evaluated": False,
            "failures": ["fresh_sealed_folds_unavailable"],
            "status": "retired_after_prior_observation",
        },
        "stress48Bps": stress_payload,
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
    historical_folds = list(plan_walk_forward(dataset.time_rows, protocol))
    folds = historical_folds[:DEVELOPMENT_FOLDS]
    if max_folds is not None:
        if max_folds < 1:
            raise MultiAssetBenchmarkError("max-folds must be positive")
        folds = folds[:max_folds]
    if not folds:
        raise MultiAssetBenchmarkError("cohort is too short for a benchmark fold")
    _emit(
        "multi_asset_v2.dataset_ready",
        assets=dataset.asset_rows,
        cohortId=dataset.cohort_id,
        evaluatedDevelopmentFolds=len(folds),
        historicalFolds=len(historical_folds),
        timeRows=dataset.time_rows,
    )

    expected_predictions: dict[str, list[np.ndarray]] = {}
    calibration_by_family: dict[str, list[dict[str, Any]]] = {}
    specs: dict[str, Any] = {}
    training_seconds: dict[str, float] = {}
    for family in families:
        if family not in MODEL_FAMILIES:
            raise MultiAssetBenchmarkError(f"unsupported model family: {family}")
        spec, factory = _model_factory(family)
        specs[family] = spec
        family_expected: list[np.ndarray] = []
        family_calibration: list[dict[str, Any]] = []
        family_started = time.perf_counter()
        for position, fold in enumerate(folds, start=1):
            fold_started = time.perf_counter()
            fit_start, fit_stop, calibration_start, calibration_stop = (
                _fold_training_windows(fold)
            )
            fit_features, fit_labels = dataset.flat_window(fit_start, fit_stop)
            calibration_features, calibration_labels = dataset.flat_window(
                calibration_start, calibration_stop
            )
            calibration_returns = dataset.forward_returns[
                calibration_start:calibration_stop
            ].reshape(-1)
            test_features = dataset.features[
                fold.test_start : fold.test_stop
            ].reshape(-1, len(dataset.feature_names))
            test_labels = dataset.labels[fold.test_start : fold.test_stop].reshape(-1)
            test_returns = dataset.forward_returns[
                fold.test_start : fold.test_stop
            ].reshape(-1)
            model = factory()
            _fit_model(model, fit_features, fit_labels)
            raw_calibration_scores = _positive_scores(model, calibration_features)
            calibration = _fit_fold_calibration(
                raw_calibration_scores,
                calibration_labels,
                calibration_returns,
            )
            raw_test_scores = _positive_scores(model, test_features)
            expected = calibration.expected_returns(raw_test_scores).reshape(
                fold.test_stop - fold.test_start, dataset.asset_rows
            )
            family_expected.append(np.asarray(expected, dtype=np.float32))
            family_calibration.append(
                {
                    "calibrationFit": _score_diagnostics(
                        raw_calibration_scores,
                        calibration_labels,
                        calibration_returns,
                        calibration,
                    ),
                    "calibrationPurgeBars": CALIBRATION_PURGE_BARS,
                    "calibrationRows": int(
                        (calibration_stop - calibration_start) * dataset.asset_rows
                    ),
                    "calibrationStartAt": _iso_from_ms(
                        int(dataset.timestamps_ms[calibration_start])
                    ),
                    "calibrationStopAt": _iso_from_ms(
                        int(dataset.timestamps_ms[calibration_stop - 1])
                    ),
                    "developmentTest": _score_diagnostics(
                        raw_test_scores,
                        test_labels,
                        test_returns,
                        calibration,
                    ),
                    "modelFitRows": int((fit_stop - fit_start) * dataset.asset_rows),
                    "modelFitStartAt": _iso_from_ms(
                        int(dataset.timestamps_ms[fit_start])
                    ),
                    "modelFitStopAt": _iso_from_ms(
                        int(dataset.timestamps_ms[fit_stop - 1])
                    ),
                }
            )
            _emit(
                "multi_asset_v2.fold_completed",
                family=family,
                fold=fold.fold,
                position=position,
                seconds=round(time.perf_counter() - fold_started, 3),
                total=len(folds),
            )
        expected_predictions[family] = family_expected
        calibration_by_family[family] = family_calibration
        training_seconds[family] = time.perf_counter() - family_started

    results: list[dict[str, Any]] = []
    for family, expected_matrices in expected_predictions.items():
        result = _evaluate_expected_returns(
            dataset,
            folds,
            expected_matrices,
            calibration_by_family[family],
        )
        result.update(
            {
                "family": family,
                "modelSpec": {
                    **specs[family].to_dict(),
                    "calibration": {
                        "expectedReturn": "isotonic-increasing",
                        "probability": "platt-sigmoid",
                        "windowBars": CALIBRATION_BARS,
                    },
                    "sha256": specs[family].sha256,
                },
                "trainingSeconds": round(training_seconds[family], 3),
            }
        )
        results.append(result)
        _emit(
            "multi_asset_v2.model_evaluated",
            developmentGatePassed=result["developmentGatePassed"],
            family=family,
        )

    ending_cohort = load_validated_cohort(cohort_manifest)
    if ending_cohort.manifest.get("contentSha256") != dataset.cohort_sha256:
        raise MultiAssetBenchmarkError("cohort changed during benchmark")
    feature_contract = {
        "dtype": "float32",
        "featureNames": list(dataset.feature_names),
        "labelHorizonBars": DEFAULT_LABEL_HORIZON,
        "portfolio": "cash-spot-long-flat-calibrated-net-edge-low-turnover",
        "schemaVersion": MULTI_ASSET_RESEARCH_SCHEMA_VERSION,
    }
    report: dict[str, Any] = {
        "benchmarkId": "mabench_v2_"
        + sha256_hex(
            canonical_json(
                {
                    "at": _iso(started),
                    "cohort": dataset.cohort_sha256,
                    "families": list(families),
                    "protocol": MULTI_ASSET_RESEARCH_SCHEMA_VERSION,
                }
            )
        )[:24],
        "completedAt": _iso(_utc_now()),
        "costProtocol": {
            "actualAccountFeeScheduleBound": False,
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
            "calibrationBars": CALIBRATION_BARS,
            "calibrationPurgeBars": CALIBRATION_PURGE_BARS,
            "evaluatedDevelopmentFolds": len(folds),
            "historicalFoldCount": len(historical_folds),
            "holdingBars": DEFAULT_LABEL_HORIZON,
            "policyCandidatesPredeclared": [
                {
                    "edgeBufferBps": edge_buffer_bps,
                    "minEntrySpacingBars": min_entry_spacing_bars,
                }
                for edge_buffer_bps, min_entry_spacing_bars in POLICY_CANDIDATES
            ],
            "priorSealedFoldsRetired": min(
                PRIOR_SEALED_FOLDS,
                max(0, len(historical_folds) - DEVELOPMENT_FOLDS),
            ),
            "scope": "retrospective-development-only",
            "testBars": TEST_BARS,
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
        description="Run the V2 research-only multi-asset development benchmark."
    )
    parser.add_argument("--cohort", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--families", default="hist_gradient_boosting")
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
        "multi_asset_v2.benchmark_completed",
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
        OSError,
        ValueError,
    ) as exc:
        _emit(
            "multi_asset_v2.benchmark_failed",
            errorType=type(exc).__name__,
            message=str(exc),
        )
        raise SystemExit(1) from exc
