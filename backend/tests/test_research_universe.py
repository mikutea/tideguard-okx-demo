from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from okx_demo_lab.config import ALLOWED_INSTRUMENTS
from okx_demo_lab.ml.universe import UniverseError, UniversePolicy, select_research_universe
from okx_demo_lab.models import OrderDraft


NOW = datetime(2026, 8, 21, 1, 0, tzinfo=timezone.utc)


def _instrument(symbol: str, *, state: str = "live", listed_days: int = 900) -> dict[str, str]:
    base, quote = symbol.split("-")
    return {
        "instId": symbol,
        "instType": "SPOT",
        "baseCcy": base,
        "quoteCcy": quote,
        "state": state,
        "ruleType": "normal",
        "tickSz": "0.01",
        "lotSz": "0.000001",
        "minSz": "0.001",
        "listTime": str(round((NOW - timedelta(days=listed_days)).timestamp() * 1_000)),
    }


def _ticker(
    symbol: str,
    *,
    volume: str,
    bid: str = "100",
    ask: str = "100.1",
    age_seconds: int = 1,
) -> dict[str, str]:
    return {
        "instId": symbol,
        "last": bid,
        "bidPx": bid,
        "askPx": ask,
        "volCcy24h": volume,
        "ts": str(round((NOW - timedelta(seconds=age_seconds)).timestamp() * 1_000)),
    }


def test_universe_selects_liquid_old_non_stable_spot_deterministically() -> None:
    instruments = [
        _instrument("BTC-USDT"),
        _instrument("ETH-USDT"),
        _instrument("SOL-USDT"),
        _instrument("USDC-USDT"),
        _instrument("NEW-USDT", listed_days=20),
        _instrument("OLD-USDT", state="suspend"),
    ]
    tickers = [
        _ticker("SOL-USDT", volume="28000000"),
        _ticker("ETH-USDT", volume="30000000"),
        _ticker("BTC-USDT", volume="40000000"),
        _ticker("USDC-USDT", volume="50000000"),
        _ticker("NEW-USDT", volume="60000000"),
        _ticker("OLD-USDT", volume="70000000"),
    ]

    snapshot = select_research_universe(
        instruments,
        tickers,
        now=NOW,
        policy=UniversePolicy(max_assets=3, min_assets=3),
    )

    assert [item.instrument for item in snapshot.members] == [
        "BTC-USDT",
        "ETH-USDT",
        "SOL-USDT",
    ]
    assert len(snapshot.sha256) == 64
    assert snapshot.to_dict()["schemaVersion"] == "moheng.research-universe.v1"
    assert snapshot.sha256 == select_research_universe(
        list(reversed(instruments)),
        list(reversed(tickers)),
        now=NOW,
        policy=UniversePolicy(max_assets=3, min_assets=3),
    ).sha256


def test_universe_rejects_stale_wide_or_duplicate_tickers() -> None:
    instruments = [_instrument("BTC-USDT"), _instrument("ETH-USDT"), _instrument("SOL-USDT")]
    with pytest.raises(UniverseError, match="unique"):
        select_research_universe(
            instruments,
            [
                _ticker("BTC-USDT", volume="40000000"),
                _ticker("BTC-USDT", volume="40000000"),
            ],
            now=NOW,
        )
    with pytest.raises(UniverseError, match="too few"):
        select_research_universe(
            instruments,
            [
                _ticker("BTC-USDT", volume="40000000", age_seconds=500),
                _ticker("ETH-USDT", volume="40000000", bid="100", ask="110"),
                _ticker("SOL-USDT", volume="100"),
            ],
            now=NOW,
        )


def test_universe_policy_is_content_addressed() -> None:
    left = UniversePolicy(excluded_base_currencies=frozenset({"USDC", "USDT"}))
    right = UniversePolicy(excluded_base_currencies=frozenset({"USDT", "USDC"}))
    assert left.sha256 == right.sha256
    with pytest.raises(UniverseError, match="USDT"):
        UniversePolicy(quote_currency="BTC")


def test_research_universe_does_not_expand_the_execution_allowlist() -> None:
    assert ALLOWED_INSTRUMENTS == frozenset({"BTC-USDT"})
    with pytest.raises(ValueError, match="BTC-USDT"):
        OrderDraft(instId="ETH-USDT", side="buy", px="100", quoteAmount="10")
