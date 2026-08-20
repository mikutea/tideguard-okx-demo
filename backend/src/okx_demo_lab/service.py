from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import sqlite3
import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable

from .audit import AuditStore, IntentIdentityConflict, utc_now
from .config import POLICY
from .models import OrderDraft
from .okx_client import (
    AmbiguousOrderError,
    CredentialIdentityError,
    DispatchBlockedError,
    OkxApiError,
    OkxClient,
    OkxClientError,
)
from .risk import account_context, evaluate
from .secrets import credentials_configured
from .state import SafetyController, SafetyError


TERMINAL_OKX_ORDER_STATES = frozenset({"filled", "canceled", "mmp_canceled"})
LIVE_OKX_ORDER_STATES = frozenset({"live", "partially_filled"})
CAA_MIN_INTERVAL_SECONDS = 1.05


class CommitBlockedBeforeDispatch(ValueError):
    """A final in-process gate proved the HTTP order was never sent."""



def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _intent_digest(
    intent_id: str,
    payload: dict[str, Any],
    decision: dict[str, Any],
    authorization: dict[str, Any],
) -> str:
    return hashlib.sha256(
        _json(
            {
                "authorization": authorization,
                "decision": decision,
                "intent": intent_id,
                "payload": payload,
            }
        ).encode()
    ).hexdigest()


def _account_fingerprint(config: list[dict[str, Any]]) -> str:
    if not config:
        raise CredentialIdentityError("OKX 未返回模拟账户身份")
    uid = str(config[0].get("uid", "")).strip()
    main_uid = str(config[0].get("mainUid", "")).strip()
    if not uid:
        raise CredentialIdentityError("OKX 模拟账户配置缺少 uid")
    material = f"Tideguard.OKX.Demo\0{main_uid}\0{uid}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()


class TradingService:
    def __init__(self, client: OkxClient, store: AuditStore, safety: SafetyController):
        self.client = client
        self.store = store
        self.safety = safety
        self._trade_lock = asyncio.Lock()
        self._caa_lock = asyncio.Lock()
        self._kill_requested = asyncio.Event()
        self._deadman_valid_until: float | None = None
        self._exchange_deadman_valid_until: float | None = None
        self._caa_outcome_unknown = False
        self._last_caa_attempt_at: float | None = None
        self._caa_sleep = asyncio.sleep
        self._emergency_epoch = 0
        self._persisted_kill_epoch = 0

    def _signal_kill(self) -> int:
        self._emergency_epoch += 1
        self._kill_requested.set()
        self._deadman_valid_until = None
        return self._emergency_epoch

    def _engage_kill(
        self,
        reason: str,
        actor: str = "user",
        *,
        already_signaled: bool = False,
    ) -> dict[str, Any]:
        epoch = self._emergency_epoch if already_signaled else self._signal_kill()
        try:
            result = self.safety.engage_kill(reason, actor=actor)
            self._persisted_kill_epoch = max(self._persisted_kill_epoch, epoch)
            return result
        except Exception:
            self.safety.abort_arm_in_memory()
            return {
                "mode": "killed",
                "armedRemainingSeconds": 0,
                "killActive": True,
                "identityBound": False,
                "armedUntil": None,
            }

    def _require_audit_integrity(self, *, dispatch: bool = False) -> None:
        try:
            valid = self.store.verify_chain()
        except Exception:
            valid = False
        if valid:
            return
        self._engage_kill("本地审计链完整性校验失败", actor="system")
        if dispatch:
            raise DispatchBlockedError("审计链无效，订单派发已被阻止")
        raise SafetyError("本地审计链无效；急停保持锁定")

    def _record_deadman_success(self) -> None:
        now = time.monotonic()
        self._exchange_deadman_valid_until = now + POLICY.deadman_seconds
        if self._kill_requested.is_set() or self.safety.status()["mode"] != "armed":
            raise DispatchBlockedError("CAA 确认到达时授权已被撤销")
        self._deadman_valid_until = now + POLICY.deadman_local_lease_seconds

    async def _write_caa(
        self,
        seconds: int,
        credential_fingerprint: str,
        *,
        emergency: bool = False,
    ) -> None:
        now = time.monotonic()
        if self._last_caa_attempt_at is not None:
            delay = CAA_MIN_INTERVAL_SECONDS - (now - self._last_caa_attempt_at)
            if delay > 0:
                await self._caa_sleep(delay)
        if not emergency:
            try:
                mode = self.safety.status()["mode"]
            except Exception:
                mode = "unknown"
            if self._kill_requested.is_set() or mode != "armed":
                raise DispatchBlockedError("CAA 写入前授权已被撤销")
        self._last_caa_attempt_at = time.monotonic()
        await self.client.cancel_all_after(
            seconds,
            expected_credential_fingerprint=credential_fingerprint,
        )

    def _dispatch_guard(self) -> None:
        self._require_audit_integrity(dispatch=True)
        try:
            mode = self.safety.status()["mode"]
        except Exception:
            mode = "unknown"
        lease_valid = (
            self._deadman_valid_until is not None
            and time.monotonic() < self._deadman_valid_until
        )
        if self._kill_requested.is_set() or mode != "armed" or not lease_valid:
            self._engage_kill("CAA 本地安全租约无效", actor="system")
            raise DispatchBlockedError("急停或授权状态已阻止订单派发")

    async def arm(self, confirmation: str) -> dict[str, Any]:
        async with self._trade_lock:
            self._require_audit_integrity()
            if confirmation != "DEMO":
                raise SafetyError("请输入 DEMO 以启用限时模拟下单。")
            if not credentials_configured():
                raise ValueError("请先在 Windows Credential Manager 配置模拟盘凭证")
            credential_fingerprint = self.client.current_credential_fingerprint()
            config = await self.client.get_account_config(
                expected_credential_fingerprint=credential_fingerprint
            )
            account_fingerprint = _account_fingerprint(config)
            potential_bindings = set(self.store.potential_order_identity_bindings())
            expected_binding = (credential_fingerprint, account_fingerprint)
            if potential_bindings and potential_bindings != {expected_binding}:
                raise ValueError(
                    "本地存在其他模拟账户身份的潜在订单；请先用原凭证核对并结案"
                )
            self._kill_requested.clear()
            self._deadman_valid_until = None
            self._exchange_deadman_valid_until = None
            try:
                state = self.safety.arm(
                    confirmation,
                    credential_fingerprint,
                    account_fingerprint,
                )
                async with self._caa_lock:
                    self._caa_outcome_unknown = True
                    await self._write_caa(
                        POLICY.deadman_seconds,
                        credential_fingerprint,
                    )
                    try:
                        self._record_deadman_success()
                    finally:
                        self._caa_outcome_unknown = False
                if self._kill_requested.is_set() or self.safety.status()["mode"] != "armed":
                    raise ValueError("启用过程中触发了急停，授权未生效")
                return state
            except BaseException:
                self._signal_kill()
                try:
                    if self.store.get_flag("kill_active") != "true":
                        self._engage_kill(
                            "模拟盘授权初始化失败",
                            actor="system",
                            already_signaled=True,
                        )
                    else:
                        self.safety.abort_arm_in_memory()
                except Exception:
                    self.safety.abort_arm_in_memory()
                raise

    async def current_identity_binding(self) -> tuple[str, str]:
        """Return non-secret fingerprints for the currently configured Demo account."""

        self._require_audit_integrity()
        if not credentials_configured():
            raise ValueError("请先在 Windows Credential Manager 配置模拟盘凭证")
        credential_fingerprint = self.client.current_credential_fingerprint()
        config = await self.client.get_account_config(
            expected_credential_fingerprint=credential_fingerprint
        )
        return credential_fingerprint, _account_fingerprint(config)

    async def arm_supervised(
        self,
        decision_id: str,
        expected_binding: tuple[str, str],
        *,
        purpose: str,
    ) -> dict[str, Any]:
        """Create a short order-dispatch arm after the coordinator verifies Codex.

        This method has no HTTP route.  It revalidates the Demo credential and
        OKX UID binding itself, refuses a pending kill signal, and establishes a
        fresh CAA lease before returning.
        """

        async with self._trade_lock:
            self._require_audit_integrity()
            if self._kill_requested.is_set():
                raise SafetyError("存在未复位的急停信号，监督授权被拒绝")
            current_binding = await self.current_identity_binding()
            if not hmac.compare_digest(current_binding[0], expected_binding[0]) or not hmac.compare_digest(
                current_binding[1], expected_binding[1]
            ):
                self._engage_kill("Codex 监督账户身份不匹配", actor="system")
                raise CredentialIdentityError("当前模拟账户与长期授权绑定不一致")
            potential_bindings = set(self.store.potential_order_identity_bindings())
            if potential_bindings and potential_bindings != {expected_binding}:
                self._engage_kill("Codex 监督检测到跨账户潜在订单", actor="system")
                raise CredentialIdentityError("存在其他账户身份的潜在订单")
            self._deadman_valid_until = None
            self._exchange_deadman_valid_until = None
            try:
                state = self.safety.arm_supervised(
                    decision_id,
                    current_binding[0],
                    current_binding[1],
                    purpose=purpose,
                )
                async with self._caa_lock:
                    self._caa_outcome_unknown = True
                    await self._write_caa(
                        POLICY.deadman_seconds,
                        current_binding[0],
                    )
                    try:
                        self._record_deadman_success()
                    finally:
                        self._caa_outcome_unknown = False
                if self._kill_requested.is_set() or self.safety.status()["mode"] != "armed":
                    raise SafetyError("监督授权期间触发急停")
                return state
            except BaseException:
                self._signal_kill()
                try:
                    if self.store.get_flag("kill_active") != "true":
                        self._engage_kill(
                            "Codex 监督授权初始化失败",
                            actor="system",
                            already_signaled=True,
                        )
                    else:
                        self.safety.abort_arm_in_memory()
                except Exception:
                    self.safety.abort_arm_in_memory()
                raise

    def _active_identity(self) -> tuple[str, str]:
        credential_fingerprint, account_fingerprint = self.safety.armed_identity()
        try:
            current = self.client.current_credential_fingerprint()
        except OkxClientError:
            self._engage_kill("演练期间模拟盘凭证不可用", actor="system")
            raise
        if not hmac.compare_digest(current, credential_fingerprint):
            self._engage_kill("演练期间模拟盘凭证身份发生变化", actor="system")
            raise CredentialIdentityError("模拟盘凭证身份已变化；已触发急停")
        return credential_fingerprint, account_fingerprint

    async def renew_deadman(self) -> None:
        async with self._caa_lock:
            credential_fingerprint, _ = self._active_identity()
            self._caa_outcome_unknown = True
            await self._write_caa(
                POLICY.deadman_seconds,
                credential_fingerprint,
            )
            try:
                self._record_deadman_success()
            finally:
                self._caa_outcome_unknown = False

    async def disarm(self, reason: str = "user") -> dict[str, Any]:
        async with self._trade_lock:
            self._deadman_valid_until = None
            return self.safety.disarm(reason)

    async def market(self) -> dict[str, Any]:
        bundle = await self.client.get_market_bundle("BTC-USDT")
        ticker = bundle["ticker"]
        candles = []
        for row in reversed(bundle["candles"]):
            if len(row) < 9:
                continue
            candles.append(
                {
                    "ts": row[0],
                    "open": row[1],
                    "high": row[2],
                    "low": row[3],
                    "close": row[4],
                    "volume": row[5],
                    "confirmed": row[8] == "1",
                }
            )
        return {
            "source": "OKX 公共行情",
            "environment": "demo-app",
            "instrument": {
                "instId": bundle["instrument"].get("instId"),
                "instType": bundle["instrument"].get("instType"),
                "state": bundle["instrument"].get("state"),
                "tickSize": bundle["instrument"].get("tickSz"),
                "lotSize": bundle["instrument"].get("lotSz"),
                "minSize": bundle["instrument"].get("minSz"),
            },
            "ticker": {
                "last": ticker.get("last"),
                "open24h": ticker.get("open24h"),
                "high24h": ticker.get("high24h"),
                "low24h": ticker.get("low24h"),
                "volume24h": ticker.get("vol24h"),
                "volumeCcy24h": ticker.get("volCcy24h"),
                "bid": ticker.get("bidPx"),
                "ask": ticker.get("askPx"),
                "ts": ticker.get("ts"),
            },
            "candles": candles,
        }

    async def account(self) -> dict[str, Any]:
        if not credentials_configured():
            return {
                "configured": False,
                "source": "尚无模拟账户数据",
                "equityUsdt": None,
                "balances": [],
            }
        binding = self.safety.armed_identity_or_none()
        expected = self._active_identity()[0] if binding else None
        data = await self.client.get_account_balance(
            expected_credential_fingerprint=expected
        )
        if not data:
            return {"configured": True, "source": "OKX 模拟账户", "equityUsdt": None, "balances": []}
        account = data[0]
        balances = []
        for item in account.get("details", []):
            if item.get("ccy") not in {"BTC", "USDT"}:
                continue
            balances.append(
                {
                    "currency": item.get("ccy"),
                    "available": item.get("availBal"),
                    "cashBalance": item.get("cashBal"),
                    "equity": item.get("eq"),
                }
            )
        return {
            "configured": True,
            "source": "OKX 模拟账户",
            "equityUsdt": account.get("totalEq"),
            "updatedAt": account.get("uTime"),
            "balances": balances,
        }

    async def pending_orders(self) -> list[dict[str, Any]]:
        if not credentials_configured():
            return []
        binding = self.safety.armed_identity_or_none()
        expected = self._active_identity()[0] if binding else None
        orders = await self.client.get_pending_orders(
            expected_credential_fingerprint=expected
        )
        return [self._safe_order(order) for order in orders if order.get("tag") == "tideguarddemo"]

    async def inspect_intent_order(
        self,
        intent_id: str,
        expected_binding: tuple[str, str],
    ) -> dict[str, Any]:
        """Resolve one Tideguard intent to a verified OKX order without arming."""

        record = self.store.get_intent(intent_id)
        if record is None or not record.get("commit_key"):
            raise ValueError("订单意图不存在或尚未派发")
        if not hmac.compare_digest(
            str(record.get("credential_fingerprint") or ""), expected_binding[0]
        ) or not hmac.compare_digest(
            str(record.get("account_fingerprint") or ""), expected_binding[1]
        ):
            self._engage_kill("自动订单回查账户身份不匹配", actor="system")
            raise CredentialIdentityError("订单意图不属于长期授权账户")
        current_binding = await self.current_identity_binding()
        if not hmac.compare_digest(current_binding[0], expected_binding[0]) or not hmac.compare_digest(
            current_binding[1], expected_binding[1]
        ):
            self._engage_kill("自动订单回查期间账户身份变化", actor="system")
            raise CredentialIdentityError("当前模拟账户身份已变化")
        cl_ord_id = str(record.get("cl_ord_id") or "").strip()
        known_ord_id = str(record.get("okx_ord_id") or "").strip()
        if not cl_ord_id:
            self._engage_kill("自动订单缺少 clOrdId", actor="system")
            raise OkxClientError("订单意图缺少 clOrdId")
        found = await self.client.get_order(
            "BTC-USDT",
            cl_ord_id,
            expected_credential_fingerprint=expected_binding[0],
        )
        if len(found) != 1:
            raise OkxClientError("OKX 未唯一确认自动订单")
        order = found[0]
        draft = OrderDraft.model_validate_json(record["payload_json"])
        ord_id = str(order.get("ordId") or "").strip()
        state = str(order.get("state") or "").strip().lower()
        if (
            not ord_id
            or str(order.get("clOrdId") or "").strip() != cl_ord_id
            or str(order.get("tag") or "") != "tideguarddemo"
            or str(order.get("instId") or "") != draft.instId
            or str(order.get("side") or "") != draft.side
            or str(order.get("ordType") or "") != draft.ordType
            or (known_ord_id and not hmac.compare_digest(ord_id, known_ord_id))
        ):
            self._engage_kill("自动订单回显身份不一致", actor="system")
            raise OkxClientError("OKX 自动订单回显不匹配")
        if state not in TERMINAL_OKX_ORDER_STATES | LIVE_OKX_ORDER_STATES:
            raise OkxClientError("OKX 自动订单状态未知")
        if state in TERMINAL_OKX_ORDER_STATES:
            self.store.update_intent(
                intent_id,
                status="terminal_verified",
                okx_ord_id=ord_id,
            )
            self.store.append(
                "order.terminal_verified",
                {
                    "intentId": intent_id,
                    "clOrdId": cl_ord_id,
                    "ordId": ord_id,
                    "state": state,
                    "actor": "autonomy",
                },
                actor="system",
                correlation_id=intent_id,
            )
        return {
            "intentId": intent_id,
            "clOrdId": cl_ord_id,
            "ordId": ord_id,
            "instId": draft.instId,
            "side": draft.side,
            "ordType": draft.ordType,
            "requestedSize": str(draft.size),
            "filledSize": str(order.get("accFillSz") or "0"),
            "averagePrice": str(order.get("avgPx") or ""),
            "state": state,
            "fee": str(order.get("fee") or "0"),
            "feeCurrency": str(order.get("feeCcy") or ""),
            "updatedAt": order.get("uTime"),
        }

    @staticmethod
    def _safe_order(order: dict[str, Any]) -> dict[str, Any]:
        return {
            "ordId": order.get("ordId"),
            "clOrdId": order.get("clOrdId"),
            "instId": order.get("instId"),
            "side": order.get("side"),
            "ordType": order.get("ordType"),
            "price": order.get("px"),
            "size": order.get("sz"),
            "filledSize": order.get("accFillSz"),
            "state": order.get("state"),
            "createdAt": order.get("cTime"),
            "updatedAt": order.get("uTime"),
            "source": "OKX 模拟账户",
        }

    async def _risk_inputs(
        self,
        draft: OrderDraft,
        credential_fingerprint: str,
    ) -> tuple[dict[str, Any], dict[str, Any], Any, int]:
        bundle = await self.client.get_market_bundle(draft.instId)
        balance_data: list[dict[str, Any]] = []
        pending: list[dict[str, Any]] = []
        balance_data = await self.client.get_account_balance(
            expected_credential_fingerprint=credential_fingerprint
        )
        pending = await self.client.get_pending_orders(
            draft.instId,
            expected_credential_fingerprint=credential_fingerprint,
        )
        account = account_context(balance_data, True)
        return bundle["ticker"], bundle["instrument"], account, len(pending)

    async def preview(
        self,
        draft: OrderDraft,
        *,
        supervisor_decision_id: str | None = None,
        supervisor_purpose: str | None = None,
    ) -> dict[str, Any]:
        supervised_context = self.safety.supervised_context()
        if supervised_context is not None:
            supplied_context = (supervisor_decision_id, supervisor_purpose)
            if supplied_context != supervised_context:
                raise SafetyError(
                    "Codex 监督授权不能创建浏览器或其他用途的订单预检"
                )
            authorization_kind = "supervisor"
        else:
            if supervisor_decision_id is not None or supervisor_purpose is not None:
                raise SafetyError("当前授权不是 Codex 监督授权")
            authorization_kind = "manual"
        credential_fingerprint, account_fingerprint = self._active_identity()
        try:
            ticker, instrument, account, open_orders = await self._risk_inputs(
                draft, credential_fingerprint
            )
        except CredentialIdentityError:
            self._engage_kill("预检期间模拟盘凭证身份发生变化", actor="system")
            raise
        decision = evaluate(
            draft,
            ticker=ticker,
            instrument=instrument,
            account=account,
            safety_mode=str(self.safety.status()["mode"]),
            open_orders=open_orders,
        )
        intent_id = uuid.uuid4().hex
        cl_ord_id = f"tg{uuid.uuid4().hex[:24]}"
        created_at = datetime.now(timezone.utc)
        expires_at = created_at + timedelta(seconds=POLICY.preview_ttl_seconds)
        payload = draft.model_dump(mode="json")
        decision_data = decision.model_dump(mode="json")
        authorization = {
            "kind": authorization_kind,
            "supervisorDecisionId": supervisor_decision_id,
            "supervisorPurpose": supervisor_purpose,
        }
        digest = _intent_digest(intent_id, payload, decision_data, authorization)
        self.store.save_intent(
            {
                "intent_id": intent_id,
                "created_at": created_at.isoformat().replace("+00:00", "Z"),
                "expires_at": expires_at.isoformat().replace("+00:00", "Z"),
                "payload_json": _json(payload),
                "decision_json": _json(decision_data),
                "digest": digest,
                "cl_ord_id": cl_ord_id,
                "status": "previewed" if decision.allowed else "rejected",
                "credential_fingerprint": credential_fingerprint,
                "account_fingerprint": account_fingerprint,
                "authorization_kind": authorization_kind,
                "supervisor_decision_id": supervisor_decision_id,
                "supervisor_purpose": supervisor_purpose,
            }
        )
        self.store.append(
            "order.previewed" if decision.allowed else "risk.rejected",
            {
                "intentId": intent_id,
                "instrument": draft.instId,
                "side": draft.side,
                "notional": str(draft.price * draft.size),
                "allowed": decision.allowed,
                "reasonCodes": decision.reasonCodes,
                "policyVersion": decision.policyVersion,
            },
            actor="codex-supervisor" if authorization_kind == "supervisor" else "user",
            correlation_id=intent_id,
        )
        return {
            "intentId": intent_id,
            "digest": digest,
            "expiresAt": expires_at.isoformat().replace("+00:00", "Z"),
            "order": payload,
            "notionalUsdt": str(draft.price * draft.size),
            "decision": decision_data,
        }

    def _hold_for_manual_review(self, intent_id: str, reason: str) -> None:
        epoch = self._signal_kill()
        try:
            self.store.mark_manual_review_and_kill(intent_id)
            self._persisted_kill_epoch = max(self._persisted_kill_epoch, epoch)
        except Exception:
            try:
                self._engage_kill(reason, actor="system", already_signaled=True)
            except Exception:
                pass
            raise
        try:
            self.safety.acknowledge_persisted_kill(reason, actor="system")
        except Exception:
            self.safety.abort_arm_in_memory()
            raise

    async def _reconcile_uncertain(
        self,
        intent_id: str,
        draft: OrderDraft,
        cl_ord_id: str,
        credential_fingerprint: str,
    ) -> dict[str, Any]:
        try:
            self._hold_for_manual_review(intent_id, "订单结果不确定，正在按 clOrdId 回查")
            self.store.append(
                "order.uncertain",
                {"intentId": intent_id, "clOrdId": cl_ord_id, "action": "query_only"},
                correlation_id=intent_id,
            )
        except Exception as exc:
            raise OkxClientError("订单结果不确定且本地记录失败；已阻止继续派发") from exc
        try:
            found = await self.client.get_order(
                draft.instId,
                cl_ord_id,
                expected_credential_fingerprint=credential_fingerprint,
            )
        except asyncio.CancelledError:
            try:
                self.store.append(
                    "order.reconcile_cancelled",
                    {"intentId": intent_id},
                    correlation_id=intent_id,
                )
            except Exception:
                pass
            raise
        except OkxClientError:
            found = []
        except Exception as exc:
            try:
                self.store.append(
                    "order.lifecycle_error",
                    {"intentId": intent_id, "errorType": type(exc).__name__},
                    correlation_id=intent_id,
                )
            except Exception:
                pass
            raise OkxClientError("订单回查异常，已锁定并要求人工核对") from exc

        if found:
            order = found[0]
            ord_id = str(order.get("ordId", "")).strip()
            returned_cl_ord_id = str(order.get("clOrdId", "")).strip()
            if ord_id and returned_cl_ord_id == cl_ord_id:
                try:
                    self.store.update_intent(
                        intent_id, status="reconciled", okx_ord_id=ord_id
                    )
                    self.store.append(
                        "order.reconciled",
                        {"intentId": intent_id, "ordId": ord_id},
                        correlation_id=intent_id,
                    )
                except Exception as exc:
                    raise OkxClientError(
                        "订单已回查但本地结案失败；急停保持锁定"
                    ) from exc
                return {
                    "intentId": intent_id,
                    "status": "reconciled",
                    "ordId": ord_id,
                    "replayed": False,
                }

        return {"intentId": intent_id, "status": "manual_review", "ordId": None, "replayed": False}

    async def commit(
        self,
        intent_id: str,
        digest: str,
        idempotency_key: str,
        *,
        additional_dispatch_guard: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        async with self._trade_lock:
            return await self._commit_locked(
                intent_id,
                digest,
                idempotency_key,
                additional_dispatch_guard=additional_dispatch_guard,
            )

    async def _commit_locked(
        self,
        intent_id: str,
        digest: str,
        idempotency_key: str,
        *,
        additional_dispatch_guard: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        record = self.store.get_intent(intent_id)
        if record is None:
            raise ValueError("预检不存在")
        if record.get("commit_key") == idempotency_key:
            return {
                "intentId": intent_id,
                "status": record["status"],
                "ordId": record.get("okx_ord_id"),
                "replayed": True,
            }
        if record.get("commit_key"):
            raise ValueError("该预检已使用另一个幂等键提交")
        if record["status"] != "previewed":
            raise ValueError("只有通过风控且未使用的预检才能提交")
        authorization_kind = str(record.get("authorization_kind") or "manual")
        supervisor_context = self.safety.supervised_context()
        if authorization_kind == "supervisor":
            expected_context = (
                str(record.get("supervisor_decision_id") or ""),
                str(record.get("supervisor_purpose") or ""),
            )
            if supervisor_context != expected_context:
                raise ValueError("Codex 监督订单与当前短时授权不匹配")
        elif authorization_kind == "manual":
            if supervisor_context is not None:
                raise ValueError("Codex 监督授权不能提交人工订单预检")
        else:
            self._engage_kill("订单授权类型损坏", actor="system")
            raise ValueError("订单授权类型无效；已触发急停")
        try:
            canonical_payload = json.loads(str(record["payload_json"]))
            canonical_decision = json.loads(str(record["decision_json"]))
        except (json.JSONDecodeError, TypeError) as exc:
            self._engage_kill("订单预检持久状态损坏", actor="system")
            raise ValueError("订单预检无法复核；已触发急停") from exc
        expected_digest = _intent_digest(
            intent_id,
            canonical_payload,
            canonical_decision,
            {
                "kind": authorization_kind,
                "supervisorDecisionId": record.get("supervisor_decision_id"),
                "supervisorPurpose": record.get("supervisor_purpose"),
            },
        )
        if not hmac.compare_digest(str(record["digest"]), expected_digest):
            self._engage_kill("订单预检摘要持久状态不一致", actor="system")
            raise ValueError("订单预检完整性校验失败；已触发急停")
        if not hmac.compare_digest(str(record["digest"]), digest):
            raise ValueError("预检摘要不匹配")
        expires_at = datetime.fromisoformat(record["expires_at"].replace("Z", "+00:00"))
        if datetime.now(timezone.utc) >= expires_at:
            self.store.update_intent(intent_id, status="expired")
            raise ValueError("预检已过期，请重新检查")
        if self.safety.status()["mode"] != "armed":
            raise ValueError("本地演练授权已失效")

        credential_fingerprint, account_fingerprint = self._active_identity()
        stored_credential = str(record.get("credential_fingerprint") or "")
        stored_account = str(record.get("account_fingerprint") or "")
        if not stored_credential or not stored_account:
            self._engage_kill("预检缺少账户身份绑定", actor="system")
            raise ValueError("预检缺少账户身份绑定；已触发急停")
        if not hmac.compare_digest(stored_credential, credential_fingerprint) or not hmac.compare_digest(
            stored_account, account_fingerprint
        ):
            self._engage_kill("预检与当前模拟账户身份不一致", actor="system")
            raise ValueError("预检不属于当前模拟账户；已触发急停")

        draft = OrderDraft.model_validate_json(record["payload_json"])
        try:
            ticker, instrument, account, open_orders = await self._risk_inputs(
                draft, credential_fingerprint
            )
        except CredentialIdentityError:
            self._engage_kill("提交复核期间模拟盘凭证身份发生变化", actor="system")
            raise
        decision = evaluate(
            draft,
            ticker=ticker,
            instrument=instrument,
            account=account,
            safety_mode=str(self.safety.status()["mode"]),
            open_orders=open_orders,
        )
        if not decision.allowed:
            self.store.update_intent(intent_id, status="rejected")
            self.store.append(
                "risk.rejected_at_commit",
                {"intentId": intent_id, "reasonCodes": decision.reasonCodes},
                correlation_id=intent_id,
            )
            raise ValueError("提交前风控不再通过：" + ", ".join(decision.reasonCodes))

        try:
            claimed = self.store.claim_intent(intent_id, idempotency_key)
        except sqlite3.IntegrityError as exc:
            raise ValueError("幂等键已被使用") from exc
        except IntentIdentityConflict as exc:
            self._engage_kill("检测到跨账户订单身份冲突", actor="system")
            raise ValueError(f"{exc}；已触发急停") from exc
        if not claimed:
            latest = self.store.get_intent(intent_id)
            if latest and latest.get("commit_key") == idempotency_key:
                return {
                    "intentId": intent_id,
                    "status": latest["status"],
                    "ordId": latest.get("okx_ord_id"),
                    "replayed": True,
                }
            raise ValueError("该预检已由另一个提交请求占用")
        self.store.append(
            "order.dispatching",
            {"intentId": intent_id, "clOrdId": record["cl_ord_id"], "environment": "demo"},
            actor="user",
            correlation_id=intent_id,
        )
        def final_dispatch_guard() -> None:
            self._dispatch_guard()
            if additional_dispatch_guard is not None:
                try:
                    additional_dispatch_guard()
                except BaseException as exc:
                    raise DispatchBlockedError(
                        "additional order dispatch gate rejected the request"
                    ) from exc

        try:
            result = await self.client.place_order(
                inst_id=draft.instId,
                side=draft.side,
                ord_type=draft.ordType,
                price=str(draft.price),
                size=str(draft.size),
                cl_ord_id=record["cl_ord_id"],
                expected_credential_fingerprint=credential_fingerprint,
                dispatch_guard=final_dispatch_guard,
            )
            ord_id = str(result.get("ordId", "")) or None
            self.store.update_intent(intent_id, status="accepted", okx_ord_id=ord_id)
            self.store.append(
                "order.accepted",
                {"intentId": intent_id, "clOrdId": record["cl_ord_id"], "ordId": ord_id},
                correlation_id=intent_id,
            )
            return {"intentId": intent_id, "status": "accepted", "ordId": ord_id, "replayed": False}
        except asyncio.CancelledError:
            self._hold_for_manual_review(intent_id, "下单任务被取消，需人工核对")
            self.store.append(
                "order.dispatch_cancelled",
                {"intentId": intent_id},
                correlation_id=intent_id,
            )
            raise
        except AmbiguousOrderError:
            return await self._reconcile_uncertain(
                intent_id,
                draft,
                record["cl_ord_id"],
                credential_fingerprint,
            )
        except DispatchBlockedError as exc:
            self.store.update_intent(intent_id, status="blocked_before_dispatch")
            self.store.append(
                "order.blocked_before_dispatch",
                {"intentId": intent_id},
                correlation_id=intent_id,
            )
            raise CommitBlockedBeforeDispatch(
                "订单在发送前被急停或授权状态阻止"
            ) from exc
        except CredentialIdentityError:
            self.store.update_intent(intent_id, status="credential_mismatch")
            self._engage_kill("下单前模拟盘凭证身份发生变化", actor="system")
            self.store.append(
                "order.credential_mismatch",
                {"intentId": intent_id},
                correlation_id=intent_id,
            )
            raise
        except OkxApiError as exc:
            self.store.update_intent(intent_id, status="rejected")
            self.store.append(
                "order.rejected",
                {"intentId": intent_id, "okxCode": exc.code},
                correlation_id=intent_id,
            )
            raise
        except OkxClientError as exc:
            self.store.update_intent(intent_id, status="transport_error")
            self.store.append(
                "order.transport_error",
                {"intentId": intent_id, "errorType": type(exc).__name__},
                correlation_id=intent_id,
            )
            raise
        except Exception as exc:
            self._hold_for_manual_review(intent_id, "下单生命周期出现未知错误，需人工核对")
            self.store.append(
                "order.lifecycle_error",
                {"intentId": intent_id, "errorType": type(exc).__name__},
                correlation_id=intent_id,
            )
            raise OkxClientError("下单状态异常，已锁定并要求人工核对") from exc

    async def emergency_stop(
        self,
        reason: str = "用户触发模拟盘急停",
        actor: str = "user",
    ) -> dict[str, Any]:
        epoch = self._signal_kill()
        try:
            armed_binding = self.safety.armed_identity_or_none()
        except Exception:
            armed_binding = None
        self._engage_kill(reason, actor=actor, already_signaled=True)
        async with self._trade_lock:
            if self._persisted_kill_epoch < epoch:
                try:
                    retry_binding = armed_binding or (None, None)
                    self.store.engage_kill_latch(
                        retry_binding[0], retry_binding[1]
                    )
                    self._persisted_kill_epoch = epoch
                    self.safety.acknowledge_persisted_kill(reason, actor=actor)
                except Exception:
                    self.safety.abort_arm_in_memory()
            try:
                safety = self.safety.status()
            except Exception:
                safety = {
                    "mode": "killed",
                    "armedRemainingSeconds": 0,
                    "killActive": True,
                    "identityBound": False,
                    "armedUntil": None,
                }
            return await self._emergency_stop_locked(safety, armed_binding)

    async def _emergency_stop_locked(
        self,
        safety: dict[str, Any],
        armed_binding: tuple[str, str] | None,
    ) -> dict[str, Any]:
        accepted_cancel_requests = 0
        failures = 0
        remaining_app_orders: int | None = None
        identity_scope_unknown = False
        try:
            potential_bindings = set(self.store.potential_order_identity_bindings())
            potential_intents = self.store.potential_order_intents()
        except Exception:
            potential_bindings = set()
            potential_intents = []
            identity_scope_unknown = True
            failures += 1
        try:
            persisted_binding = self.store.get_kill_identity()
        except Exception:
            persisted_binding = None
            identity_scope_unknown = True
            failures += 1
        expected_binding = armed_binding or persisted_binding
        if expected_binding is None and len(potential_bindings) == 1:
            candidate = next(iter(potential_bindings))
            if candidate[0] and candidate[1]:
                expected_binding = (candidate[0], candidate[1])

        can_use_credentials = credentials_configured()
        expected_credential: str | None = None
        identity_conflict = bool(potential_bindings) and (
            expected_binding is None
            or any(
                not binding[0]
                or not binding[1]
                or binding != expected_binding
                for binding in potential_bindings
            )
        )
        if identity_conflict:
            can_use_credentials = False
            failures += 1
        if identity_scope_unknown and expected_binding is None:
            can_use_credentials = False
        if can_use_credentials:
            try:
                current_credential = self.client.current_credential_fingerprint()
                if expected_binding and not hmac.compare_digest(
                    current_credential, expected_binding[0]
                ):
                    raise CredentialIdentityError("当前凭证与急停账户身份不一致")
                expected_credential = expected_binding[0] if expected_binding else current_credential
                config = await self.client.get_account_config(
                    expected_credential_fingerprint=expected_credential
                )
                current_account = _account_fingerprint(config)
                if expected_binding and not hmac.compare_digest(
                    current_account, expected_binding[1]
                ):
                    raise CredentialIdentityError("当前 OKX 账户与急停账户身份不一致")
            except OkxClientError:
                can_use_credentials = False
                failures += 1
        elif expected_binding:
            failures += 1

        if can_use_credentials and expected_credential:
            try:
                async with self._caa_lock:
                    now = time.monotonic()
                    remaining = (
                        self._exchange_deadman_valid_until - now
                        if self._exchange_deadman_valid_until is not None
                        else 0.0
                    )
                    if (
                        self._caa_outcome_unknown
                        or remaining <= 0
                        or remaining > POLICY.emergency_deadman_seconds
                    ):
                        self._caa_outcome_unknown = True
                        await self._write_caa(
                            POLICY.emergency_deadman_seconds,
                            expected_credential,
                            emergency=True,
                        )
                        self._exchange_deadman_valid_until = (
                            time.monotonic() + POLICY.emergency_deadman_seconds
                        )
                        self._caa_outcome_unknown = False
            except OkxClientError:
                failures += 1

            pending_for_cancel: list[dict[str, Any]] | None = None
            attempted_cl_ord_ids: set[str] = set()
            try:
                pending_for_cancel = await self.client.get_pending_orders(
                    "BTC-USDT",
                    expected_credential_fingerprint=expected_credential,
                )
            except OkxClientError:
                failures += 1

            if pending_for_cancel is not None:
                for order in pending_for_cancel:
                    if order.get("tag") != "tideguarddemo" or not order.get("clOrdId"):
                        continue
                    cl_ord_id = str(order["clOrdId"])
                    attempted_cl_ord_ids.add(cl_ord_id)
                    try:
                        await self.client.cancel_order(
                            "BTC-USDT",
                            cl_ord_id,
                            expected_credential_fingerprint=expected_credential,
                        )
                        accepted_cancel_requests += 1
                    except OkxClientError:
                        failures += 1

            for intent in potential_intents:
                cl_ord_id = str(intent.get("cl_ord_id", "")).strip()
                if not cl_ord_id or cl_ord_id in attempted_cl_ord_ids:
                    continue
                if expected_binding and (
                    intent.get("credential_fingerprint") != expected_binding[0]
                    or intent.get("account_fingerprint") != expected_binding[1]
                ):
                    failures += 1
                    continue
                try:
                    found = await self.client.get_order(
                        "BTC-USDT",
                        cl_ord_id,
                        expected_credential_fingerprint=expected_credential,
                    )
                    if not found:
                        failures += 1
                        continue
                    order = found[0]
                    if (
                        str(order.get("clOrdId", "")).strip() != cl_ord_id
                        or str(order.get("tag", "")) != "tideguarddemo"
                    ):
                        failures += 1
                        continue
                    state = str(order.get("state", "")).strip().lower()
                    if state in LIVE_OKX_ORDER_STATES:
                        await self.client.cancel_order(
                            "BTC-USDT",
                            cl_ord_id,
                            expected_credential_fingerprint=expected_credential,
                        )
                        attempted_cl_ord_ids.add(cl_ord_id)
                        accepted_cancel_requests += 1
                    elif state not in TERMINAL_OKX_ORDER_STATES:
                        failures += 1
                except OkxClientError:
                    failures += 1

            try:
                remaining_app_orders = len(
                    [
                        order
                        for order in await self.client.get_pending_orders(
                            "BTC-USDT",
                            expected_credential_fingerprint=expected_credential,
                        )
                        if order.get("tag") == "tideguarddemo"
                    ]
                )
            except OkxClientError:
                failures += 1
        if identity_scope_unknown:
            remaining_app_orders = None
        try:
            self.store.append(
                "safety.cancel_attempt_complete",
                {
                    "acceptedCancelRequests": accepted_cancel_requests,
                    "remainingAppOrders": remaining_app_orders,
                    "failures": failures,
                    "note": "accepted_request_is_not_confirmed_cancel_and_does_not_reverse_fills",
                },
            )
        except Exception:
            pass
        return {
            "safety": safety,
            "acceptedCancelRequests": accepted_cancel_requests,
            "remainingAppOrders": remaining_app_orders,
            "failures": failures,
        }

    async def reset_kill(self, confirmation: str) -> dict[str, Any]:
        self._require_audit_integrity()
        expected_epoch = self._emergency_epoch
        expected_generation = self.store.get_kill_generation()
        async with self._trade_lock:
            configured = credentials_configured()
            potential_orders = self.store.potential_order_intents()
            potential_bindings = set(self.store.potential_order_identity_bindings())
            expected_binding = self.store.get_kill_identity()
            if expected_binding is None and len(potential_bindings) == 1:
                candidate = next(iter(potential_bindings))
                if candidate[0] and candidate[1]:
                    expected_binding = (candidate[0], candidate[1])
            if potential_bindings and (
                expected_binding is None
                or any(
                    not binding[0]
                    or not binding[1]
                    or binding != expected_binding
                    for binding in potential_bindings
                )
            ):
                raise ValueError("潜在订单缺少唯一账户身份，急停必须保持锁定")
            if expected_binding and not configured:
                raise ValueError("恢复原模拟盘凭证并成功核对订单后才能解除急停")

            expected_credential: str | None = None
            if configured:
                current_credential = self.client.current_credential_fingerprint()
                if expected_binding and not hmac.compare_digest(
                    current_credential, expected_binding[0]
                ):
                    raise ValueError("当前凭证不是触发急停时绑定的模拟盘凭证")
                expected_credential = expected_binding[0] if expected_binding else current_credential
                config = await self.client.get_account_config(
                    expected_credential_fingerprint=expected_credential
                )
                current_account = _account_fingerprint(config)
                if expected_binding and not hmac.compare_digest(
                    current_account, expected_binding[1]
                ):
                    raise ValueError("当前 OKX 账户不是触发急停时绑定的账户")
                pending = [
                    order
                    for order in await self.client.get_pending_orders(
                        "BTC-USDT",
                        expected_credential_fingerprint=expected_credential,
                    )
                    if order.get("tag") == "tideguarddemo"
                ]
                if pending:
                    raise ValueError("仍有本程序挂单，不能解除急停")

            if potential_orders:
                if not expected_binding or not expected_credential:
                    raise ValueError("存在潜在订单且无法确认原模拟账户身份")
                for review in potential_orders:
                    if (
                        review.get("credential_fingerprint") != expected_binding[0]
                        or review.get("account_fingerprint") != expected_binding[1]
                    ):
                        raise ValueError("潜在订单与急停账户身份不一致")
                    found = await self.client.get_order(
                        "BTC-USDT",
                        str(review["cl_ord_id"]),
                        expected_credential_fingerprint=expected_credential,
                    )
                    if not found:
                        raise ValueError("潜在订单仍无法由 OKX 确认，急停必须保持锁定")
                    order = found[0]
                    ord_id = str(order.get("ordId", "")).strip()
                    known_ord_id = str(review.get("okx_ord_id") or "").strip()
                    state = str(order.get("state", "")).strip().lower()
                    if (
                        not ord_id
                        or str(order.get("clOrdId", "")).strip() != review["cl_ord_id"]
                        or str(order.get("tag", "")) != "tideguarddemo"
                        or (known_ord_id and ord_id != known_ord_id)
                        or state not in TERMINAL_OKX_ORDER_STATES
                    ):
                        raise ValueError("潜在订单尚未处于可结案的终态")
                    self.store.update_intent(
                        str(review["intent_id"]),
                        status="terminal_verified",
                        okx_ord_id=ord_id,
                    )
                    self.store.append(
                        "order.terminal_verified",
                        {
                            "intentId": review["intent_id"],
                            "clOrdId": review["cl_ord_id"],
                            "ordId": ord_id,
                            "state": state,
                        },
                        correlation_id=str(review["intent_id"]),
                    )
            if self._emergency_epoch != expected_epoch:
                raise SafetyError("核对期间出现了新的急停事件；急停保持锁定")
            result = self.safety.reset_kill(confirmation, expected_generation)
            self._kill_requested.clear()
            self._deadman_valid_until = None
            self._exchange_deadman_valid_until = None
            self._caa_outcome_unknown = False
            return result
