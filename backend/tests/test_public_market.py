from __future__ import annotations

import httpx
import pytest

from okx_demo_lab.public_market import OkxPublicMarketClient, PublicMarketError


@pytest.mark.asyncio
async def test_public_spot_universe_client_has_no_demo_or_credential_headers() -> None:
    captured: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if request.url.path == "/api/v5/public/instruments":
            data = [{"instId": "BTC-USDT", "instType": "SPOT"}]
        elif request.url.path == "/api/v5/market/tickers":
            data = [{"instId": "BTC-USDT", "last": "1"}]
        else:
            raise AssertionError(request.url.path)
        return httpx.Response(200, json={"code": "0", "msg": "", "data": data})

    client = OkxPublicMarketClient(transport=httpx.MockTransport(handler))
    try:
        assert not hasattr(client, "private_request")
        assert not hasattr(client, "place_order")
        result = await client.get_spot_universe_inputs()
    finally:
        await client.close()

    assert result["instruments"][0]["instId"] == "BTC-USDT"
    assert result["tickers"][0]["instId"] == "BTC-USDT"
    assert [request.url.params["instType"] for request in captured] == ["SPOT", "SPOT"]
    assert all("x-simulated-trading" not in request.headers for request in captured)
    assert all("ok-access-key" not in request.headers for request in captured)


@pytest.mark.asyncio
async def test_public_research_client_rejects_non_allowlisted_paths_and_bad_envelopes() -> None:
    client = OkxPublicMarketClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json={"code": "0", "data": "bad"})
        )
    )
    try:
        with pytest.raises(PublicMarketError, match="allowlisted"):
            await client._get("/api/v5/trade/orders-pending", {})
        with pytest.raises(PublicMarketError, match="data"):
            await client._get("/api/v5/market/tickers", {"instType": "SPOT"})
    finally:
        await client.close()
