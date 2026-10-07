"""Minimum test cases T-001 .. T-020 (Core System Design v0.1, section 38)."""

from __future__ import annotations

import pytest

from conftest import make_proposal as mkprop

from qat.app import build_paper_stack
from qat.core.models import (
    Currency,
    Fill,
    InvalidStateTransition,
    Market,
    Order,
    OrderStatus,
    Side,
)
from qat.paper.broker import BrokerError
from qat.portfolio.ledger import LedgerError, PortfolioLedger
from qat.portfolio.reconciliation import reconcile


# T-001 -----------------------------------------------------------------------
def test_t001_non_positive_quantity_rejected():
    with pytest.raises(ValueError):
        mkprop(Market.CRYPTO, "BTC/KRW", Side.BUY, 0)
    with pytest.raises(ValueError):
        mkprop(Market.CRYPTO, "BTC/KRW", Side.BUY, -5)


# T-002 -----------------------------------------------------------------------
def test_t002_buy_exceeding_cash_blocked():
    stack = build_paper_stack(starting_cash=1_000)
    res = stack.submit_trade_proposal(mkprop(Market.KR, "005930", Side.BUY, 100), 50)
    assert not res.accepted
    assert res.stage == "risk"
    assert "insufficient_cash" in res.reason


# T-003 -----------------------------------------------------------------------
def test_t003_sell_exceeding_holdings_blocked():
    stack = build_paper_stack(starting_cash=1_000_000)
    res = stack.submit_trade_proposal(mkprop(Market.KR, "005930", Side.SELL, 10), 100)
    assert not res.accepted
    assert "insufficient_position" in res.reason

    ledger = PortfolioLedger(1_000_000)
    with pytest.raises(LedgerError):
        ledger.apply_fill(
            Fill(
                order_id="x",
                market=Market.KR,
                symbol="005930",
                side=Side.SELL,
                quantity=1,
                price=100,
                currency=Currency.KRW,
            )
        )


# T-004 -----------------------------------------------------------------------
def test_t004_partial_fill_accounting():
    stack = build_paper_stack(starting_cash=1_000_000)
    res = stack.submit_trade_proposal(mkprop(Market.KR, "005930", Side.BUY, 100), 100)
    assert res.accepted

    for qty in (40, 20, 40):
        stack.settle(stack.broker.simulate_fill(res.order_id, 100, qty))

    order = stack.broker.orders[res.order_id]
    assert order.status is OrderStatus.FILLED
    assert order.filled_quantity == pytest.approx(100)

    pos = stack.ledger.get_position(Market.KR, "005930")
    assert pos.quantity == pytest.approx(100)
    assert pos.avg_cost == pytest.approx(100.0)
    assert stack.ledger.cash[Currency.KRW] == pytest.approx(1_000_000 - 100 * 100)

    with pytest.raises(BrokerError):
        stack.broker.simulate_fill(res.order_id, 100, 1)


# T-005 -----------------------------------------------------------------------
def test_t005_duplicate_fill_ignored():
    stack = build_paper_stack(starting_cash=1_000_000)
    res = stack.submit_trade_proposal(mkprop(Market.KR, "005930", Side.BUY, 10), 100)
    fill = stack.broker.simulate_fill(res.order_id, 100)

    assert stack.ledger.apply_fill(fill) is True
    cash_after_first = stack.ledger.cash[Currency.KRW]
    assert stack.ledger.apply_fill(fill) is False  # INV-04
    assert stack.ledger.cash[Currency.KRW] == cash_after_first
    assert stack.ledger.get_position(Market.KR, "005930").quantity == pytest.approx(10)


# T-006 -----------------------------------------------------------------------
def test_t006_filled_order_cannot_refill():
    stack = build_paper_stack(starting_cash=1_000_000)
    res = stack.submit_trade_proposal(mkprop(Market.KR, "005930", Side.BUY, 10), 100)
    stack.broker.simulate_fill(res.order_id, 100)
    assert stack.broker.orders[res.order_id].status is OrderStatus.FILLED
    with pytest.raises(BrokerError):
        stack.broker.simulate_fill(res.order_id, 100)


# T-007 -----------------------------------------------------------------------
def test_t007_cancelled_order_cannot_fill():
    stack = build_paper_stack(starting_cash=1_000_000)
    res = stack.submit_trade_proposal(mkprop(Market.KR, "005930", Side.BUY, 10), 100)
    stack.cancel_order(res.order_id)  # broker cancel + reservation release
    assert stack.broker.orders[res.order_id].status is OrderStatus.CANCELLED
    assert stack.ledger.reserved_cash[Currency.KRW] == pytest.approx(0.0)
    with pytest.raises(BrokerError):
        stack.broker.simulate_fill(res.order_id, 100)


# T-008 -----------------------------------------------------------------------
def test_t008_commission_correctness():
    stack = build_paper_stack(starting_cash=1_000_000, commission_rate=0.001)
    res = stack.submit_trade_proposal(mkprop(Market.KR, "005930", Side.BUY, 10), 100)
    fill = stack.broker.simulate_fill(res.order_id, 100)
    stack.settle(fill)
    assert fill.commission == pytest.approx(10 * 100 * 0.001)
    assert stack.ledger.cash[Currency.KRW] == pytest.approx(1_000_000 - 1000 - 1.0)
    assert stack.ledger.fees[Currency.KRW] == pytest.approx(1.0)


# T-009 -----------------------------------------------------------------------
def test_t009_tax_correctness_on_sell():
    stack = build_paper_stack(starting_cash=1_000_000, tax_rate_sell=0.0018)
    buy = stack.submit_trade_proposal(mkprop(Market.KR, "005930", Side.BUY, 10), 100)
    stack.settle(stack.broker.simulate_fill(buy.order_id, 100))

    sell = stack.submit_trade_proposal(mkprop(Market.KR, "005930", Side.SELL, 10), 110)
    fill = stack.broker.simulate_fill(sell.order_id, 110)
    stack.settle(fill)

    assert fill.tax == pytest.approx(10 * 110 * 0.0018)
    assert stack.ledger.taxes[Currency.KRW] == pytest.approx(1.98)
    assert stack.ledger.realized_pnl[Currency.KRW] == pytest.approx(1100 - 1000 - 1.98)


# T-010 -----------------------------------------------------------------------
def test_t010_slippage_applied():
    stack = build_paper_stack(starting_cash=1_000_000, slippage_bps=10)
    res = stack.submit_trade_proposal(mkprop(Market.KR, "005930", Side.BUY, 10), 100)
    fill = stack.broker.simulate_fill(res.order_id, 100)
    assert fill.price == pytest.approx(100 * 1.001)
    assert fill.slippage_estimate == pytest.approx(abs(fill.price - 100) * 10)


# T-011 -----------------------------------------------------------------------
def test_t011_risk_max_order_notional_block():
    stack = build_paper_stack(starting_cash=1_000_000, risk_kwargs={"max_order_notional": 500})
    res = stack.submit_trade_proposal(mkprop(Market.KR, "005930", Side.BUY, 10), 100)
    assert not res.accepted
    assert "max_order_notional" in res.reason


# T-012 -----------------------------------------------------------------------
def test_t012_risk_max_exposure_block():
    stack = build_paper_stack(starting_cash=1_000_000, risk_kwargs={"max_symbol_exposure": 0.5})
    res = stack.submit_trade_proposal(mkprop(Market.KR, "005930", Side.BUY, 6000), 100)
    assert not res.accepted
    assert "max_symbol_exposure" in res.reason


# T-013 -----------------------------------------------------------------------
def test_t013_daily_loss_block():
    stack = build_paper_stack(starting_cash=1_000_000, risk_kwargs={"daily_loss_limit": 1000})
    stack.risk.start_new_day()
    stack.ledger.realized_pnl[Currency.KRW] = -5000.0
    res = stack.submit_trade_proposal(mkprop(Market.KR, "005930", Side.BUY, 1), 100)
    assert not res.accepted
    assert "daily_loss_limit" in res.reason


# T-014 -----------------------------------------------------------------------
def test_t014_drawdown_block():
    stack = build_paper_stack(starting_cash=1_000_000, risk_kwargs={"max_drawdown": 0.10})
    stack.risk.observe_equity(1_000_000)
    stack.ledger.cash[Currency.KRW] = 800_000.0
    res = stack.submit_trade_proposal(mkprop(Market.KR, "005930", Side.BUY, 1), 100)
    assert not res.accepted
    assert "max_drawdown" in res.reason


# T-015 -----------------------------------------------------------------------
def test_t015_kill_switch_block():
    stack = build_paper_stack(starting_cash=1_000_000, risk_kwargs={"kill_switch": True})
    res = stack.submit_trade_proposal(mkprop(Market.KR, "005930", Side.BUY, 1), 100)
    assert not res.accepted
    assert "kill_switch" in res.reason


# T-016 -----------------------------------------------------------------------
def test_t016_duplicate_order_block():
    stack = build_paper_stack(
        starting_cash=1_000_000, integrity_kwargs={"duplicate_window_seconds": 30}
    )
    first = stack.submit_trade_proposal(mkprop(Market.KR, "005930", Side.BUY, 10), 100)
    assert first.accepted
    second = stack.submit_trade_proposal(mkprop(Market.KR, "005930", Side.BUY, 10), 100)
    assert not second.accepted
    assert "duplicate_order" in second.reason


# T-017 -----------------------------------------------------------------------
def test_t017_opposing_order_detected():
    stack = build_paper_stack(starting_cash=1_000_000)
    seeded = stack.submit_trade_proposal(mkprop(Market.KR, "005930", Side.BUY, 10), 100)
    stack.settle(stack.broker.simulate_fill(seeded.order_id, 100))

    open_buy = stack.submit_trade_proposal(mkprop(Market.KR, "005930", Side.BUY, 5), 100)
    assert open_buy.accepted

    sell = stack.submit_trade_proposal(mkprop(Market.KR, "005930", Side.SELL, 5), 100)
    assert not sell.accepted
    assert "opposing_order" in sell.reason


# T-018 -----------------------------------------------------------------------
def test_t018_compliance_unknown_blocks_in_live():
    stack = build_paper_stack(
        mode="LIVE",
        starting_cash=1_000_000,
        compliance_kwargs={"tradable_symbols": {"005930"}},
    )
    res = stack.submit_trade_proposal(mkprop(Market.KR, "000660", Side.BUY, 1), 100)
    assert not res.accepted
    assert res.stage == "compliance"
    assert "unknown_blocked_in_live" in res.reason


def test_t018b_compliance_unknown_stays_unknown_in_paper():
    stack = build_paper_stack(
        mode="PAPER",
        starting_cash=1_000_000,
        compliance_kwargs={"tradable_symbols": {"005930"}},
    )
    res = stack.submit_trade_proposal(mkprop(Market.KR, "000660", Side.BUY, 1), 100)
    assert not res.accepted
    assert "compliance:UNKNOWN" in res.reason
    assert "symbol_not_verified" in res.reason


# T-019 -----------------------------------------------------------------------
def test_t019_reconciliation_detects_mismatch():
    stack = build_paper_stack(starting_cash=1_000_000)
    res = stack.submit_trade_proposal(mkprop(Market.KR, "005930", Side.BUY, 10), 100)
    stack.settle(stack.broker.simulate_fill(res.order_id, 100))

    ok = reconcile(
        stack.ledger,
        broker_cash={Currency.KRW: stack.ledger.cash[Currency.KRW]},
        broker_positions={(Market.KR, "005930"): 10},
    )
    assert ok.ok

    bad = reconcile(
        stack.ledger,
        broker_cash={Currency.KRW: 999_999.0},
        broker_positions={(Market.KR, "005930"): 7},
    )
    assert not bad.ok
    assert bad.reasons


# T-020 -----------------------------------------------------------------------
def test_t020_no_execution_bypass():
    stack = build_paper_stack(starting_cash=1_000_000)
    gateway = stack.gateway

    assert hasattr(gateway, "submit_trade_proposal")
    assert not hasattr(gateway, "broker")
    assert not hasattr(gateway, "submit_order")
    assert not hasattr(gateway, "ledger")

    proposal = mkprop(Market.KR, "005930", Side.BUY, 1)
    order = Order(proposal=proposal)  # CREATED
    with pytest.raises(InvalidStateTransition):
        stack.broker.submit_order(order)  # requires APPROVED
    with pytest.raises(InvalidStateTransition):
        order.transition_to(OrderStatus.SUBMITTED)  # CREATED -> SUBMITTED illegal
