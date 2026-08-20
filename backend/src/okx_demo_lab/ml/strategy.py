from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Any, Mapping


DEMO_ENVIRONMENT = "okx_demo"
ALLOWED_INSTRUMENT = "BTC-USDT"
LEGACY_MODEL_SCHEMA_VERSION = "tideguard.linear-logit.v1"
MODEL_SCHEMA_VERSION = "tideguard.linear-logit.v2"
MAX_ARTIFACT_BYTES = 1_000_000
MAX_FEATURES = 256
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_FEATURE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")


class ModelArtifactError(ValueError):
    pass


class ProposalRejected(ValueError):
    pass


def canonical_json(value: Any) -> str:
    """Return the only JSON encoding used for hashes and frozen artifacts."""

    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ModelArtifactError("value is not canonical JSON") from exc


def sha256_hex(value: bytes | str) -> str:
    material = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(material).hexdigest()


def _require_hash(name: str, value: str) -> None:
    if not _HASH_RE.fullmatch(value):
        raise ModelArtifactError(f"{name} must be a lowercase sha256")


def _utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return _utc(value, "timestamp").isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_iso(value: Any, name: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ModelArtifactError(f"{name} must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ModelArtifactError(f"{name} must be an ISO timestamp") from exc
    try:
        return _utc(parsed, name)
    except ValueError as exc:
        raise ModelArtifactError(str(exc)) from exc


def feature_schema_hash(feature_names: tuple[str, ...]) -> str:
    return sha256_hex(canonical_json({"features": list(feature_names)}))


@dataclass(frozen=True)
class FrozenLinearModel:
    """A small, data-only model format; it never deserializes executable objects."""

    feature_names: tuple[str, ...]
    means: tuple[float, ...]
    scales: tuple[float, ...]
    coefficients: tuple[float, ...]
    intercept: float
    buy_threshold: float = 0.60
    sell_threshold: float = 0.40

    def __post_init__(self) -> None:
        width = len(self.feature_names)
        if not 1 <= width <= MAX_FEATURES:
            raise ModelArtifactError("model must contain between 1 and 256 features")
        if len(set(self.feature_names)) != width or any(
            not _FEATURE_RE.fullmatch(name) for name in self.feature_names
        ):
            raise ModelArtifactError("feature names must be unique safe identifiers")
        if any(len(values) != width for values in (self.means, self.scales, self.coefficients)):
            raise ModelArtifactError("model vectors must match feature_names")
        numbers = (*self.means, *self.scales, *self.coefficients, self.intercept)
        if any(not math.isfinite(float(value)) for value in numbers):
            raise ModelArtifactError("model contains a non-finite number")
        if any(float(scale) <= 0 for scale in self.scales):
            raise ModelArtifactError("feature scales must be positive")
        if not (
            0.0 < float(self.sell_threshold) < 0.5 < float(self.buy_threshold) < 1.0
        ):
            raise ModelArtifactError("signal thresholds must straddle 0.5")

    def score(self, features: Mapping[str, float]) -> float:
        if set(features) != set(self.feature_names):
            raise ProposalRejected("runtime features do not exactly match the frozen schema")
        linear = float(self.intercept)
        for name, mean, scale, coefficient in zip(
            self.feature_names,
            self.means,
            self.scales,
            self.coefficients,
            strict=True,
        ):
            value = float(features[name])
            if not math.isfinite(value):
                raise ProposalRejected("runtime features contain a non-finite value")
            linear += ((value - float(mean)) / float(scale)) * float(coefficient)
        if linear >= 0:
            exp_value = math.exp(-min(linear, 700.0))
            return 1.0 / (1.0 + exp_value)
        exp_value = math.exp(max(linear, -700.0))
        return exp_value / (1.0 + exp_value)

    def action(self, features: Mapping[str, float]) -> tuple[str, float]:
        score = self.score(features)
        if score >= self.buy_threshold:
            return "buy", score
        if score <= self.sell_threshold:
            return "sell", score
        return "hold", score

    def to_dict(self) -> dict[str, Any]:
        return {
            "buy_threshold": self.buy_threshold,
            "coefficients": list(self.coefficients),
            "feature_names": list(self.feature_names),
            "intercept": self.intercept,
            "means": list(self.means),
            "scales": list(self.scales),
            "sell_threshold": self.sell_threshold,
        }

    @classmethod
    def from_dict(cls, value: Any) -> FrozenLinearModel:
        expected = {
            "buy_threshold",
            "coefficients",
            "feature_names",
            "intercept",
            "means",
            "scales",
            "sell_threshold",
        }
        if not isinstance(value, dict) or set(value) != expected:
            raise ModelArtifactError("model object has missing or unexpected fields")
        if (
            not isinstance(value["feature_names"], list)
            or any(not isinstance(item, str) for item in value["feature_names"])
            or any(
                not isinstance(value[key], list)
                or any(
                    isinstance(item, bool) or not isinstance(item, (int, float))
                    for item in value[key]
                )
                for key in ("means", "scales", "coefficients")
            )
            or any(
                isinstance(value[key], bool) or not isinstance(value[key], (int, float))
                for key in ("intercept", "buy_threshold", "sell_threshold")
            )
        ):
            raise ModelArtifactError("model object contains invalid value types")
        try:
            return cls(
                feature_names=tuple(str(item) for item in value["feature_names"]),
                means=tuple(float(item) for item in value["means"]),
                scales=tuple(float(item) for item in value["scales"]),
                coefficients=tuple(float(item) for item in value["coefficients"]),
                intercept=float(value["intercept"]),
                buy_threshold=float(value["buy_threshold"]),
                sell_threshold=float(value["sell_threshold"]),
            )
        except ModelArtifactError:
            raise
        except (TypeError, ValueError, OverflowError) as exc:
            raise ModelArtifactError("model object contains invalid values") from exc


@dataclass(frozen=True)
class ModelManifest:
    dataset_sha256: str
    training_config_sha256: str
    validation_run_id: str
    code_revision: str
    trained_from: datetime
    trained_through: datetime
    created_at: datetime
    trainer: str
    random_seed: int
    feature_schema_sha256: str
    model_family: str = "linear_logit"
    fit_dataset_sha256: str | None = None
    fit_rows: int | None = None
    benchmark_cohort_id: str | None = None
    market_snapshot_sha256: str | None = None
    split_protocol_sha256: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "dataset_sha256",
            "training_config_sha256",
            "feature_schema_sha256",
        ):
            _require_hash(name, str(getattr(self, name)))
        start = _utc(self.trained_from, "trained_from")
        end = _utc(self.trained_through, "trained_through")
        created = _utc(self.created_at, "created_at")
        if start >= end or end > created:
            raise ModelArtifactError("manifest training timestamps are inconsistent")
        if not self.validation_run_id.strip() or len(self.validation_run_id) > 128:
            raise ModelArtifactError("validation_run_id is required")
        if not self.code_revision.strip() or len(self.code_revision) > 128:
            raise ModelArtifactError("code_revision is required")
        if not self.trainer.strip() or len(self.trainer) > 128:
            raise ModelArtifactError("trainer is required")
        if self.model_family != "linear_logit":
            raise ModelArtifactError("only the data-only linear_logit family is supported")
        if (
            isinstance(self.random_seed, bool)
            or not isinstance(self.random_seed, int)
            or not 0 <= self.random_seed <= 2**32 - 1
        ):
            raise ModelArtifactError("random_seed is outside the supported range")
        v2_present = any(
            value is not None
            for value in (
                self.fit_dataset_sha256,
                self.fit_rows,
                self.benchmark_cohort_id,
                self.market_snapshot_sha256,
                self.split_protocol_sha256,
            )
        )
        if v2_present:
            if self.fit_dataset_sha256 is None or self.fit_rows is None:
                raise ModelArtifactError("v2 manifest requires a bound final-fit dataset")
            _require_hash("fit_dataset_sha256", self.fit_dataset_sha256)
            if isinstance(self.fit_rows, bool) or self.fit_rows < 2:
                raise ModelArtifactError("v2 manifest final-fit row count is invalid")
            cohort_values = (
                self.benchmark_cohort_id,
                self.market_snapshot_sha256,
                self.split_protocol_sha256,
            )
            if any(value is not None for value in cohort_values) and not all(
                value is not None for value in cohort_values
            ):
                raise ModelArtifactError("v2 manifest cohort binding is incomplete")
            if self.benchmark_cohort_id is not None:
                suffix = self.benchmark_cohort_id.removeprefix("cohort_")
                if (
                    not self.benchmark_cohort_id.startswith("cohort_")
                    or not 8 <= len(suffix) <= 64
                    or any(character not in "0123456789abcdef" for character in suffix)
                ):
                    raise ModelArtifactError("v2 manifest cohort ID is invalid")
                _require_hash("market_snapshot_sha256", str(self.market_snapshot_sha256))
                _require_hash("split_protocol_sha256", str(self.split_protocol_sha256))

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "code_revision": self.code_revision,
            "created_at": _iso(self.created_at),
            "dataset_sha256": self.dataset_sha256,
            "feature_schema_sha256": self.feature_schema_sha256,
            "model_family": self.model_family,
            "random_seed": self.random_seed,
            "trained_from": _iso(self.trained_from),
            "trained_through": _iso(self.trained_through),
            "trainer": self.trainer,
            "training_config_sha256": self.training_config_sha256,
            "validation_run_id": self.validation_run_id,
        }
        if self.fit_dataset_sha256 is not None:
            value.update(
                {
                    "benchmark_cohort_id": self.benchmark_cohort_id,
                    "fit_dataset_sha256": self.fit_dataset_sha256,
                    "fit_rows": self.fit_rows,
                    "market_snapshot_sha256": self.market_snapshot_sha256,
                    "split_protocol_sha256": self.split_protocol_sha256,
                }
            )
        return value

    @classmethod
    def from_dict(cls, value: Any) -> ModelManifest:
        legacy_expected = {
            "code_revision",
            "created_at",
            "dataset_sha256",
            "feature_schema_sha256",
            "model_family",
            "random_seed",
            "trained_from",
            "trained_through",
            "trainer",
            "training_config_sha256",
            "validation_run_id",
        }
        v2_expected = legacy_expected | {
            "benchmark_cohort_id",
            "fit_dataset_sha256",
            "fit_rows",
            "market_snapshot_sha256",
            "split_protocol_sha256",
        }
        fields = frozenset(value) if isinstance(value, dict) else frozenset()
        if not isinstance(value, dict) or fields not in {
            frozenset(legacy_expected),
            frozenset(v2_expected),
        }:
            raise ModelArtifactError("manifest has missing or unexpected fields")
        is_v2 = set(value) == v2_expected
        if isinstance(value["random_seed"], bool) or not isinstance(value["random_seed"], int):
            raise ModelArtifactError("manifest random_seed must be an integer")
        if is_v2 and (
            isinstance(value["fit_rows"], bool)
            or not isinstance(value["fit_rows"], int)
        ):
            raise ModelArtifactError("manifest fit_rows must be an integer")
        if any(
            not isinstance(value[key], str)
            for key in legacy_expected
            if key != "random_seed"
        ):
            raise ModelArtifactError("manifest fields have invalid value types")
        if is_v2:
            if not isinstance(value["fit_dataset_sha256"], str):
                raise ModelArtifactError("manifest fit dataset hash is invalid")
            for key in (
                "benchmark_cohort_id",
                "market_snapshot_sha256",
                "split_protocol_sha256",
            ):
                if value[key] is not None and not isinstance(value[key], str):
                    raise ModelArtifactError("manifest cohort fields have invalid value types")
        try:
            return cls(
                dataset_sha256=str(value["dataset_sha256"]),
                training_config_sha256=str(value["training_config_sha256"]),
                validation_run_id=str(value["validation_run_id"]),
                code_revision=str(value["code_revision"]),
                trained_from=_parse_iso(value["trained_from"], "trained_from"),
                trained_through=_parse_iso(value["trained_through"], "trained_through"),
                created_at=_parse_iso(value["created_at"], "created_at"),
                trainer=str(value["trainer"]),
                random_seed=int(value["random_seed"]),
                feature_schema_sha256=str(value["feature_schema_sha256"]),
                model_family=str(value["model_family"]),
                fit_dataset_sha256=(
                    str(value["fit_dataset_sha256"]) if is_v2 else None
                ),
                fit_rows=int(value["fit_rows"]) if is_v2 else None,
                benchmark_cohort_id=(value["benchmark_cohort_id"] if is_v2 else None),
                market_snapshot_sha256=(
                    value["market_snapshot_sha256"] if is_v2 else None
                ),
                split_protocol_sha256=(
                    value["split_protocol_sha256"] if is_v2 else None
                ),
            )
        except ModelArtifactError:
            raise
        except (TypeError, ValueError, OverflowError) as exc:
            raise ModelArtifactError("manifest contains invalid values") from exc


@dataclass(frozen=True)
class FrozenModelBundle:
    manifest: ModelManifest
    model: FrozenLinearModel
    schema_version: str | None = None

    def __post_init__(self) -> None:
        expected = feature_schema_hash(self.model.feature_names)
        if not hmac.compare_digest(expected, self.manifest.feature_schema_sha256):
            raise ModelArtifactError("manifest feature schema does not match the model")
        inferred = (
            MODEL_SCHEMA_VERSION
            if self.manifest.fit_dataset_sha256 is not None
            else LEGACY_MODEL_SCHEMA_VERSION
        )
        if self.schema_version is None:
            object.__setattr__(self, "schema_version", inferred)
        elif self.schema_version not in {
            LEGACY_MODEL_SCHEMA_VERSION,
            MODEL_SCHEMA_VERSION,
        }:
            raise ModelArtifactError("unsupported artifact schema version")
        if self.schema_version != inferred:
            raise ModelArtifactError("artifact schema and manifest fields do not match")

    def to_bytes(self) -> bytes:
        return canonical_json(
            {
                "manifest": self.manifest.to_dict(),
                "model": self.model.to_dict(),
                "schema_version": self.schema_version,
            }
        ).encode("utf-8")

    @property
    def artifact_sha256(self) -> str:
        return sha256_hex(self.to_bytes())

    @property
    def model_id(self) -> str:
        return f"mdl_{self.artifact_sha256[:24]}"

    @classmethod
    def from_bytes(
        cls, raw: bytes, *, expected_sha256: str | None = None
    ) -> FrozenModelBundle:
        if not isinstance(raw, bytes) or not 1 <= len(raw) <= MAX_ARTIFACT_BYTES:
            raise ModelArtifactError("artifact size is invalid")
        actual_hash = sha256_hex(raw)
        if expected_sha256 is not None:
            _require_hash("expected_sha256", expected_sha256)
            if not hmac.compare_digest(actual_hash, expected_sha256):
                raise ModelArtifactError("artifact hash mismatch")
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ModelArtifactError("artifact is not valid UTF-8 JSON") from exc
        if not isinstance(value, dict) or set(value) != {
            "manifest",
            "model",
            "schema_version",
        }:
            raise ModelArtifactError("artifact has missing or unexpected fields")
        if value["schema_version"] not in {
            LEGACY_MODEL_SCHEMA_VERSION,
            MODEL_SCHEMA_VERSION,
        }:
            raise ModelArtifactError("unsupported artifact schema version")
        bundle = cls(
            manifest=ModelManifest.from_dict(value["manifest"]),
            model=FrozenLinearModel.from_dict(value["model"]),
            schema_version=str(value["schema_version"]),
        )
        if bundle.to_bytes() != raw:
            raise ModelArtifactError("artifact is not in canonical form")
        return bundle


@dataclass(frozen=True)
class DemoStrategyPolicy:
    fixed_notional_usdt: Decimal = Decimal("10")
    max_signal_age_seconds: int = 330
    max_market_age_seconds: int = 8
    order_type: str = "post_only"

    def __post_init__(self) -> None:
        if not Decimal("0") < self.fixed_notional_usdt <= Decimal("25"):
            raise ValueError("fixed demo notional must be in (0, 25] USDT")
        if not 1 <= self.max_market_age_seconds <= 8:
            raise ValueError("market age cannot exceed the hard 8 second limit")
        if not 1 <= self.max_signal_age_seconds <= 600:
            raise ValueError("signal age cannot exceed 600 seconds")
        if self.order_type not in {"limit", "post_only", "ioc"}:
            raise ValueError("only limit, post_only and ioc demo orders are supported")

    @property
    def policy_sha256(self) -> str:
        return sha256_hex(
            canonical_json(
                {
                    "environment": DEMO_ENVIRONMENT,
                    "fixed_notional_usdt": str(self.fixed_notional_usdt),
                    "instrument": ALLOWED_INSTRUMENT,
                    "max_market_age_seconds": self.max_market_age_seconds,
                    "max_signal_age_seconds": self.max_signal_age_seconds,
                    "order_type": self.order_type,
                }
            )
        )


@dataclass(frozen=True)
class MarketSnapshot:
    observed_at: datetime
    candle_closed_at: datetime
    candle_confirmed: bool
    instrument: str
    bid: Decimal
    ask: Decimal
    tick_size: Decimal
    lot_size: Decimal
    min_size: Decimal

    def __post_init__(self) -> None:
        observed = _utc(self.observed_at, "observed_at")
        closed = _utc(self.candle_closed_at, "candle_closed_at")
        if closed > observed:
            raise ProposalRejected("candle close cannot be in the future")
        if not isinstance(self.candle_confirmed, bool):
            raise ProposalRejected("candle_confirmed must be boolean")
        if self.instrument != ALLOWED_INSTRUMENT:
            raise ProposalRejected("only BTC-USDT is supported")
        values = (self.bid, self.ask, self.tick_size, self.lot_size, self.min_size)
        if any(not isinstance(value, Decimal) for value in values):
            raise ProposalRejected("market prices and instrument steps must use Decimal")
        if any(not value.is_finite() or value <= 0 for value in values):
            raise ProposalRejected("market prices and instrument steps must be positive")
        if self.bid > self.ask:
            raise ProposalRejected("market bid cannot exceed ask")


@dataclass(frozen=True)
class OrderProposal:
    signal_id: str
    idempotency_key: str
    model_id: str
    artifact_sha256: str
    policy_sha256: str
    evidence_sha256: str
    observed_at: datetime
    candle_closed_at: datetime
    instrument: str
    side: str
    order_type: str
    price: Decimal
    size: Decimal
    score: float
    environment: str = DEMO_ENVIRONMENT

    @property
    def notional_usdt(self) -> Decimal:
        return self.price * self.size


def _aligned_price(value: Decimal, step: Decimal, *, round_up: bool) -> Decimal:
    rounding = ROUND_CEILING if round_up else ROUND_FLOOR
    units = (value / step).to_integral_value(rounding=rounding)
    return units * step


def _aligned_size(value: Decimal, step: Decimal) -> Decimal:
    units = (value / step).to_integral_value(rounding=ROUND_FLOOR)
    return units * step


def build_order_proposal(
    bundle: FrozenModelBundle,
    *,
    features: Mapping[str, float],
    market: MarketSnapshot,
    policy: DemoStrategyPolicy,
    now: datetime,
) -> OrderProposal | None:
    """Convert a frozen-model signal into a bounded proposal, never an exchange call."""

    now_utc = _utc(now, "now")
    observed = _utc(market.observed_at, "observed_at")
    closed = _utc(market.candle_closed_at, "candle_closed_at")
    market_age = (now_utc - observed).total_seconds()
    signal_age = (now_utc - closed).total_seconds()
    if market_age < 0 or market_age > policy.max_market_age_seconds:
        raise ProposalRejected("market snapshot is stale or from the future")
    if signal_age < 0 or signal_age > policy.max_signal_age_seconds:
        raise ProposalRejected("completed candle is stale or from the future")
    if not market.candle_confirmed:
        raise ProposalRejected("signals require an exchange-confirmed completed candle")

    side, score = bundle.model.action(features)
    if side == "hold":
        return None
    if side == "sell":
        raise ProposalRejected(
            "v0.2 model automation is BUY-entry-only; automatic SELL/exit is not implemented"
        )
    raw_price = market.bid if side == "buy" else market.ask
    price = _aligned_price(raw_price, market.tick_size, round_up=side == "sell")
    size = _aligned_size(policy.fixed_notional_usdt / price, market.lot_size)
    if size < market.min_size or size <= 0:
        raise ProposalRejected("fixed notional is below the current minimum order size")
    notional = price * size
    if notional <= 0 or notional > policy.fixed_notional_usdt or notional > Decimal("25"):
        raise ProposalRejected("proposal exceeds the frozen demo sizing policy")

    normalized_features = {name: float(features[name]) for name in bundle.model.feature_names}
    evidence_sha = sha256_hex(
        canonical_json(
            {
                "artifact_sha256": bundle.artifact_sha256,
                "candle_closed_at": _iso(closed),
                "features": normalized_features,
                "instrument": market.instrument,
                "market_observed_at": _iso(observed),
                "policy_sha256": policy.policy_sha256,
                "score": score,
                "side": side,
            }
        )
    )
    signal_sha = sha256_hex(
        canonical_json(
            {
                "artifact_sha256": bundle.artifact_sha256,
                "candle_closed_at": _iso(closed),
                "evidence_sha256": evidence_sha,
                "instrument": market.instrument,
            }
        )
    )
    return OrderProposal(
        signal_id=f"sig_{signal_sha[:32]}",
        idempotency_key=f"tgml-{signal_sha}",
        model_id=bundle.model_id,
        artifact_sha256=bundle.artifact_sha256,
        policy_sha256=policy.policy_sha256,
        evidence_sha256=evidence_sha,
        observed_at=observed,
        candle_closed_at=closed,
        instrument=market.instrument,
        side=side,
        order_type=policy.order_type,
        price=price,
        size=size,
        score=score,
    )
