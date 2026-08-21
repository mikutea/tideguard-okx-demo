from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import secrets
import sqlite3
import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Awaitable, Callable

from .audit import AuditStore, IntentIdentityConflict, utc_now
from .config import policy_for_profile
from .environment import (
    EnvironmentAcknowledgements,
    EnvironmentSwitchError,
    RuntimeEnvironment,
    SWITCH_PHRASES,
    SwitchChallengeStore,
    canonical_sha256,
    challenge_public,
)
from .models import OrderDraft, RiskCheck
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
from .profile import DEMO_PROFILE, EnvironmentProfile, profile_for


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


def _account_fingerprint(
    config: list[dict[str, Any]], profile: EnvironmentProfile = DEMO_PROFILE
) -> str:
    if not config:
        raise CredentialIdentityError(f"OKX 未返回{profile.display_name}账户身份")
    uid = str(config[0].get("uid", "")).strip()
    main_uid = str(config[0].get("mainUid", "")).strip()
    if not uid:
        raise CredentialIdentityError(f"OKX {profile.display_name}配置缺少 uid")
    material = f"{profile.credential_service}\0{main_uid}\0{uid}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()


class TradingService:
    def __init__(
        self,
        client: OkxClient,
        store: AuditStore,
        safety: SafetyController,
        *,
        execution_environment_guard: Callable[[], None] | None = None,
    ):
        self.client = client
        self.store = store
        self.safety = safety
        self.profile = getattr(client, "profile", DEMO_PROFILE)
        self.policy = policy_for_profile(self.profile)
        self._execution_environment_guard = execution_environment_guard
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

    def _assert_environment_execution_allowed(self) -> None:
        if self._execution_environment_guard is None:
            return
        try:
            self._execution_environment_guard()
        except EnvironmentSwitchError as exc:
            raise SafetyError(str(exc)) from exc

    def _credentials_configured(self) -> bool:
        # Preserve the legacy no-argument test seam for the default Demo profile.
        if self.profile.name == "demo":
            return credentials_configured()
        return credentials_configured(self.profile)

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
        self._exchange_deadman_valid_until = now + self.policy.deadman_seconds
        if self._kill_requested.is_set() or self.safety.status()["mode"] != "armed":
            raise DispatchBlockedError("CAA 确认到达时授权已被撤销")
        self._deadman_valid_until = now + self.policy.deadman_local_lease_seconds

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
        self._assert_environment_execution_allowed()
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
            self._assert_environment_execution_allowed()
            self._require_audit_integrity()
            expected_confirmation = (
                "DEMO" if self.profile.name == "demo" else "我确认使用真实资金"
            )
            if confirmation != expected_confirmation:
                if self.profile.name == "demo":
                    raise SafetyError("请输入 DEMO 以启用限时模拟下单。")
                raise SafetyError("实盘手工授权确认短语不匹配。")
            if not self._credentials_configured():
                raise ValueError("请先在 Windows Credential Manager 配置模拟盘凭证")
            credential_fingerprint = self.client.current_credential_fingerprint()
            config = await self.client.get_account_config(
                expected_credential_fingerprint=credential_fingerprint
            )
            if self.profile.name == "live":
                validate_profile_config(self.profile, config)
            account_fingerprint = _account_fingerprint(config, self.profile)
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
                        self.policy.deadman_seconds,
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
        """Return non-secret fingerprints for the immutable runtime profile."""

        self._assert_environment_execution_allowed()
        self._require_audit_integrity()
        if not self._credentials_configured():
            raise ValueError(
                f"请先在 Windows Credential Manager 配置{self.profile.display_name}凭证"
            )
        credential_fingerprint = self.client.current_credential_fingerprint()
        config = await self.client.get_account_config(
            expected_credential_fingerprint=credential_fingerprint
        )
        return credential_fingerprint, _account_fingerprint(config, self.profile)

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
            self._assert_environment_execution_allowed()
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
                        self.policy.deadman_seconds,
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
                self.policy.deadman_seconds,
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

    async def _prepare_environment_switch_locked(self) -> dict[str, Any]:
        """Caller holds `_trade_lock`; revoke only proven pre-dispatch state."""

        self._deadman_valid_until = None
        self.safety.disarm("environment_switch_preflight")
        async with self._caa_lock:
            if self._caa_outcome_unknown:
                raise EnvironmentSwitchError(
                    "Cancel-All-After 状态未知，不能切换环境"
                )
            self._exchange_deadman_valid_until = None
        revoked = self.store.revoke_uncommitted_previews_for_environment_switch()
        blockers = self.store.environment_switch_blocking_intents()
        return {
            "safety": self.safety.status(),
            "revokedPreviews": revoked,
            "blockingIntentCount": len(blockers),
            "caaOutcomeKnown": True,
        }

    async def prepare_environment_switch(self) -> dict[str, Any]:
        """Stop local dispatch capability and revoke proven pre-dispatch previews."""

        async with self._trade_lock:
            return await self._prepare_environment_switch_locked()

    async def begin_environment_transition(self) -> dict[str, Any]:
        """Become the final writer after the runtime transition gate is latched."""

        epoch = self._signal_kill()
        async with self._trade_lock:
            state = self._engage_kill(
                "环境切换进入最终核对，交易执行已锁定",
                actor="user",
                already_signaled=True,
            )
            try:
                persisted, _generation = self.store.get_kill_state()
            except Exception as exc:
                raise EnvironmentSwitchError(
                    "无法确认当前环境急停已持久化；切换保持锁定"
                ) from exc
            if not persisted or self._persisted_kill_epoch < epoch:
                raise EnvironmentSwitchError(
                    "当前环境急停未完整持久化；切换保持锁定"
                )
            prepared = await self._prepare_environment_switch_locked()
            return {**prepared, "safety": state, "transitionGate": True}

    async def engage_environment_switch_kill(
        self, identity_binding: tuple[str, str]
    ) -> dict[str, Any]:
        if not all(identity_binding):
            raise EnvironmentSwitchError("当前环境身份绑定缺失，不能确认切换")
        async with self._trade_lock:
            self._deadman_valid_until = None
            self._exchange_deadman_valid_until = None
            epoch = self._signal_kill()
            generation = self.store.engage_kill_latch(*identity_binding)
            self._persisted_kill_epoch = max(self._persisted_kill_epoch, epoch)
            return self.safety.acknowledge_persisted_kill(
                f"环境切换已确认，重启前保持急停（generation={generation}）",
                actor="user",
            )

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
            "environment": self.profile.name,
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
        if not self._credentials_configured():
            return {
                "configured": False,
                "source": f"尚无{self.profile.display_name}账户数据",
                "equityUsdt": None,
                "balances": [],
            }
        binding = self.safety.armed_identity_or_none()
        expected = (
            self._active_identity()[0]
            if binding
            else self.client.current_credential_fingerprint()
        )
        data = await self.client.get_account_balance(
            expected_credential_fingerprint=expected
        )
        if not data:
            return {
                "configured": True,
                "source": self.profile.display_name,
                "equityUsdt": None,
                "balances": [],
            }
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
            "source": self.profile.display_name,
            "environment": self.profile.name,
            "equityUsdt": account.get("totalEq"),
            "updatedAt": account.get("uTime"),
            "balances": balances,
        }

    async def pending_orders(self) -> list[dict[str, Any]]:
        if not self._credentials_configured():
            return []
        binding = self.safety.armed_identity_or_none()
        expected = (
            self._active_identity()[0]
            if binding
            else self.client.current_credential_fingerprint()
        )
        orders = await self.client.get_pending_orders(
            expected_credential_fingerprint=expected
        )
        return [
            self._safe_order(order)
            for order in orders
            if order.get("tag") == self.profile.order_tag
        ]

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
            raise CredentialIdentityError(f"当前{self.profile.display_name}账户身份已变化")
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
            or str(order.get("tag") or "") != self.profile.order_tag
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

    def _safe_order(self, order: dict[str, Any]) -> dict[str, Any]:
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
            "source": self.profile.display_name,
            "environment": self.profile.name,
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
            None if self.profile.name == "live" else draft.instId,
            expected_credential_fingerprint=credential_fingerprint,
        )
        account = account_context(balance_data, True)
        return bundle["ticker"], bundle["instrument"], account, len(pending)

    def _evaluate_policy(
        self,
        draft: OrderDraft,
        *,
        ticker: dict[str, Any],
        instrument: dict[str, Any],
        account: Any,
        open_orders: int,
    ) -> Any:
        decision = evaluate(
            draft,
            ticker=ticker,
            instrument=instrument,
            account=account,
            safety_mode=str(self.safety.status()["mode"]),
            open_orders=open_orders,
        )
        if self.profile.name == "demo":
            return decision

        notional = draft.price * draft.size
        equity_limit = account.equity_usdt * self.policy.max_order_equity_fraction
        effective_limit = (
            min(self.policy.max_order_notional_usdt, equity_limit)
            if equity_limit > 0
            else Decimal("0")
        )
        replacement_checks = [
            RiskCheck(
                key="single_order",
                label="实盘单笔风险",
                passed=bool(
                    account.configured
                    and effective_limit > 0
                    and notional <= effective_limit
                ),
                current=f"{notional:.4f} USDT",
                limit=f"≤ {effective_limit:.4f} USDT",
                reason="固定 10 USDT 与权益 0.05% 取更小值",
            ),
            RiskCheck(
                key="open_orders",
                label="实盘未完成订单",
                passed=open_orders < self.policy.max_open_orders,
                current=str(open_orders),
                limit=f"< {self.policy.max_open_orders}",
                reason="实盘最多允许一个未完成订单",
            ),
            RiskCheck(
                key="credentials",
                label="实盘凭证",
                passed=bool(account.configured),
                current="已配置" if account.configured else "未配置",
                limit="独立 Windows 本机保护",
                reason="实盘凭证不得复用 Demo 凭证",
            ),
        ]
        checks = [
            check
            for check in decision.checks
            if check.key not in {"single_order", "open_orders", "credentials"}
        ] + replacement_checks
        reason_codes = [check.key for check in checks if not check.passed]
        return decision.model_copy(
            update={
                "allowed": not reason_codes,
                "policyVersion": self.policy.policy_version,
                "checks": checks,
                "reasonCodes": reason_codes,
            }
        )

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
        decision = self._evaluate_policy(
            draft,
            ticker=ticker,
            instrument=instrument,
            account=account,
            open_orders=open_orders,
        )
        intent_id = uuid.uuid4().hex
        cl_ord_id = f"tg{uuid.uuid4().hex[:24]}"
        created_at = datetime.now(timezone.utc)
        expires_at = created_at + timedelta(seconds=self.policy.preview_ttl_seconds)
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
        decision = self._evaluate_policy(
            draft,
            ticker=ticker,
            instrument=instrument,
            account=account,
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
            {
                "intentId": intent_id,
                "clOrdId": record["cl_ord_id"],
                "environment": self.profile.name,
            },
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

        can_use_credentials = self._credentials_configured()
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
                current_account = _account_fingerprint(config, self.profile)
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
                        or remaining > self.policy.emergency_deadman_seconds
                    ):
                        self._caa_outcome_unknown = True
                        await self._write_caa(
                            self.policy.emergency_deadman_seconds,
                            expected_credential,
                            emergency=True,
                        )
                        self._exchange_deadman_valid_until = (
                            time.monotonic() + self.policy.emergency_deadman_seconds
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
                    if (
                        order.get("tag") != self.profile.order_tag
                        or not order.get("clOrdId")
                    ):
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
                        or str(order.get("tag", "")) != self.profile.order_tag
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
                        if order.get("tag") == self.profile.order_tag
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
        self._assert_environment_execution_allowed()
        self._require_audit_integrity()
        expected_epoch = self._emergency_epoch
        expected_generation = self.store.get_kill_generation()
        async with self._trade_lock:
            configured = self._credentials_configured()
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
                current_account = _account_fingerprint(config, self.profile)
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
                    if order.get("tag") == self.profile.order_tag
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
                        or str(order.get("tag", "")) != self.profile.order_tag
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


def _permission_set(config: dict[str, Any]) -> set[str]:
    raw = str(config.get("perm", ""))
    normalized = raw.replace(";", ",").replace(" ", ",")
    return {item.strip().lower() for item in normalized.split(",") if item.strip()}


def validate_profile_config(
    profile: EnvironmentProfile, config: list[dict[str, Any]]
) -> dict[str, Any]:
    if len(config) != 1:
        raise EnvironmentSwitchError("OKX 账户配置必须唯一")
    item = config[0]
    account_fingerprint = _account_fingerprint(config, profile)
    permissions = _permission_set(item)
    missing = {"read_only", "trade"} - permissions
    if missing:
        raise EnvironmentSwitchError("目标凭证必须同时具有 read_only 和 trade 权限")
    if profile.name == "live" and "withdraw" in permissions:
        raise EnvironmentSwitchError("实盘凭证包含 withdraw 权限，切换被拒绝")
    if str(item.get("acctLv", "")).strip() != "1":
        raise EnvironmentSwitchError("目标账户必须处于 Spot mode (acctLv=1)")
    ip_binding = str(item.get("ip", "")).strip()
    if profile.name == "live" and not ip_binding:
        raise EnvironmentSwitchError("实盘 API Key 必须绑定至少一个 IP")
    return {
        "accountFingerprint": account_fingerprint,
        "permissions": sorted(permissions),
        "ipBound": bool(ip_binding),
        "spotMode": True,
    }


class EnvironmentSwitchService:
    """Server-side, restart-required environment transition protocol."""

    def __init__(
        self,
        *,
        runtime: RuntimeEnvironment,
        data_root: Path,
        trading: TradingService,
        audit: AuditStore,
        autonomy: Any,
        client_factory: Callable[[EnvironmentProfile], OkxClient] | None = None,
        stop_legacy_automation: Callable[[str], Awaitable[Any]] | None = None,
        challenges: SwitchChallengeStore | None = None,
    ):
        self.runtime = runtime
        self.data_root = data_root
        self.trading = trading
        self.audit = audit
        self.autonomy = autonomy
        self._client_factory = client_factory or (lambda profile: OkxClient(profile=profile))
        self._stop_legacy_automation = stop_legacy_automation
        self._challenges = challenges or SwitchChallengeStore()
        self._lock = asyncio.Lock()

    def status(self) -> dict[str, Any]:
        return {
            **self.runtime.status(),
            "credentialConfigured": self.trading._credentials_configured(),
            "credentialService": self.trading.profile.credential_service,
            "orderTag": self.trading.profile.order_tag,
            "simulatedTradingHeader": self.trading.profile.simulated_trading,
            "liveManualTradingAvailable": True,
            "liveAutomationAvailable": False,
            "independentLiveAuthorizationRequired": True,
            "safety": self.trading.safety.status(),
            "riskPolicy": {
                "version": self.trading.policy.policy_version,
                "maxOrderNotionalUsdt": str(
                    self.trading.policy.max_order_notional_usdt
                ),
                "maxOrderEquityFraction": str(
                    self.trading.policy.max_order_equity_fraction
                ),
                "maxOpenOrders": self.trading.policy.max_open_orders,
                "automationArmTtlSeconds": (
                    self.trading.policy.automation_arm_ttl_seconds
                ),
            },
        }

    async def _stop_automation(self, target: EnvironmentProfile) -> dict[str, Any]:
        reason = f"environment_switch_to_{target.name}"
        legacy_result: Any = None
        if self._stop_legacy_automation is not None:
            legacy_result = await self._stop_legacy_automation(reason)
        current_state = self.autonomy.disable_master(
            reason, now=datetime.now(timezone.utc)
        )
        prepared = await self.trading.prepare_environment_switch()
        return {
            "legacyStopped": self._stop_legacy_automation is None
            or bool(legacy_result is not False),
            "masterDisabled": current_state.get("desiredMode") == "disabled",
            "prepared": prepared,
        }

    def _target_stores(self, target: EnvironmentProfile) -> tuple[AuditStore, Any]:
        from .ml.autonomy import AutonomyStore

        target_dir = target.runtime_data_dir(self.data_root)
        return (
            AuditStore(target_dir / "state.sqlite3"),
            AutonomyStore(target_dir / "autonomy.sqlite3"),
        )

    @staticmethod
    def _check(key: str, passed: bool, detail: str) -> dict[str, Any]:
        return {"key": key, "passed": bool(passed), "detail": detail}

    async def _preflight_locked(
        self, target_name: str, *, transition_owned: bool = False
    ) -> dict[str, Any]:
        target = profile_for(target_name)
        source = self.runtime.active_profile
        checks: list[dict[str, Any]] = []
        evidence: dict[str, Any] = {"source": source.name, "target": target.name}

        runtime_status = self.runtime.status()
        same_environment = target.name == source.name
        selector_ready = bool(
            runtime_status["selectorValid"]
            and not runtime_status["restartRequired"]
            and (
                not runtime_status.get("transitionPending")
                or (
                    transition_owned
                    and runtime_status.get("transitionTarget") == target.name
                )
            )
        )
        checks.append(
            self._check(
                "different_environment",
                not same_environment,
                "目标环境必须与当前进程环境不同",
            )
        )
        checks.append(
            self._check(
                "no_pending_restart",
                selector_ready,
                "已有待重启切换时不能再次切换",
            )
        )
        if same_environment or not selector_ready:
            evidence["checks"] = checks
            result = {
                "allowed": False,
                "source": source.name,
                "target": target.name,
                "checks": checks,
            }
            result["preflightSha256"] = canonical_sha256(evidence)
            return result

        try:
            stopped = await self._stop_automation(target)
            automation_ok = bool(
                stopped["legacyStopped"]
                and stopped["masterDisabled"]
                and stopped["prepared"]["caaOutcomeKnown"]
            )
            checks.append(
                self._check(
                    "automation_stopped",
                    automation_ok,
                    "当前环境 master、旧 permit 与本地授权均已关闭",
                )
            )
        except Exception as exc:
            checks.append(
                self._check(
                    "automation_stopped",
                    False,
                    f"自动化关闭失败：{type(exc).__name__}",
                )
            )

        current_chain = self.audit.verify_chain()
        current_blockers = self.audit.environment_switch_blocking_intents()
        try:
            current_position = self.autonomy.active_position()
        except Exception:
            current_position = {"status": "unknown"}
        checks.extend(
            [
                self._check(
                    "current_audit_valid",
                    current_chain,
                    "当前环境审计链必须有效",
                ),
                self._check(
                    "current_no_unresolved_intents",
                    not current_blockers,
                    f"当前环境阻塞订单意图：{len(current_blockers)}",
                ),
                self._check(
                    "current_no_model_position",
                    current_position is None,
                    "当前环境不能有模型持仓或人工核对持仓",
                ),
            ]
        )
        evidence["currentBlockingIntents"] = len(current_blockers)
        evidence["currentPositionActive"] = current_position is not None

        target_audit, target_autonomy = self._target_stores(target)
        try:
            target_autonomy.disable_master(
                f"environment_switch_target_{target.name}",
                now=datetime.now(timezone.utc),
            )
            target_position = target_autonomy.active_position()
        except Exception:
            target_position = {"status": "unknown"}
        target_chain = target_audit.verify_chain()
        target_blockers = target_audit.environment_switch_blocking_intents()
        checks.extend(
            [
                self._check(
                    "target_audit_valid",
                    target_chain,
                    "目标环境独立审计链必须有效",
                ),
                self._check(
                    "target_no_unresolved_intents",
                    not target_blockers,
                    f"目标环境阻塞订单意图：{len(target_blockers)}",
                ),
                self._check(
                    "target_no_model_position",
                    target_position is None,
                    "目标环境不能保留模型持仓",
                ),
            ]
        )
        evidence["targetBlockingIntents"] = len(target_blockers)
        evidence["targetPositionActive"] = target_position is not None

        try:
            current_credential = self.trading.client.current_credential_fingerprint()
            current_config = await self.trading.client.get_account_config(
                expected_credential_fingerprint=current_credential
            )
            current_account = _account_fingerprint(current_config, source)
            current_orders = await self.trading.client.get_pending_orders(
                None,
                expected_credential_fingerprint=current_credential,
            )
            current_tideguard_orders = [
                order
                for order in current_orders
                if str(order.get("tag", "")) == source.order_tag
            ]
            checks.append(
                self._check(
                    "current_pending_orders_zero",
                    not current_orders,
                    (
                        f"当前环境全部现货挂单：{len(current_orders)}；"
                        f"其中墨衡挂单：{len(current_tideguard_orders)}"
                    ),
                )
            )
            evidence["currentCredentialFingerprint"] = current_credential
            evidence["currentAccountFingerprint"] = current_account
            evidence["currentTideguardOrders"] = len(current_tideguard_orders)
            evidence["currentPendingOrders"] = len(current_orders)
        except Exception as exc:
            checks.append(
                self._check(
                    "current_pending_orders_zero",
                    False,
                    f"无法证明当前环境挂单为零：{type(exc).__name__}",
                )
            )

        target_client = self._client_factory(target)
        try:
            target_credential = target_client.current_credential_fingerprint()
            target_config = await target_client.get_account_config(
                expected_credential_fingerprint=target_credential
            )
            validated = validate_profile_config(target, target_config)
            target_orders = await target_client.get_pending_orders(
                None,
                expected_credential_fingerprint=target_credential,
            )
            target_tideguard_orders = [
                order
                for order in target_orders
                if str(order.get("tag", "")) == target.order_tag
            ]
            checks.extend(
                [
                    self._check(
                        "target_credentials_valid",
                        True,
                        "目标环境凭证和账户身份已由 OKX config 验证",
                    ),
                    self._check(
                        "target_permissions_valid",
                        True,
                        "read_only + trade、无实盘 withdraw、Spot mode",
                    ),
                    self._check(
                        "target_pending_orders_zero",
                        not target_orders,
                        (
                            f"目标环境全部现货挂单：{len(target_orders)}；"
                            f"其中墨衡挂单：{len(target_tideguard_orders)}"
                        ),
                    ),
                ]
            )
            evidence.update(
                {
                    "targetCredentialFingerprint": target_credential,
                    "targetAccountFingerprint": validated["accountFingerprint"],
                    "targetPermissions": validated["permissions"],
                    "targetIpBound": validated["ipBound"],
                    "targetSpotMode": validated["spotMode"],
                    "targetTideguardOrders": len(target_tideguard_orders),
                    "targetPendingOrders": len(target_orders),
                }
            )
        except Exception as exc:
            checks.extend(
                [
                    self._check(
                        "target_credentials_valid",
                        False,
                        f"目标环境凭证验证失败：{type(exc).__name__}",
                    ),
                    self._check(
                        "target_permissions_valid",
                        False,
                        str(exc)[:240],
                    ),
                    self._check(
                        "target_pending_orders_zero",
                        False,
                        "目标环境挂单状态无法确认",
                    ),
                ]
            )
        finally:
            await target_client.close()

        evidence["checks"] = [
            {"key": check["key"], "passed": check["passed"]} for check in checks
        ]
        result = {
            "allowed": all(check["passed"] for check in checks),
            "source": source.name,
            "target": target.name,
            "checks": checks,
        }
        result["preflightSha256"] = canonical_sha256(evidence)
        result["binding"] = {
            "currentCredentialFingerprint": evidence.get(
                "currentCredentialFingerprint"
            ),
            "currentAccountFingerprint": evidence.get("currentAccountFingerprint"),
            "targetCredentialFingerprint": evidence.get("targetCredentialFingerprint"),
            "targetAccountFingerprint": evidence.get("targetAccountFingerprint"),
        }
        return result

    async def preflight(self, target_name: str) -> dict[str, Any]:
        async with self._lock:
            result = await self._preflight_locked(target_name)
            self.audit.append(
                "environment.switch_preflight",
                {
                    "source": result["source"],
                    "target": result["target"],
                    "allowed": result["allowed"],
                    "preflightSha256": result["preflightSha256"],
                    "failedChecks": [
                        check["key"] for check in result["checks"] if not check["passed"]
                    ],
                },
                actor="user",
            )
            return result

    async def challenge(self, target_name: str) -> dict[str, Any]:
        async with self._lock:
            preflight = await self._preflight_locked(target_name)
            if not preflight["allowed"]:
                self.audit.append(
                    "environment.switch_challenge_rejected",
                    {
                        "source": preflight["source"],
                        "target": preflight["target"],
                        "failedChecks": [
                            check["key"]
                            for check in preflight["checks"]
                            if not check["passed"]
                        ],
                    },
                    actor="user",
                )
                raise EnvironmentSwitchError("环境切换预检未通过")
            nonce, challenge = self._challenges.issue(
                source=self.runtime.active_profile.name,
                target=profile_for(target_name).name,
                preflight_sha256=preflight["preflightSha256"],
            )
            public = challenge_public(challenge, nonce)
            self.audit.append(
                "environment.switch_challenge_issued",
                {
                    "source": challenge.source,
                    "target": challenge.target,
                    "nonceSha256": challenge.nonce_sha256,
                    "preflightSha256": challenge.preflight_sha256,
                    "readyAt": public["readyAt"],
                    "expiresAt": public["expiresAt"],
                },
                actor="user",
            )
            return {**public, "preflight": preflight}

    async def confirm(
        self,
        *,
        target_name: str,
        nonce: str,
        confirmation: str,
        acknowledgements: EnvironmentAcknowledgements,
    ) -> dict[str, Any]:
        target = profile_for(target_name)
        acknowledged = acknowledgements.model_dump()

        async with self._lock:
            try:
                challenge = self._challenges.consume(
                    nonce,
                    source=self.runtime.active_profile.name,
                    target=target.name,
                )
            except EnvironmentSwitchError as exc:
                self.audit.append(
                    "environment.switch_confirm_rejected",
                    {
                        "source": self.runtime.active_profile.name,
                        "target": target.name,
                        "reason": "challenge_invalid",
                    },
                    actor="user",
                )
                raise
            if confirmation != SWITCH_PHRASES[target.name]:
                self.audit.append(
                    "environment.switch_confirm_rejected",
                    {
                        "source": challenge.source,
                        "target": challenge.target,
                        "reason": "confirmation_phrase_mismatch",
                    },
                    actor="user",
                )
                raise EnvironmentSwitchError("环境切换确认短语不匹配")
            if not all(acknowledged.values()):
                self.audit.append(
                    "environment.switch_confirm_rejected",
                    {
                        "source": challenge.source,
                        "target": challenge.target,
                        "reason": "acknowledgements_incomplete",
                    },
                    actor="user",
                )
                raise EnvironmentSwitchError("环境切换需要完成全部风险确认")
            # Latch a process-monotonic gate before the first await in the
            # final phase.  A failed final recheck deliberately leaves this
            # process killed/blocked until restart.
            self.runtime.begin_transition(target)
            await self.trading.begin_environment_transition()
            preflight = await self._preflight_locked(
                target.name, transition_owned=True
            )
            if not preflight["allowed"]:
                self.audit.append(
                    "environment.switch_confirm_rejected",
                    {
                        "source": challenge.source,
                        "target": challenge.target,
                        "reason": "preflight_changed",
                    },
                    actor="user",
                )
                raise EnvironmentSwitchError("确认时环境切换预检已不再通过")
            if not hmac.compare_digest(
                challenge.preflight_sha256, preflight["preflightSha256"]
            ):
                self.audit.append(
                    "environment.switch_confirm_rejected",
                    {
                        "source": challenge.source,
                        "target": challenge.target,
                        "reason": "preflight_digest_mismatch",
                    },
                    actor="user",
                )
                raise EnvironmentSwitchError("确认时账户或安全状态已变化")

            binding = preflight["binding"]
            target_credential = str(binding["targetCredentialFingerprint"] or "")
            target_account = str(binding["targetAccountFingerprint"] or "")
            if not target_credential or not target_account:
                raise EnvironmentSwitchError("目标环境身份绑定缺失")

            switch_id = f"env_{secrets.token_hex(14)}"
            target_audit, target_autonomy = self._target_stores(target)
            target_autonomy.disable_master(
                f"environment_switch_confirmed_{switch_id}",
                now=datetime.now(timezone.utc),
            )
            target_generation = target_audit.engage_kill_latch(
                target_credential, target_account
            )
            target_audit.append(
                "environment.switch_target_locked",
                {
                    "switchId": switch_id,
                    "source": self.runtime.active_profile.name,
                    "target": target.name,
                    "killGeneration": target_generation,
                    "automationDisabled": True,
                },
                actor="user",
            )
            current_credential = str(binding["currentCredentialFingerprint"] or "")
            current_account = str(binding["currentAccountFingerprint"] or "")
            current_safety = await self.trading.engage_environment_switch_kill(
                (current_credential, current_account)
            )
            try:
                current_killed, _ = self.audit.get_kill_state()
            except Exception as exc:
                raise EnvironmentSwitchError(
                    "无法确认当前环境最终急停状态；切换保持锁定"
                ) from exc
            if not current_killed:
                raise EnvironmentSwitchError(
                    "当前环境最终急停未持久化；切换保持锁定"
                )
            try:
                self.runtime.selector.persist(target, switch_id=switch_id)
            except Exception as exc:
                self.audit.append(
                    "environment.switch_persist_failed",
                    {
                        "switchId": switch_id,
                        "target": target.name,
                        "errorType": type(exc).__name__,
                    },
                    actor="system",
                )
                raise EnvironmentSwitchError(
                    "环境选择持久化失败；当前与目标环境均保持急停"
                ) from exc
            self.audit.append(
                "environment.switch_confirmed",
                {
                    "switchId": switch_id,
                    "source": self.runtime.active_profile.name,
                    "target": target.name,
                    "preflightSha256": preflight["preflightSha256"],
                    "restartRequired": True,
                    "automationDisabled": True,
                    "killActive": True,
                },
                actor="user",
            )
            return {
                **self.runtime.status(),
                "switchId": switch_id,
                "source": self.runtime.active_profile.name,
                "target": target.name,
                "restartRequired": True,
                "operatingMode": "observe",
                "killActive": True,
                "targetKillActive": True,
                "safety": current_safety,
            }
