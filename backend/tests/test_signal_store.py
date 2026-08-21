from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from okx_demo_lab.ml.alternative_data import (
    PublicTextEvent,
    SentimentScore,
    SourcePolicy,
)
from okx_demo_lab.ml.signal_store import SignalStore, SignalStoreError


NOW = datetime(2026, 8, 21, 1, 0, tzinfo=timezone.utc)
MODEL_HASH = "a" * 64


def _policy(
    *, source: str = "official-rss", headline_allowed: bool = True
) -> SourcePolicy:
    return SourcePolicy(
        source_id=source,
        license_id="official-terms-v1",
        headline_storage_allowed=headline_allowed,
        full_text_storage_allowed=False,
        redistribution_allowed=False,
        reliability_weight=0.9,
    )


def _event(
    event_id: str,
    *,
    revision: int = 0,
    published_at: datetime | None = None,
    first_seen_at: datetime | None = None,
    fetched_at: datetime | None = None,
    historical_backfill: bool = False,
    headline: str | None = None,
) -> PublicTextEvent:
    published = published_at or NOW - timedelta(minutes=30)
    first_seen = first_seen_at or published + timedelta(minutes=1)
    fetched = fetched_at or first_seen + timedelta(minutes=1)
    return PublicTextEvent(
        source_id="official-rss",
        source_event_id=event_id,
        asset="BTC",
        published_at=published,
        first_seen_at=first_seen,
        fetched_at=fetched,
        headline=headline or f"Event {event_id} revision {revision}",
        url=f"https://example.test/{event_id}",
        language="en",
        revision=revision,
        historical_backfill=historical_backfill,
    )


def _score(
    event: PublicTextEvent,
    *,
    scored_at: datetime,
    positive: float = 0.7,
    negative: float = 0.1,
) -> SentimentScore:
    return SentimentScore(
        event_sha256=event.sha256,
        model_sha256=MODEL_HASH,
        scored_at=scored_at,
        positive=positive,
        neutral=1.0 - positive - negative,
        negative=negative,
        asset_relevance=0.8,
    )


def _append_pair(
    store: SignalStore,
    event: PublicTextEvent,
    *,
    event_observed_at: datetime,
    score_observed_at: datetime,
) -> None:
    assert store.append_event(
        event, _policy(), observed_at=event_observed_at
    )["status"] == "accepted"
    assert store.append_score(
        _score(event, scored_at=score_observed_at),
        observed_at=score_observed_at,
    )["status"] == "accepted"


def test_append_query_snapshot_and_quality_report_are_content_addressed(tmp_path) -> None:
    store = SignalStore(tmp_path / "signals.sqlite3")
    event = _event("one")
    _append_pair(
        store,
        event,
        event_observed_at=NOW - timedelta(minutes=20),
        score_observed_at=NOW - timedelta(minutes=19),
    )

    records = store.point_in_time(
        asset="BTC",
        as_of=NOW,
        lookback=timedelta(hours=1),
        model_sha256=MODEL_HASH,
    )
    assert len(records) == 1
    assert records[0].event == event
    assert records[0].policy_sha256 == _policy().sha256
    assert len(records[0].content_sha256) == 64
    assert len(records[0].score_sha256) == 64

    snapshot = store.create_snapshot(
        asset="BTC",
        as_of=NOW,
        lookback=timedelta(hours=1),
        model_sha256=MODEL_HASH,
        now=NOW,
    )
    assert snapshot.snapshot_id.startswith("sig_")
    assert snapshot.row_count == 1
    assert store.snapshot_records(snapshot.snapshot_id) == records

    quality = store.quality_report()
    assert quality["schemaVersion"] == "moheng.signal-store.v1"
    assert quality["healthy"] is True
    assert quality["sourcePolicies"] == 1
    assert quality["storedEvents"] == 1
    assert quality["storedScores"] == 1
    assert quality["unresolvedConflicts"] == 0
    with sqlite3.connect(store.path) as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0].lower() == "delete"
        assert db.execute("PRAGMA synchronous").fetchone()[0] == 2
        policy = db.execute(
            "SELECT policy_sha256, license_id FROM source_policies"
        ).fetchone()
        assert policy == (_policy().sha256, "official-terms-v1")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            db.execute("UPDATE signal_events SET asset = 'ETH'")


def test_duplicate_never_rewrites_first_seen_and_conflicts_are_quarantined(
    tmp_path,
) -> None:
    store = SignalStore(tmp_path / "signals.sqlite3")
    original = _event("immutable")
    accepted_at = NOW - timedelta(minutes=20)
    assert store.append_event(original, _policy(), observed_at=accepted_at)[
        "status"
    ] == "accepted"

    later_observation = _event(
        "immutable",
        published_at=original.published_at,
        first_seen_at=original.first_seen_at + timedelta(minutes=2),
        fetched_at=original.fetched_at + timedelta(minutes=3),
    )
    assert store.append_event(
        later_observation,
        _policy(),
        observed_at=NOW - timedelta(minutes=15),
    )["status"] == "duplicate"

    regressed = _event(
        "immutable",
        published_at=original.published_at,
        first_seen_at=original.first_seen_at - timedelta(seconds=30),
        fetched_at=original.fetched_at,
    )
    assert store.append_event(
        regressed, _policy(), observed_at=NOW - timedelta(minutes=14)
    )["status"] == "conflict"

    disguised_backfill = _event(
        "immutable",
        published_at=original.published_at,
        first_seen_at=original.first_seen_at,
        fetched_at=original.fetched_at,
        historical_backfill=True,
    )
    assert store.append_event(
        disguised_backfill,
        _policy(),
        observed_at=NOW - timedelta(minutes=13),
    )["status"] == "conflict"

    with sqlite3.connect(store.path) as db:
        stored = db.execute(
            "SELECT first_seen_at, historical_backfill FROM signal_events"
        ).fetchone()
        assert stored[0] == original.first_seen_at.isoformat(
            timespec="milliseconds"
        ).replace("+00:00", "Z")
        assert stored[1] == 0
        reasons = {
            row[0] for row in db.execute("SELECT reason FROM event_conflicts")
        }
    assert any("first_seen regression" in reason for reason in reasons)
    assert any("historical provenance mismatch" in reason for reason in reasons)
    quality = store.status()
    assert quality["storedEvents"] == 1
    assert quality["eventObservations"] == 4
    assert quality["duplicateObservations"] == 1
    assert quality["eventConflicts"] == 2
    assert quality["healthy"] is False
    with pytest.raises(SignalStoreError, match="conflicts"):
        store.create_snapshot(
            asset="BTC",
            as_of=NOW,
            lookback=timedelta(hours=1),
            model_sha256=MODEL_HASH,
            now=NOW,
        )


def test_future_and_late_accepted_data_are_excluded_point_in_time(tmp_path) -> None:
    store = SignalStore(tmp_path / "signals.sqlite3")
    future_published = _event(
        "future-published",
        published_at=NOW + timedelta(minutes=4),
        first_seen_at=NOW,
        fetched_at=NOW,
    )
    _append_pair(
        store,
        future_published,
        event_observed_at=NOW,
        score_observed_at=NOW,
    )

    late = _event("late-import")
    _append_pair(
        store,
        late,
        event_observed_at=NOW + timedelta(minutes=10),
        score_observed_at=NOW + timedelta(minutes=10),
    )

    future_score_event = _event("future-score")
    assert store.append_event(
        future_score_event, _policy(), observed_at=NOW
    )["status"] == "accepted"
    assert store.append_score(
        _score(future_score_event, scored_at=NOW + timedelta(minutes=3)),
        observed_at=NOW + timedelta(minutes=3),
    )["status"] == "accepted"

    at_now = store.point_in_time(
        asset="BTC",
        as_of=NOW,
        lookback=timedelta(hours=1),
        model_sha256=MODEL_HASH,
    )
    assert at_now == ()
    at_four = store.point_in_time(
        asset="BTC",
        as_of=NOW + timedelta(minutes=4),
        lookback=timedelta(hours=1),
        model_sha256=MODEL_HASH,
    )
    assert {item.event.source_event_id for item in at_four} == {
        "future-published",
        "future-score",
    }
    at_ten = store.point_in_time(
        asset="BTC",
        as_of=NOW + timedelta(minutes=10),
        lookback=timedelta(hours=1),
        model_sha256=MODEL_HASH,
    )
    assert {item.event.source_event_id for item in at_ten} == {
        "future-published",
        "future-score",
        "late-import",
    }


def test_revision_visibility_is_append_only_and_never_falls_back_unscored(
    tmp_path,
) -> None:
    store = SignalStore(tmp_path / "signals.sqlite3")
    revision_zero = _event(
        "revised",
        revision=0,
        published_at=NOW - timedelta(minutes=30),
        first_seen_at=NOW - timedelta(minutes=29),
        fetched_at=NOW - timedelta(minutes=28),
    )
    _append_pair(
        store,
        revision_zero,
        event_observed_at=NOW - timedelta(minutes=27),
        score_observed_at=NOW - timedelta(minutes=26),
    )
    revision_one = _event(
        "revised",
        revision=1,
        published_at=NOW - timedelta(minutes=30),
        first_seen_at=NOW - timedelta(minutes=20),
        fetched_at=NOW - timedelta(minutes=19),
    )
    assert store.append_event(
        revision_one,
        _policy(),
        observed_at=NOW - timedelta(minutes=18),
    )["status"] == "accepted"

    before_revision = store.point_in_time(
        asset="BTC",
        as_of=NOW - timedelta(minutes=25),
        lookback=timedelta(hours=1),
        model_sha256=MODEL_HASH,
    )
    assert [item.event.revision for item in before_revision] == [0]
    while_latest_is_unscored = store.point_in_time(
        asset="BTC",
        as_of=NOW - timedelta(minutes=17),
        lookback=timedelta(hours=1),
        model_sha256=MODEL_HASH,
    )
    assert while_latest_is_unscored == ()

    assert store.append_score(
        _score(revision_one, scored_at=NOW - timedelta(minutes=16)),
        observed_at=NOW - timedelta(minutes=16),
    )["status"] == "accepted"
    after_revision = store.point_in_time(
        asset="BTC",
        as_of=NOW - timedelta(minutes=15),
        lookback=timedelta(hours=1),
        model_sha256=MODEL_HASH,
    )
    assert [item.event.revision for item in after_revision] == [1]

    time_travelling_revision = _event(
        "revised",
        revision=2,
        published_at=NOW - timedelta(minutes=40),
        first_seen_at=NOW - timedelta(minutes=39),
        fetched_at=NOW - timedelta(minutes=38),
        historical_backfill=True,
    )
    assert store.append_event(
        time_travelling_revision,
        _policy(),
        observed_at=NOW - timedelta(minutes=10),
    )["status"] == "conflict"
    assert store.status()["storedEvents"] == 2


def test_historical_backfill_is_retrospective_only_and_cannot_be_relabelled(
    tmp_path,
) -> None:
    store = SignalStore(tmp_path / "signals.sqlite3")
    historical = _event(
        "archive",
        published_at=NOW - timedelta(days=1),
        first_seen_at=NOW - timedelta(minutes=5),
        fetched_at=NOW - timedelta(minutes=4),
        historical_backfill=True,
    )
    _append_pair(
        store,
        historical,
        event_observed_at=NOW - timedelta(minutes=3),
        score_observed_at=NOW - timedelta(minutes=2),
    )
    prospective = store.point_in_time(
        asset="BTC",
        as_of=NOW,
        lookback=timedelta(hours=1),
        model_sha256=MODEL_HASH,
    )
    retrospective = store.point_in_time(
        asset="BTC",
        as_of=NOW,
        lookback=timedelta(hours=1),
        model_sha256=MODEL_HASH,
        prospective_only=False,
    )
    assert prospective == ()
    assert [item.event.source_event_id for item in retrospective] == ["archive"]

    relabelled = _event(
        "archive",
        published_at=historical.published_at,
        first_seen_at=historical.first_seen_at,
        fetched_at=historical.fetched_at,
        historical_backfill=False,
    )
    assert store.append_event(
        relabelled, _policy(), observed_at=NOW
    )["status"] == "conflict"
    assert store.point_in_time(
        asset="BTC",
        as_of=NOW,
        lookback=timedelta(hours=1),
        model_sha256=MODEL_HASH,
    ) == ()


def test_score_duplicate_is_stable_and_nondeterministic_rescore_is_quarantined(
    tmp_path,
) -> None:
    store = SignalStore(tmp_path / "signals.sqlite3")
    event = _event("score-stability")
    assert store.append_event(event, _policy(), observed_at=NOW)["status"] == "accepted"
    original = _score(event, scored_at=NOW)
    assert store.append_score(original, observed_at=NOW)["status"] == "accepted"

    same_output_later = _score(event, scored_at=NOW + timedelta(minutes=1))
    assert store.append_score(
        same_output_later, observed_at=NOW + timedelta(minutes=1)
    )["status"] == "duplicate"
    different_output = _score(
        event,
        scored_at=NOW + timedelta(minutes=2),
        positive=0.2,
        negative=0.6,
    )
    assert store.append_score(
        different_output, observed_at=NOW + timedelta(minutes=2)
    )["status"] == "conflict"

    record = store.point_in_time(
        asset="BTC",
        as_of=NOW + timedelta(minutes=3),
        lookback=timedelta(hours=1),
        model_sha256=MODEL_HASH,
    )[0]
    assert record.score == original
    quality = store.status()
    assert quality["storedScores"] == 1
    assert quality["scoreObservations"] == 3
    assert quality["duplicateObservations"] == 1
    assert quality["scoreConflicts"] == 1
    assert quality["healthy"] is False


def test_invalid_future_observation_rights_and_unknown_score_fail_closed(
    tmp_path,
) -> None:
    store = SignalStore(tmp_path / "signals.sqlite3")
    future_fetch = _event(
        "future-fetch",
        published_at=NOW,
        first_seen_at=NOW,
        fetched_at=NOW + timedelta(minutes=1),
    )
    with pytest.raises(SignalStoreError, match="fetched_at"):
        store.append_event(future_fetch, _policy(), observed_at=NOW)
    with pytest.raises(SignalStoreError, match="does not allow"):
        store.append_event(_event("rights"), _policy(headline_allowed=False), observed_at=NOW)
    unknown = _event("unknown")
    with pytest.raises(SignalStoreError, match="unknown event"):
        store.append_score(_score(unknown, scored_at=NOW), observed_at=NOW)
    assert store.status()["storedEvents"] == 0


def test_snapshot_is_immutable_against_later_ingest_and_detects_tampering(tmp_path) -> None:
    store = SignalStore(tmp_path / "signals.sqlite3")
    first = _event("first")
    _append_pair(
        store,
        first,
        event_observed_at=NOW - timedelta(minutes=20),
        score_observed_at=NOW - timedelta(minutes=19),
    )
    snapshot = store.create_snapshot(
        asset="BTC",
        as_of=NOW,
        lookback=timedelta(hours=1),
        model_sha256=MODEL_HASH,
        now=NOW,
    )

    late = _event("accepted-later")
    _append_pair(
        store,
        late,
        event_observed_at=NOW + timedelta(minutes=1),
        score_observed_at=NOW + timedelta(minutes=1),
    )
    assert [
        item.event.source_event_id for item in store.snapshot_records(snapshot.snapshot_id)
    ] == ["first"]

    with sqlite3.connect(store.path) as db:
        db.execute("DROP TRIGGER no_update_signal_events")
        db.execute(
            "UPDATE signal_events SET event_json = replace(event_json, 'Event first', 'Altered')"
        )
    quality = store.status()
    assert quality["healthy"] is False
    assert quality["integrityErrors"] >= 1
    with pytest.raises(SignalStoreError, match="integrity"):
        store.snapshot_records(snapshot.snapshot_id)
