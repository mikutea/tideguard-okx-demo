from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
import numpy as np

from okx_demo_lab.ml.execution import (
    AUTO_SESSION_CONFIRMATION,
    AutomationDenied,
    AutomationLedger,
    DemoAutoExecutor,
    ManualReviewRequired,
    authorize_demo_session,
)
from okx_demo_lab.ml.registry import (
    PROMOTION_CONFIRMATION,
    ChampionSnapshot,
    ModelRegistry,
    PromotionDenied,
    PromotionPolicy,
)
from okx_demo_lab.ml.pipeline import (
    BAR_MILLISECONDS,
    build_observations,
    parse_completed_candles,
    prepare_training_arrays,
    prepare_training_dataset,
    train_and_register_candidate,
)
from okx_demo_lab.ml.strategy import (
    DemoStrategyPolicy,
    FrozenLinearModel,
    FrozenModelBundle,
    MarketSnapshot,
    ModelArtifactError,
    ModelManifest,
    ProposalRejected,
    build_order_proposal,
    canonical_json,
    feature_schema_hash,
)
from okx_demo_lab.ml.walk_forward import (
    LEGACY_BRACKET_EVALUATION_MODE,
    LEGACY_BRACKET_VALIDATION_SCHEMA_VERSION,
    LEGACY_LONG_ONLY_EVALUATION_MODE,
    LEGACY_LONG_ONLY_VALIDATION_SCHEMA_VERSION,
    LEGACY_VALIDATION_SCHEMA_VERSION,
    Observation,
    TrainingConfig,
    ValidationError,
    ValidationReport,
    WalkForwardSpec,
    _evaluate_fold,
    dataset_sha256,
    fit_linear_model,
    plan_walk_forward,
    run_walk_forward,
)


NOW = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)


def observations(count: int = 72) -> tuple[Observation, ...]:
    rows = []
    for index in range(count):
        positive = index % 2 == 0
        rows.append(
            Observation(
                observed_at=NOW - timedelta(minutes=5 * (count - index)),
                features=(2.0 if positive else -2.0,),
                label=1 if positive else 0,
                forward_return=0.01 if positive else -0.01,
            )
        )
    return tuple(rows)


class SyntheticCandles:
    def __init__(self, count: int):
        self.count = count
        self.first = (
            round(NOW.timestamp() * 1_000) - (count + 2) * BAR_MILLISECONDS
        )

    def __len__(self) -> int:
        return self.count

    def __iter__(self):
        for index in range(self.count):
            timestamp = self.first + index * BAR_MILLISECONDS
            close = 20_000.0 + index * 0.1 + (index % 17) * 0.05
            yield [
                str(timestamp),
                str(close - 0.1),
                str(close + 1.0),
                str(close - 1.0),
                str(close),
                str(10 + index % 11),
                "0",
                "0",
                "1",
            ]


def validation_and_bundle() -> tuple[object, FrozenModelBundle, tuple[Observation, ...], TrainingConfig]:
    rows = observations()
    config = TrainingConfig(epochs=120, round_trip_cost_bps=10)
    report = run_walk_forward(
        rows,
        ("signal",),
        WalkForwardSpec(
            train_size=20,
            test_size=10,
            step_size=10,
            label_horizon=2,
            expanding=False,
            benchmark_cohort_id="cohort_" + "1" * 24,
            market_snapshot_sha256="2" * 64,
        ),
        config,
        created_at=NOW,
    )
    model = fit_linear_model(rows, ("signal",), config)
    manifest = ModelManifest(
        dataset_sha256=report.dataset_sha256,
        fit_dataset_sha256=dataset_sha256(rows, ("signal",)),
        fit_rows=len(rows),
        training_config_sha256=report.training_config_sha256,
        validation_run_id=report.validation_run_id,
        code_revision="f" * 40,
        trained_from=rows[0].observed_at,
        trained_through=rows[-1].observed_at,
        created_at=NOW,
        trainer="offline-test",
        random_seed=7,
        feature_schema_sha256=report.feature_schema_sha256,
        benchmark_cohort_id=report.benchmark_cohort_id,
        market_snapshot_sha256=report.market_snapshot_sha256,
        split_protocol_sha256=report.split_protocol_sha256,
    )
    return report, FrozenModelBundle(manifest=manifest, model=model), rows, config


def promoted_champion(tmp_path) -> tuple[ChampionSnapshot, DemoStrategyPolicy]:
    report, bundle, _, _ = validation_and_bundle()
    registry = ModelRegistry(tmp_path / "registry.sqlite3")
    model_id = registry.register_candidate(bundle.to_bytes())
    registry.record_validation(model_id, report, recorded_at=NOW)
    champion = registry.promote(
        model_id,
        policy=PromotionPolicy(
            min_folds=3,
            min_oos_rows=40,
            min_trades=10,
            min_aggregate_accuracy=0.8,
            min_aggregate_net_return=0.01,
            min_worst_fold_net_return=0.01,
            max_drawdown=0.05,
        ),
        reviewer="human-reviewer",
        rationale="Outer folds were inspected and costs were included.",
        confirmation=PROMOTION_CONFIRMATION,
        expected_generation=0,
        approved_at=NOW,
    )
    return champion, DemoStrategyPolicy(fixed_notional_usdt=Decimal("10"))


def proposal_for(champion: ChampionSnapshot, policy: DemoStrategyPolicy):
    market = MarketSnapshot(
        observed_at=NOW,
        candle_closed_at=NOW - timedelta(minutes=5),
        candle_confirmed=True,
        instrument="BTC-USDT",
        bid=Decimal("100"),
        ask=Decimal("101"),
        tick_size=Decimal("1"),
        lot_size=Decimal("0.001"),
        min_size=Decimal("0.001"),
    )
    result = build_order_proposal(
        champion.bundle,
        features={"signal": 2.0},
        market=market,
        policy=policy,
        now=NOW,
    )
    assert result is not None
    return result


def test_walk_forward_has_purge_gap_and_non_overlapping_outer_windows():
    spec = WalkForwardSpec(
        train_size=10,
        test_size=5,
        step_size=5,
        label_horizon=2,
        embargo_size=1,
    )
    folds = plan_walk_forward(31, spec)
    seen_test_rows: set[int] = set()
    for fold in folds:
        assert fold.train_stop + spec.label_horizon + spec.embargo_size == fold.test_start
        assert not (set(fold.test_indices) & seen_test_rows)
        seen_test_rows.update(fold.test_indices)
    with pytest.raises(ValidationError, match="non-overlapping"):
        WalkForwardSpec(train_size=10, test_size=5, step_size=4)


def test_walk_forward_is_deterministic_and_charges_costs():
    rows = observations()
    spec = WalkForwardSpec(train_size=20, test_size=10, step_size=10)
    config = TrainingConfig(epochs=80, round_trip_cost_bps=15)
    first = run_walk_forward(rows, ("signal",), spec, config, created_at=NOW)
    second = run_walk_forward(rows, ("signal",), spec, config, created_at=NOW)
    assert first.report_sha256 == second.report_sha256
    assert first.trades > 0
    assert first.aggregate_accuracy > 0.9
    assert all(fold.net_return < fold.gross_return for fold in first.folds)
    assert first.round_trip_cost_bps == 15


def test_numpy_prepared_dataset_matches_reference_feature_and_label_semantics():
    raw = list(SyntheticCandles(120))
    config = TrainingConfig(
        epochs=5,
        round_trip_cost_bps=24,
        stop_loss_fraction=0.015,
        take_profit_fraction=0.025,
    )
    reference = build_observations(
        parse_completed_candles(raw, now=NOW),
        label_horizon=12,
        round_trip_cost_bps=24,
        stop_loss_fraction=0.015,
        take_profit_fraction=0.025,
    )
    prepared = prepare_training_dataset(raw, now=NOW, training_config=config)
    array_prepared = prepare_training_arrays(
        np.asarray([int(row[0]) for row in raw], dtype=np.int64),
        np.asarray([[float(value) for value in row[1:6]] for row in raw]),
        now=NOW,
        training_config=config,
    )
    matrix = prepared.observations
    np.testing.assert_allclose(
        matrix.features,
        np.asarray([row.features for row in reference]),
        rtol=1e-12,
        atol=1e-12,
    )
    np.testing.assert_allclose(
        matrix.forward_returns,
        np.asarray([row.forward_return for row in reference]),
        rtol=1e-12,
        atol=1e-12,
    )
    assert matrix.labels.tolist() == [row.label for row in reference]
    np.testing.assert_array_equal(
        array_prepared.observations.observed_at_ms, matrix.observed_at_ms
    )
    np.testing.assert_allclose(array_prepared.observations.features, matrix.features)
    np.testing.assert_array_equal(array_prepared.observations.labels, matrix.labels)
    np.testing.assert_allclose(
        array_prepared.observations.forward_returns, matrix.forward_returns
    )


def test_100k_history_matrix_has_bounded_build_time_and_memory():
    config = TrainingConfig(epochs=5, round_trip_cost_bps=24)
    started = time.perf_counter()
    prepared = prepare_training_dataset(
        SyntheticCandles(100_000),
        now=NOW,
        training_config=config,
    )
    build_seconds = time.perf_counter() - started
    matrix = prepared.observations
    matrix_bytes = (
        matrix.observed_at_ms.nbytes
        + matrix.features.nbytes
        + matrix.labels.nbytes
        + matrix.forward_returns.nbytes
    )
    assert len(matrix) == 100_000 - 48 - 12
    assert matrix_bytes < 20 * 1024 * 1024
    assert build_seconds < 15

    fit_started = time.perf_counter()
    first = fit_linear_model(matrix, tuple(f"f{index}" for index in range(16)), config)
    second = fit_linear_model(matrix, tuple(f"f{index}" for index in range(16)), config)
    assert first == second
    assert time.perf_counter() - fit_started < 15


def test_v4_candidate_binds_snapshot_cohort_and_recent_final_fit(tmp_path):
    raw = list(SyntheticCandles(140))
    config = TrainingConfig(epochs=5, round_trip_cost_bps=24)
    prepared = prepare_training_dataset(raw, now=NOW, training_config=config)
    registry = ModelRegistry(tmp_path / "v4-registry.sqlite3")
    result = train_and_register_candidate(
        raw,
        registry,
        now=NOW,
        code_revision="v4-test",
        training_config=config,
        walk_forward_spec=WalkForwardSpec(
            train_size=20,
            test_size=20,
            step_size=20,
            label_horizon=12,
            expanding=False,
            benchmark_cohort_id="cohort_" + "3" * 24,
            market_snapshot_sha256="4" * 64,
        ),
        prepared_dataset=prepared,
        final_fit_rows=30,
    )
    bundle = registry.load_model(result.model_id)
    assert bundle.schema_version == "tideguard.linear-logit.v2"
    assert bundle.manifest.fit_rows == 30
    assert bundle.manifest.benchmark_cohort_id == "cohort_" + "3" * 24
    assert bundle.manifest.market_snapshot_sha256 == "4" * 64
    assert bundle.manifest.split_protocol_sha256 == result.report.split_protocol_sha256
    assert bundle.manifest.trained_from == prepared.observations.observed_at(
        len(prepared.observations) - 30
    )


class _FixedActionModel:
    def __init__(self, action: str):
        self.action_name = action
        self.seen: list[float] = []

    def action(self, features):
        self.seen.append(float(features["signal"]))
        score = 0.9 if self.action_name == "buy" else 0.1
        return self.action_name, score


def _indexed_oos_rows(count: int, forward_return: float) -> tuple[Observation, ...]:
    return tuple(
        Observation(
            observed_at=NOW + timedelta(minutes=5 * index),
            features=(float(index),),
            label=1 if forward_return > 0 else 0,
            forward_return=forward_return,
        )
        for index in range(count)
    )


def test_long_only_oos_never_credits_sell_as_short_profit():
    model = _FixedActionModel("sell")
    trades, accuracy, gross, net, drawdown, evaluated = _evaluate_fold(
        model,
        _indexed_oos_rows(20, -0.01),
        ("signal",),
        12.0,
        3,
    )
    assert trades == 0
    assert gross == 0
    assert net == 0
    assert drawdown == 0
    assert accuracy == 1
    assert evaluated == 17


def test_long_only_oos_continuous_buys_use_non_overlapping_capital():
    model = _FixedActionModel("buy")
    trades, _accuracy, gross, net, _drawdown, evaluated = _evaluate_fold(
        model,
        _indexed_oos_rows(10, 0.01),
        ("signal",),
        10.0,
        3,
    )
    assert trades == 3
    assert evaluated == 3
    assert model.seen == [0.0, 3.0, 6.0]
    assert gross == pytest.approx((1.01**3) - 1)
    assert net == pytest.approx((1.009**3) - 1)


def test_long_only_oos_ignores_every_signal_inside_the_holding_window():
    model = _FixedActionModel("buy")
    _evaluate_fold(
        model,
        _indexed_oos_rows(14, 0.005),
        ("signal",),
        12.0,
        4,
    )
    assert model.seen == [0.0, 4.0, 8.0]


def test_legacy_validation_remains_readable_but_cannot_be_promoted():
    report, _bundle, _rows, _config = validation_and_bundle()
    legacy_payload = report.to_dict()
    for key in (
        "benchmark_cohort_id",
        "market_snapshot_sha256",
        "split_protocol_sha256",
    ):
        legacy_payload["walk_forward_spec"].pop(key)
    legacy_payload["schema_version"] = LEGACY_VALIDATION_SCHEMA_VERSION
    legacy_payload.pop("evaluation_mode")
    legacy_report = ValidationReport.from_dict(legacy_payload)

    assert legacy_report.to_dict() == legacy_payload
    assert "unsupported_evaluation_semantics" in PromotionPolicy().failures(legacy_report)

    v2_payload = report.to_dict()
    for key in (
        "benchmark_cohort_id",
        "market_snapshot_sha256",
        "split_protocol_sha256",
    ):
        v2_payload["walk_forward_spec"].pop(key)
    v2_payload["schema_version"] = LEGACY_LONG_ONLY_VALIDATION_SCHEMA_VERSION
    v2_payload["evaluation_mode"] = LEGACY_LONG_ONLY_EVALUATION_MODE
    v2_report = ValidationReport.from_dict(v2_payload)
    assert v2_report.to_dict() == v2_payload
    assert "unsupported_evaluation_semantics" in PromotionPolicy().failures(v2_report)

    v3_payload = report.to_dict()
    for key in (
        "benchmark_cohort_id",
        "market_snapshot_sha256",
        "split_protocol_sha256",
    ):
        v3_payload["walk_forward_spec"].pop(key)
    v3_payload["schema_version"] = LEGACY_BRACKET_VALIDATION_SCHEMA_VERSION
    v3_payload["evaluation_mode"] = LEGACY_BRACKET_EVALUATION_MODE
    v3_report = ValidationReport.from_dict(v3_payload)
    assert v3_report.to_dict() == v3_payload
    assert "unsupported_evaluation_semantics" in PromotionPolicy().failures(v3_report)


def test_frozen_bundle_is_canonical_strict_and_tamper_evident():
    _, bundle, _, _ = validation_and_bundle()
    raw = bundle.to_bytes()
    loaded = FrozenModelBundle.from_bytes(raw, expected_sha256=bundle.artifact_sha256)
    assert loaded == bundle
    with pytest.raises(ModelArtifactError, match="hash mismatch"):
        FrozenModelBundle.from_bytes(raw + b" ", expected_sha256=bundle.artifact_sha256)
    decoded = json.loads(raw)
    decoded["unexpected"] = True
    with pytest.raises(ModelArtifactError, match="unexpected"):
        FrozenModelBundle.from_bytes(canonical_json(decoded).encode())
    unsafe = bundle.model.to_dict()
    unsafe["intercept"] = float("nan")
    with pytest.raises(ModelArtifactError, match="non-finite"):
        FrozenLinearModel.from_dict(unsafe)


def test_legacy_v1_model_artifact_remains_readable():
    _report, bundle, _rows, _config = validation_and_bundle()
    legacy_manifest = replace(
        bundle.manifest,
        fit_dataset_sha256=None,
        fit_rows=None,
        benchmark_cohort_id=None,
        market_snapshot_sha256=None,
        split_protocol_sha256=None,
    )
    legacy = FrozenModelBundle(manifest=legacy_manifest, model=bundle.model)
    assert legacy.schema_version == "tideguard.linear-logit.v1"
    assert FrozenModelBundle.from_bytes(legacy.to_bytes()) == legacy


def test_legacy_v1_artifact_with_v3_report_cannot_receive_new_promotion(tmp_path):
    report, bundle, _rows, _config = validation_and_bundle()
    payload = report.to_dict()
    for key in (
        "benchmark_cohort_id",
        "market_snapshot_sha256",
        "split_protocol_sha256",
    ):
        payload["walk_forward_spec"].pop(key)
    payload["schema_version"] = LEGACY_BRACKET_VALIDATION_SCHEMA_VERSION
    payload["evaluation_mode"] = LEGACY_BRACKET_EVALUATION_MODE
    legacy_report = ValidationReport.from_dict(payload)
    legacy_manifest = replace(
        bundle.manifest,
        validation_run_id=legacy_report.validation_run_id,
        fit_dataset_sha256=None,
        fit_rows=None,
        benchmark_cohort_id=None,
        market_snapshot_sha256=None,
        split_protocol_sha256=None,
    )
    legacy = FrozenModelBundle(manifest=legacy_manifest, model=bundle.model)
    registry = ModelRegistry(tmp_path / "legacy-registry.sqlite3")
    model_id = registry.register_candidate(legacy.to_bytes())
    registry.record_validation(model_id, legacy_report, recorded_at=NOW)
    with pytest.raises(PromotionDenied, match="legacy model artifacts"):
        registry.promote(
            model_id,
            policy=PromotionPolicy(
                min_folds=1,
                min_oos_rows=1,
                min_trades=0,
                min_round_trip_cost_bps=1,
                min_aggregate_accuracy=0,
                min_aggregate_net_return=-1,
                min_worst_fold_net_return=-0.99,
                max_drawdown=1,
            ),
            reviewer="test",
            rationale="Legacy artifacts stay readable but cannot receive a v4 promotion.",
            confirmation=PROMOTION_CONFIRMATION,
            expected_generation=0,
            approved_at=NOW,
        )


def test_runtime_features_must_exactly_match_frozen_schema():
    _, bundle, _, _ = validation_and_bundle()
    with pytest.raises(ProposalRejected, match="exactly match"):
        bundle.model.score({"signal": 2.0, "online_added_feature": 1.0})


def test_registry_requires_bound_validation_and_manual_cas_promotion(tmp_path):
    report, bundle, _, _ = validation_and_bundle()
    registry = ModelRegistry(tmp_path / "registry.sqlite3")
    model_id = registry.register_candidate(bundle.to_bytes())
    registry.record_validation(model_id, report, recorded_at=NOW)
    policy = PromotionPolicy(
        min_folds=3,
        min_oos_rows=40,
        min_trades=10,
        min_aggregate_accuracy=0.8,
        min_aggregate_net_return=0.01,
        min_worst_fold_net_return=0.01,
        max_drawdown=0.05,
    )
    kwargs = dict(
        policy=policy,
        reviewer="human-reviewer",
        rationale="Walk-forward evidence and fold dispersion were reviewed.",
        expected_generation=0,
        approved_at=NOW,
    )
    with pytest.raises(PromotionDenied, match="confirmation"):
        registry.promote(model_id, confirmation="yes", **kwargs)
    champion = registry.promote(model_id, confirmation=PROMOTION_CONFIRMATION, **kwargs)
    assert champion.model_id == model_id
    assert champion.generation == 1
    assert registry.load_champion() == champion
    assert registry.list_models()[0]["state"] == "champion"
    assert registry.get_validation(report.validation_run_id)["reportSha256"] == report.report_sha256
    assert registry.champion_summary()["generation"] == 1
    assert registry.promotion_history()[0]["modelId"] == model_id
    with pytest.raises(PromotionDenied, match="generation"):
        registry.promote(model_id, confirmation=PROMOTION_CONFIRMATION, **kwargs)


def test_registry_rechecks_artifact_blob_at_promotion(tmp_path):
    report, bundle, _, _ = validation_and_bundle()
    path = tmp_path / "registry.sqlite3"
    registry = ModelRegistry(path)
    model_id = registry.register_candidate(bundle.to_bytes())
    registry.record_validation(model_id, report, recorded_at=NOW)
    with sqlite3.connect(path) as db:
        db.execute(
            "UPDATE model_candidates SET artifact_blob = ? WHERE model_id = ?",
            (bundle.to_bytes() + b" ", model_id),
        )
    with pytest.raises(PromotionDenied, match="integrity"):
        registry.promote(
            model_id,
            policy=PromotionPolicy(min_folds=1, min_oos_rows=1),
            reviewer="human-reviewer",
            rationale="The model was reviewed before the tamper test.",
            confirmation=PROMOTION_CONFIRMATION,
            expected_generation=0,
            approved_at=NOW,
        )


def test_proposal_uses_fixed_policy_and_completed_fresh_market(tmp_path):
    champion, policy = promoted_champion(tmp_path)
    proposal = proposal_for(champion, policy)
    assert proposal.environment == "okx_demo"
    assert proposal.instrument == "BTC-USDT"
    assert proposal.side == "buy"
    assert proposal.price == Decimal("100")
    assert proposal.size == Decimal("0.1")
    assert proposal.notional_usdt == policy.fixed_notional_usdt

    stale_market = MarketSnapshot(
        observed_at=NOW - timedelta(seconds=9),
        candle_closed_at=NOW - timedelta(minutes=5),
        candle_confirmed=True,
        instrument="BTC-USDT",
        bid=Decimal("100"),
        ask=Decimal("101"),
        tick_size=Decimal("1"),
        lot_size=Decimal("0.001"),
        min_size=Decimal("0.001"),
    )
    with pytest.raises(ProposalRejected, match="stale"):
        build_order_proposal(
            champion.bundle,
            features={"signal": 2.0},
            market=stale_market,
            policy=policy,
            now=NOW,
        )

    with pytest.raises(ProposalRejected, match="BUY-entry-only"):
        build_order_proposal(
            champion.bundle,
            features={"signal": -2.0},
            market=replace(stale_market, observed_at=NOW),
            policy=policy,
            now=NOW,
        )


class FakeTradingPort:
    def __init__(self, *, allowed: bool = True, fail_commit: bool = False):
        self.allowed = allowed
        self.fail_commit = fail_commit
        self.previews = []
        self.commits = []

    async def preview(self, draft):
        self.previews.append(draft)
        return {
            "intentId": "intent-1",
            "digest": "a" * 64,
            "decision": {"allowed": self.allowed, "reasonCodes": []},
        }

    async def commit(self, intent_id, digest, idempotency_key):
        self.commits.append((intent_id, digest, idempotency_key))
        if self.fail_commit:
            raise TimeoutError("ambiguous")
        return {"intentId": intent_id, "status": "accepted", "ordId": "123", "replayed": False}


def permit_for(champion: ChampionSnapshot, policy: DemoStrategyPolicy):
    return authorize_demo_session(
        champion,
        policy,
        issued_by="human-operator",
        confirmation=AUTO_SESSION_CONFIRMATION,
        issued_at=NOW,
        ttl_seconds=120,
        max_orders=1,
        max_total_notional_usdt=Decimal("10"),
    )


@pytest.mark.asyncio
async def test_executor_uses_preview_commit_once_and_deduplicates_signal(tmp_path):
    champion, policy = promoted_champion(tmp_path)
    proposal = proposal_for(champion, policy)
    permit = permit_for(champion, policy)
    port = FakeTradingPort()
    executor = DemoAutoExecutor(AutomationLedger(tmp_path / "automation.sqlite3"))
    result = await executor.execute(proposal, permit, champion, port, now=NOW)
    assert result.status == "accepted"
    assert len(port.previews) == 1
    assert port.commits == [("intent-1", "a" * 64, proposal.idempotency_key)]
    assert executor.ledger.permit_status(permit.permit_id)["usedOrders"] == 1
    assert executor.ledger.recent_executions()[0]["status"] == "completed"
    with pytest.raises(AutomationDenied, match="budget is exhausted"):
        await executor.execute(proposal, permit, champion, port, now=NOW)
    assert len(port.commits) == 1


@pytest.mark.asyncio
async def test_executor_defense_in_depth_rejects_sell_before_preview(tmp_path):
    champion, policy = promoted_champion(tmp_path)
    sell_proposal = replace(proposal_for(champion, policy), side="sell")
    permit = permit_for(champion, policy)
    port = FakeTradingPort()
    executor = DemoAutoExecutor(AutomationLedger(tmp_path / "automation.sqlite3"))
    with pytest.raises(AutomationDenied, match="SELL is forbidden"):
        await executor.execute(sell_proposal, permit, champion, port, now=NOW)
    assert port.previews == []
    assert port.commits == []


@pytest.mark.asyncio
async def test_executor_never_commits_a_rejected_preview(tmp_path):
    champion, policy = promoted_champion(tmp_path)
    proposal = proposal_for(champion, policy)
    permit = permit_for(champion, policy)
    port = FakeTradingPort(allowed=False)
    executor = DemoAutoExecutor(AutomationLedger(tmp_path / "automation.sqlite3"))
    result = await executor.execute(proposal, permit, champion, port, now=NOW)
    assert result.status == "preview_rejected"
    assert port.commits == []


@pytest.mark.asyncio
async def test_executor_fails_closed_on_unknown_commit_without_retry(tmp_path):
    champion, policy = promoted_champion(tmp_path)
    proposal = proposal_for(champion, policy)
    permit = permit_for(champion, policy)
    port = FakeTradingPort(fail_commit=True)
    ledger = AutomationLedger(tmp_path / "automation.sqlite3")
    executor = DemoAutoExecutor(ledger)
    with pytest.raises(ManualReviewRequired, match="retry is forbidden"):
        await executor.execute(proposal, permit, champion, port, now=NOW)
    assert len(port.commits) == 1
    pending = ledger.pending_manual_review()
    assert pending[0]["signal_id"] == proposal.signal_id
    assert pending[0]["status"] == "manual_review"


@pytest.mark.asyncio
async def test_executor_preserves_task_cancellation_and_marks_manual_review(tmp_path):
    champion, policy = promoted_champion(tmp_path)
    proposal = proposal_for(champion, policy)
    permit = permit_for(champion, policy)
    started = asyncio.Event()

    class BlockingPort(FakeTradingPort):
        async def commit(self, intent_id, digest, idempotency_key):
            self.commits.append((intent_id, digest, idempotency_key))
            started.set()
            await asyncio.Event().wait()

    port = BlockingPort()
    ledger = AutomationLedger(tmp_path / "automation.sqlite3")
    executor = DemoAutoExecutor(ledger)
    task = asyncio.create_task(
        executor.execute(proposal, permit, champion, port, now=NOW)
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=1)
    assert task.cancelled()
    pending = ledger.pending_manual_review()
    assert pending[0]["signal_id"] == proposal.signal_id
    assert pending[0]["status"] == "manual_review"


def test_permit_is_short_lived_and_bound_to_current_champion(tmp_path):
    champion, policy = promoted_champion(tmp_path)
    with pytest.raises(AutomationDenied, match="confirmation"):
        authorize_demo_session(
            champion,
            policy,
            issued_by="human-operator",
            confirmation="enable",
            issued_at=NOW,
            ttl_seconds=120,
            max_orders=1,
            max_total_notional_usdt=Decimal("10"),
        )
    with pytest.raises(AutomationDenied, match="order count"):
        authorize_demo_session(
            champion,
            policy,
            issued_by="human-operator",
            confirmation=AUTO_SESSION_CONFIRMATION,
            issued_at=NOW,
            ttl_seconds=120,
            max_orders=2,
            max_total_notional_usdt=Decimal("10"),
        )


@pytest.mark.asyncio
async def test_persisted_permit_revocation_blocks_future_claim(tmp_path):
    champion, policy = promoted_champion(tmp_path)
    proposal = proposal_for(champion, policy)
    permit = permit_for(champion, policy)
    ledger = AutomationLedger(tmp_path / "automation.sqlite3")
    ledger.register_permit(permit, now=NOW)
    ledger.revoke_permit(permit.permit_id, now=NOW)
    executor = DemoAutoExecutor(ledger)
    port = FakeTradingPort()
    with pytest.raises(AutomationDenied, match="revoked"):
        await executor.execute(proposal, permit, champion, port, now=NOW)
    assert port.previews == []
    assert port.commits == []
    with pytest.raises(AutomationDenied, match="TTL"):
        authorize_demo_session(
            champion,
            policy,
            issued_by="human-operator",
            confirmation=AUTO_SESSION_CONFIRMATION,
            issued_at=NOW,
            ttl_seconds=601,
            max_orders=1,
            max_total_notional_usdt=Decimal("10"),
        )
