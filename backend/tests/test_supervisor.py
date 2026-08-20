from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from okx_demo_lab.ml.autonomy import (
    AUTONOMY_ENABLE_CONFIRMATION,
    AutonomyPolicy,
    AutonomyStore,
    SupervisorDecision,
    SupervisorDenied,
)
from okx_demo_lab.ml.registry import PromotionPolicy
from okx_demo_lab.ml.supervisor import CodexSupervisor


NOW = datetime(2026, 8, 20, 0, 0, tzinfo=timezone.utc)
MODEL_ID = "mdl_" + "1" * 24
ARTIFACT = "a" * 64
EVIDENCE = "e" * 64


class FakeAudit:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict, str]] = []

    def verify_chain(self) -> bool:
        return True

    def append(self, event_type, payload, *, actor="system", **_):
        self.events.append((event_type, payload, actor))


class FakeRegistry:
    def __init__(self) -> None:
        self.generation = 0
        self._champion = None
        self.rejected: list[str] = []

    def list_models(self, limit=100):
        return []

    def get_validation(self, _):
        return None

    def get_generation(self):
        return self.generation

    def champion_summary(self):
        return self._champion

    def promote(self, model_id, **kwargs):
        assert kwargs["expected_generation"] == self.generation
        self.generation += 1
        self._champion = {
            "modelId": model_id,
            "artifactSha256": ARTIFACT,
            "generation": self.generation,
        }
        return SimpleNamespace(
            model_id=model_id,
            artifact_sha256=ARTIFACT,
            generation=self.generation,
        )

    def reject_candidate(self, model_id):
        self.rejected.append(model_id)


def model_review(state="validated"):
    return {
        "artifactSha256": ARTIFACT,
        "createdAt": NOW.isoformat(),
        "comparisonFailures": [],
        "deterministicFailures": [],
        "metrics": {"netReturn": 0.1},
        "modelId": MODEL_ID,
        "shadow": {
            "settledBuys": 25,
            "durationDays": 8.0,
            "netReturn": 0.02,
            "maxDrawdown": 0.01,
        },
        "shadowFailures": [],
        "state": state,
        "trainedThrough": NOW.isoformat(),
        "trainer": "test",
        "validationRunId": "val-test",
    }


def pack(autonomy: AutonomyStore, registry: FakeRegistry, *, state="validated"):
    return {
        "auditChainValid": True,
        "autonomyPolicy": {},
        "autonomyState": autonomy.state(),
        "activePosition": autonomy.active_position(),
        "champion": registry.champion_summary(),
        "championSupervisorApproved": bool(registry.champion_summary()),
        "demoPerformance": autonomy.demo_performance(),
        "evidenceSha256": EVIDENCE,
        "generatedAt": NOW.isoformat(),
        "generation": registry.get_generation(),
        "models": [model_review(state)],
        "promotionPolicy": {},
        "schemaVersion": "tideguard.codex-review.v1",
    }


def supervisor(tmp_path):
    registry = FakeRegistry()
    autonomy = AutonomyStore(tmp_path / "autonomy.sqlite3")
    audit = FakeAudit()
    instance = CodexSupervisor(
        registry=registry,  # type: ignore[arg-type]
        autonomy=autonomy,
        audit=audit,  # type: ignore[arg-type]
        promotion_policy=PromotionPolicy(),
        autonomy_policy=AutonomyPolicy(),
    )
    return instance, registry, autonomy, audit


def test_codex_approval_is_evidence_bound_and_applied_after_promotion(
    tmp_path, monkeypatch
):
    instance, registry, autonomy, audit = supervisor(tmp_path)
    monkeypatch.setattr(instance, "review_pack", lambda **_: pack(autonomy, registry))

    with pytest.raises(SupervisorDenied, match="evidence changed"):
        instance.approve_candidate(
            MODEL_ID,
            expected_evidence_sha256="f" * 64,
            rationale="Codex rejects a stale review pack before changing the champion.",
            now=NOW,
        )

    champion = instance.approve_candidate(
        MODEL_ID,
        expected_evidence_sha256=EVIDENCE,
        rationale="Codex verified the current OOS and shadow evidence for this candidate.",
        now=NOW,
    )
    assert champion["generation"] == 1
    decisions = autonomy.recent_decisions()
    assert decisions[0]["kind"] == "promote"
    assert decisions[0]["appliedAt"] is not None
    assert audit.events[0][0] == "ml.codex_promoted"


def test_codex_lease_requires_user_demo_master_and_becomes_active_only_after_audit(
    tmp_path, monkeypatch
):
    instance, registry, autonomy, audit = supervisor(tmp_path)
    registry.generation = 1
    registry._champion = {
        "modelId": MODEL_ID,
        "artifactSha256": ARTIFACT,
        "generation": 1,
    }
    autonomy.enable_master(
        mode="demo",
        credential_fingerprint="c" * 64,
        account_fingerprint="d" * 64,
        confirmation=AUTONOMY_ENABLE_CONFIRMATION,
        now=NOW,
    )
    promotion = SupervisorDecision(
        kind="promote",
        subject_model_id=MODEL_ID,
        artifact_sha256=ARTIFACT,
        expected_generation=0,
        policy_sha256=instance.autonomy_policy.policy_sha256,
        evidence_sha256="a" * 64,
        rationale="Codex applied the tested champion before any execution lease.",
        issued_at=NOW - timedelta(minutes=5),
        expires_at=NOW + timedelta(minutes=55),
    )
    autonomy.record_supervisor_decision(promotion, now=NOW - timedelta(minutes=5))
    autonomy.mark_decision_applied(promotion.decision_id, now=NOW - timedelta(minutes=4))
    monkeypatch.setattr(instance, "review_pack", lambda **_: pack(autonomy, registry))

    result = instance.issue_execution_lease(
        expected_evidence_sha256=EVIDENCE,
        rationale="Codex grants a bounded Demo execution lease after all gates pass.",
        now=NOW,
    )
    active = autonomy.active_lease(
        model_id=MODEL_ID,
        artifact_sha256=ARTIFACT,
        generation=1,
        policy_sha256=instance.autonomy_policy.policy_sha256,
        now=NOW + timedelta(hours=1),
    )
    assert active and active["decisionId"] == result["decisionId"]
    assert active["appliedAt"] is not None
    assert audit.events[0][0] == "ml.codex_execution_lease"


def test_review_evidence_is_stable_across_read_times(tmp_path):
    instance, _, autonomy, _ = supervisor(tmp_path)
    first = instance.review_pack(now=NOW)
    autonomy.set_runtime_status("disabled", reason=None, now=NOW + timedelta(minutes=1))
    second = instance.review_pack(now=NOW + timedelta(hours=1))
    assert first["generatedAt"] != second["generatedAt"]
    assert first["evidenceSha256"] == second["evidenceSha256"]


def test_codex_cannot_approve_failed_shadow_or_nonvalidated_candidate(
    tmp_path, monkeypatch
):
    instance, registry, autonomy, _ = supervisor(tmp_path)
    failed = pack(autonomy, registry, state="validated")
    failed["models"][0]["shadowFailures"] = ["shadow_net_return_not_positive"]
    monkeypatch.setattr(instance, "review_pack", lambda **_: failed)
    with pytest.raises(SupervisorDenied, match="gates failed"):
        instance.approve_candidate(
            MODEL_ID,
            expected_evidence_sha256=EVIDENCE,
            rationale="Codex refuses a model whose shadow performance does not pass.",
            now=NOW,
        )

    not_validated = pack(autonomy, registry, state="candidate")
    monkeypatch.setattr(instance, "review_pack", lambda **_: not_validated)
    with pytest.raises(SupervisorDenied, match="validated"):
        instance.approve_candidate(
            MODEL_ID,
            expected_evidence_sha256=EVIDENCE,
            rationale="Codex refuses any candidate before its validation is finalized.",
            now=NOW,
        )


def test_codex_rejects_a_challenger_that_does_not_improve_on_champion(
    tmp_path, monkeypatch
):
    instance, registry, autonomy, _ = supervisor(tmp_path)
    registry.generation = 2
    registry._champion = {
        "modelId": "mdl_" + "2" * 24,
        "artifactSha256": "b" * 64,
        "generation": 2,
    }
    evidence = pack(autonomy, registry)
    evidence["models"][0]["comparisonFailures"] = [
        "challenger_oos_improvement_insufficient"
    ]
    monkeypatch.setattr(instance, "review_pack", lambda **_: evidence)
    with pytest.raises(SupervisorDenied, match="improvement"):
        instance.approve_candidate(
            MODEL_ID,
            expected_evidence_sha256=EVIDENCE,
            rationale="Codex refuses a challenger that does not improve the champion baseline.",
            now=NOW,
        )


def test_codex_lease_rejects_a_champion_without_applied_promotion(
    tmp_path, monkeypatch
):
    instance, registry, autonomy, _ = supervisor(tmp_path)
    registry.generation = 1
    registry._champion = {
        "modelId": MODEL_ID,
        "artifactSha256": ARTIFACT,
        "generation": 1,
    }
    autonomy.enable_master(
        mode="demo",
        credential_fingerprint="c" * 64,
        account_fingerprint="d" * 64,
        confirmation=AUTONOMY_ENABLE_CONFIRMATION,
        now=NOW,
    )
    evidence = pack(autonomy, registry)
    evidence["championSupervisorApproved"] = False
    monkeypatch.setattr(instance, "review_pack", lambda **_: evidence)
    with pytest.raises(SupervisorDenied, match="fully applied"):
        instance.issue_execution_lease(
            expected_evidence_sha256=EVIDENCE,
            rationale="Codex refuses execution until the promotion decision is fully audited.",
            now=NOW,
        )


def test_codex_rejection_is_bound_to_one_candidate(tmp_path, monkeypatch):
    instance, registry, autonomy, audit = supervisor(tmp_path)
    evidence = pack(autonomy, registry)
    monkeypatch.setattr(instance, "review_pack", lambda **_: evidence)
    decision_id = instance.reject_candidate(
        MODEL_ID,
        expected_evidence_sha256=EVIDENCE,
        rationale="Codex rejects this exact dominated challenger and stops its shadow work.",
        now=NOW,
    )
    assert registry.rejected == [MODEL_ID]
    decision = autonomy.recent_decisions()[0]
    assert decision["decisionId"] == decision_id
    assert decision["modelId"] == MODEL_ID
    assert decision["appliedAt"] is not None
    assert audit.events[0][1]["modelId"] == MODEL_ID
