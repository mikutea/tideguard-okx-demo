from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Iterable
from typing import Any, AsyncIterator

import httpx

from .config import OKX_BASE_URL


PUBLIC_RESEARCH_ENDPOINTS = frozenset(
    {
        "/api/v5/public/instruments",
        "/api/v5/market/tickers",
        "/api/v5/market/history-candles",
    }
)


class PublicMarketError(RuntimeError):
    pass


class PublicMarketApiError(PublicMarketError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"OKX public {code}: {message}")


class OkxPublicMarketClient:
    """Credential-free client for universe research; it has no private methods."""

    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        history_page_delay_seconds: float | None = None,
        history_instruments: Iterable[str] | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=OKX_BASE_URL,
            timeout=httpx.Timeout(8.0, connect=5.0),
            transport=transport,
            headers={"User-Agent": "Moheng-Public-Research/0.4"},
        )
        self._history_page_delay_seconds = (
            0.0
            if transport is not None and history_page_delay_seconds is None
            else 0.11
            if history_page_delay_seconds is None
            else max(0.0, min(float(history_page_delay_seconds), 2.0))
        )
        self._history_instruments = (
            frozenset(str(item).strip() for item in history_instruments)
            if history_instruments is not None
            else frozenset()
        )
        if any(
            not re.fullmatch(r"[A-Z0-9]{2,24}-USDT", item)
            for item in self._history_instruments
        ):
            raise PublicMarketError("public history instrument allowlist is invalid")
        self._history_rate_lock = asyncio.Lock()
        self._last_history_request_at: float | None = None

    async def close(self) -> None:
        await self._client.aclose()

    async def _request_data(self, path: str, params: dict[str, str]) -> list[Any]:
        if path not in PUBLIC_RESEARCH_ENDPOINTS:
            raise PublicMarketError("public research API path is not allowlisted")
        response = await self._client.get(path, params=params)
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError as exc:
            raise PublicMarketError("public research response is not JSON") from exc
        if not isinstance(payload, dict):
            raise PublicMarketError("public research response envelope is invalid")
        code = str(payload.get("code", ""))
        if code != "0":
            raise PublicMarketApiError(code or "unknown", str(payload.get("msg", "request failed")))
        data = payload.get("data")
        if not isinstance(data, list):
            raise PublicMarketError("public research response data is invalid")
        return data

    async def _get(self, path: str, params: dict[str, str]) -> list[dict[str, Any]]:
        try:
            data = await self._request_data(path, params)
        except (httpx.RequestError, httpx.HTTPStatusError) as exc:
            raise PublicMarketError("public research request failed") from exc
        if any(not isinstance(item, dict) for item in data):
            raise PublicMarketError("public research response data is invalid")
        return [dict(item) for item in data]

    async def get_spot_universe_inputs(self) -> dict[str, list[dict[str, Any]]]:
        instruments = await self._get(
            "/api/v5/public/instruments", {"instType": "SPOT"}
        )
        tickers = await self._get("/api/v5/market/tickers", {"instType": "SPOT"})
        if not instruments or not tickers:
            raise PublicMarketError("OKX returned an empty public SPOT universe")
        return {"instruments": instruments, "tickers": tickers}

    def _validate_series(self, inst_id: str, bar: str) -> None:
        if (
            not re.fullmatch(r"[A-Z0-9]{2,24}-USDT", inst_id)
            or bar != "5m"
        ):
            raise PublicMarketError("public history series must be a USDT SPOT 5m instrument")
        if inst_id not in self._history_instruments:
            raise PublicMarketError(
                "public history instrument is not in the frozen research universe"
            )

    async def _history_page(
        self,
        *,
        inst_id: str,
        bar: str,
        after: int | None,
        page_limit: int,
    ) -> list[list[Any]]:
        params = {"instId": inst_id, "bar": bar, "limit": str(page_limit)}
        if after is not None:
            params["after"] = str(after)
        last_error: BaseException | None = None
        for attempt in range(5):
            try:
                await self._throttle_history_request()
                data = await self._request_data("/api/v5/market/history-candles", params)
                if any(
                    not isinstance(row, list)
                    or len(row) != 9
                    or any(
                        not isinstance(value, (str, int, float)) or isinstance(value, bool)
                        for value in row
                    )
                    for row in data
                ):
                    raise PublicMarketError("public history candle rows are malformed")
                return [list(row) for row in data]
            except PublicMarketApiError as exc:
                last_error = exc
                retryable = exc.code == "50011"
            except httpx.HTTPStatusError as exc:
                last_error = exc
                retryable = exc.response.status_code == 429 or exc.response.status_code >= 500
            except (httpx.TimeoutException, httpx.RequestError) as exc:
                last_error = exc
                retryable = True
            if not retryable:
                raise last_error
            if attempt == 4:
                break
            delay = min(max(self._history_page_delay_seconds, 0.1) * (2**attempt), 5.0)
            if self._history_page_delay_seconds == 0:
                delay = 0
            if delay:
                await asyncio.sleep(delay)
        raise PublicMarketError("public history page retries were exhausted") from last_error

    async def _throttle_history_request(self) -> None:
        """Rate-limit every physical history request across all iterators/retries."""

        async with self._history_rate_lock:
            current = time.monotonic()
            if self._last_history_request_at is not None:
                remaining = (
                    self._history_page_delay_seconds
                    - (current - self._last_history_request_at)
                )
                if remaining > 0:
                    await asyncio.sleep(remaining)
                    current = time.monotonic()
            self._last_history_request_at = current

    async def iter_history_candle_pages(
        self,
        inst_id: str,
        *,
        bar: str = "5m",
        after: int | None = None,
        page_limit: int = 300,
    ) -> AsyncIterator[tuple[list[list[Any]], int | None]]:
        """Yield strictly older public pages; terminal emptiness is only a hint.

        Durable origin confirmation belongs to the store and must occur in a
        later run.  Three local empty probes merely reduce transient empty
        responses; they never prove an exchange-history origin on their own.
        """

        self._validate_series(inst_id, bar)
        if not 1 <= page_limit <= 300:
            raise PublicMarketError("public history page limit must be 1-300")
        if after is not None and (isinstance(after, bool) or after <= 0):
            raise PublicMarketError("public history cursor is invalid")
        cursor = after
        pages_seen = 0
        empty_probes = 0
        while True:
            if pages_seen >= 20_000:
                raise PublicMarketError("public history exceeded the page safety limit")
            page = await self._history_page(
                inst_id=inst_id,
                bar=bar,
                after=cursor,
                page_limit=page_limit,
            )
            if not page:
                empty_probes += 1
                if empty_probes >= 3:
                    yield [], cursor
                    return
                delay = min(max(self._history_page_delay_seconds, 0.5) * (2 ** (empty_probes - 1)), 2.0)
                if self._history_page_delay_seconds == 0:
                    delay = 0
                if delay:
                    await asyncio.sleep(delay)
                continue
            empty_probes = 0
            pages_seen += 1
            timestamps: list[int] = []
            for row in page:
                timestamp_text = str(row[0]).strip()
                if not timestamp_text.isdigit() or int(timestamp_text) <= 0:
                    raise PublicMarketError("public history contains an invalid timestamp")
                timestamps.append(int(timestamp_text))
            if len(timestamps) != len(set(timestamps)):
                raise PublicMarketError("public history page contains duplicate timestamps")
            if cursor is not None and any(timestamp >= cursor for timestamp in timestamps):
                raise PublicMarketError("public history page crossed its requested cursor")
            next_cursor = min(timestamps)
            if cursor is not None and next_cursor >= cursor:
                raise PublicMarketError("public history cursor did not decrease")
            yield page, next_cursor
            cursor = next_cursor


__all__ = ["OkxPublicMarketClient", "PublicMarketApiError", "PublicMarketError"]
