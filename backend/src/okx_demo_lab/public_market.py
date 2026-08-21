from __future__ import annotations

from typing import Any

import httpx

from .config import OKX_BASE_URL


PUBLIC_RESEARCH_ENDPOINTS = frozenset(
    {
        "/api/v5/public/instruments",
        "/api/v5/market/tickers",
    }
)


class PublicMarketError(RuntimeError):
    pass


class OkxPublicMarketClient:
    """Credential-free client for universe research; it has no private methods."""

    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._client = httpx.AsyncClient(
            base_url=OKX_BASE_URL,
            timeout=httpx.Timeout(8.0, connect=5.0),
            transport=transport,
            headers={"User-Agent": "Moheng-Public-Research/0.4"},
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, params: dict[str, str]) -> list[dict[str, Any]]:
        if path not in PUBLIC_RESEARCH_ENDPOINTS:
            raise PublicMarketError("public research API path is not allowlisted")
        try:
            response = await self._client.get(path, params=params)
            response.raise_for_status()
            payload = response.json()
        except (httpx.RequestError, httpx.HTTPStatusError, ValueError) as exc:
            raise PublicMarketError("public research request failed") from exc
        if not isinstance(payload, dict) or str(payload.get("code", "")) != "0":
            raise PublicMarketError("public research response envelope is invalid")
        data = payload.get("data")
        if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
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


__all__ = ["OkxPublicMarketClient", "PublicMarketError"]
