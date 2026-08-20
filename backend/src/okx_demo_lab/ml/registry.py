from __future__ import annotations

import hashlib
import hmac
import json
import math
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .strategy import (
    MODEL_SCHEMA_VERSION,
    FrozenModelBundle,
    ModelArtifactError,
    canonical_json,
    sha256_hex,
)
from .walk_forward import (
    LONG_ONLY_EVALUATION_MODE,
    VALIDATION_SCHEMA_VERSION,
    ValidationError,
    ValidationReport,
)


PROMOTION_CONFIRMATION = "PROMOTE OKX DEMO CHAMPION"


class RegistryError(RuntimeError):
    pass


class PromotionDenied(RegistryError):
    pass


def _utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise RegistryError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return _utc(value, "timestamp").isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class PromotionPolicy:
    min_folds: int = 3
    min_oos_rows: int = 200
    min_trades: int = 10
    min_round_trip_cost_bps: float = 5.0
    min_aggregate_accuracy: float = 0.50
    min_aggregate_net_return: float = 0.0
    min_worst_fold_net_return: float = -0.05
    max_drawdown: float = 0.15

    def __post_init__(self) -> None:
        numbers = (
            self.min_round_trip_cost_bps,
            self.min_aggregate_accuracy,
            self.min_aggregate_net_return,
            self.min_worst_fold_net_return,
            self.max_drawdown,
        )
        if any(not math.isfinite(float(value)) for value in numbers):
            raise ValueError("promotion policy must contain finite values")
        if self.min_folds < 1 or self.min_oos_rows < 1 or self.min_trades < 0:
            raise ValueError("promotion policy counts are invalid")
        if self.min_round_trip_cost_bps <= 0:
            raise ValueError("promotion policy must require positive costs")
        if not 0 <= self.min_aggregate_accuracy <= 1:
            raise ValueError("minimum accuracy is invalid")
        if not -1 < self.min_worst_fold_net_return <= 1:
            raise ValueError("worst-fold threshold is invalid")
        if not 0 <= self.max_drawdown <= 1:
            raise ValueError("drawdown threshold is invalid")

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_drawdown": self.max_drawdown,
            "min_aggregate_accuracy": self.min_aggregate_accuracy,
            "min_aggregate_net_return": self.min_aggregate_net_return,
            "min_folds": self.min_folds,
            "min_oos_rows": self.min_oos_rows,
            "min_round_trip_cost_bps": self.min_round_trip_cost_bps,
            "min_trades": self.min_trades,
            "min_worst_fold_net_return": self.min_worst_fold_net_return,
        }

    @property
    def policy_sha256(self) -> str:
        return sha256_hex(canonical_json(self.to_dict()))

    def failures(self, report: ValidationReport) -> tuple[str, ...]:
        failures: list[str] = []
        checks = (
            (
                report.schema_version == VALIDATION_SCHEMA_VERSION
                and report.evaluation_mode == LONG_ONLY_EVALUATION_MODE,
                "unsupported_evaluation_semantics",
            ),
            (len(report.folds) >= self.min_folds, "insufficient_folds"),
            (report.oos_rows >= self.min_oos_rows, "insufficient_oos_rows"),
            (report.trades >= self.min_trades, "insufficient_trades"),
            (
                report.round_trip_cost_bps >= self.min_round_trip_cost_bps,
                "cost_assumption_too_low",
            ),
            (
                report.aggregate_accuracy >= self.min_aggregate_accuracy,
                "aggregate_accuracy_below_gate",
            ),
            (
                report.aggregate_net_return >= self.min_aggregate_net_return,
                "aggregate_net_return_below_gate",
            ),
            (
                report.worst_fold_net_return >= self.min_worst_fold_net_return,
                "worst_fold_below_gate",
            ),
            (report.max_drawdown <= self.max_drawdown, "drawdown_above_gate"),
        )
        failures.extend(reason for passed, reason in checks if not passed)
        return tuple(failures)


@dataclass(frozen=True)
class ChampionSnapshot:
    model_id: str
    artifact_sha256: str
    generation: int
    bundle: FrozenModelBundle


class ModelRegistry:
    """Separate append-oriented registry for frozen, data-only model bundles."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA foreign_keys=ON")
        return db

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        db = self._connect()
        try:
            with db:
                yield db
        finally:
            db.close()

    def _initialize(self) -> None:
        with self._connection() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS model_candidates (
                    model_id TEXT PRIMARY KEY,
                    artifact_sha256 TEXT NOT NULL UNIQUE,
                    artifact_blob BLOB NOT NULL,
                    manifest_json TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('candidate', 'validated', 'champion', 'retired', 'rejected')),
                    trainer TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS validation_reports (
                    validation_run_id TEXT PRIMARY KEY,
                    model_id TEXT NOT NULL UNIQUE REFERENCES model_candidates(model_id),
                    report_sha256 TEXT NOT NULL UNIQUE,
                    report_json TEXT NOT NULL,
                    recorded_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS registry_state (
                    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                    champion_model_id TEXT REFERENCES model_candidates(model_id),
                    generation INTEGER NOT NULL CHECK(generation >= 0),
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS promotions (
                    promotion_id TEXT PRIMARY KEY,
                    generation INTEGER NOT NULL UNIQUE,
                    model_id TEXT NOT NULL REFERENCES model_candidates(model_id),
                    previous_model_id TEXT,
                    artifact_sha256 TEXT NOT NULL,
                    validation_run_id TEXT NOT NULL,
                    report_sha256 TEXT NOT NULL,
                    policy_sha256 TEXT NOT NULL,
                    reviewer TEXT NOT NULL,
                    rationale TEXT NOT NULL,
                    approved_at TEXT NOT NULL
                );
                """
            )
            db.execute(
                """
                INSERT OR IGNORE INTO registry_state
                (singleton, champion_model_id, generation, updated_at)
                VALUES (1, NULL, 0, ?)
                """,
                (_iso(datetime.now(timezone.utc)),),
            )

    @staticmethod
    def _bundle_from_row(row: sqlite3.Row) -> FrozenModelBundle:
        raw = bytes(row["artifact_blob"])
        expected = str(row["artifact_sha256"])
        bundle = FrozenModelBundle.from_bytes(raw, expected_sha256=expected)
        if bundle.model_id != row["model_id"]:
            raise RegistryError("registry model identity is inconsistent")
        if canonical_json(bundle.manifest.to_dict()) != row["manifest_json"]:
            raise RegistryError("registry manifest is inconsistent")
        return bundle

    def register_candidate(self, raw_artifact: bytes) -> str:
        try:
            bundle = FrozenModelBundle.from_bytes(raw_artifact)
        except ModelArtifactError as exc:
            raise RegistryError(str(exc)) from exc
        model_id = bundle.model_id
        artifact_sha = bundle.artifact_sha256
        with self._lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT artifact_sha256, artifact_blob FROM model_candidates WHERE model_id = ?",
                (model_id,),
            ).fetchone()
            if existing:
                if not hmac.compare_digest(str(existing["artifact_sha256"]), artifact_sha) or not hmac.compare_digest(
                    bytes(existing["artifact_blob"]), raw_artifact
                ):
                    raise RegistryError("model_id collision in registry")
                return model_id
            db.execute(
                """
                INSERT INTO model_candidates
                (model_id, artifact_sha256, artifact_blob, manifest_json, state, trainer, created_at)
                VALUES (?, ?, ?, ?, 'candidate', ?, ?)
                """,
                (
                    model_id,
                    artifact_sha,
                    raw_artifact,
                    canonical_json(bundle.manifest.to_dict()),
                    bundle.manifest.trainer,
                    _iso(bundle.manifest.created_at),
                ),
            )
        return model_id

    def record_validation(
        self, model_id: str, report: ValidationReport, *, recorded_at: datetime
    ) -> str:
        report_json = canonical_json(report.to_dict())
        with self._lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM model_candidates WHERE model_id = ?", (model_id,)
            ).fetchone()
            if row is None:
                raise RegistryError("candidate model does not exist")
            bundle = self._bundle_from_row(row)
            manifest = bundle.manifest
            mismatches = []
            if manifest.validation_run_id != report.validation_run_id:
                mismatches.append("validation_run_id")
            if manifest.dataset_sha256 != report.dataset_sha256:
                mismatches.append("dataset_sha256")
            if manifest.training_config_sha256 != report.training_config_sha256:
                mismatches.append("training_config_sha256")
            if manifest.feature_schema_sha256 != report.feature_schema_sha256:
                mismatches.append("feature_schema_sha256")
            if manifest.benchmark_cohort_id != report.benchmark_cohort_id:
                mismatches.append("benchmark_cohort_id")
            if manifest.market_snapshot_sha256 != report.market_snapshot_sha256:
                mismatches.append("market_snapshot_sha256")
            if manifest.split_protocol_sha256 != report.split_protocol_sha256:
                mismatches.append("split_protocol_sha256")
            if mismatches:
                raise RegistryError("validation does not bind the candidate: " + ", ".join(mismatches))
            existing = db.execute(
                "SELECT report_sha256, report_json FROM validation_reports WHERE model_id = ?",
                (model_id,),
            ).fetchone()
            if existing:
                if not hmac.compare_digest(str(existing["report_sha256"]), report.report_sha256) or not hmac.compare_digest(
                    str(existing["report_json"]), report_json
                ):
                    raise RegistryError("candidate already has a different validation report")
                return report.validation_run_id
            db.execute(
                """
                INSERT INTO validation_reports
                (validation_run_id, model_id, report_sha256, report_json, recorded_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    report.validation_run_id,
                    model_id,
                    report.report_sha256,
                    report_json,
                    _iso(recorded_at),
                ),
            )
            db.execute(
                "UPDATE model_candidates SET state = 'validated' WHERE model_id = ? AND state = 'candidate'",
                (model_id,),
            )
        return report.validation_run_id

    def get_generation(self) -> int:
        with self._connection() as db:
            row = db.execute(
                "SELECT generation FROM registry_state WHERE singleton = 1"
            ).fetchone()
        if row is None or int(row["generation"]) < 0:
            raise RegistryError("registry state is missing or invalid")
        return int(row["generation"])

    def list_models(self, limit: int = 100) -> list[dict[str, Any]]:
        """Return API-safe registry metadata without artifact bytes or file paths."""

        safe_limit = max(1, min(int(limit), 200))
        with self._connection() as db:
            rows = db.execute(
                """
                SELECT m.model_id, m.artifact_sha256, m.manifest_json, m.state,
                       m.trainer, m.created_at, v.validation_run_id,
                       v.report_sha256, v.recorded_at
                FROM model_candidates AS m
                LEFT JOIN validation_reports AS v ON v.model_id = m.model_id
                ORDER BY CASE WHEN m.state = 'champion' THEN 0 ELSE 1 END,
                         m.created_at DESC
                LIMIT ?
                """,
                (safe_limit,),
            ).fetchall()
        models: list[dict[str, Any]] = []
        for row in rows:
            try:
                manifest = json.loads(str(row["manifest_json"]))
            except json.JSONDecodeError as exc:
                raise RegistryError("registry manifest cannot be decoded") from exc
            models.append(
                {
                    "modelId": row["model_id"],
                    "artifactSha256": row["artifact_sha256"],
                    "state": row["state"],
                    "trainer": row["trainer"],
                    "createdAt": row["created_at"],
                    "benchmarkCohortId": manifest.get("benchmark_cohort_id"),
                    "trainedThrough": manifest.get("trained_through"),
                    "trainedFrom": manifest.get("trained_from"),
                    "datasetSha256": manifest.get("dataset_sha256"),
                    "fitDatasetSha256": manifest.get("fit_dataset_sha256"),
                    "fitRows": manifest.get("fit_rows"),
                    "marketSnapshotSha256": manifest.get("market_snapshot_sha256"),
                    "splitProtocolSha256": manifest.get("split_protocol_sha256"),
                    "trainingConfigSha256": manifest.get("training_config_sha256"),
                    "featureSchemaSha256": manifest.get("feature_schema_sha256"),
                    "validationRunId": row["validation_run_id"],
                    "reportSha256": row["report_sha256"],
                    "validationRecordedAt": row["recorded_at"],
                }
            )
        return models

    def get_validation(self, validation_run_id: str) -> dict[str, Any] | None:
        with self._connection() as db:
            row = db.execute(
                """
                SELECT validation_run_id, model_id, report_sha256, report_json, recorded_at
                FROM validation_reports WHERE validation_run_id = ?
                """,
                (validation_run_id,),
            ).fetchone()
        if row is None:
            return None
        report_json = str(row["report_json"])
        if not hmac.compare_digest(sha256_hex(report_json), str(row["report_sha256"])):
            raise RegistryError("validation report hash mismatch")
        try:
            report = ValidationReport.from_dict(json.loads(report_json))
        except (json.JSONDecodeError, ValidationError) as exc:
            raise RegistryError("validation report cannot be decoded") from exc
        if not hmac.compare_digest(report.report_sha256, str(row["report_sha256"])):
            raise RegistryError("validation report is not canonical")
        return {
            "validationRunId": row["validation_run_id"],
            "modelId": row["model_id"],
            "reportSha256": row["report_sha256"],
            "recordedAt": row["recorded_at"],
            "report": report.to_dict(),
        }

    def champion_summary(self) -> dict[str, Any] | None:
        champion = self.load_champion()
        if champion is None:
            return None
        with self._connection() as db:
            promotion = db.execute(
                """
                SELECT promotion_id, reviewer, rationale, policy_sha256,
                       validation_run_id, report_sha256, approved_at
                FROM promotions WHERE generation = ?
                """,
                (champion.generation,),
            ).fetchone()
        if promotion is None:
            raise RegistryError("champion has no promotion record")
        return {
            "modelId": champion.model_id,
            "artifactSha256": champion.artifact_sha256,
            "generation": champion.generation,
            "promotionId": promotion["promotion_id"],
            "reviewer": promotion["reviewer"],
            "rationale": promotion["rationale"],
            "policySha256": promotion["policy_sha256"],
            "validationRunId": promotion["validation_run_id"],
            "reportSha256": promotion["report_sha256"],
            "approvedAt": promotion["approved_at"],
        }

    def promotion_history(self, limit: int = 50) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 200))
        with self._connection() as db:
            rows = db.execute(
                """
                SELECT promotion_id, generation, model_id, previous_model_id,
                       artifact_sha256, validation_run_id, report_sha256,
                       policy_sha256, reviewer, rationale, approved_at
                FROM promotions ORDER BY generation DESC LIMIT ?
                """,
                (safe_limit,),
            ).fetchall()
        return [
            {
                "promotionId": row["promotion_id"],
                "generation": row["generation"],
                "modelId": row["model_id"],
                "previousModelId": row["previous_model_id"],
                "artifactSha256": row["artifact_sha256"],
                "validationRunId": row["validation_run_id"],
                "reportSha256": row["report_sha256"],
                "policySha256": row["policy_sha256"],
                "reviewer": row["reviewer"],
                "rationale": row["rationale"],
                "approvedAt": row["approved_at"],
            }
            for row in rows
        ]

    def promote(
        self,
        model_id: str,
        *,
        policy: PromotionPolicy,
        reviewer: str,
        rationale: str,
        confirmation: str,
        expected_generation: int,
        approved_at: datetime,
    ) -> ChampionSnapshot:
        if confirmation != PROMOTION_CONFIRMATION:
            raise PromotionDenied("manual promotion confirmation does not match")
        if not reviewer.strip() or len(reviewer.strip()) > 128:
            raise PromotionDenied("a bounded human reviewer identity is required")
        if len(rationale.strip()) < 16 or len(rationale.strip()) > 2_000:
            raise PromotionDenied("a review rationale of 16-2000 characters is required")
        approved = _iso(approved_at)
        with self._lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            state = db.execute(
                "SELECT champion_model_id, generation FROM registry_state WHERE singleton = 1"
            ).fetchone()
            if state is None or int(state["generation"]) != expected_generation:
                raise PromotionDenied("registry generation changed during review")
            previous_model_id = state["champion_model_id"]
            if previous_model_id == model_id:
                raise PromotionDenied("model is already the active champion")
            row = db.execute(
                "SELECT * FROM model_candidates WHERE model_id = ?", (model_id,)
            ).fetchone()
            if row is None or row["state"] != "validated":
                raise PromotionDenied("only a validated candidate can be promoted")
            try:
                bundle = self._bundle_from_row(row)
            except (ModelArtifactError, RegistryError) as exc:
                raise PromotionDenied("candidate artifact integrity check failed") from exc
            if bundle.schema_version != MODEL_SCHEMA_VERSION:
                raise PromotionDenied("legacy model artifacts cannot receive a new promotion")
            validation = db.execute(
                "SELECT * FROM validation_reports WHERE model_id = ?", (model_id,)
            ).fetchone()
            if validation is None:
                raise PromotionDenied("candidate has no bound validation report")
            report_json = str(validation["report_json"])
            if not hmac.compare_digest(sha256_hex(report_json), str(validation["report_sha256"])):
                raise PromotionDenied("validation report hash mismatch")
            try:
                report = ValidationReport.from_dict(json.loads(report_json))
            except (json.JSONDecodeError, ValidationError) as exc:
                raise PromotionDenied("validation report is invalid") from exc
            if report.report_sha256 != validation["report_sha256"]:
                raise PromotionDenied("validation report is not canonical")
            failures = policy.failures(report)
            if failures:
                raise PromotionDenied("promotion gates failed: " + ", ".join(failures))

            next_generation = expected_generation + 1
            if previous_model_id:
                db.execute(
                    "UPDATE model_candidates SET state = 'retired' WHERE model_id = ? AND state = 'champion'",
                    (previous_model_id,),
                )
            updated = db.execute(
                "UPDATE model_candidates SET state = 'champion' WHERE model_id = ? AND state = 'validated'",
                (model_id,),
            )
            if updated.rowcount != 1:
                raise PromotionDenied("candidate state changed during promotion")
            state_update = db.execute(
                """
                UPDATE registry_state
                SET champion_model_id = ?, generation = ?, updated_at = ?
                WHERE singleton = 1 AND generation = ?
                """,
                (model_id, next_generation, approved, expected_generation),
            )
            if state_update.rowcount != 1:
                raise PromotionDenied("registry generation changed during promotion")
            promotion_material = canonical_json(
                {
                    "approved_at": approved,
                    "artifact_sha256": bundle.artifact_sha256,
                    "generation": next_generation,
                    "model_id": model_id,
                    "policy_sha256": policy.policy_sha256,
                    "previous_model_id": previous_model_id,
                    "report_sha256": report.report_sha256,
                    "reviewer": reviewer.strip(),
                }
            )
            promotion_id = f"prom_{hashlib.sha256(promotion_material.encode()).hexdigest()[:24]}"
            db.execute(
                """
                INSERT INTO promotions
                (promotion_id, generation, model_id, previous_model_id, artifact_sha256,
                 validation_run_id, report_sha256, policy_sha256, reviewer, rationale, approved_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    promotion_id,
                    next_generation,
                    model_id,
                    previous_model_id,
                    bundle.artifact_sha256,
                    report.validation_run_id,
                    report.report_sha256,
                    policy.policy_sha256,
                    reviewer.strip(),
                    rationale.strip(),
                    approved,
                ),
            )
        return ChampionSnapshot(
            model_id=model_id,
            artifact_sha256=bundle.artifact_sha256,
            generation=next_generation,
            bundle=bundle,
        )

    def load_champion(self) -> ChampionSnapshot | None:
        with self._connection() as db:
            state = db.execute(
                "SELECT champion_model_id, generation FROM registry_state WHERE singleton = 1"
            ).fetchone()
            if state is None or state["champion_model_id"] is None:
                return None
            row = db.execute(
                "SELECT * FROM model_candidates WHERE model_id = ? AND state = 'champion'",
                (state["champion_model_id"],),
            ).fetchone()
        if row is None:
            raise RegistryError("champion state does not resolve to one frozen model")
        bundle = self._bundle_from_row(row)
        return ChampionSnapshot(
            model_id=bundle.model_id,
            artifact_sha256=bundle.artifact_sha256,
            generation=int(state["generation"]),
            bundle=bundle,
        )

    def load_model(self, model_id: str) -> FrozenModelBundle:
        """Load any registered data-only model for shadow evaluation.

        This never executes pickle/joblib code and revalidates the canonical
        artifact and manifest on every load.
        """

        if not model_id.startswith("mdl_") or len(model_id) != 28:
            raise RegistryError("model identity is invalid")
        with self._connection() as db:
            row = db.execute(
                "SELECT * FROM model_candidates WHERE model_id = ?", (model_id,)
            ).fetchone()
        if row is None:
            raise RegistryError("model does not exist")
        return self._bundle_from_row(row)

    def reject_candidate(self, model_id: str) -> None:
        """Atomically retire a non-executable candidate from future shadow work."""

        with self._lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            updated = db.execute(
                """
                UPDATE model_candidates SET state = 'rejected'
                WHERE model_id = ? AND state IN ('candidate', 'validated')
                """,
                (model_id,),
            )
            if updated.rowcount != 1:
                raise RegistryError("only a non-executable candidate can be rejected")

    def rollback_to(
        self,
        model_id: str,
        *,
        policy: PromotionPolicy,
        reviewer: str,
        rationale: str,
        expected_generation: int,
        approved_at: datetime,
    ) -> ChampionSnapshot:
        """Reactivate a previously promoted model with a new generation CAS."""

        if not reviewer.strip() or len(reviewer.strip()) > 128:
            raise PromotionDenied("a bounded rollback reviewer identity is required")
        if len(rationale.strip()) < 16 or len(rationale.strip()) > 2_000:
            raise PromotionDenied("a rollback rationale of 16-2000 characters is required")
        approved = _iso(approved_at)
        with self._lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            state = db.execute(
                "SELECT champion_model_id, generation FROM registry_state WHERE singleton = 1"
            ).fetchone()
            if state is None or int(state["generation"]) != expected_generation:
                raise PromotionDenied("registry generation changed during rollback")
            current_model_id = state["champion_model_id"]
            if not current_model_id or current_model_id == model_id:
                raise PromotionDenied("rollback target is not a previous champion")
            prior_promotion = db.execute(
                "SELECT 1 FROM promotions WHERE model_id = ? LIMIT 1", (model_id,)
            ).fetchone()
            row = db.execute(
                "SELECT * FROM model_candidates WHERE model_id = ?", (model_id,)
            ).fetchone()
            if prior_promotion is None or row is None or row["state"] != "retired":
                raise PromotionDenied("rollback target is not a retired verified champion")
            try:
                bundle = self._bundle_from_row(row)
            except (ModelArtifactError, RegistryError) as exc:
                raise PromotionDenied("rollback artifact integrity check failed") from exc
            validation = db.execute(
                "SELECT * FROM validation_reports WHERE model_id = ?", (model_id,)
            ).fetchone()
            if validation is None:
                raise PromotionDenied("rollback target has no validation report")
            report_json = str(validation["report_json"])
            if not hmac.compare_digest(
                sha256_hex(report_json), str(validation["report_sha256"])
            ):
                raise PromotionDenied("rollback validation report hash mismatch")
            try:
                report = ValidationReport.from_dict(json.loads(report_json))
            except (json.JSONDecodeError, ValidationError) as exc:
                raise PromotionDenied("rollback validation report is invalid") from exc
            failures = policy.failures(report)
            if failures:
                raise PromotionDenied(
                    "rollback model no longer passes gates: " + ", ".join(failures)
                )
            next_generation = expected_generation + 1
            retired = db.execute(
                "UPDATE model_candidates SET state = 'retired' WHERE model_id = ? AND state = 'champion'",
                (current_model_id,),
            )
            activated = db.execute(
                "UPDATE model_candidates SET state = 'champion' WHERE model_id = ? AND state = 'retired'",
                (model_id,),
            )
            if retired.rowcount != 1 or activated.rowcount != 1:
                raise PromotionDenied("champion state changed during rollback")
            state_update = db.execute(
                """
                UPDATE registry_state
                SET champion_model_id = ?, generation = ?, updated_at = ?
                WHERE singleton = 1 AND generation = ?
                """,
                (model_id, next_generation, approved, expected_generation),
            )
            if state_update.rowcount != 1:
                raise PromotionDenied("registry generation changed during rollback")
            material = canonical_json(
                {
                    "approved_at": approved,
                    "artifact_sha256": bundle.artifact_sha256,
                    "generation": next_generation,
                    "model_id": model_id,
                    "policy_sha256": policy.policy_sha256,
                    "previous_model_id": current_model_id,
                    "report_sha256": report.report_sha256,
                    "reviewer": reviewer.strip(),
                    "rollback": True,
                }
            )
            promotion_id = f"prom_{hashlib.sha256(material.encode()).hexdigest()[:24]}"
            db.execute(
                """
                INSERT INTO promotions
                (promotion_id, generation, model_id, previous_model_id, artifact_sha256,
                 validation_run_id, report_sha256, policy_sha256, reviewer, rationale, approved_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    promotion_id,
                    next_generation,
                    model_id,
                    current_model_id,
                    bundle.artifact_sha256,
                    report.validation_run_id,
                    report.report_sha256,
                    policy.policy_sha256,
                    reviewer.strip(),
                    rationale.strip(),
                    approved,
                ),
            )
        return ChampionSnapshot(
            model_id=model_id,
            artifact_sha256=bundle.artifact_sha256,
            generation=next_generation,
            bundle=bundle,
        )
