from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from okx_demo_lab.ml.alternative_data import (
    AlternativeDataError,
    PublicTextEvent,
    SentimentScore,
    SourcePolicy,
    aggregate_sentiment_features,
    sentiment_join_contract_sha256,
)
from okx_demo_lab.ml.strategy import canonical_json, sha256_hex


BAR = datetime(2026, 8, 21, 1, 0, tzinfo=timezone.utc)
MODEL_HASH = "a" * 64
SOURCE_REGISTRY_SHA256 = "39dd69486cd3b7349eb2e42d49d476e4de7bde235f48d2a7a88b40872c87178f"


def _event(
    event_id: str,
    *,
    minutes_before: int = 10,
    fetched_minutes_before: int = 9,
    revision: int = 0,
    backfill: bool = False,
    source: str = "official-rss",
) -> PublicTextEvent:
    published = BAR - timedelta(minutes=minutes_before)
    return PublicTextEvent(
        source_id=source,
        source_event_id=event_id,
        asset="BTC",
        published_at=published,
        first_seen_at=published + timedelta(seconds=5),
        fetched_at=BAR - timedelta(minutes=fetched_minutes_before),
        headline=f"  Event   {event_id}  ",
        url=f"https://example.test/{event_id}",
        language="en",
        revision=revision,
        historical_backfill=backfill,
    )


def _score(event: PublicTextEvent, positive: float, negative: float) -> SentimentScore:
    return SentimentScore(
        event_sha256=event.sha256,
        model_sha256=MODEL_HASH,
        scored_at=event.fetched_at,
        positive=positive,
        neutral=1.0 - positive - negative,
        negative=negative,
        asset_relevance=0.8,
    )


def _policy(source: str = "official-rss") -> SourcePolicy:
    return SourcePolicy(source, "source-terms-v1", True, False, False, 0.8)


def test_sentiment_features_exclude_backfill_future_fetch_and_future_score() -> None:
    valid = _event("valid")
    backfill = _event("backfill", backfill=True)
    future_fetch = _event("future-fetch", fetched_minutes_before=-1)
    future_score_event = _event("future-score")
    future_score = SentimentScore(
        event_sha256=future_score_event.sha256,
        model_sha256=MODEL_HASH,
        scored_at=BAR + timedelta(seconds=1),
        positive=0.8,
        neutral=0.1,
        negative=0.1,
        asset_relevance=1.0,
    )

    features = aggregate_sentiment_features(
        [valid, backfill, future_fetch, future_score_event],
        [_score(valid, 0.7, 0.1), _score(backfill, 0.9, 0.0), _score(future_fetch, 0.9, 0.0), future_score],
        [_policy()],
        asset="BTC",
        bar_closed_at=BAR,
    )

    assert features.event_count == 1
    assert features.net_sentiment == pytest.approx(0.6)
    assert features.minutes_since_latest == pytest.approx(10 - 5 / 60)


def test_sentiment_features_choose_latest_revision_visible_before_bar() -> None:
    first = _event("same", revision=0)
    second = _event("same", revision=1, minutes_before=5, fetched_minutes_before=4)
    features = aggregate_sentiment_features(
        [first, second],
        [_score(first, 0.1, 0.7), _score(second, 0.8, 0.1)],
        [_policy()],
        asset="BTC",
        bar_closed_at=BAR,
    )
    assert features.event_count == 1
    assert features.net_sentiment == pytest.approx(0.7)


def test_historical_backfill_is_available_only_for_explicit_research() -> None:
    event = _event("old", backfill=True)
    score = _score(event, 0.7, 0.2)
    prospective = aggregate_sentiment_features(
        [event], [score], [_policy()], asset="BTC", bar_closed_at=BAR
    )
    retrospective = aggregate_sentiment_features(
        [event],
        [score],
        [_policy()],
        asset="BTC",
        bar_closed_at=BAR,
        prospective_only=False,
    )
    assert prospective.event_count == 0
    assert retrospective.event_count == 1


def test_text_event_rejects_impossible_time_and_score_probabilities() -> None:
    with pytest.raises(AlternativeDataError, match="first_seen"):
        PublicTextEvent(
            source_id="official-rss",
            source_event_id="x",
            asset="BTC",
            published_at=BAR,
            first_seen_at=BAR + timedelta(minutes=10),
            fetched_at=BAR,
            headline="headline",
            url="https://example.test/x",
            language="en",
        )
    event = _event("score")
    with pytest.raises(AlternativeDataError, match="sum"):
        SentimentScore(event.sha256, MODEL_HASH, BAR, 0.6, 0.6, 0.0, 1.0)


def test_join_contract_binds_source_rights_time_rule_and_lookback() -> None:
    base = sentiment_join_contract_sha256([_policy()], lookback=timedelta(hours=24))
    assert len(base) == 64
    assert base != sentiment_join_contract_sha256(
        [_policy()], lookback=timedelta(hours=12)
    )
    assert base != sentiment_join_contract_sha256(
        [SourcePolicy("official-rss", "source-terms-v2", True, False, False, 0.8)],
        lookback=timedelta(hours=24),
    )


def test_machine_source_registry_is_unique_hashed_and_rejects_prohibited_platforms() -> None:
    path = Path(__file__).parents[2] / "research" / "alternative-data-sources.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    sources = value["sources"]
    by_id = {item["id"]: item for item in sources}
    assert len(by_id) == len(sources)
    assert sha256_hex(canonical_json(value)) == SOURCE_REGISTRY_SHA256
    assert by_id["reddit"]["status"] == "rejected"
    assert by_id["telegram"]["status"] == "rejected"
    assert by_id["x-twitter"]["status"] == "rejected-without-written-authorization"
    assert all(item["fullTextStorage"] is False for item in sources)
