from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable
from urllib.parse import urlencode

import httpx

from .config import (
    ALLOWED_INSTRUMENTS,
    ALLOWED_PRIVATE_ENDPOINTS,
    ALLOWED_PUBLIC_ENDPOINTS,
    OKX_BASE_URL,
    POLICY,
    SIMULATED_HEADER,
)
from .secrets import Credentials, credential_fingerprint, get_credentials


class OkxClientError(RuntimeError):
    pass


class OkxApiError(OkxClientError):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"OKX {code}: {message}")


class AmbiguousOrderError(OkxClientError):
    pass


class CredentialIdentityError(OkxClientError):
    pass


class DispatchBlockedError(OkxClientError):
    pass


class OkxClient:
    def __init__(
        self,
        *,
        credentials_provider: Callable[[], Credentials | None] = get_credentials,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._credentials_provider = credentials_provider
        self._client = httpx.AsyncClient(
            base_url=OKX_BASE_URL,
            timeout=httpx.Timeout(8.0, connect=5.0),
            transport=transport,
            headers={"User-Agent": "Tideguard/0.1 demo-only"},
        )

    async def close(self) -> None:
        await self._client.aclose()

    def _load_credentials(self) -> Credentials | None:
        try:
            return self._credentials_provider()
        except Exception as exc:
            raise CredentialIdentityError("无法从原生凭证库读取模拟盘凭证") from exc

    def current_credential_fingerprint(self) -> str:
        credentials = self._load_credentials()
        if credentials is None:
            raise CredentialIdentityError("模拟盘凭证不可用")
        return credential_fingerprint(credentials)

    @staticmethod
    def sign(
        secret: str,
        timestamp: str,
        method: str,
        request_path: str,
        body: str = "",
    ) -> str:
        prehash = f"{timestamp}{method.upper()}{request_path}{body}"
        digest = hmac.new(secret.encode(), prehash.encode(), hashlib.sha256).digest()
        return base64.b64encode(digest).decode()

    @staticmethod
    def _timestamp() -> str:
        return (
            datetime.now(timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )

    @staticmethod
    def _request_path(path: str, params: dict[str, str] | None) -> str:
        return f"{path}?{urlencode(params)}" if params else path

    @staticmethod
    def _validate_envelope(
        payload: Any, *, allow_candle_rows: bool = False
    ) -> list[Any]:
        if not isinstance(payload, dict):
            raise OkxClientError("OKX 响应结构异常")
        raw_code = payload.get("code")
        if not isinstance(raw_code, (str, int)) or isinstance(raw_code, bool):
            raise OkxClientError("OKX 响应缺少有效 code")
        code = str(raw_code)
        if code != "0":
            raise OkxApiError(code or "unknown", str(payload.get("msg", "请求失败")))
        data = payload.get("data", [])
        if not isinstance(data, list):
            raise OkxClientError("OKX 响应 data 结构异常")
        if allow_candle_rows:
            valid_candles = all(
                isinstance(item, list)
                and len(item) == 9
                and all(
                    isinstance(value, (str, int, float)) and not isinstance(value, bool)
                    for value in item
                )
                for item in data
            )
            if not valid_candles:
                raise OkxClientError("OKX K 线响应必须是 9 项标量数组")
        elif any(not isinstance(item, dict) for item in data):
            raise OkxClientError("OKX 响应 data 项结构异常")
        return data

    async def public_get(self, path: str, params: dict[str, str] | None = None) -> list[Any]:
        if path not in ALLOWED_PUBLIC_ENDPOINTS:
            raise OkxClientError("公共 API 路径不在白名单")
        request_path = self._request_path(path, params)
        response = await self._client.get(
            request_path,
            headers={"x-simulated-trading": SIMULATED_HEADER},
        )
        response.raise_for_status()
        return self._validate_envelope(
            response.json(),
            allow_candle_rows=path
            in {"/api/v5/market/candles", "/api/v5/market/history-candles"},
        )

    async def private_request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        body: dict[str, str] | None = None,
        order_dispatch: bool = False,
        expected_credential_fingerprint: str | None = None,
        dispatch_guard: Callable[[], None] | None = None,
    ) -> list[dict[str, Any]]:
        upper_method = method.upper()
        if (upper_method, path) not in ALLOWED_PRIVATE_ENDPOINTS:
            raise OkxClientError("私有 API 路径不在白名单")
        credentials = self._load_credentials()
        if credentials is None:
            raise OkxClientError("尚未在 Windows Credential Manager 配置凭证")
        current_fingerprint = credential_fingerprint(credentials)
        if expected_credential_fingerprint and not hmac.compare_digest(
            current_fingerprint, expected_credential_fingerprint
        ):
            raise CredentialIdentityError("模拟盘凭证身份已变化，操作被拒绝")

        request_path = self._request_path(path, params)
        body_text = json.dumps(body, separators=(",", ":"), ensure_ascii=False) if body else ""
        timestamp = self._timestamp()
        headers = {
            "Content-Type": "application/json",
            "OK-ACCESS-KEY": credentials.api_key,
            "OK-ACCESS-SIGN": self.sign(
                credentials.api_secret, timestamp, upper_method, request_path, body_text
            ),
            "OK-ACCESS-PASSPHRASE": credentials.passphrase,
            "OK-ACCESS-TIMESTAMP": timestamp,
            "x-simulated-trading": SIMULATED_HEADER,
        }
        if order_dispatch:
            now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
            headers["expTime"] = str(now_ms + POLICY.request_expiry_ms)
        try:
            if order_dispatch and dispatch_guard:
                dispatch_guard()
            response = await self._client.request(
                upper_method,
                request_path,
                headers=headers,
                content=body_text.encode("utf-8") if body_text else None,
            )
            response.raise_for_status()
            try:
                payload = response.json()
                if order_dispatch and isinstance(payload, dict):
                    outer_code = str(payload.get("code", ""))
                    raw_data = payload.get("data")
                    inner_timeout = isinstance(raw_data, list) and any(
                        isinstance(item, dict) and str(item.get("sCode", "")) == "50004"
                        for item in raw_data
                    )
                    if outer_code == "50004" or inner_timeout:
                        raise AmbiguousOrderError(
                            "OKX 返回 50004，下单结果未知，必须按 clOrdId 回查"
                        )
                return self._validate_envelope(payload)
            except OkxApiError as exc:
                if order_dispatch and exc.code == "50004":
                    raise AmbiguousOrderError("下单请求结果未知，必须按 clOrdId 回查") from exc
                raise
            except (OkxClientError, ValueError, TypeError) as exc:
                if order_dispatch:
                    raise AmbiguousOrderError("下单响应异常，必须按 clOrdId 回查") from exc
                if isinstance(exc, OkxClientError):
                    raise
                raise OkxClientError("OKX 响应无法解析") from exc
        except httpx.TimeoutException as exc:
            if order_dispatch:
                raise AmbiguousOrderError("下单请求超时，成交状态未知") from exc
            raise OkxClientError("OKX 请求超时") from exc
        except (httpx.RequestError, httpx.HTTPStatusError) as exc:
            if order_dispatch:
                raise AmbiguousOrderError("下单传输结果未知，必须按 clOrdId 回查") from exc
            raise OkxClientError("OKX 网络或 HTTP 请求失败") from exc

    async def get_market_bundle(self, inst_id: str = "BTC-USDT") -> dict[str, Any]:
        ticker = await self.public_get("/api/v5/market/ticker", {"instId": inst_id})
        candles = await self.public_get(
            "/api/v5/market/candles", {"instId": inst_id, "bar": "5m", "limit": "96"}
        )
        instruments = await self.public_get(
            "/api/v5/public/instruments", {"instType": "SPOT", "instId": inst_id}
        )
        if not ticker or not instruments:
            raise OkxClientError("OKX 未返回所需现货数据")
        ticker_item = ticker[0]
        if ticker_item.get("instId") != inst_id:
            raise OkxClientError("OKX 行情品种与请求不一致")
        try:
            last = Decimal(str(ticker_item.get("last", "")))
        except (InvalidOperation, ValueError):
            raise OkxClientError("OKX 行情缺少有效最新价") from None
        if not last.is_finite() or last <= 0:
            raise OkxClientError("OKX 行情缺少有效最新价")
        ticker_timestamp_text = str(ticker_item.get("ts", "")).strip()
        if not ticker_timestamp_text.isdigit():
            raise OkxClientError("OKX 行情缺少有效时间戳")
        ticker_timestamp = int(ticker_timestamp_text)
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        if ticker_timestamp <= 0 or ticker_timestamp > now_ms + POLICY.request_expiry_ms:
            raise OkxClientError("OKX 行情时间戳无效或来自未来")
        instrument = instruments[0]
        if instrument.get("instType") != "SPOT" or instrument.get("instId") != inst_id:
            raise OkxClientError("交易品种未被 OKX 确认为 SPOT")
        return {"ticker": ticker[0], "candles": candles, "instrument": instrument}

    async def get_history_candles(
        self,
        inst_id: str = "BTC-USDT",
        *,
        bar: str = "5m",
        limit: int = 2_000,
    ) -> list[list[Any]]:
        if inst_id not in ALLOWED_INSTRUMENTS or bar != "5m":
            raise OkxClientError("模型训练只允许 BTC-USDT 的 5m 公共 K 线")
        if not 300 <= limit <= 5_000:
            raise OkxClientError("模型训练 K 线数量必须在 300–5000 之间")

        rows_by_timestamp: dict[int, list[Any]] = {}
        after: str | None = None
        while len(rows_by_timestamp) < limit:
            page_size = min(300, limit - len(rows_by_timestamp))
            params = {"instId": inst_id, "bar": bar, "limit": str(page_size)}
            if after:
                params["after"] = after
            page = await self.public_get("/api/v5/market/history-candles", params)
            if not page:
                break

            timestamps: list[int] = []
            for row in page:
                timestamp_text = str(row[0]).strip()
                if not timestamp_text.isdigit():
                    raise OkxClientError("OKX 历史 K 线包含无效时间戳")
                timestamp = int(timestamp_text)
                timestamps.append(timestamp)
                if str(row[8]) == "1":
                    rows_by_timestamp[timestamp] = row

            next_after = str(min(timestamps))
            if next_after == after:
                raise OkxClientError("OKX 历史 K 线分页游标未前进")
            after = next_after
            if len(page) < page_size:
                break

        return [rows_by_timestamp[key] for key in sorted(rows_by_timestamp)][-limit:]

    async def get_account_balance(
        self, *, expected_credential_fingerprint: str | None = None
    ) -> list[dict[str, Any]]:
        return await self.private_request(
            "GET",
            "/api/v5/account/balance",
            expected_credential_fingerprint=expected_credential_fingerprint,
        )

    async def get_account_config(
        self, *, expected_credential_fingerprint: str | None = None
    ) -> list[dict[str, Any]]:
        return await self.private_request(
            "GET",
            "/api/v5/account/config",
            expected_credential_fingerprint=expected_credential_fingerprint,
        )

    async def get_pending_orders(
        self,
        inst_id: str = "BTC-USDT",
        *,
        expected_credential_fingerprint: str | None = None,
    ) -> list[dict[str, Any]]:
        orders: list[dict[str, Any]] = []
        seen_order_ids: set[str] = set()
        after: str | None = None
        for _ in range(5):
            params = {"instType": "SPOT", "instId": inst_id, "limit": "100"}
            if after:
                params["after"] = after
            page = await self.private_request(
                "GET",
                "/api/v5/trade/orders-pending",
                params=params,
                expected_credential_fingerprint=expected_credential_fingerprint,
            )
            for order in page:
                ord_id = str(order.get("ordId", "")).strip()
                if not ord_id or ord_id in seen_order_ids:
                    raise OkxClientError("OKX 挂单分页缺少唯一 ordId")
                seen_order_ids.add(ord_id)
                orders.append(order)
            if len(page) < 100:
                return orders
            cursor = str(page[-1].get("ordId", "")).strip()
            if not cursor or cursor == after:
                raise OkxClientError("OKX 挂单分页游标无效")
            after = cursor
        return orders

    async def get_order(
        self,
        inst_id: str,
        cl_ord_id: str,
        *,
        expected_credential_fingerprint: str | None = None,
    ) -> list[dict[str, Any]]:
        return await self.private_request(
            "GET",
            "/api/v5/trade/order",
            params={"instId": inst_id, "clOrdId": cl_ord_id},
            expected_credential_fingerprint=expected_credential_fingerprint,
        )

    async def place_order(
        self,
        *,
        inst_id: str,
        side: str,
        ord_type: str,
        price: str,
        size: str,
        cl_ord_id: str,
        expected_credential_fingerprint: str | None = None,
        dispatch_guard: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        body = {
            "instId": inst_id,
            "tdMode": "cash",
            "side": side,
            "ordType": ord_type,
            "px": price,
            "sz": size,
            "clOrdId": cl_ord_id,
            "tag": "tideguarddemo",
        }
        data = await self.private_request(
            "POST",
            "/api/v5/trade/order",
            body=body,
            order_dispatch=True,
            expected_credential_fingerprint=expected_credential_fingerprint,
            dispatch_guard=dispatch_guard,
        )
        if not data:
            raise AmbiguousOrderError("OKX 未返回订单结果，必须按 clOrdId 回查")
        item = data[0]
        item_code = str(item.get("sCode", ""))
        if not item_code:
            raise AmbiguousOrderError("OKX 下单响应缺少 sCode，必须按 clOrdId 回查")
        if item_code == "50004":
            raise AmbiguousOrderError("OKX 返回 50004，必须按 clOrdId 回查")
        if item_code != "0":
            raise OkxApiError(item_code, str(item.get("sMsg", "下单被拒绝")))
        if not str(item.get("ordId", "")).strip():
            raise AmbiguousOrderError("OKX 接受响应缺少 ordId，必须按 clOrdId 回查")
        if str(item.get("clOrdId", "")).strip() != cl_ord_id:
            raise AmbiguousOrderError("OKX 下单响应 clOrdId 不匹配，必须回查")
        if str(item.get("tag", "")) != "tideguarddemo":
            raise AmbiguousOrderError("OKX 下单响应 tag 不匹配，必须回查")
        return item

    async def cancel_order(
        self,
        inst_id: str,
        cl_ord_id: str,
        *,
        expected_credential_fingerprint: str | None = None,
    ) -> dict[str, Any]:
        data = await self.private_request(
            "POST",
            "/api/v5/trade/cancel-order",
            body={"instId": inst_id, "clOrdId": cl_ord_id},
            expected_credential_fingerprint=expected_credential_fingerprint,
        )
        if not data:
            raise OkxClientError("OKX 未返回撤单请求结果")
        item = data[0]
        item_code = str(item.get("sCode", ""))
        if not item_code:
            raise OkxClientError("OKX 撤单响应缺少 sCode")
        if item_code != "0":
            raise OkxApiError(item_code, str(item.get("sMsg", "撤单请求被拒绝")))
        return item

    async def cancel_all_after(
        self,
        seconds: int,
        *,
        expected_credential_fingerprint: str | None = None,
    ) -> None:
        if seconds not in {0, *range(10, 121)}:
            raise OkxClientError("Cancel-All-After 仅允许 0 或 10–120 秒")
        request_started_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        data = await self.private_request(
            "POST",
            "/api/v5/trade/cancel-all-after",
            body={"timeOut": str(seconds), "tag": "tideguarddemo"},
            expected_credential_fingerprint=expected_credential_fingerprint,
        )
        response_received_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        if not data:
            raise OkxClientError("OKX 未确认 Cancel-All-After")
        item = data[0]
        if str(item.get("tag", "")) != "tideguarddemo":
            raise OkxClientError("OKX CAA 响应 tag 不匹配")
        trigger_text = str(item.get("triggerTime", "")).strip()
        if not trigger_text.isdigit():
            raise OkxClientError("OKX CAA 响应缺少有效 triggerTime")
        timestamp_text = str(item.get("ts", "")).strip()
        if not timestamp_text.isdigit() or int(timestamp_text) <= 0:
            raise OkxClientError("OKX CAA 响应缺少有效 ts")
        trigger_time = int(trigger_text)
        timestamp = int(timestamp_text)
        if seconds == 0 and trigger_time != 0:
            raise OkxClientError("OKX CAA 响应未确认请求的启用状态")
        if seconds > 0:
            timestamp_scale = 1_000 if timestamp >= 1_000_000_000_000 else 1
            if trigger_time - timestamp != seconds * timestamp_scale:
                raise OkxClientError("OKX CAA 响应倒计时与请求不一致")
        timestamp_ms = timestamp if timestamp >= 1_000_000_000_000 else timestamp * 1_000
        if not (
            request_started_ms - POLICY.request_expiry_ms
            <= timestamp_ms
            <= response_received_ms + POLICY.request_expiry_ms
        ):
            raise OkxClientError("OKX CAA 响应 ts 不在当前请求时间窗口")
