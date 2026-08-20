from __future__ import annotations

import hmac
from datetime import datetime, timedelta, timezone
from collections.abc import Callable
from typing import Any

from ..audit import AuditStore
from .autonomy import (
    SUPERVISOR_ACTOR,
    AutonomyPolicy,
    AutonomyStore,
    SupervisorDecision,
    SupervisorDenied,
)
from .registry import (
    PROMOTION_CONFIRMATION,
    ModelRegistry,
    PromotionDenied,
    PromotionPolicy,
)
from .strategy import canonical_json, sha256_hex
from .walk_forward import ValidationReport


SUPERVISOR_REVIEW_SCHEMA = "tideguard.codex-review.v1"


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise SupervisorDenied("supervisor timestamp must be timezone-aware")
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return _utc(value).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class CodexSupervisor:
    """Build and apply content-addressed, secret-free Codex decisions."""

    def __init__(
        self,
        *,
        registry: ModelRegistry,
        autonomy: AutonomyStore,
        audit: AuditStore,
        promotion_policy: PromotionPolicy,
        autonomy_policy: AutonomyPolicy,
        market_snapshot_validator: Callable[[str | None], bool] | None = None,
    ) -> None:
        self.registry = registry
        self.autonomy = autonomy
        self.audit = audit
        self.promotion_policy = promotion_policy
        self.autonomy_policy = autonomy_policy
        self.market_snapshot_validator = market_snapshot_validator

    def _model_reviews(self) -> list[dict[str, Any]]:
        reviews: list[dict[str, Any]] = []
        for model in self.registry.list_models(limit=100):
            validation_run_id = str(model.get("validationRunId") or "")
            validation = (
                self.registry.get_validation(validation_run_id)
                if validation_run_id
                else None
            )
            metrics: dict[str, Any] | None = None
            deterministic_failures: list[str] = ["validation_missing"]
            if validation:
                report = ValidationReport.from_dict(validation["report"])
                deterministic_failures = list(self.promotion_policy.failures(report))
                if self.market_snapshot_validator and not self.market_snapshot_validator(
                    report.market_snapshot_sha256
                ):
                    deterministic_failures.append("market_snapshot_not_current")
                metrics = {
                    "aggregateAccuracy": report.aggregate_accuracy,
                    "benchmarkCohortId": report.benchmark_cohort_id,
                    "evaluationDatasetSha256": report.dataset_sha256,
                    "evaluationMode": report.evaluation_mode,
                    "folds": len(report.folds),
                    "maxDrawdown": report.max_drawdown,
                    "marketSnapshotSha256": report.market_snapshot_sha256,
                    "netReturn": report.aggregate_net_return,
                    "oosRows": report.oos_rows,
                    "reportSha256": report.report_sha256,
                    "roundTripCostBps": report.round_trip_cost_bps,
                    "splitProtocolSha256": report.split_protocol_sha256,
                    "testFrom": report.folds[0].test_start_at.isoformat(),
                    "testThrough": report.folds[-1].test_stop_at.isoformat(),
                    "trades": report.trades,
                    "worstFoldNetReturn": report.worst_fold_net_return,
                }
            shadow = self.autonomy.shadow_summary(str(model["modelId"]))
            shadow_failures = self.shadow_failures(shadow)
            reviews.append(
                {
                    "artifactSha256": model["artifactSha256"],
                    "comparisonBaselineCohort": None,
                    "comparisonBaselineModelId": None,
                    "createdAt": model["createdAt"],
                    "comparisonFailures": [],
                    "deterministicFailures": deterministic_failures,
                    "fitDatasetSha256": model.get("fitDatasetSha256"),
                    "fitRows": model.get("fitRows"),
                    "metrics": metrics,
                    "modelId": model["modelId"],
                    "shadow": shadow,
                    "shadowFailures": list(shadow_failures),
                    "state": model["state"],
                    "trainedThrough": model["trainedThrough"],
                    "trainedFrom": model.get("trainedFrom"),
                    "trainer": model["trainer"],
                    "trainingConfigSha256": model.get("trainingConfigSha256"),
                    "validationRunId": model.get("validationRunId"),
                }
            )
        champion = self.registry.champion_summary()
        if champion:
            champion_review = next(
                (
                    item
                    for item in reviews
                    if item["modelId"] == champion["modelId"]
                ),
                None,
            )
            champion_metrics = champion_review.get("metrics") if champion_review else None
            champion_training_config = (
                champion_review.get("trainingConfigSha256") if champion_review else None
            )
            comparison_fields = (
                "benchmarkCohortId",
                "evaluationDatasetSha256",
                "marketSnapshotSha256",
                "splitProtocolSha256",
                "testFrom",
                "testThrough",
            )

            def same_cohort(left: dict[str, Any], right: dict[str, Any]) -> bool:
                return all(
                    left.get(field) and left.get(field) == right.get(field)
                    for field in comparison_fields
                )

            for item in reviews:
                if item["modelId"] == champion["modelId"] or item["state"] != "validated":
                    continue
                candidate_metrics = item.get("metrics")
                failures: list[str] = []
                if not candidate_metrics or not champion_metrics:
                    failures.append("champion_comparison_missing")
                else:
                    baseline = (
                        champion_review
                        if same_cohort(candidate_metrics, champion_metrics)
                        else next(
                            (
                                review
                                for review in reviews
                                if champion_training_config
                                and review.get("trainingConfigSha256")
                                == champion_training_config
                                and review.get("metrics")
                                and same_cohort(
                                    candidate_metrics,
                                    review["metrics"],
                                )
                            ),
                            None,
                        )
                    )
                    baseline_metrics = baseline.get("metrics") if baseline else None
                    if not baseline_metrics:
                        failures.append("champion_comparison_missing")
                    else:
                        item["comparisonBaselineModelId"] = baseline["modelId"]
                        item["comparisonBaselineCohort"] = baseline_metrics.get(
                            "benchmarkCohortId"
                        )
                        required_net = float(baseline_metrics["netReturn"]) + float(
                            self.autonomy_policy.min_challenger_oos_improvement
                        )
                        if float(candidate_metrics["netReturn"]) < required_net:
                            failures.append("challenger_oos_improvement_insufficient")
                        maximum_drawdown = float(baseline_metrics["maxDrawdown"]) + float(
                            self.autonomy_policy.max_challenger_drawdown_regression
                        )
                        if float(candidate_metrics["maxDrawdown"]) > maximum_drawdown:
                            failures.append("challenger_drawdown_regression")
                item["comparisonFailures"] = failures
        return reviews

    def shadow_failures(self, summary: dict[str, Any]) -> tuple[str, ...]:
        failures: list[str] = []
        if int(summary.get("settledBuys") or 0) < self.autonomy_policy.shadow_min_settled:
            failures.append("shadow_buys_insufficient")
        if float(summary.get("durationDays") or 0.0) < self.autonomy_policy.shadow_min_days:
            failures.append("shadow_duration_insufficient")
        if float(summary.get("netReturn") or 0.0) <= 0:
            failures.append("shadow_net_return_not_positive")
        if float(summary.get("maxDrawdown", 1.0)) > float(
            self.autonomy_policy.max_demo_drawdown
        ):
            failures.append("shadow_drawdown_above_limit")
        return tuple(failures)

    def review_pack(self, *, now: datetime) -> dict[str, Any]:
        generated = _utc(now)
        body = {
            "auditChainValid": self.audit.verify_chain(),
            "autonomyPolicy": {
                **self.autonomy_policy.to_dict(),
                "policySha256": self.autonomy_policy.policy_sha256,
            },
            "autonomyState": self.autonomy.state(),
            "activePosition": self.autonomy.active_position(),
            "champion": self.registry.champion_summary(),
            "demoPerformance": self.autonomy.demo_performance(),
            "generatedAt": _iso(generated),
            "generation": self.registry.get_generation(),
            "models": self._model_reviews(),
            "promotionPolicy": {
                **self.promotion_policy.to_dict(),
                "policySha256": self.promotion_policy.policy_sha256,
            },
            "schemaVersion": SUPERVISOR_REVIEW_SCHEMA,
        }
        champion = body["champion"]
        body["championSupervisorApproved"] = bool(
            champion
            and self.autonomy.applied_champion_decision(
                model_id=str(champion["modelId"]),
                artifact_sha256=str(champion["artifactSha256"]),
                generation=int(champion["generation"]),
            )
        )
        evidence_body = {key: value for key, value in body.items() if key != "generatedAt"}
        evidence_sha = sha256_hex(canonical_json(evidence_body))
        return {**body, "evidenceSha256": evidence_sha}

    @staticmethod
    def _require_evidence(pack: dict[str, Any], expected: str) -> None:
        actual = str(pack.get("evidenceSha256") or "")
        if not expected or not hmac.compare_digest(actual, expected):
            raise SupervisorDenied("supervisor review evidence changed")

    @staticmethod
    def _find_model(pack: dict[str, Any], model_id: str) -> dict[str, Any]:
        for model in pack["models"]:
            if model["modelId"] == model_id:
                return model
        raise SupervisorDenied("candidate is absent from the current review pack")

    def approve_candidate(
        self,
        model_id: str,
        *,
        expected_evidence_sha256: str,
        rationale: str,
        now: datetime,
    ) -> dict[str, Any]:
        current = _utc(now)
        pack = self.review_pack(now=current)
        self._require_evidence(pack, expected_evidence_sha256)
        if not pack["auditChainValid"]:
            raise SupervisorDenied("audit chain is invalid")
        if pack["activePosition"] is not None:
            raise SupervisorDenied("candidate promotion requires a flat model position")
        model = self._find_model(pack, model_id)
        if model["state"] != "validated":
            raise SupervisorDenied("only a validated candidate can be approved")
        failures = [
            *model["deterministicFailures"],
            *model["shadowFailures"],
            *model["comparisonFailures"],
        ]
        if failures:
            raise SupervisorDenied("candidate gates failed: " + ", ".join(failures))
        decision = SupervisorDecision(
            kind="promote",
            subject_model_id=model_id,
            artifact_sha256=str(model["artifactSha256"]),
            expected_generation=int(pack["generation"]),
            policy_sha256=self.autonomy_policy.policy_sha256,
            evidence_sha256=str(pack["evidenceSha256"]),
            rationale=rationale,
            issued_at=current,
            expires_at=current + timedelta(hours=1),
        )
        self.autonomy.record_supervisor_decision(decision, now=current)
        try:
            champion = self.registry.promote(
                model_id,
                policy=self.promotion_policy,
                reviewer=SUPERVISOR_ACTOR,
                rationale=rationale,
                confirmation=PROMOTION_CONFIRMATION,
                expected_generation=int(pack["generation"]),
                approved_at=current,
            )
            self.audit.append(
                "ml.codex_promoted",
                {
                    "decisionId": decision.decision_id,
                    "evidenceSha256": decision.evidence_sha256,
                    "generation": champion.generation,
                    "modelId": champion.model_id,
                },
                actor=SUPERVISOR_ACTOR,
            )
            self.autonomy.mark_decision_applied(decision.decision_id, now=current)
        except Exception:
            self.autonomy.set_runtime_status(
                "suspended",
                reason="Codex 晋级事务未完整落盘",
                now=current,
            )
            raise
        return self.registry.champion_summary() or {}

    def issue_execution_lease(
        self,
        *,
        expected_evidence_sha256: str,
        rationale: str,
        now: datetime,
    ) -> dict[str, Any]:
        current = _utc(now)
        pack = self.review_pack(now=current)
        self._require_evidence(pack, expected_evidence_sha256)
        if not pack["auditChainValid"]:
            raise SupervisorDenied("audit chain is invalid")
        if pack["autonomyState"]["desiredMode"] != "demo":
            raise SupervisorDenied("the user has not enabled long-run Demo mode")
        if not pack["autonomyState"]["identityBound"]:
            raise SupervisorDenied("long-run Demo account identity is not bound")
        if pack["activePosition"] and pack["activePosition"]["status"] == "manual_review":
            raise SupervisorDenied("a model position requires manual reconciliation")
        champion = pack["champion"]
        if not champion:
            raise SupervisorDenied("there is no active champion")
        if not pack.get("championSupervisorApproved"):
            raise SupervisorDenied("champion has no fully applied Codex promotion decision")
        model = self._find_model(pack, str(champion["modelId"]))
        failures = [
            *model["deterministicFailures"],
            *model["shadowFailures"],
        ]
        if failures:
            raise SupervisorDenied("champion lease gates failed: " + ", ".join(failures))
        decision = SupervisorDecision(
            kind="lease",
            subject_model_id=str(champion["modelId"]),
            artifact_sha256=str(champion["artifactSha256"]),
            expected_generation=int(champion["generation"]),
            policy_sha256=self.autonomy_policy.policy_sha256,
            evidence_sha256=str(pack["evidenceSha256"]),
            rationale=rationale,
            issued_at=current,
            expires_at=current
            + timedelta(hours=self.autonomy_policy.supervisor_lease_hours),
        )
        self.autonomy.record_supervisor_decision(decision, now=current)
        try:
            self.audit.append(
                "ml.codex_execution_lease",
                {
                    "decisionId": decision.decision_id,
                    "evidenceSha256": decision.evidence_sha256,
                    "expiresAt": _iso(decision.expires_at),
                    "generation": decision.expected_generation,
                    "modelId": decision.subject_model_id,
                },
                actor=SUPERVISOR_ACTOR,
            )
            self.autonomy.mark_decision_applied(decision.decision_id, now=current)
        except Exception:
            self.autonomy.set_runtime_status(
                "suspended",
                reason="Codex 执行 lease 审计未完整落盘",
                now=current,
            )
            raise
        return {
            "decisionId": decision.decision_id,
            "modelId": decision.subject_model_id,
            "generation": decision.expected_generation,
            "issuedAt": _iso(decision.issued_at),
            "expiresAt": _iso(decision.expires_at),
            "evidenceSha256": decision.evidence_sha256,
        }

    def reject_candidate(
        self,
        model_id: str,
        *,
        expected_evidence_sha256: str,
        rationale: str,
        now: datetime,
    ) -> str:
        current = _utc(now)
        pack = self.review_pack(now=current)
        self._require_evidence(pack, expected_evidence_sha256)
        model = self._find_model(pack, model_id)
        if model["state"] not in {"candidate", "validated"}:
            raise SupervisorDenied("only a non-executable candidate can be rejected")
        decision = SupervisorDecision(
            kind="reject",
            subject_model_id=model_id,
            artifact_sha256=str(model["artifactSha256"]),
            expected_generation=int(pack["generation"]),
            policy_sha256=self.autonomy_policy.policy_sha256,
            evidence_sha256=str(pack["evidenceSha256"]),
            rationale=rationale,
            issued_at=current,
            expires_at=current + timedelta(hours=1),
        )
        self.autonomy.record_supervisor_decision(decision, now=current)
        try:
            self.registry.reject_candidate(model_id)
            self.audit.append(
                "ml.codex_rejected",
                {
                    "decisionId": decision.decision_id,
                    "evidenceSha256": decision.evidence_sha256,
                    "modelId": model_id,
                },
                actor=SUPERVISOR_ACTOR,
            )
            self.autonomy.mark_decision_applied(decision.decision_id, now=current)
        except Exception:
            self.autonomy.set_runtime_status(
                "suspended",
                reason="Codex 拒绝候选事务未完整落盘",
                now=current,
            )
            raise
        return decision.decision_id

    def rollback_champion(
        self,
        model_id: str,
        *,
        expected_evidence_sha256: str,
        rationale: str,
        now: datetime,
    ) -> dict[str, Any]:
        current = _utc(now)
        pack = self.review_pack(now=current)
        self._require_evidence(pack, expected_evidence_sha256)
        if not pack["auditChainValid"]:
            raise SupervisorDenied("audit chain is invalid")
        if pack["activePosition"] is not None:
            raise SupervisorDenied("rollback requires a flat model position")
        target = self._find_model(pack, model_id)
        if target["state"] != "retired":
            raise SupervisorDenied("rollback target is not a retired champion")
        failures = [
            *target["deterministicFailures"],
            *target["shadowFailures"],
        ]
        if failures:
            raise SupervisorDenied("rollback target gates failed: " + ", ".join(failures))
        decision = SupervisorDecision(
            kind="rollback",
            subject_model_id=model_id,
            artifact_sha256=str(target["artifactSha256"]),
            expected_generation=int(pack["generation"]),
            policy_sha256=self.autonomy_policy.policy_sha256,
            evidence_sha256=str(pack["evidenceSha256"]),
            rationale=rationale,
            issued_at=current,
            expires_at=current + timedelta(hours=1),
        )
        self.autonomy.record_supervisor_decision(decision, now=current)
        try:
            champion = self.registry.rollback_to(
                model_id,
                policy=self.promotion_policy,
                reviewer=SUPERVISOR_ACTOR,
                rationale=rationale,
                expected_generation=int(pack["generation"]),
                approved_at=current,
            )
            self.audit.append(
                "ml.codex_rollback",
                {
                    "decisionId": decision.decision_id,
                    "evidenceSha256": decision.evidence_sha256,
                    "generation": champion.generation,
                    "modelId": champion.model_id,
                },
                actor=SUPERVISOR_ACTOR,
            )
            self.autonomy.mark_decision_applied(decision.decision_id, now=current)
        except Exception:
            self.autonomy.set_runtime_status(
                "suspended",
                reason="Codex 回滚事务未完整落盘",
                now=current,
            )
            raise
        return self.registry.champion_summary() or {}

    def suspend(self, *, rationale: str, now: datetime) -> str:
        current = _utc(now)
        pack = self.review_pack(now=current)
        decision = SupervisorDecision(
            kind="suspend",
            subject_model_id=None,
            artifact_sha256=None,
            expected_generation=int(pack["generation"]),
            policy_sha256=self.autonomy_policy.policy_sha256,
            evidence_sha256=str(pack["evidenceSha256"]),
            rationale=rationale,
            issued_at=current,
            expires_at=current + timedelta(hours=1),
        )
        self.autonomy.record_supervisor_decision(decision, now=current)
        self.autonomy.disable_master(rationale, now=current)
        self.audit.append(
            "ml.codex_suspended",
            {"decisionId": decision.decision_id, "reason": rationale},
            actor=SUPERVISOR_ACTOR,
        )
        self.autonomy.mark_decision_applied(decision.decision_id, now=current)
        return decision.decision_id


__all__ = ["CodexSupervisor", "SUPERVISOR_REVIEW_SCHEMA"]
