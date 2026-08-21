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
    from .multi_asset_benchmark import (
        CALIBRATION_BARS,
        CALIBRATION_PURGE_BARS,
        POLICY_CANDIDATES,
        _fit_fold_calibration,
        _fold_training_windows,
        _score_diagnostics,
    )
except ImportError:  # pragma: no cover - direct script execution
    from model_benchmark import (
        MODEL_FAMILIES,
        _fit_model,
        _installed_versions,
        _model_factory,
        _positive_scores,
    )
    from multi_asset_benchmark import (
        CALIBRATION_BARS,
        CALIBRATION_PURGE_BARS,
        POLICY_CANDIDATES,
        _fit_fold_calibration,
        _fold_training_windows,
        _score_diagnostics,
    )
from okx_demo_lab.ml.historical_replay import (
    HISTORICAL_REPLAY_SCHEMA_VERSION,
    HistoricalReplayError,
    ReplayBrokerConfig,
    ReplayEpisodeBinding,
    ReplayPolicy,
    run_historical_replay,
)
from okx_demo_lab.ml.multi_asset_cohort import (
    MultiAssetCohortError,
    load_validated_cohort,
)
from okx_demo_lab.ml.multi_asset_research import prepare_multi_asset_dataset
from okx_demo_lab.ml.pipeline import DEFAULT_LABEL_HORIZON
from okx_demo_lab.ml.strategy import canonical_json, sha256_hex
from okx_demo_lab.ml.walk_forward import (
    TrainingConfig,
    ValidationError,
    WalkForwardSpec,
    plan_walk_forward,
)


REPLAY_REPORT_SCHEMA_VERSION = "moheng.historical-replay-report.v1"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
RESEARCH_DATA_ROOT = PROJECT_ROOT / ".research-data"
TRAIN_BARS = 365 * 24 * 12
RETRAIN_BARS = 30 * 24 * 12
STANDARD_BROKER = ReplayBrokerConfig(
    fee_bps_per_side=8.0,
    slippage_bps_per_side=4.0,
    checkpoint_stride_bars=288,
)
STRESS_BROKER = ReplayBrokerConfig(
    fee_bps_per_side=8.0,
    slippage_bps_per_side=16.0,
    checkpoint_stride_bars=288,
)
PROMOTION_BLOCKERS = (
    "historical_replay_development_only",
    "fixed_current_survivor_cohort",
    "no_fresh_sealed_oos",
    "historical_order_book_unavailable",
    "static_ohlcv_fill_model",
    "requires_90_day_forward_public_shadow",
    "manual_model_review_required",
)


class ReplayResearchError(RuntimeError):
    pass


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _iso_from_ms(value: int) -> str:
    return (
        datetime.fromtimestamp(value / 1_000, tz=timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _emit(event: str, **fields: Any) -> None:
    print(canonical_json({"event": event, "at": _iso(_utc_now()), **fields}), flush=True)


def _inside_research_subdir(path: Path, subdir: str) -> Path:
    resolved = path.expanduser().resolve()
    try:
        resolved.relative_to((RESEARCH_DATA_ROOT / subdir).resolve())
    except ValueError as exc:
        raise ReplayResearchError(
            f"historical replay path must stay under project .research-data/{subdir}"
        ) from exc
    return resolved


def replay_walk_forward_spec(
    *,
    train_bars: int = TRAIN_BARS,
    replay_bars: int = RETRAIN_BARS,
) -> WalkForwardSpec:
    return WalkForwardSpec(
        train_size=train_bars,
        test_size=replay_bars,
        step_size=replay_bars,
        label_horizon=DEFAULT_LABEL_HORIZON,
        embargo_size=1,
        expanding=False,
    )


def _policy_key(edge_buffer_bps: float, min_entry_spacing_bars: int) -> str:
    return f"buffer-{edge_buffer_bps:g}bps_spacing-{min_entry_spacing_bars}bars"


def _development_failures(result: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    if len(result["trades"]) < 20:
        failures.append("trades_insufficient")
    if float(result["profitableTradeRate"]) < 0.50:
        failures.append("profitable_trade_rate_below_gate")
    if float(result["grossPnlReturn"]) < 0.0:
        failures.append("gross_return_below_gate")
    if float(result["netReturn"]) < 0.005:
        failures.append("net_return_below_gate")
    if float(result["maxDrawdown"]) > 0.10:
        failures.append("drawdown_above_gate")
    if float(result["tradesPerDay"]) > 3.0:
        failures.append("turnover_above_gate")
    submitted = int(result["ordersSubmitted"])
    rejected = int(result["ordersRejected"])
    if submitted and rejected / submitted > 0.05:
        failures.append("fill_rejection_rate_above_gate")
    return failures


def _stress_failures(result: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    if float(result["grossPnlReturn"]) < 0.0:
        failures.append("stress_gross_return_below_zero")
    if float(result["netReturn"]) < 0.0:
        failures.append("stress_net_return_below_zero")
    if float(result["maxDrawdown"]) > 0.15:
        failures.append("stress_drawdown_above_gate")
    return failures


def _summary(result: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in result.items()
        if key not in {"checkpoints", "trades"}
    } | {"trades": len(result["trades"])}


def _evaluate_policies(
    instruments: Sequence[str],
    timestamps_ms: np.ndarray,
    candles: np.ndarray,
    expected_returns: np.ndarray,
    episode_indices: np.ndarray,
    bindings: Sequence[ReplayEpisodeBinding],
) -> dict[str, Any]:
    policy_development: dict[str, Any] = {}
    ordinary_by_policy: dict[tuple[float, int], dict[str, Any]] = {}
    ranked: list[tuple[bool, float, float, float, int, float]] = []
    for edge_buffer_bps, spacing_bars in POLICY_CANDIDATES:
        policy = ReplayPolicy(edge_buffer_bps, spacing_bars)
        ordinary = run_historical_replay(
            instruments,
            timestamps_ms,
            candles,
            expected_returns,
            episode_indices,
            bindings,
            policy=policy,
            broker=STANDARD_BROKER,
        )
        failures = _development_failures(ordinary)
        ordinary_by_policy[(edge_buffer_bps, spacing_bars)] = ordinary
        policy_development[_policy_key(edge_buffer_bps, spacing_bars)] = {
            **_summary(ordinary),
            "developmentGatePassed": not failures,
            "failures": failures,
        }
        ranked.append(
            (
                not failures,
                float(ordinary["netReturn"]),
                -float(ordinary["maxDrawdown"]),
                -float(ordinary["turnoverMultiple"]),
                spacing_bars,
                edge_buffer_bps,
            )
        )
    chosen_rank = max(ranked)
    chosen_key = (float(chosen_rank[-1]), int(chosen_rank[-2]))
    chosen_policy = ReplayPolicy(*chosen_key)
    ordinary = ordinary_by_policy[chosen_key]
    ordinary_failures = _development_failures(ordinary)
    stress = run_historical_replay(
        instruments,
        timestamps_ms,
        candles,
        expected_returns,
        episode_indices,
        bindings,
        policy=chosen_policy,
        broker=STRESS_BROKER,
    )
    stress_failures = _stress_failures(stress)
    return {
        "chosenPolicy": chosen_policy.to_dict(STANDARD_BROKER),
        "decision": "research_only",
        "developmentGatePassed": not ordinary_failures and not stress_failures,
        "ordinary": {
            **ordinary,
            "developmentGatePassed": not ordinary_failures,
            "failures": ordinary_failures,
        },
        "policyDevelopment": policy_development,
        "promotionBlockers": list(PROMOTION_BLOCKERS),
        "shadowDaysCredited": 0,
        "stress48Bps": {
            **_summary(stress),
            "developmentGatePassed": not stress_failures,
            "failures": stress_failures,
        },
    }


def run_replay_research(
    *,
    cohort_manifest: Path,
    family: str,
    max_episodes: int | None,
) -> dict[str, Any]:
    started = _utc_now()
    wall_started = time.perf_counter()
    if family not in MODEL_FAMILIES:
        raise ReplayResearchError(f"unsupported model family: {family}")
    versions = _installed_versions()
    cohort = load_validated_cohort(cohort_manifest)
    dataset = prepare_multi_asset_dataset(
        cohort,
        now=started,
        training_config=TrainingConfig(
            round_trip_cost_bps=STANDARD_BROKER.round_trip_cost_bps
        ),
    )
    raw_delta_ms = int(dataset.timestamps_ms[0]) - int(cohort.timestamps[0])
    raw_offset = int(raw_delta_ms // 300_000)
    if (
        raw_delta_ms % 300_000 != 0
        or raw_offset < 0
        or raw_offset + dataset.time_rows > int(cohort.timestamps.size)
        or not np.array_equal(
            dataset.timestamps_ms,
            cohort.timestamps[raw_offset : raw_offset + dataset.time_rows],
        )
    ):
        raise ReplayResearchError("prepared features are not aligned to cohort candles")
    protocol = replay_walk_forward_spec()
    folds = list(plan_walk_forward(dataset.time_rows, protocol))
    if max_episodes is not None:
        if max_episodes < 1:
            raise ReplayResearchError("max-episodes must be positive")
        folds = folds[:max_episodes]
    if not folds:
        raise ReplayResearchError("cohort is too short for one replay episode")
    if any(left.test_stop != right.test_start for left, right in zip(folds, folds[1:])):
        raise ReplayResearchError("replay episode test windows are not contiguous")

    spec, factory = _model_factory(family)
    expected_chunks: list[np.ndarray] = []
    episode_payloads: list[dict[str, Any]] = []
    bindings: list[ReplayEpisodeBinding] = []
    episode_index_chunks: list[np.ndarray] = []
    training_seconds = 0.0
    diagnostic_rows = 0
    weighted_raw_brier = 0.0
    weighted_calibrated_brier = 0.0
    _emit(
        "historical_replay.dataset_ready",
        assets=dataset.asset_rows,
        cohortId=dataset.cohort_id,
        episodes=len(folds),
        timeRows=dataset.time_rows,
    )
    for position, fold in enumerate(folds, start=1):
        episode_started = time.perf_counter()
        fit_start, fit_stop, calibration_start, calibration_stop = (
            _fold_training_windows(fold)
        )
        label_complete_index = calibration_stop - 1 + DEFAULT_LABEL_HORIZON
        available_index = label_complete_index + protocol.embargo_size
        if available_index >= fold.test_start:
            raise ReplayResearchError("episode labels are not available before replay")
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
            fold.test_stop - fold.test_start,
            dataset.asset_rows,
        )
        expected_chunks.append(np.asarray(expected, dtype=np.float32))
        episode_index_chunks.append(
            np.full(
                fold.test_stop - fold.test_start,
                position - 1,
                dtype=np.int32,
            )
        )
        available_at_ms = int(dataset.timestamps_ms[available_index])
        episode_id = "replay_episode_" + sha256_hex(
            canonical_json(
                {
                    "cohort": dataset.cohort_sha256,
                    "family": family,
                    "fitStart": fit_start,
                    "fitStop": fit_stop,
                    "calibrationStart": calibration_start,
                    "calibrationStop": calibration_stop,
                    "testStart": int(fold.test_start),
                    "testStop": int(fold.test_stop),
                }
            )
        )[:24]
        bindings.append(ReplayEpisodeBinding(episode_id, available_at_ms))
        diagnostics = _score_diagnostics(
            raw_test_scores,
            test_labels,
            test_returns,
            calibration,
        )
        rows = int(diagnostics["rows"])
        diagnostic_rows += rows
        weighted_raw_brier += float(diagnostics["rawBrier"]) * rows
        weighted_calibrated_brier += float(diagnostics["calibratedBrier"]) * rows
        elapsed = time.perf_counter() - episode_started
        training_seconds += elapsed
        episode_payloads.append(
            {
                "availableAt": _iso_from_ms(available_at_ms),
                "calibrationStartAt": _iso_from_ms(
                    int(dataset.timestamps_ms[calibration_start])
                ),
                "calibrationStopAt": _iso_from_ms(
                    int(dataset.timestamps_ms[calibration_stop - 1])
                ),
                "diagnostics": diagnostics,
                "episode": position - 1,
                "episodeId": episode_id,
                "assetRows": dataset.asset_rows,
                "fitRows": (fit_stop - fit_start) * dataset.asset_rows,
                "calibrationRows": (
                    calibration_stop - calibration_start
                )
                * dataset.asset_rows,
                "replayRows": (
                    fold.test_stop - fold.test_start
                )
                * dataset.asset_rows,
                "fitStartAt": _iso_from_ms(int(dataset.timestamps_ms[fit_start])),
                "fitStopAt": _iso_from_ms(int(dataset.timestamps_ms[fit_stop - 1])),
                "labelCompleteAt": _iso_from_ms(
                    int(dataset.timestamps_ms[label_complete_index])
                ),
                "replayStartAt": _iso_from_ms(
                    int(dataset.timestamps_ms[fold.test_start])
                ),
                "replayStopAt": _iso_from_ms(
                    int(dataset.timestamps_ms[fold.test_stop - 1])
                ),
                "trainingSeconds": elapsed,
            }
        )
        _emit(
            "historical_replay.episode_completed",
            episode=position,
            seconds=round(elapsed, 3),
            total=len(folds),
        )

    replay_start = folds[0].test_start
    replay_stop = folds[-1].test_stop
    replay_timestamps = dataset.timestamps_ms[replay_start:replay_stop]
    replay_candles = cohort.candles[
        raw_offset + replay_start : raw_offset + replay_stop
    ]
    expected_matrix = np.concatenate(expected_chunks, axis=0)
    episode_indices = np.concatenate(episode_index_chunks, axis=0)
    if (
        expected_matrix.shape != (replay_timestamps.size, dataset.asset_rows)
        or episode_indices.shape != replay_timestamps.shape
    ):
        raise ReplayResearchError("replay prediction matrix is incomplete")
    replay_started = time.perf_counter()
    result = _evaluate_policies(
        dataset.instruments,
        replay_timestamps,
        replay_candles,
        expected_matrix,
        episode_indices,
        bindings,
    )
    replay_seconds = time.perf_counter() - replay_started
    total_seconds = time.perf_counter() - wall_started
    ending_cohort = load_validated_cohort(cohort_manifest)
    if ending_cohort.manifest.get("contentSha256") != dataset.cohort_sha256:
        raise ReplayResearchError("cohort changed during historical replay")
    simulated_seconds = replay_timestamps.size * 300.0
    report: dict[str, Any] = {
        "completedAt": _iso(_utc_now()),
        "dataset": {
            "assetRows": dataset.asset_rows,
            "cohortId": dataset.cohort_id,
            "cohortSha256": dataset.cohort_sha256,
            "firstReplayAt": _iso_from_ms(int(replay_timestamps[0])),
            "instruments": list(dataset.instruments),
            "lastReplayAt": _iso_from_ms(int(replay_timestamps[-1])),
            "replayTimeRows": int(replay_timestamps.size),
            "source": "okx-public-v5-confirmed-5m-frozen-cohort",
        },
        "decision": "research_only",
        "episodes": episode_payloads,
        "execution": {
            "decisionToFillLatencyBars": STANDARD_BROKER.latency_bars,
            "engineSchemaVersion": HISTORICAL_REPLAY_SCHEMA_VERSION,
            "executionAllowlistChanged": False,
            "historicalReplayOnly": True,
            "orderCapability": False,
            "privateApi": False,
            "publicDataOnly": True,
        },
        "leakageAudit": {
            "calibrationPurgeBars": CALIBRATION_PURGE_BARS,
            "episodeAvailabilityBound": True,
            "futureLabelsBeforeDecision": 0,
            "nextBarExecution": True,
            "sameBarFillAllowed": False,
            "strictFiveMinuteGrid": True,
        },
        "model": {
            "calibratedBrier": weighted_calibrated_brier / diagnostic_rows,
            "calibrationImproved": (
                weighted_calibrated_brier <= weighted_raw_brier
            ),
            "family": family,
            "rawBrier": weighted_raw_brier / diagnostic_rows,
            "spec": spec.to_dict(),
            "specSha256": spec.sha256,
        },
        "packages": versions,
        "promotable": False,
        "promotionBlockers": list(PROMOTION_BLOCKERS),
        "protocol": {
            "calibrationBars": CALIBRATION_BARS,
            "episodeCount": len(folds),
            "holdingBars": DEFAULT_LABEL_HORIZON,
            "retrainEveryBars": RETRAIN_BARS,
            "retrainEveryDays": RETRAIN_BARS / 288.0,
            "scope": "retrospective-development-only",
            "policySelectionScope": "same-replay-development-only",
            "trainBars": TRAIN_BARS,
            "walkForward": protocol.to_dict(),
        },
        "replayId": "hreplay_"
        + sha256_hex(
            canonical_json(
                {
                    "at": _iso(started),
                    "cohort": dataset.cohort_sha256,
                    "family": family,
                    "protocol": REPLAY_REPORT_SCHEMA_VERSION,
                }
            )
        )[:24],
        "result": result,
        "schemaVersion": REPLAY_REPORT_SCHEMA_VERSION,
        "shadowDaysCredited": 0,
        "startedAt": _iso(started),
        "timing": {
            "compressionMultiple": simulated_seconds / total_seconds,
            "replaySeconds": replay_seconds,
            "simulatedMarketSeconds": simulated_seconds,
            "totalWallSeconds": total_seconds,
            "trainingSeconds": training_seconds,
        },
    }
    report["reportSha256"] = sha256_hex(canonical_json(report))
    return report


def _write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise ReplayResearchError("historical replay evidence already exists")
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the research-only V3 historical market replay."
    )
    parser.add_argument("--cohort", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--family", default="hist_gradient_boosting")
    parser.add_argument("--max-episodes", type=int)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    cohort = _inside_research_subdir(args.cohort, "cohorts")
    output = _inside_research_subdir(args.output, "replays")
    if cohort.name != "manifest.json" or output.suffix.lower() != ".json":
        raise ReplayResearchError("historical replay cohort or output filename is invalid")
    report = run_replay_research(
        cohort_manifest=cohort,
        family=args.family,
        max_episodes=args.max_episodes,
    )
    _write_report(output, report)
    _emit(
        "historical_replay.completed",
        output=str(output),
        reportSha256=report["reportSha256"],
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (
        HistoricalReplayError,
        MultiAssetCohortError,
        ReplayResearchError,
        ValidationError,
        OSError,
        ValueError,
    ) as exc:
        _emit(
            "historical_replay.failed",
            errorType=type(exc).__name__,
            message=str(exc),
        )
        raise SystemExit(1) from exc
