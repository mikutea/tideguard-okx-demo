from __future__ import annotations

import argparse
import importlib.metadata
import json
import math
from datetime import datetime, timezone

from okx_demo_lab.ml.alternative_data import PublicTextEvent, SentimentScore
from okx_demo_lab.ml.strategy import canonical_json, sha256_hex


EXPECTED_VERSION = "3.3.2"


class VaderAdapterError(RuntimeError):
    pass


class VaderSentimentAdapter:
    """Deterministic, explainable baseline; never a direct trading signal."""

    def __init__(self) -> None:
        version = importlib.metadata.version("vaderSentiment")
        if version != EXPECTED_VERSION:
            raise VaderAdapterError(
                f"vaderSentiment version drift: expected {EXPECTED_VERSION}, found {version}"
            )
        from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

        self.analyzer = SentimentIntensityAnalyzer()
        lexicon = {
            str(key): float(value)
            for key, value in sorted(self.analyzer.lexicon.items())
        }
        if any(not math.isfinite(value) for value in lexicon.values()):
            raise VaderAdapterError("VADER lexicon contains non-finite values")
        self.spec = {
            "adapter": "moheng-vader-baseline-v1",
            "library": "vaderSentiment",
            "libraryVersion": version,
            "lexiconSha256": sha256_hex(canonical_json(lexicon)),
            "output": "positive-neutral-negative-probabilities",
        }
        self.model_sha256 = sha256_hex(canonical_json(self.spec))

    def score(
        self,
        event: PublicTextEvent,
        *,
        scored_at: datetime,
        asset_relevance: float,
    ) -> SentimentScore:
        values = self.analyzer.polarity_scores(event.headline)
        return SentimentScore(
            event_sha256=event.sha256,
            model_sha256=self.model_sha256,
            scored_at=scored_at,
            positive=float(values["pos"]),
            neutral=float(values["neu"]),
            negative=float(values["neg"]),
            asset_relevance=asset_relevance,
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect the frozen VADER baseline spec.")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if not args.self_test:
        parser.error("only --self-test is supported; source collectors own event input")
    adapter = VaderSentimentAdapter()
    event = PublicTextEvent(
        source_id="adapter-self-test",
        source_event_id="1",
        asset="BTC",
        published_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        first_seen_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        fetched_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        headline="Bitcoin market remains stable after the update",
        url="https://example.invalid/self-test",
        language="en",
    )
    score = adapter.score(
        event,
        scored_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        asset_relevance=1.0,
    )
    print(
        json.dumps(
            {
                "modelSha256": adapter.model_sha256,
                "negative": score.negative,
                "neutral": score.neutral,
                "positive": score.positive,
                "spec": adapter.spec,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
