from __future__ import annotations

import httpx
import pytest
import time

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


@pytest.mark.asyncio
async def test_public_history_pages_are_strictly_decreasing_and_accept_short_pages() -> None:
    requests: list[str | None] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        after = request.url.params.get("after")
        requests.append(after)
        if len(requests) >= 3:
            rows: list[list[str]] = []
        else:
            upper = 1_000 if after is None else int(after) - 1
            rows = [
                [str(upper), "1", "2", "0.5", "1.5", "10", "15", "15", "1"],
                [str(upper - 1), "1", "2", "0.5", "1.5", "10", "15", "15", "1"],
            ]
        return httpx.Response(200, json={"code": "0", "msg": "", "data": rows})

    client = OkxPublicMarketClient(
        transport=httpx.MockTransport(handler),
        history_page_delay_seconds=0,
        history_instruments={"ETH-USDT"},
    )
    try:
        pages = [
            (page, cursor)
            async for page, cursor in client.iter_history_candle_pages(
                "ETH-USDT", page_limit=300
            )
        ]
    finally:
        await client.close()

    assert [cursor for _page, cursor in pages] == [999, 997, 997]
    assert [len(page) for page, _cursor in pages] == [2, 2, 0]
    assert requests == [None, "999", "997", "997", "997"]


@pytest.mark.asyncio
async def test_public_history_retries_50011_and_rejects_stalled_cursor() -> None:
    attempts = 0

    async def retry_handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return httpx.Response(200, json={"code": "50011", "msg": "slow", "data": []})
        return httpx.Response(
            200,
            json={"code": "0", "msg": "", "data": [["100", "1", "2", "0.5", "1.5", "1", "1", "1", "1"]]},
        )

    retry_client = OkxPublicMarketClient(
        transport=httpx.MockTransport(retry_handler),
        history_page_delay_seconds=0,
        history_instruments={"ETH-USDT"},
    )
    try:
        page, cursor = await anext(retry_client.iter_history_candle_pages("ETH-USDT"))
    finally:
        await retry_client.close()
    assert attempts == 3
    assert len(page) == 1 and cursor == 100

    async def stalled_handler(request: httpx.Request) -> httpx.Response:
        timestamp = request.url.params.get("after") or "100"
        return httpx.Response(
            200,
            json={"code": "0", "msg": "", "data": [[timestamp, "1", "2", "0.5", "1.5", "1", "1", "1", "1"]]},
        )

    stalled = OkxPublicMarketClient(
        transport=httpx.MockTransport(stalled_handler),
        history_page_delay_seconds=0,
        history_instruments={"ETH-USDT"},
    )
    try:
        with pytest.raises(PublicMarketError, match="cursor"):
            async for _page, _cursor in stalled.iter_history_candle_pages(
                "ETH-USDT", after=100
            ):
                pass
    finally:
        await stalled.close()


@pytest.mark.asyncio
async def test_public_history_requires_frozen_universe_membership() -> None:
    client = OkxPublicMarketClient(
        transport=httpx.MockTransport(lambda _request: pytest.fail("network called")),
        history_page_delay_seconds=0,
        history_instruments={"ETH-USDT"},
    )
    try:
        with pytest.raises(PublicMarketError, match="frozen research universe"):
            await anext(client.iter_history_candle_pages("SOL-USDT"))
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_public_history_rejects_mixed_page_that_crosses_cursor() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        rows = [
            ["99", "1", "2", "0.5", "1.5", "1", "1", "1", "1"],
            ["101", "1", "2", "0.5", "1.5", "1", "1", "1", "1"],
        ]
        return httpx.Response(200, json={"code": "0", "msg": "", "data": rows})

    client = OkxPublicMarketClient(
        transport=httpx.MockTransport(handler),
        history_page_delay_seconds=0,
        history_instruments={"ETH-USDT"},
    )
    try:
        with pytest.raises(PublicMarketError, match="crossed"):
            await anext(
                client.iter_history_candle_pages("ETH-USDT", after=100)
            )
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_public_history_rate_gate_is_shared_across_independent_iterators() -> None:
    requested_at: list[float] = []

    async def handler(_request: httpx.Request) -> httpx.Response:
        requested_at.append(time.monotonic())
        row = ["100", "1", "2", "0.5", "1.5", "1", "1", "1", "1"]
        return httpx.Response(200, json={"code": "0", "msg": "", "data": [row]})

    client = OkxPublicMarketClient(
        transport=httpx.MockTransport(handler),
        history_page_delay_seconds=0.03,
        history_instruments={"ETH-USDT"},
    )
    first = client.iter_history_candle_pages("ETH-USDT")
    second = client.iter_history_candle_pages("ETH-USDT")
    try:
        await anext(first)
        await anext(second)
    finally:
        await first.aclose()
        await second.aclose()
        await client.close()
    assert len(requested_at) == 2
    assert requested_at[1] - requested_at[0] >= 0.025
