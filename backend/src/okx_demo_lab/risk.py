from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from .config import POLICY
from .models import OrderDraft, RiskCheck, RiskDecision


@dataclass(frozen=True)
class AccountContext:
    configured: bool
    equity_usdt: Decimal = Decimal("0")
    available_usdt: Decimal = Decimal("0")
    available_btc: Decimal = Decimal("0")
    open_orders: int = 0


def decimal_or_zero(value: Any) -> Decimal:
    try:
        return Decimal(str(value or "0"))
    except (InvalidOperation, ValueError):
        return Decimal("0")


def account_context(data: list[dict[str, Any]], configured: bool) -> AccountContext:
    if not configured or not data:
        return AccountContext(configured=False)
    account = data[0]
    balances = {item.get("ccy"): item for item in account.get("details", [])}
    return AccountContext(
        configured=True,
        equity_usdt=decimal_or_zero(account.get("totalEq")),
        available_usdt=decimal_or_zero(balances.get("USDT", {}).get("availBal")),
        available_btc=decimal_or_zero(balances.get("BTC", {}).get("availBal")),
    )


def _aligned(value: Decimal, step: Decimal) -> bool:
    if step <= 0:
        return False
    return value % step == 0


def evaluate(
    draft: OrderDraft,
    *,
    ticker: dict[str, Any],
    instrument: dict[str, Any],
    account: AccountContext,
    safety_mode: str,
    open_orders: int,
) -> RiskDecision:
    checks: list[RiskCheck] = []

    def add(key: str, label: str, passed: bool, current: str, limit: str, reason: str) -> None:
        checks.append(
            RiskCheck(
                key=key,
                label=label,
                passed=passed,
                current=current,
                limit=limit,
                reason=reason,
            )
        )

    is_spot = instrument.get("instType") == "SPOT" and instrument.get("state") == "live"
    add("spot_only", "仅现货 / 零杠杆", is_spot, str(instrument.get("instType", "未知")), "SPOT + cash", "OKX 品种元数据校验")

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    ticker_ms = int(str(ticker.get("ts", "0")) or 0)
    age_seconds = max(0, (now_ms - ticker_ms) / 1000)
    fresh = 0 <= age_seconds <= POLICY.stale_market_seconds
    add("market_fresh", "行情新鲜度", fresh, f"{age_seconds:.1f} 秒", f"≤ {POLICY.stale_market_seconds} 秒", "超时行情禁止下单")

    last = decimal_or_zero(ticker.get("last"))
    deviation = abs(draft.price - last) / last if last > 0 else Decimal("999")
    price_ok = last > 0 and deviation <= POLICY.max_price_deviation
    add("price_deviation", "限价偏离", price_ok, f"{deviation * 100:.3f}%", f"≤ {POLICY.max_price_deviation * 100}%", "相对最新价的绝对偏离")

    tick_size = decimal_or_zero(instrument.get("tickSz"))
    lot_size = decimal_or_zero(instrument.get("lotSz"))
    min_size = decimal_or_zero(instrument.get("minSz"))
    precision_ok = _aligned(draft.price, tick_size) and _aligned(draft.size, lot_size) and draft.size >= min_size
    add("instrument_precision", "交易精度", precision_ok, f"价格 {draft.price} / 数量 {draft.size}", f"tick {tick_size}, lot {lot_size}, min {min_size}", "必须满足 OKX 当前品种规则")

    notional = draft.price * draft.size
    equity_limit = account.equity_usdt * POLICY.max_order_equity_fraction
    effective_limit = min(POLICY.max_order_notional_usdt, equity_limit) if equity_limit > 0 else Decimal("0")
    notional_ok = account.configured and effective_limit > 0 and notional <= effective_limit
    add("single_order", "单笔风险", notional_ok, f"{notional:.4f} USDT", f"≤ {effective_limit:.4f} USDT", "固定 25 USDT 与权益 0.10% 取更小值")

    balance = account.available_usdt if draft.side == "buy" else account.available_btc
    needed = notional if draft.side == "buy" else draft.size
    balance_ok = account.configured and balance >= needed
    unit = "USDT" if draft.side == "buy" else "BTC"
    add("balance", "可用余额", balance_ok, f"{balance} {unit}", f"≥ {needed} {unit}", "只读取模拟账户余额")

    order_count_ok = open_orders < POLICY.max_open_orders
    add("open_orders", "未完成订单", order_count_ok, str(open_orders), f"< {POLICY.max_open_orders}", "限制并发挂单")

    armed = safety_mode == "armed"
    add("armed", "本地演练授权", armed, safety_mode, "armed", "重启、超时或急停后必须重新启用")

    configured = account.configured
    add("credentials", "模拟盘凭证", configured, "已配置" if configured else "未配置", "Windows 本机保护", "凭证不会进入前端或日志")

    reason_codes = [check.key for check in checks if not check.passed]
    return RiskDecision(
        allowed=not reason_codes,
        policyVersion=POLICY.policy_version,
        checks=checks,
        reasonCodes=reason_codes,
    )
