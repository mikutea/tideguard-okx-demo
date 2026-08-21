from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import sqlite3

import pytest

from okx_demo_lab.ml.autonomy import (
    AUTONOMY_ENABLE_CONFIRMATION,
    AutonomyError,
    AutonomyPolicy,
    AutonomyStore,
    PositionStateError,
    SupervisorDecision,
    SupervisorDenied,
)


NOW = datetime(2026, 8, 20, 0, 0, tzinfo=timezone.utc)
HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
HASH_D = "d" * 64


def store(tmp_path) -> AutonomyStore:
    return AutonomyStore(tmp_path / "autonomy.sqlite3")


def lease(*, expires_in_hours: int = 6) -> SupervisorDecision:
    return SupervisorDecision(
        kind="lease",
        subject_model_id="mdl_" + "1" * 24,
        artifact_sha256=HASH_A,
        expected_generation=1,
        policy_sha256=HASH_B,
        evidence_sha256=HASH_C,
        rationale="Codex verified deterministic gates and issued a bounded Demo lease.",
        issued_at=NOW,
        expires_at=NOW + timedelta(hours=expires_in_hours),
    )


def create_position(db: AutonomyStore, *, signal: str = "sig-entry") -> str:
    return db.create_entry_position(
        model_id="mdl_" + "1" * 24,
        artifact_sha256=HASH_A,
        champion_generation=1,
        policy_sha256=HASH_B,
        credential_fingerprint=HASH_C,
        account_fingerprint=HASH_D,
        entry_signal_id=signal,
        supervisor_decision_id="sup_" + "1" * 28,
        requested_size=Decimal("0.001"),
        entry_candle_at=NOW,
        exit_due_at=NOW + timedelta(hours=1),
        hard_exit_at=NOW + timedelta(hours=2),
        now=NOW,
    )


def test_autonomy_defaults_disabled_and_requires_exact_confirmation(tmp_path):
    db = store(tmp_path)
    assert db.state()["desiredMode"] == "disabled"
    assert db.bound_identity() is None

    with pytest.raises(AutonomyError, match="confirmation"):
        db.enable_master(
            mode="demo",
            credential_fingerprint=HASH_C,
            account_fingerprint=HASH_D,
            confirmation="almost",
            now=NOW,
        )

    enabled = db.enable_master(
        mode="demo",
        credential_fingerprint=HASH_C,
        account_fingerprint=HASH_D,
        confirmation=AUTONOMY_ENABLE_CONFIRMATION,
        now=NOW,
    )
    assert enabled["desiredMode"] == "demo"
    assert enabled["runtimeStatus"] == "waiting_supervisor"
    assert enabled["identityBound"] is True
    assert db.bound_identity() == (HASH_C, HASH_D)


def test_supervisor_lease_is_content_addressed_and_strictly_bounded(tmp_path):
    db = store(tmp_path)
    decision = lease()
    decision_id = db.record_supervisor_decision(decision, now=NOW)
    assert decision_id == decision.decision_id
    assert (
        db.active_lease(
            model_id=decision.subject_model_id or "",
            artifact_sha256=HASH_A,
            generation=1,
            policy_sha256=HASH_B,
            now=NOW + timedelta(hours=1),
        )
        is None
    )
    db.mark_decision_applied(decision_id, now=NOW)
    active = db.active_lease(
        model_id=decision.subject_model_id or "",
        artifact_sha256=HASH_A,
        generation=1,
        policy_sha256=HASH_B,
        now=NOW + timedelta(hours=1),
    )
    assert active and active["decisionId"] == decision_id
    assert (
        db.active_lease(
            model_id=decision.subject_model_id or "",
            artifact_sha256=HASH_A,
            generation=1,
            policy_sha256=HASH_B,
            now=NOW + timedelta(hours=7),
        )
        is None
    )

    with pytest.raises(SupervisorDenied, match="24 hours"):
        SupervisorDecision(
            **{
                **decision.__dict__,
                "expires_at": NOW + timedelta(hours=25),
            }
        )


def test_shadow_sell_never_receives_short_profit(tmp_path):
    db = store(tmp_path)
    policy = AutonomyPolicy()
    buy_id = db.record_shadow_signal(
        model_id="mdl_" + "1" * 24,
        artifact_sha256=HASH_A,
        candle_closed_at=NOW,
        due_at=NOW + timedelta(hours=1),
        action="buy",
        score=0.8,
        entry_close=Decimal("100"),
        policy_sha256=policy.policy_sha256,
        round_trip_cost_bps=10,
    )
    sell_id = db.record_shadow_signal(
        model_id="mdl_" + "1" * 24,
        artifact_sha256=HASH_A,
        candle_closed_at=NOW + timedelta(minutes=5),
        due_at=NOW + timedelta(hours=1, minutes=5),
        action="sell",
        score=0.2,
        entry_close=Decimal("100"),
        policy_sha256=policy.policy_sha256,
        round_trip_cost_bps=10,
    )
    db.settle_shadow(
        buy_id,
        entry_open=Decimal("100"),
        exit_price=Decimal("110"),
        round_trip_cost_bps=10,
        exit_reason="time_exit",
        policy_sha256=policy.policy_sha256,
        now=NOW + timedelta(hours=1),
    )
    db.settle_shadow(
        sell_id,
        entry_open=Decimal("100"),
        exit_price=Decimal("90"),
        round_trip_cost_bps=10,
        exit_reason="time_exit",
        policy_sha256=policy.policy_sha256,
        now=NOW + timedelta(hours=2),
    )
    summary = db.shadow_summary(
        "mdl_" + "1" * 24, policy_sha256=policy.policy_sha256
    )
    assert summary["settledSignals"] == 2
    assert summary["settledBuys"] == 1
    assert summary["netReturn"] == pytest.approx((110 * 0.9995) / (100 * 1.0005) - 1)


def test_shadow_settlement_is_bound_to_the_frozen_policy(tmp_path):
    db = store(tmp_path)
    policy = AutonomyPolicy()
    signal_id = db.record_shadow_signal(
        model_id="mdl_" + "1" * 24,
        artifact_sha256=HASH_A,
        candle_closed_at=NOW,
        due_at=NOW + timedelta(minutes=65),
        action="buy",
        score=0.8,
        entry_close=Decimal("100"),
        policy_sha256=policy.policy_sha256,
        round_trip_cost_bps=24,
    )

    with pytest.raises(AutonomyError, match="frozen policy"):
        db.settle_shadow(
            signal_id,
            entry_open=Decimal("101"),
            exit_price=Decimal("102"),
            round_trip_cost_bps=24,
            exit_reason="time_exit",
            policy_sha256=HASH_B,
            now=NOW + timedelta(minutes=65),
        )


def test_position_uses_only_confirmed_fills_and_closes_exact_inventory(tmp_path):
    db = store(tmp_path)
    policy = AutonomyPolicy()
    position_id = create_position(db)
    db.attach_entry_order(
        position_id,
        intent_id="intent-entry",
        ord_id="ord-entry",
        cl_ord_id="cl-entry",
        now=NOW,
    )
    position = db.resolve_entry(
        position_id,
        filled_size=Decimal("0.0006"),
        average_price=Decimal("100000"),
        terminal_state="canceled",
        policy=policy,
        now=NOW + timedelta(seconds=1),
    )
    assert position["status"] == "long"
    assert position["filledSize"] == "0.0006"
    assert position["remainingSize"] == "0.0006"
    assert Decimal(position["stopPrice"]) == Decimal("98500.000")

    db.mark_exit_submitted(
        position_id,
        intent_id="intent-exit-1",
        ord_id="ord-exit-1",
        cl_ord_id="cl-exit-1",
        now=NOW + timedelta(hours=1),
    )
    position = db.resolve_exit(
        position_id,
        filled_size=Decimal("0.0002"),
        average_price=Decimal("101000"),
        terminal_state="canceled",
        max_exit_attempts=policy.max_exit_attempts,
        now=NOW + timedelta(hours=1, seconds=1),
    )
    assert position["status"] == "long"
    assert position["remainingSize"] == "0.0004"

    db.mark_exit_submitted(
        position_id,
        intent_id="intent-exit-2",
        ord_id="ord-exit-2",
        cl_ord_id="cl-exit-2",
        now=NOW + timedelta(hours=1, minutes=1),
    )
    position = db.resolve_exit(
        position_id,
        filled_size=Decimal("0.0004"),
        average_price=Decimal("102000"),
        terminal_state="filled",
        max_exit_attempts=policy.max_exit_attempts,
        now=NOW + timedelta(hours=1, minutes=1, seconds=1),
    )
    assert position["status"] == "closed"
    assert position["remainingSize"] == "0"
    assert position["realizedReturn"] == pytest.approx(1 / 60)
    assert db.active_position() is None


def test_position_rejects_overfills_and_multiple_active_positions(tmp_path):
    db = store(tmp_path)
    position_id = create_position(db)
    with pytest.raises(PositionStateError, match="another model position"):
        create_position(db, signal="different-signal")
    with pytest.raises(PositionStateError, match="exceeds"):
        db.resolve_entry(
            position_id,
            filled_size=Decimal("0.002"),
            average_price=Decimal("100000"),
            terminal_state="filled",
            policy=AutonomyPolicy(),
            now=NOW + timedelta(seconds=1),
        )


def test_entry_fee_in_btc_reduces_model_owned_inventory(tmp_path):
    db = store(tmp_path)
    position_id = create_position(db)
    position = db.resolve_entry(
        position_id,
        filled_size=Decimal("0.001"),
        average_price=Decimal("100000"),
        terminal_state="filled",
        policy=AutonomyPolicy(),
        fee=Decimal("-0.000001"),
        fee_currency="BTC",
        now=NOW + timedelta(seconds=1),
    )
    assert position["filledSize"] == "0.001"
    assert position["remainingSize"] == "0.000999"
    assert position["entryFee"] == "-0.000001"


def test_realized_return_uses_net_quote_after_entry_and_exit_fees(tmp_path):
    db = store(tmp_path)
    position_id = create_position(db)
    db.resolve_entry(
        position_id,
        filled_size=Decimal("0.001"),
        average_price=Decimal("100000"),
        terminal_state="filled",
        policy=AutonomyPolicy(),
        fee=Decimal("-0.10"),
        fee_currency="USDT",
        now=NOW + timedelta(seconds=1),
    )
    db.mark_exit_submitted(
        position_id,
        intent_id="intent-net-fee-exit",
        ord_id="ord-net-fee-exit",
        cl_ord_id="cl-net-fee-exit",
        now=NOW + timedelta(hours=1),
    )
    position = db.resolve_exit(
        position_id,
        filled_size=Decimal("0.001"),
        average_price=Decimal("101000"),
        terminal_state="filled",
        max_exit_attempts=5,
        fee=Decimal("-0.10"),
        fee_currency="USDT",
        now=NOW + timedelta(hours=1, seconds=1),
    )
    assert position["status"] == "closed"
    assert Decimal(position["exitQuoteValue"]) == Decimal("100.900")
    assert position["realizedReturn"] == pytest.approx(float(Decimal("100.9") / Decimal("100.1") - 1))


def test_dust_is_never_written_off_as_a_closed_position(tmp_path):
    db = store(tmp_path)
    position_id = create_position(db)
    db.resolve_entry(
        position_id,
        filled_size=Decimal("0.000001"),
        average_price=Decimal("100000"),
        terminal_state="canceled",
        policy=AutonomyPolicy(),
        now=NOW + timedelta(seconds=1),
    )
    position = db.close_dust(
        position_id,
        dust_size=Decimal("0.000001"),
        reason="below exchange minimum",
        now=NOW + timedelta(hours=1),
    )
    assert position["status"] == "manual_review"
    assert position["remainingSize"] == "0.000001"
    assert db.demo_performance()["closedPositions"] == 0


def test_position_row_tampering_fails_closed_before_inventory_can_be_used(tmp_path):
    db = store(tmp_path)
    position_id = create_position(db)
    db.resolve_entry(
        position_id,
        filled_size=Decimal("0.001"),
        average_price=Decimal("100000"),
        terminal_state="filled",
        policy=AutonomyPolicy(),
        now=NOW + timedelta(seconds=1),
    )
    connection = sqlite3.connect(db.path)
    try:
        with connection:
            connection.execute(
                "UPDATE positions SET remaining_size = '1' WHERE position_id = ?",
                (position_id,),
            )
    finally:
        connection.close()
    with pytest.raises(PositionStateError, match="integrity hash"):
        db.active_position()


def test_exit_attempt_limit_enters_manual_review(tmp_path):
    db = store(tmp_path)
    policy = AutonomyPolicy(max_exit_attempts=1)
    position_id = create_position(db)
    db.resolve_entry(
        position_id,
        filled_size=Decimal("0.001"),
        average_price=Decimal("100000"),
        terminal_state="filled",
        policy=policy,
        now=NOW + timedelta(seconds=1),
    )
    db.mark_exit_submitted(
        position_id,
        intent_id="intent-exit",
        ord_id="ord-exit",
        cl_ord_id="cl-exit",
        now=NOW + timedelta(hours=1),
    )
    position = db.resolve_exit(
        position_id,
        filled_size=Decimal("0"),
        average_price=None,
        terminal_state="canceled",
        max_exit_attempts=1,
        now=NOW + timedelta(hours=1, seconds=1),
    )
    assert position["status"] == "manual_review"
    assert position["remainingSize"] == "0.001"


def test_training_and_daily_entry_claims_are_crash_visible(tmp_path):
    db = store(tmp_path)
    assert db.training_due(now=NOW, interval_hours=24) is True
    run_id = db.start_training(now=NOW)
    db.update_training_progress(
        run_id,
        phase="walk_forward",
        current=1,
        total=3,
        snapshot_id="dset_" + "a" * 24,
        data_rows=905_000,
    )
    running = db.latest_training()
    assert running and running["phase"] == "walk_forward"
    assert running["progressCurrent"] == 1
    assert running["progressTotal"] == 3
    assert running["snapshotId"] == "dset_" + "a" * 24
    assert running["dataRows"] == 905_000
    db.finish_training(
        run_id,
        model_id="mdl_" + "1" * 24,
        result={"ok": True},
        error_type=None,
        now=NOW + timedelta(minutes=1),
    )
    assert db.latest_training()["phase"] == "completed"
    assert db.training_due(now=NOW + timedelta(hours=23), interval_hours=24) is False
    assert db.training_due(now=NOW + timedelta(hours=25), interval_hours=24) is True
    assert db.claim_daily_entry(now=NOW, maximum=2) == 1
    assert db.claim_daily_entry(now=NOW + timedelta(hours=1), maximum=2) == 2
    with pytest.raises(AutonomyError, match="budget"):
        db.claim_daily_entry(now=NOW + timedelta(hours=2), maximum=2)


def test_crash_interrupted_training_is_failed_and_retried_on_short_interval(tmp_path):
    db = store(tmp_path)
    run_id = db.start_training(now=NOW)
    with pytest.raises(sqlite3.IntegrityError):
        db.start_training(now=NOW + timedelta(minutes=1))
    assert db.recover_running_training(now=NOW + timedelta(minutes=2)) == 1
    latest = db.latest_training()
    assert latest and latest["runId"] == run_id
    assert latest["status"] == "failed"
    assert latest["errorType"] == "ProcessRestart"
    assert latest["phase"] == "failed"
    assert db.training_due(
        now=NOW + timedelta(minutes=30), interval_hours=24, retry_hours=1
    ) is False
    assert db.training_due(
        now=NOW + timedelta(hours=2), interval_hours=24, retry_hours=1
    ) is True


def test_master_mode_cannot_change_while_position_is_active(tmp_path):
    db = store(tmp_path)
    create_position(db)
    with pytest.raises(AutonomyError, match="active"):
        db.enable_master(
            mode="shadow",
            credential_fingerprint=HASH_C,
            account_fingerprint=HASH_D,
            confirmation=AUTONOMY_ENABLE_CONFIRMATION,
            now=NOW,
        )


def test_v1_autonomy_database_migrates_training_progress_without_losing_runs(tmp_path):
    path = tmp_path / "legacy-autonomy.sqlite3"
    with sqlite3.connect(path) as db:
        db.executescript(
            """
            CREATE TABLE autonomy_state (
                singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                schema_version TEXT NOT NULL,
                desired_mode TEXT NOT NULL,
                runtime_status TEXT NOT NULL,
                credential_fingerprint TEXT,
                account_fingerprint TEXT,
                suspended_reason TEXT,
                state_version INTEGER NOT NULL,
                enabled_at TEXT,
                updated_at TEXT NOT NULL
            );
            INSERT INTO autonomy_state VALUES
                (1, 'tideguard.autonomy.v1', 'disabled', 'disabled',
                 NULL, NULL, NULL, 0, NULL, '2026-08-20T00:00:00.000Z');
            CREATE TABLE training_runs (
                run_id TEXT PRIMARY KEY,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                status TEXT NOT NULL,
                model_id TEXT,
                error_type TEXT,
                result_json TEXT
            );
            INSERT INTO training_runs VALUES
                ('train_legacy', '2026-08-20T00:00:00.000Z',
                 '2026-08-20T00:01:00.000Z', 'completed',
                 'mdl_legacy', NULL, '{"ok":true}');
            """
        )
    migrated = AutonomyStore(path)
    assert migrated.summary()["schemaVersion"] == "tideguard.autonomy.v3"
    latest = migrated.latest_training()
    assert latest and latest["runId"] == "train_legacy"
    assert latest["phase"] == "completed"
    assert latest["progressCurrent"] == 0
    assert latest["snapshotId"] is None


def test_v2_shadow_rows_are_migrated_but_excluded_from_v2_evidence(tmp_path):
    path = tmp_path / "legacy-shadow.sqlite3"
    with sqlite3.connect(path) as db:
        db.executescript(
            """
            CREATE TABLE autonomy_state (
                singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                schema_version TEXT NOT NULL,
                desired_mode TEXT NOT NULL,
                runtime_status TEXT NOT NULL,
                credential_fingerprint TEXT,
                account_fingerprint TEXT,
                suspended_reason TEXT,
                state_version INTEGER NOT NULL,
                enabled_at TEXT,
                updated_at TEXT NOT NULL
            );
            INSERT INTO autonomy_state VALUES
                (1, 'tideguard.autonomy.v2', 'disabled', 'disabled',
                 NULL, NULL, NULL, 0, NULL, '2026-08-20T00:00:00.000Z');
            CREATE TABLE shadow_signals (
                signal_id TEXT PRIMARY KEY,
                model_id TEXT NOT NULL,
                artifact_sha256 TEXT NOT NULL,
                candle_closed_at TEXT NOT NULL,
                due_at TEXT NOT NULL,
                action TEXT NOT NULL,
                score REAL NOT NULL,
                entry_close TEXT NOT NULL,
                exit_close TEXT,
                net_return REAL,
                settled_at TEXT,
                UNIQUE(model_id, candle_closed_at)
            );
            INSERT INTO shadow_signals VALUES
                ('shadow_legacy', 'mdl_111111111111111111111111',
                 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
                 '2026-08-20T00:00:00.000Z', '2026-08-20T01:00:00.000Z',
                 'buy', 0.8, '100', '110', 0.09, '2026-08-20T01:00:00.000Z');
            """
        )

    migrated = AutonomyStore(path)
    summary = migrated.shadow_summary(
        "mdl_" + "1" * 24, policy_sha256=AutonomyPolicy().policy_sha256
    )
    assert migrated.summary()["schemaVersion"] == "tideguard.autonomy.v3"
    assert summary["settledBuys"] == 0
    assert summary["excludedLegacyOrPolicyMismatch"] == 1
