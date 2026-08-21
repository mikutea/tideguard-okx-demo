from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

from .strategy import canonical_json, sha256_hex


ALTERNATIVE_DATA_SCHEMA_VERSION = "moheng.public-text-event.v1"
SENTIMENT_FEATURE_SCHEMA_VERSION = "moheng.sentiment-features.v1"


class AlternativeDataError(ValueError):
    pass


def _utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise AlternativeDataError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return _utc(value, "timestamp").isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


@dataclass(frozen=True)
class SourcePolicy:
    source_id: str
    license_id: str
    headline_storage_allowed: bool
    full_text_storage_allowed: bool
    redistribution_allowed: bool
    reliability_weight: float

    def __post_init__(self) -> None:
        if (
            not self.source_id
            or len(self.source_id) > 128
            or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789-_" for character in self.source_id)
        ):
            raise AlternativeDataError("alternative-data source ID is invalid")
        if not self.license_id or len(self.license_id) > 128:
            raise AlternativeDataError("alternative-data license ID is invalid")
        if not all(
            isinstance(value, bool)
            for value in (
                self.headline_storage_allowed,
                self.full_text_storage_allowed,
                self.redistribution_allowed,
            )
        ):
            raise AlternativeDataError("alternative-data usage flags must be boolean")
        if not math.isfinite(self.reliability_weight) or not 0.0 < self.reliability_weight <= 1.0:
            raise AlternativeDataError("alternative-data reliability weight is invalid")

    def to_dict(self) -> dict[str, Any]:
        return {
            "fullTextStorageAllowed": self.full_text_storage_allowed,
            "headlineStorageAllowed": self.headline_storage_allowed,
            "licenseId": self.license_id,
            "redistributionAllowed": self.redistribution_allowed,
            "reliabilityWeight": self.reliability_weight,
            "sourceId": self.source_id,
        }

    @property
    def sha256(self) -> str:
        return sha256_hex(canonical_json(self.to_dict()))


@dataclass(frozen=True)
class PublicTextEvent:
    source_id: str
    source_event_id: str
    asset: str
    published_at: datetime
    first_seen_at: datetime
    fetched_at: datetime
    headline: str
    url: str
    language: str
    revision: int = 0
    historical_backfill: bool = False

    def __post_init__(self) -> None:
        published = _utc(self.published_at, "published_at")
        first_seen = _utc(self.first_seen_at, "first_seen_at")
        fetched = _utc(self.fetched_at, "fetched_at")
        if published > fetched + timedelta(minutes=5):
            raise AlternativeDataError("published_at is implausibly after fetched_at")
        if first_seen > fetched + timedelta(seconds=5):
            raise AlternativeDataError("first_seen_at is after fetched_at")
        if not self.source_id or not self.source_event_id or len(self.source_event_id) > 512:
            raise AlternativeDataError("alternative-data event identity is invalid")
        if not self.asset or len(self.asset) > 32 or self.asset.upper() != self.asset:
            raise AlternativeDataError("alternative-data asset is invalid")
        normalized_headline = " ".join(self.headline.split())
        if not normalized_headline or len(normalized_headline) > 2_000:
            raise AlternativeDataError("alternative-data headline is invalid")
        if not self.url.startswith(("https://", "http://")) or len(self.url) > 4_096:
            raise AlternativeDataError("alternative-data URL is invalid")
        if not self.language or len(self.language) > 16:
            raise AlternativeDataError("alternative-data language is invalid")
        if isinstance(self.revision, bool) or not 0 <= self.revision <= 1_000_000:
            raise AlternativeDataError("alternative-data revision is invalid")
        if not isinstance(self.historical_backfill, bool):
            raise AlternativeDataError("historical_backfill must be boolean")
        object.__setattr__(self, "headline", normalized_headline)

    @property
    def available_at(self) -> datetime:
        # We never trust an old publisher timestamp to mean the system really
        # observed the article at that historical instant.
        return max(_utc(self.published_at, "published_at"), _utc(self.first_seen_at, "first_seen_at"))

    @property
    def prospective_eligible(self) -> bool:
        return not self.historical_backfill

    def to_dict(self) -> dict[str, Any]:
        return {
            "asset": self.asset,
            "availableAt": _iso(self.available_at),
            "fetchedAt": _iso(self.fetched_at),
            "firstSeenAt": _iso(self.first_seen_at),
            "headline": self.headline,
            "historicalBackfill": self.historical_backfill,
            "language": self.language,
            "publishedAt": _iso(self.published_at),
            "revision": self.revision,
            "schemaVersion": ALTERNATIVE_DATA_SCHEMA_VERSION,
            "sourceEventId": self.source_event_id,
            "sourceId": self.source_id,
            "url": self.url,
        }

    @property
    def sha256(self) -> str:
        return sha256_hex(canonical_json(self.to_dict()))


@dataclass(frozen=True)
class SentimentScore:
    event_sha256: str
    model_sha256: str
    scored_at: datetime
    positive: float
    neutral: float
    negative: float
    asset_relevance: float

    def __post_init__(self) -> None:
        for name, value in (("event", self.event_sha256), ("model", self.model_sha256)):
            if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
                raise AlternativeDataError(f"sentiment {name} hash is invalid")
        _utc(self.scored_at, "scored_at")
        values = (self.positive, self.neutral, self.negative, self.asset_relevance)
        if any(not math.isfinite(value) or not 0.0 <= value <= 1.0 for value in values):
            raise AlternativeDataError("sentiment probabilities are invalid")
        if not math.isclose(self.positive + self.neutral + self.negative, 1.0, abs_tol=1e-6):
            raise AlternativeDataError("sentiment probabilities must sum to one")


@dataclass(frozen=True)
class SentimentFeatures:
    asset: str
    bar_closed_at: datetime
    lookback_seconds: int
    event_count: int
    source_count: int
    positive_mean: float
    negative_mean: float
    neutral_mean: float
    net_sentiment: float
    disagreement: float
    relevance_mean: float
    minutes_since_latest: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "asset": self.asset,
            "barClosedAt": _iso(self.bar_closed_at),
            "disagreement": self.disagreement,
            "eventCount": self.event_count,
            "lookbackSeconds": self.lookback_seconds,
            "minutesSinceLatest": self.minutes_since_latest,
            "negativeMean": self.negative_mean,
            "netSentiment": self.net_sentiment,
            "neutralMean": self.neutral_mean,
            "positiveMean": self.positive_mean,
            "relevanceMean": self.relevance_mean,
            "schemaVersion": SENTIMENT_FEATURE_SCHEMA_VERSION,
            "sourceCount": self.source_count,
        }


def sentiment_join_contract_sha256(
    source_policies: Sequence[SourcePolicy],
    *,
    lookback: timedelta,
    prospective_only: bool = True,
) -> str:
    if lookback <= timedelta(0) or lookback > timedelta(days=30):
        raise AlternativeDataError("sentiment feature lookback is invalid")
    if not isinstance(prospective_only, bool):
        raise AlternativeDataError("prospective_only must be boolean")
    policies = sorted((item.to_dict() for item in source_policies), key=lambda item: item["sourceId"])
    if len({item["sourceId"] for item in policies}) != len(policies):
        raise AlternativeDataError("alternative-data source policies are duplicated")
    return sha256_hex(
        canonical_json(
            {
                "availabilityRule": "max(published_at,first_seen_at)<=bar_closed_at",
                "deduplicationRule": "latest_visible_revision_per_source_event",
                "lookbackSeconds": round(lookback.total_seconds()),
                "missingnessRule": "explicit_zero_vector",
                "prospectiveOnly": prospective_only,
                "schemaVersion": SENTIMENT_FEATURE_SCHEMA_VERSION,
                "sourcePolicies": policies,
            }
        )
    )


def aggregate_sentiment_features(
    events: Sequence[PublicTextEvent],
    scores: Sequence[SentimentScore],
    source_policies: Sequence[SourcePolicy],
    *,
    asset: str,
    bar_closed_at: datetime,
    lookback: timedelta = timedelta(hours=24),
    prospective_only: bool = True,
) -> SentimentFeatures:
    """Build a lagged weak-signal vector without trusting backfilled timestamps."""

    closed = _utc(bar_closed_at, "bar_closed_at")
    if not asset or asset.upper() != asset:
        raise AlternativeDataError("sentiment feature asset is invalid")
    if lookback <= timedelta(0) or lookback > timedelta(days=30):
        raise AlternativeDataError("sentiment feature lookback is invalid")
    policies = {item.source_id: item for item in source_policies}
    if len(policies) != len(source_policies):
        raise AlternativeDataError("alternative-data source policies are duplicated")
    score_by_event = {item.event_sha256: item for item in scores}
    if len(score_by_event) != len(scores):
        raise AlternativeDataError("sentiment scores are duplicated")
    cutoff = closed - lookback
    selected: dict[tuple[str, str], tuple[PublicTextEvent, SentimentScore, SourcePolicy]] = {}
    for event in events:
        if event.asset != asset or event.available_at <= cutoff or event.available_at > closed:
            continue
        if _utc(event.fetched_at, "fetched_at") > closed:
            continue
        if prospective_only and not event.prospective_eligible:
            continue
        policy = policies.get(event.source_id)
        score = score_by_event.get(event.sha256)
        if policy is None or score is None or not policy.headline_storage_allowed:
            continue
        if _utc(score.scored_at, "scored_at") > closed:
            continue
        key = (event.source_id, event.source_event_id)
        current = selected.get(key)
        if current is None or event.revision > current[0].revision:
            selected[key] = (event, score, policy)
    if not selected:
        return SentimentFeatures(
            asset=asset,
            bar_closed_at=closed,
            lookback_seconds=round(lookback.total_seconds()),
            event_count=0,
            source_count=0,
            positive_mean=0.0,
            negative_mean=0.0,
            neutral_mean=0.0,
            net_sentiment=0.0,
            disagreement=0.0,
            relevance_mean=0.0,
            minutes_since_latest=float(lookback.total_seconds() / 60.0),
        )
    rows = list(selected.values())
    weights = [policy.reliability_weight * score.asset_relevance for _event, score, policy in rows]
    total_weight = sum(weights)
    if total_weight <= 0:
        raise AlternativeDataError("sentiment feature weights are zero")
    positive = sum(score.positive * weight for (_event, score, _policy), weight in zip(rows, weights, strict=True)) / total_weight
    negative = sum(score.negative * weight for (_event, score, _policy), weight in zip(rows, weights, strict=True)) / total_weight
    neutral = sum(score.neutral * weight for (_event, score, _policy), weight in zip(rows, weights, strict=True)) / total_weight
    nets = [score.positive - score.negative for _event, score, _policy in rows]
    net = positive - negative
    disagreement = math.sqrt(sum((value - net) ** 2 for value in nets) / len(nets))
    latest = max(event.available_at for event, _score, _policy in rows)
    return SentimentFeatures(
        asset=asset,
        bar_closed_at=closed,
        lookback_seconds=round(lookback.total_seconds()),
        event_count=len(rows),
        source_count=len({event.source_id for event, _score, _policy in rows}),
        positive_mean=positive,
        negative_mean=negative,
        neutral_mean=neutral,
        net_sentiment=net,
        disagreement=disagreement,
        relevance_mean=sum(score.asset_relevance for _event, score, _policy in rows) / len(rows),
        minutes_since_latest=(closed - latest).total_seconds() / 60.0,
    )


__all__ = [
    "ALTERNATIVE_DATA_SCHEMA_VERSION",
    "SENTIMENT_FEATURE_SCHEMA_VERSION",
    "AlternativeDataError",
    "PublicTextEvent",
    "SentimentFeatures",
    "SentimentScore",
    "SourcePolicy",
    "aggregate_sentiment_features",
    "sentiment_join_contract_sha256",
]
