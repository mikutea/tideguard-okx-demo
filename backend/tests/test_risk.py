from datetime import datetime, timezone
from decimal import Decimal

from okx_demo_lab.models import OrderDraft
from okx_demo_lab.risk import AccountContext, evaluate


def valid_inputs():
    ticker = {
        "last": "64000.0",
        "ts": str(int(datetime.now(timezone.utc).timestamp() * 1000)),
    }
    instrument = {
        "instId": "BTC-USDT",
        "instType": "SPOT",
        "state": "live",
        "tickSz": "0.1",
        "lotSz": "0.00001",
        "minSz": "0.00001",
    }
    account = AccountContext(
        configured=True,
        equity_usdt=Decimal("100000"),
        available_usdt=Decimal("1000"),
        available_btc=Decimal("1"),
    )
    return ticker, instrument, account


def test_safe_limit_order_can_pass() -> None:
    ticker, instrument, account = valid_inputs()
    decision = evaluate(
        OrderDraft(instId="BTC-USDT", side="buy", price="64000.0", size="0.00020"),
        ticker=ticker,
        instrument=instrument,
        account=account,
        safety_mode="armed",
        open_orders=0,
    )
    assert decision.allowed
    assert not decision.reasonCodes


def test_observe_mode_and_large_notional_fail_closed() -> None:
    ticker, instrument, account = valid_inputs()
    decision = evaluate(
        OrderDraft(instId="BTC-USDT", side="buy", price="64000.0", size="0.01000"),
        ticker=ticker,
        instrument=instrument,
        account=account,
        safety_mode="observe",
        open_orders=0,
    )
    assert not decision.allowed
    assert "single_order" in decision.reasonCodes
    assert "armed" in decision.reasonCodes


def test_stale_market_fails_closed() -> None:
    ticker, instrument, account = valid_inputs()
    ticker["ts"] = "1"
    decision = evaluate(
        OrderDraft(instId="BTC-USDT", side="sell", price="64000.0", size="0.00020"),
        ticker=ticker,
        instrument=instrument,
        account=account,
        safety_mode="armed",
        open_orders=0,
    )
    assert not decision.allowed
    assert "market_fresh" in decision.reasonCodes
