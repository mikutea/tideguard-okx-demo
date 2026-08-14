from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pytest

from okx_demo_lab.okx_client import (
    AmbiguousOrderError,
    CredentialIdentityError,
    OkxApiError,
    OkxClient,
    OkxClientError,
)
from okx_demo_lab.secrets import Credentials, credential_fingerprint


@pytest.mark.asyncio
async def test_every_request_has_demo_header() -> None:
    captured: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if request.url.path == "/api/v5/public/time":
            return httpx.Response(200, json={"code": "0", "msg": "", "data": [{"ts": "1"}]})
        return httpx.Response(200, json={"code": "0", "msg": "", "data": [{"acctLv": "1"}]})

    client = OkxClient(
        credentials_provider=lambda: Credentials("demo-key", "demo-secret", "demo-passphrase"),
        transport=httpx.MockTransport(handler),
    )
    try:
        await client.public_get("/api/v5/public/time")
        await client.get_account_config()
    finally:
        await client.close()

    assert len(captured) == 2
    assert all(request.headers["x-simulated-trading"] == "1" for request in captured)
    assert captured[1].headers["ok-access-key"] == "demo-key"


@pytest.mark.asyncio
async def test_private_endpoint_allowlist_is_fail_closed() -> None:
    client = OkxClient(credentials_provider=lambda: Credentials("a", "b", "c"))
    try:
        with pytest.raises(OkxClientError, match="白名单"):
            await client.private_request("POST", "/api/v5/asset/withdrawal", body={"amt": "1"})
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_history_candles_paginate_and_return_chronological_confirmed_rows() -> None:
    cursors: list[str | None] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        after = request.url.params.get("after")
        cursors.append(after)
        if after is None:
            timestamps = range(1_000, 700, -1)
        elif after == "701":
            timestamps = range(700, 400, -1)
        elif after == "401":
            timestamps = range(400, 350, -1)
        else:
            timestamps = []
        rows = [
            [str(ts), "1", "2", "0.5", "1.5", "10", "15", "15", "1"]
            for ts in timestamps
        ]
        return httpx.Response(200, json={"code": "0", "msg": "", "data": rows})

    client = OkxClient(transport=httpx.MockTransport(handler))
    try:
        rows = await client.get_history_candles(limit=650)
    finally:
        await client.close()

    assert cursors == [None, "701", "401"]
    assert len(rows) == 650
    assert rows[0][0] == "351"
    assert rows[-1][0] == "1000"


@pytest.mark.asyncio
async def test_history_candles_reject_unapproved_market_or_timeframe() -> None:
    client = OkxClient()
    try:
        with pytest.raises(OkxClientError, match="只允许"):
            await client.get_history_candles("ETH-USDT")
        with pytest.raises(OkxClientError, match="只允许"):
            await client.get_history_candles(bar="1h")
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_place_order_forces_cash_and_application_tag() -> None:
    captured: list[dict[str, str]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        captured.append(body)
        return httpx.Response(
            200,
            json={
                "code": "0",
                "msg": "",
                "data": [
                    {
                        "sCode": "0",
                        "ordId": "42",
                        "clOrdId": body["clOrdId"],
                        "tag": body["tag"],
                    }
                ],
            },
        )

    client = OkxClient(
        credentials_provider=lambda: Credentials("a", "b", "c"),
        transport=httpx.MockTransport(handler),
    )
    try:
        await client.place_order(
            inst_id="BTC-USDT",
            side="buy",
            ord_type="limit",
            price="64000.0",
            size="0.0002",
            cl_ord_id="tg1234567890",
        )
    finally:
        await client.close()
    assert captured[0]["tdMode"] == "cash"
    assert captured[0]["tag"] == "tideguarddemo"
    assert "lever" not in captured[0]


@pytest.mark.asyncio
async def test_order_item_50004_is_ambiguous() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"code": "0", "msg": "", "data": [{"sCode": "50004", "sMsg": "timeout"}]},
        )

    client = OkxClient(
        credentials_provider=lambda: Credentials("a", "b", "c"),
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(AmbiguousOrderError):
            await client.place_order(
                inst_id="BTC-USDT",
                side="buy",
                ord_type="limit",
                price="64000.0",
                size="0.0002",
                cl_ord_id="tg50004",
            )
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_outer_failure_with_inner_50004_is_ambiguous() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "code": "1",
                "msg": "All operations failed",
                "data": [{"sCode": "50004", "sMsg": "timeout"}],
            },
        )

    client = OkxClient(
        credentials_provider=lambda: Credentials("a", "b", "c"),
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(AmbiguousOrderError):
            await client.place_order(
                inst_id="BTC-USDT",
                side="buy",
                ord_type="limit",
                price="64000.0",
                size="0.0002",
                cl_ord_id="tg-outer-inner-50004",
            )
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_success_without_order_id_is_ambiguous() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": "0", "msg": "", "data": [{"sCode": "0"}]})

    client = OkxClient(
        credentials_provider=lambda: Credentials("a", "b", "c"),
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(AmbiguousOrderError):
            await client.place_order(
                inst_id="BTC-USDT",
                side="buy",
                ord_type="limit",
                price="64000.0",
                size="0.0002",
                cl_ord_id="tg-no-ord-id",
            )
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_success_with_wrong_client_order_id_is_ambiguous() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "code": "0",
                "msg": "",
                "data": [
                    {
                        "sCode": "0",
                        "ordId": "old-42",
                        "clOrdId": "different",
                        "tag": "tideguarddemo",
                    }
                ],
            },
        )

    client = OkxClient(
        credentials_provider=lambda: Credentials("a", "b", "c"),
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(AmbiguousOrderError, match="clOrdId"):
            await client.place_order(
                inst_id="BTC-USDT",
                side="buy",
                ord_type="limit",
                price="64000.0",
                size="0.0002",
                cl_ord_id="tg-current",
            )
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_missing_envelope_code_is_ambiguous_for_order() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"sCode": "0", "ordId": "42"}]})

    client = OkxClient(
        credentials_provider=lambda: Credentials("a", "b", "c"),
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(AmbiguousOrderError):
            await client.place_order(
                inst_id="BTC-USDT",
                side="buy",
                ord_type="limit",
                price="64000.0",
                size="0.0002",
                cl_ord_id="tg-no-envelope-code",
            )
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_missing_item_code_is_ambiguous_for_order() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": "0", "msg": "", "data": [{"ordId": "42"}]})

    client = OkxClient(
        credentials_provider=lambda: Credentials("a", "b", "c"),
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(AmbiguousOrderError):
            await client.place_order(
                inst_id="BTC-USDT",
                side="buy",
                ord_type="limit",
                price="64000.0",
                size="0.0002",
                cl_ord_id="tg-no-item-code",
            )
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_cancel_rejects_inner_error_code() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"code": "0", "msg": "", "data": [{"sCode": "51400", "sMsg": "not found"}]},
        )

    client = OkxClient(
        credentials_provider=lambda: Credentials("a", "b", "c"),
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(OkxApiError):
            await client.cancel_order("BTC-USDT", "tg-missing")
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_order_transport_error_is_ambiguous() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection reset", request=request)

    client = OkxClient(
        credentials_provider=lambda: Credentials("a", "b", "c"),
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(AmbiguousOrderError):
            await client.place_order(
                inst_id="BTC-USDT",
                side="buy",
                ord_type="limit",
                price="64000.0",
                size="0.0002",
                cl_ord_id="tg-reset",
            )
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_private_request_rejects_credential_swap_before_network() -> None:
    requests = 0

    async def handler(_: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, json={"code": "0", "msg": "", "data": []})

    original = Credentials("account-a-key", "secret-a", "pass-a")
    replacement = Credentials("account-b-key", "secret-b", "pass-b")
    client = OkxClient(
        credentials_provider=lambda: replacement,
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(CredentialIdentityError, match="身份已变化"):
            await client.get_account_balance(
                expected_credential_fingerprint=credential_fingerprint(original)
            )
    finally:
        await client.close()

    assert requests == 0


@pytest.mark.asyncio
async def test_cancel_all_after_requires_structured_confirmation() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": "0", "msg": "", "data": []})

    client = OkxClient(
        credentials_provider=lambda: Credentials("a", "b", "c"),
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(OkxClientError, match="未确认"):
            await client.cancel_all_after(20)
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "timestamp_scale",
    [1, 1_000],
)
async def test_cancel_all_after_accepts_matching_timeout(
    timestamp_scale: int,
) -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        timestamp = int(datetime.now(timezone.utc).timestamp() * timestamp_scale)
        return httpx.Response(
            200,
            json={
                "code": "0",
                "msg": "",
                "data": [
                    {
                        "triggerTime": str(timestamp + 20 * timestamp_scale),
                        "tag": "tideguarddemo",
                        "ts": str(timestamp),
                    }
                ],
            },
        )

    client = OkxClient(
        credentials_provider=lambda: Credentials("a", "b", "c"),
        transport=httpx.MockTransport(handler),
    )
    try:
        await client.cancel_all_after(20)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_cancel_all_after_rejects_mismatched_timeout() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        timestamp = int(datetime.now(timezone.utc).timestamp() * 1_000)
        return httpx.Response(
            200,
            json={
                "code": "0",
                "msg": "",
                "data": [
                    {
                        "triggerTime": str(timestamp + 1),
                        "tag": "tideguarddemo",
                        "ts": str(timestamp),
                    }
                ],
            },
        )

    client = OkxClient(
        credentials_provider=lambda: Credentials("a", "b", "c"),
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(OkxClientError, match="倒计时"):
            await client.cancel_all_after(20)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_cancel_all_after_rejects_stale_confirmation() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        timestamp = int(datetime.now(timezone.utc).timestamp() * 1_000) - 60_000
        return httpx.Response(
            200,
            json={
                "code": "0",
                "msg": "",
                "data": [
                    {
                        "triggerTime": str(timestamp + 20_000),
                        "tag": "tideguarddemo",
                        "ts": str(timestamp),
                    }
                ],
            },
        )

    client = OkxClient(
        credentials_provider=lambda: Credentials("a", "b", "c"),
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(OkxClientError, match="时间窗口"):
            await client.cancel_all_after(20)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_market_bundle_accepts_official_candle_rows() -> None:
    timestamp = str(int(datetime.now(timezone.utc).timestamp() * 1_000))
    candle = [timestamp, "1", "2", "0.5", "1.5", "10", "15", "15", "1"]

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v5/market/ticker":
            data: list[object] = [
                {"instId": "BTC-USDT", "last": "1.5", "ts": timestamp}
            ]
        elif request.url.path == "/api/v5/market/candles":
            data = [candle]
        elif request.url.path == "/api/v5/public/instruments":
            data = [{"instId": "BTC-USDT", "instType": "SPOT", "state": "live"}]
        else:
            raise AssertionError(request.url.path)
        return httpx.Response(200, json={"code": "0", "msg": "", "data": data})

    client = OkxClient(transport=httpx.MockTransport(handler))
    try:
        bundle = await client.get_market_bundle("BTC-USDT")
    finally:
        await client.close()

    assert bundle["candles"] == [candle]
    assert bundle["instrument"]["instType"] == "SPOT"


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["instrument", "price", "future", "timestamp"])
async def test_market_bundle_rejects_invalid_ticker(fault: str) -> None:
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1_000)
    ticker = {"instId": "BTC-USDT", "last": "1.5", "ts": str(now_ms)}
    if fault == "instrument":
        ticker["instId"] = "ETH-USDT"
    elif fault == "price":
        ticker["last"] = "0"
    elif fault == "future":
        ticker["ts"] = str(now_ms + 60_000)
    else:
        ticker["ts"] = "0"

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v5/market/ticker":
            data: list[object] = [ticker]
        elif request.url.path == "/api/v5/market/candles":
            data = [[str(now_ms), "1", "2", "0.5", "1.5", "10", "15", "15", "1"]]
        elif request.url.path == "/api/v5/public/instruments":
            data = [{"instId": "BTC-USDT", "instType": "SPOT", "state": "live"}]
        else:
            raise AssertionError(request.url.path)
        return httpx.Response(200, json={"code": "0", "msg": "", "data": data})

    client = OkxClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(OkxClientError, match="行情"):
            await client.get_market_bundle("BTC-USDT")
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "malformed",
    [
        ["1", "2", "3", "4", "5", "6", "7", "8"],
        ["1", "2", "3", "4", "5", "6", "7", "8", {"nested": True}],
    ],
)
async def test_candles_reject_nested_or_wrong_length_rows(
    malformed: list[object],
) -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"code": "0", "msg": "", "data": [malformed]}
        )

    client = OkxClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(OkxClientError, match="9 项标量数组"):
            await client.public_get("/api/v5/market/candles")
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_pending_orders_are_fully_paginated() -> None:
    cursors: list[str | None] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        after = request.url.params.get("after")
        cursors.append(after)
        if after is None:
            data = [{"ordId": str(value)} for value in range(200, 100, -1)]
        else:
            data = [{"ordId": "100", "clOrdId": "tg-late", "tag": "tideguarddemo"}]
        return httpx.Response(200, json={"code": "0", "msg": "", "data": data})

    client = OkxClient(
        credentials_provider=lambda: Credentials("a", "b", "c"),
        transport=httpx.MockTransport(handler),
    )
    try:
        orders = await client.get_pending_orders("BTC-USDT")
    finally:
        await client.close()

    assert len(orders) == 101
    assert cursors == [None, "101"]
    assert orders[-1]["clOrdId"] == "tg-late"


@pytest.mark.asyncio
async def test_pending_order_pagination_rejects_duplicate_order_ids() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("after") is None:
            data = [{"ordId": str(value)} for value in range(100, 0, -1)]
        else:
            data = [{"ordId": "1"}]
        return httpx.Response(200, json={"code": "0", "msg": "", "data": data})

    client = OkxClient(
        credentials_provider=lambda: Credentials("a", "b", "c"),
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(OkxClientError, match="唯一 ordId"):
            await client.get_pending_orders("BTC-USDT")
    finally:
        await client.close()
