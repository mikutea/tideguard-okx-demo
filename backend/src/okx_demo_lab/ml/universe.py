from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

from .strategy import canonical_json, sha256_hex


UNIVERSE_SCHEMA_VERSION = "moheng.research-universe.v1"
DEFAULT_STABLE_BASES = frozenset(
    {"USDT", "USDC", "USDG", "DAI", "FDUSD", "TUSD", "USDE", "USDS"}
)
LEVERAGED_SUFFIXES = ("3L", "3S", "5L", "5S", "BULL", "BEAR")


class UniverseError(ValueError):
    pass


def _utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise UniverseError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return _utc(value, "timestamp").isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _positive_decimal(value: Any, name: str, *, allow_zero: bool = False) -> Decimal:
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise UniverseError(f"{name} is not a valid decimal") from exc
    if not parsed.is_finite() or parsed < 0 or (parsed == 0 and not allow_zero):
        raise UniverseError(f"{name} must be finite and positive")
    return parsed


@dataclass(frozen=True)
class UniversePolicy:
    quote_currency: str = "USDT"
    max_assets: int = 8
    min_assets: int = 3
    min_listing_days: int = 730
    min_quote_volume_24h: Decimal = Decimal("25000000")
    max_spread_bps: Decimal = Decimal("10")
    max_tick_bps: Decimal = Decimal("5")
    max_ticker_age_seconds: int = 120
    excluded_base_currencies: frozenset[str] = DEFAULT_STABLE_BASES

    def __post_init__(self) -> None:
        if self.quote_currency != "USDT":
            raise UniverseError("research universe is currently fixed to USDT quote")
        if not 1 <= self.min_assets <= self.max_assets <= 20:
            raise UniverseError("research universe asset bounds are invalid")
        if not 365 <= self.min_listing_days <= 3_650:
            raise UniverseError("research universe listing age is invalid")
        if not Decimal("1000000") <= self.min_quote_volume_24h <= Decimal("10000000000"):
            raise UniverseError("research universe volume threshold is invalid")
        if not Decimal("1") <= self.max_spread_bps <= Decimal("100"):
            raise UniverseError("research universe spread threshold is invalid")
        if not Decimal("0.1") <= self.max_tick_bps <= Decimal("20"):
            raise UniverseError("research universe tick threshold is invalid")
        if not 10 <= self.max_ticker_age_seconds <= 600:
            raise UniverseError("research universe ticker freshness is invalid")
        if any(
            not isinstance(item, str) or not item or item.upper() != item
            for item in self.excluded_base_currencies
        ):
            raise UniverseError("research universe exclusions are invalid")

    def to_dict(self) -> dict[str, Any]:
        return {
            "excludedBaseCurrencies": sorted(self.excluded_base_currencies),
            "maxAssets": self.max_assets,
            "maxSpreadBps": str(self.max_spread_bps),
            "maxTickBps": str(self.max_tick_bps),
            "maxTickerAgeSeconds": self.max_ticker_age_seconds,
            "minAssets": self.min_assets,
            "minListingDays": self.min_listing_days,
            "minQuoteVolume24h": str(self.min_quote_volume_24h),
            "quoteCurrency": self.quote_currency,
        }

    @property
    def sha256(self) -> str:
        return sha256_hex(canonical_json(self.to_dict()))


@dataclass(frozen=True)
class UniverseMember:
    instrument: str
    base_currency: str
    quote_currency: str
    listed_at: datetime
    last: Decimal
    bid: Decimal
    ask: Decimal
    quote_volume_24h: Decimal
    spread_bps: Decimal
    tick_size: Decimal
    lot_size: Decimal
    min_size: Decimal
    ticker_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "ask": str(self.ask),
            "baseCurrency": self.base_currency,
            "bid": str(self.bid),
            "instrument": self.instrument,
            "last": str(self.last),
            "listedAt": _iso(self.listed_at),
            "lotSize": str(self.lot_size),
            "minSize": str(self.min_size),
            "quoteCurrency": self.quote_currency,
            "quoteVolume24h": str(self.quote_volume_24h),
            "spreadBps": str(self.spread_bps),
            "tickSize": str(self.tick_size),
            "tickerAt": _iso(self.ticker_at),
        }


@dataclass(frozen=True)
class UniverseSnapshot:
    created_at: datetime
    policy_sha256: str
    members: tuple[UniverseMember, ...]
    instrument_rows: int
    ticker_rows: int

    def __post_init__(self) -> None:
        _utc(self.created_at, "created_at")
        if len(self.policy_sha256) != 64:
            raise UniverseError("research universe policy hash is invalid")
        if not self.members or self.instrument_rows < 1 or self.ticker_rows < 1:
            raise UniverseError("research universe snapshot is empty")
        instruments = [item.instrument for item in self.members]
        if len(instruments) != len(set(instruments)):
            raise UniverseError("research universe contains duplicate instruments")

    def to_dict(self) -> dict[str, Any]:
        return {
            "createdAt": _iso(self.created_at),
            "instrumentRows": self.instrument_rows,
            "members": [item.to_dict() for item in self.members],
            "policySha256": self.policy_sha256,
            "schemaVersion": UNIVERSE_SCHEMA_VERSION,
            "tickerRows": self.ticker_rows,
        }

    @property
    def sha256(self) -> str:
        return sha256_hex(canonical_json(self.to_dict()))


def _instrument_map(rows: Sequence[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    values: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise UniverseError("OKX instrument row is malformed")
        instrument = str(row.get("instId", "")).strip()
        if not instrument or instrument in values:
            raise UniverseError("OKX instrument IDs must be present and unique")
        values[instrument] = row
    return values


def select_research_universe(
    instruments: Sequence[Mapping[str, Any]],
    tickers: Sequence[Mapping[str, Any]],
    *,
    now: datetime,
    policy: UniversePolicy | None = None,
) -> UniverseSnapshot:
    """Select a provisional public-data universe without changing order policy.

    The result is only a discovery snapshot.  Each member still needs complete
    history, gap/conflict checks, aligned cohort validation and portfolio-level
    OOS evidence before it can enter even Demo shadow.
    """

    current = _utc(now, "now")
    active_policy = policy or UniversePolicy()
    instrument_by_id = _instrument_map(instruments)
    candidates: list[UniverseMember] = []
    seen_tickers: set[str] = set()
    listing_cutoff = current - timedelta(days=active_policy.min_listing_days)
    stale_cutoff = current - timedelta(seconds=active_policy.max_ticker_age_seconds)
    for ticker in tickers:
        if not isinstance(ticker, Mapping):
            raise UniverseError("OKX ticker row is malformed")
        instrument = str(ticker.get("instId", "")).strip()
        if not instrument or instrument in seen_tickers:
            raise UniverseError("OKX ticker IDs must be present and unique")
        seen_tickers.add(instrument)
        metadata = instrument_by_id.get(instrument)
        if metadata is None:
            continue
        base = str(metadata.get("baseCcy", "")).strip().upper()
        quote = str(metadata.get("quoteCcy", "")).strip().upper()
        if (
            metadata.get("instType") != "SPOT"
            or metadata.get("state") != "live"
            or metadata.get("ruleType") != "normal"
            or quote != active_policy.quote_currency
            or instrument != f"{base}-{quote}"
            or base in active_policy.excluded_base_currencies
            or any(base.endswith(suffix) for suffix in LEVERAGED_SUFFIXES)
        ):
            continue
        list_time_text = str(metadata.get("listTime", "")).strip()
        ticker_time_text = str(ticker.get("ts", "")).strip()
        if not list_time_text.isdigit() or not ticker_time_text.isdigit():
            continue
        listed_at = datetime.fromtimestamp(int(list_time_text) / 1_000, tz=timezone.utc)
        ticker_at = datetime.fromtimestamp(int(ticker_time_text) / 1_000, tz=timezone.utc)
        if listed_at > listing_cutoff or ticker_at < stale_cutoff or ticker_at > current + timedelta(seconds=5):
            continue
        try:
            last = _positive_decimal(ticker.get("last"), "last")
            bid = _positive_decimal(ticker.get("bidPx"), "bid")
            ask = _positive_decimal(ticker.get("askPx"), "ask")
            quote_volume = _positive_decimal(ticker.get("volCcy24h"), "quote volume", allow_zero=True)
            tick_size = _positive_decimal(metadata.get("tickSz"), "tick size")
            lot_size = _positive_decimal(metadata.get("lotSz"), "lot size")
            min_size = _positive_decimal(metadata.get("minSz"), "minimum size")
        except UniverseError:
            continue
        if bid > ask or quote_volume < active_policy.min_quote_volume_24h:
            continue
        midpoint = (bid + ask) / Decimal("2")
        spread_bps = (ask - bid) / midpoint * Decimal("10000")
        tick_bps = tick_size / midpoint * Decimal("10000")
        if (
            not spread_bps.is_finite()
            or spread_bps > active_policy.max_spread_bps
            or not tick_bps.is_finite()
            or tick_bps > active_policy.max_tick_bps
        ):
            continue
        candidates.append(
            UniverseMember(
                instrument=instrument,
                base_currency=base,
                quote_currency=quote,
                listed_at=listed_at,
                last=last,
                bid=bid,
                ask=ask,
                quote_volume_24h=quote_volume,
                spread_bps=spread_bps,
                tick_size=tick_size,
                lot_size=lot_size,
                min_size=min_size,
                ticker_at=ticker_at,
            )
        )
    candidates.sort(key=lambda item: (-item.quote_volume_24h, item.instrument))
    selected = tuple(candidates[: active_policy.max_assets])
    if len(selected) < active_policy.min_assets:
        raise UniverseError("too few public instruments passed the research universe policy")
    return UniverseSnapshot(
        created_at=current,
        policy_sha256=active_policy.sha256,
        members=selected,
        instrument_rows=len(instruments),
        ticker_rows=len(tickers),
    )


__all__ = [
    "UNIVERSE_SCHEMA_VERSION",
    "UniverseError",
    "UniverseMember",
    "UniversePolicy",
    "UniverseSnapshot",
    "select_research_universe",
]
