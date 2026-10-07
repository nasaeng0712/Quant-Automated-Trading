"""Fix Batch #1.1 - new cases T-021 .. T-034.

FIX-01 multi-currency exposure, FIX-02 reservations, FIX-03 transaction-cost
accounting, FIX-04 terminal/idempotency lockdown, FIX-05 LIMIT price gating.
"""

from __future__ import annotations

import pytest

from conftest import make_proposal as mkprop

from qat.app import build_paper_stack
from qat.core.fx import StaticFXRateProvider
from qat.core.models import (
    Currency,
    Fill,
    Market,
    OrderStatus,
    OrderType,
    Position,
    Side,
)
from qat.execution.settlement import FillOutcome
from qat.paper.broker import BrokerError


# ---------------------------------------------------------------- FIX-01
def test_t021_multi_currency_exposure_converted_to_base():
    fx = StaticFXRateProvider({(Currency.USD, Currency.KRW): 1400.0})
    stack = build_paper_stack(
        starting_cash=1_000_000,
        base_currency=Currency.KRW,
        fx_provider=fx,
        risk_kwargs={"max_base_exposure": 10_000_000},
    )
    stack.ledger.positions[("KR", "005930")] = Position(quantity=50_000, avg_cost=100.0)  # 5,000,000 KRW
    stack.ledger.positions[("US", "AAPL")] = Position(quantity=25, avg_cost=200.0)        # 5,000 USD

    res = stack.submit_trade_proposal(mkprop(Market.KR, "005930", Side.BUY, 1), 100)
    assert not res.accepted
    assert res.stage == "risk"
    assert "max_base_exposure" in res.reason
    # naive 5,000,000 + 5,000 = 5,005,000 would have passed the 10,000,000 limit.


def test_t022_missing_fx_fails_closed():
    fx = StaticFXRateProvider({})  # no USD -> KRW rate
    stack = build_paper_stack(
        starting_cash=1_000_000,
        base_currency=Currency.KRW,
        fx_provider=fx,
        risk_kwargs={"max_base_exposure": 10_000_000},
    )
    stack.ledger.positions[("US", "AAPL")] = Position(quantity=25, avg_cost=200.0)

    res = stack.submit_trade_proposal(mkprop(Market.KR, "005930", Side.BUY, 1), 100)
    assert not res.accepted
    assert res.stage == "risk"
    assert "fx_unavailable" in res.reason


# ---------------------------------------------------------------- FIX-02
def test_t023_consecutive_buy_reservation():
    stack = build_paper_stack(starting_cash=1_000)
    first = stack.submit_trade_proposal(mkprop(Market.KR, "005930", Side.BUY, 700), 1)
    assert first.accepted
    assert stack.ledger.reserved_cash[Currency.KRW] >= 700
    assert stack.ledger.available_cash(Currency.KRW) == pytest.approx(300)

    # second order needs 500 but only 300 is available - still unfilled first order
    second = stack.submit_trade_proposal(mkprop(Market.KR, "005930", Side.BUY, 500), 1)
    assert not second.accepted
    assert "insufficient_cash" in second.reason


def test_t024_reservation_released_on_cancel():
    stack = build_paper_stack(starting_cash=1_000)
    res = stack.submit_trade_proposal(mkprop(Market.KR, "005930", Side.BUY, 700), 1)
    assert res.accepted
    assert stack.ledger.available_cash(Currency.KRW) == pytest.approx(300)

    stack.cancel_order(res.order_id)
    assert stack.broker.orders[res.order_id].status is OrderStatus.CANCELLED
    assert stack.ledger.available_cash(Currency.KRW) == pytest.approx(1_000)
    assert stack.ledger.reserved_cash[Currency.KRW] == pytest.approx(0.0)


def test_t025_partial_fill_reservation_tracking():
    stack = build_paper_stack(starting_cash=1_000_000)
    res = stack.submit_trade_proposal(mkprop(Market.KR, "005930", Side.BUY, 100), 100)
    assert res.accepted
    assert stack.ledger.reserved_cash[Currency.KRW] == pytest.approx(10_000)

    outcome = stack.settle(stack.broker.simulate_fill(res.order_id, 100, 40))
    assert outcome is FillOutcome.APPLIED
    assert stack.broker.orders[res.order_id].status is OrderStatus.PARTIALLY_FILLED
    assert stack.ledger.get_position(Market.KR, "005930").quantity == pytest.approx(40)
    # 40 filled -> 60 still reserved
    assert stack.ledger.reserved_cash[Currency.KRW] == pytest.approx(6_000)
    assert stack.broker.orders[res.order_id].reservation.cash_remaining == pytest.approx(6_000)


def test_t026_sell_position_reservation():
    stack = build_paper_stack(starting_cash=1_000_000)
    buy = stack.submit_trade_proposal(mkprop(Market.KR, "005930", Side.BUY, 100), 100)
    stack.settle(stack.broker.simulate_fill(buy.order_id, 100))

    first = stack.submit_trade_proposal(mkprop(Market.KR, "005930", Side.SELL, 80), 100)
    assert first.accepted
    assert stack.ledger.available_quantity(Market.KR, "005930") == pytest.approx(20)

    second = stack.submit_trade_proposal(mkprop(Market.KR, "005930", Side.SELL, 30), 100)
    assert not second.accepted
    assert "insufficient_position" in second.reason


# ---------------------------------------------------------------- FIX-03
def test_t027_buy_cost_basis_includes_fees():
    stack = build_paper_stack(starting_cash=1_000_000, commission_rate=0.01)  # 1%
    res = stack.submit_trade_proposal(mkprop(Market.KR, "005930", Side.BUY, 10), 100)
    stack.settle(stack.broker.simulate_fill(res.order_id, 100))

    pos = stack.ledger.get_position(Market.KR, "005930")
    # gross 1000 + commission 10 = 1010 basis over 10 shares
    assert pos.avg_cost == pytest.approx(101.0)
    assert stack.ledger.cash[Currency.KRW] == pytest.approx(1_000_000 - 1010)


def test_t028_realized_pnl_no_double_count_of_buy_costs():
    stack = build_paper_stack(starting_cash=1_000_000, commission_rate=0.01)
    buy = stack.submit_trade_proposal(mkprop(Market.KR, "005930", Side.BUY, 10), 100)
    stack.settle(stack.broker.simulate_fill(buy.order_id, 100))  # basis 1010, avg 101

    sell = stack.submit_trade_proposal(mkprop(Market.KR, "005930", Side.SELL, 10), 110)
    stack.settle(stack.broker.simulate_fill(sell.order_id, 110))

    # proceeds 1100; sell commission 11; realized = 1100 - 1010 - 11 = 79
    # (double counting the buy commission would give 69; ignoring it would give 89)
    assert stack.ledger.realized_pnl[Currency.KRW] == pytest.approx(79.0)
    assert stack.ledger.fees[Currency.KRW] == pytest.approx(21.0)  # 10 buy + 11 sell, once each


def test_t028b_slippage_not_charged_twice():
    stack = build_paper_stack(starting_cash=1_000_000, slippage_bps=50)  # 0.5%
    res = stack.submit_trade_proposal(mkprop(Market.KR, "005930", Side.BUY, 10), 100)
    fill = stack.broker.simulate_fill(res.order_id, 100)
    stack.settle(fill)
    # cash out == quantity * fill_price only (slippage already inside fill.price)
    assert fill.price == pytest.approx(100.5)
    assert stack.ledger.cash[Currency.KRW] == pytest.approx(1_000_000 - 10 * 100.5)
    assert fill.slippage_estimate == pytest.approx(0.5 * 10)  # recorded, not charged


# ---------------------------------------------------------------- FIX-04
def test_t029_duplicate_fill_idempotency_full_snapshot():
    stack = build_paper_stack(starting_cash=1_000_000, commission_rate=0.005, tax_rate_sell=0.001)
    res = stack.submit_trade_proposal(mkprop(Market.KR, "005930", Side.BUY, 10), 100)
    fill = stack.broker.simulate_fill(res.order_id, 100)
    assert stack.settle(fill) is FillOutcome.APPLIED

    def snapshot():
        return (
            dict(stack.ledger.cash),
            {k: (v.quantity, v.avg_cost) for k, v in stack.ledger.positions.items()},
            dict(stack.ledger.realized_pnl),
            dict(stack.ledger.fees),
            dict(stack.ledger.taxes),
        )

    before = snapshot()
    assert stack.settle(fill) is FillOutcome.IGNORED_DUPLICATE
    assert snapshot() == before


def test_t030_terminal_filled_late_fill_blocked():
    stack = build_paper_stack(starting_cash=1_000_000)
    res = stack.submit_trade_proposal(mkprop(Market.KR, "005930", Side.BUY, 10), 100)
    stack.settle(stack.broker.simulate_fill(res.order_id, 100))
    order = stack.broker.orders[res.order_id]
    assert order.status is OrderStatus.FILLED

    with pytest.raises(BrokerError):
        stack.broker.simulate_fill(res.order_id, 100)

    cash_before = dict(stack.ledger.cash)
    late = Fill(
        order_id=order.order_id, market=Market.KR, symbol="005930", side=Side.BUY,
        quantity=5, price=100, currency=Currency.KRW,
    )
    assert stack.settlement.apply_fill(order, late) is FillOutcome.BLOCKED_TERMINAL
    assert dict(stack.ledger.cash) == cash_before


def test_t031_cancelled_late_fill_not_applied():
    stack = build_paper_stack(starting_cash=1_000_000)
    res = stack.submit_trade_proposal(mkprop(Market.KR, "005930", Side.BUY, 10), 100)
    stack.cancel_order(res.order_id)
    order = stack.broker.orders[res.order_id]
    assert order.status is OrderStatus.CANCELLED

    with pytest.raises(BrokerError):
        stack.broker.simulate_fill(res.order_id, 100)

    cash_before = dict(stack.ledger.cash)
    pos_before = stack.ledger.get_position(Market.KR, "005930").quantity
    late = Fill(
        order_id=order.order_id, market=Market.KR, symbol="005930", side=Side.BUY,
        quantity=5, price=100, currency=Currency.KRW,
    )
    assert stack.settlement.apply_fill(order, late) is FillOutcome.BLOCKED_CANCELLED
    assert dict(stack.ledger.cash) == cash_before
    assert stack.ledger.get_position(Market.KR, "005930").quantity == pos_before


# ---------------------------------------------------------------- FIX-05
def test_t032_buy_limit_price_gate():
    stack = build_paper_stack(starting_cash=1_000_000)
    res = stack.submit_trade_proposal(
        mkprop(Market.KR, "005930", Side.BUY, 10, order_type=OrderType.LIMIT, limit_price=100),
        reference_price=100,
    )
    assert res.accepted
    assert stack.broker.simulate_fill(res.order_id, 101) is None  # ref above limit
    assert stack.broker.orders[res.order_id].status is OrderStatus.SUBMITTED

    fill = stack.broker.simulate_fill(res.order_id, 100)  # ref at limit
    assert fill is not None
    assert fill.price <= 100


def test_t033_sell_limit_price_gate():
    stack = build_paper_stack(starting_cash=1_000_000)
    buy = stack.submit_trade_proposal(mkprop(Market.KR, "005930", Side.BUY, 10), 100)
    stack.settle(stack.broker.simulate_fill(buy.order_id, 100))

    res = stack.submit_trade_proposal(
        mkprop(Market.KR, "005930", Side.SELL, 10, order_type=OrderType.LIMIT, limit_price=100),
        reference_price=100,
    )
    assert res.accepted
    assert stack.broker.simulate_fill(res.order_id, 99) is None  # ref below limit
    assert stack.broker.orders[res.order_id].status is OrderStatus.SUBMITTED

    fill = stack.broker.simulate_fill(res.order_id, 100)
    assert fill is not None
    assert fill.price >= 100


def test_t034_limit_slippage_never_worse_than_limit():
    stack = build_paper_stack(starting_cash=1_000_000, slippage_bps=50)  # 0.5%
    buy = stack.submit_trade_proposal(
        mkprop(Market.KR, "005930", Side.BUY, 10, order_type=OrderType.LIMIT, limit_price=100),
        reference_price=100,
    )
    fill_buy = stack.broker.simulate_fill(buy.order_id, 100)
    assert fill_buy is not None
    assert fill_buy.price <= 100  # not 100.5
    stack.settle(fill_buy)

    sell = stack.submit_trade_proposal(
        mkprop(Market.KR, "005930", Side.SELL, 10, order_type=OrderType.LIMIT, limit_price=100),
        reference_price=100,
    )
    fill_sell = stack.broker.simulate_fill(sell.order_id, 100)
    assert fill_sell is not None
    assert fill_sell.price >= 100  # not 99.5
