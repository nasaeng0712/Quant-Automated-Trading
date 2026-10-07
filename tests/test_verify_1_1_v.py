"""Verification Batch #1.1-V - minimal reproduction of the four re-audit claims.

Each test asserts the CORRECT / expected behaviour. A test that PASSES means the
claimed defect is NOT REPRODUCED; a test that FAILS reproduces a real defect.
"""

from __future__ import annotations

import pytest

from conftest import make_proposal as mkprop

from qat.app import build_paper_stack
from qat.core.fx import MissingFXRateError, StaticFXRateProvider
from qat.core.models import Currency, Market, OrderStatus, OrderType, Position, Side
from qat.execution.settlement import FillOutcome


# ============================================================ V-01
def test_v01_buy_limit_slippage_never_exceeds_limit():
    """BUY LIMIT 100, reference 99.9 (below limit), slippage 0.5%.
    raw slipped price = 99.9 * 1.005 = 100.3995 -> must be clamped to <= 100."""
    stack = build_paper_stack(starting_cash=1_000_000, slippage_bps=50)
    res = stack.submit_trade_proposal(
        mkprop(Market.KR, "005930", Side.BUY, 10, order_type=OrderType.LIMIT, limit_price=100),
        reference_price=99.9,
    )
    assert res.accepted
    fill = stack.broker.simulate_fill(res.order_id, 99.9)
    assert fill is not None
    print(f"V-01 BUY  reference=99.9 slip=0.5%  -> fill_price={fill.price}")
    assert fill.price <= 100.0 + 1e-9


def test_v01_sell_limit_slippage_never_below_limit():
    """SELL LIMIT 100, reference 100.1 (above limit), slippage 0.5%.
    raw slipped price = 100.1 * 0.995 = 99.5995 -> must be clamped to >= 100."""
    stack = build_paper_stack(starting_cash=1_000_000, slippage_bps=50)
    buy = stack.submit_trade_proposal(mkprop(Market.KR, "005930", Side.BUY, 10), 100)
    stack.settle(stack.broker.simulate_fill(buy.order_id, 100))

    res = stack.submit_trade_proposal(
        mkprop(Market.KR, "005930", Side.SELL, 10, order_type=OrderType.LIMIT, limit_price=100),
        reference_price=100.1,
    )
    assert res.accepted
    fill = stack.broker.simulate_fill(res.order_id, 100.1)
    assert fill is not None
    print(f"V-01 SELL reference=100.1 slip=0.5% -> fill_price={fill.price}")
    assert fill.price >= 100.0 - 1e-9


# ============================================================ V-02
def test_v02_missing_fx_rate_fails_closed_no_fallback():
    """USD exposure present, no USD->KRW rate. Must end as
    MissingFXRateError -> UNKNOWN -> order not approved. No 1.0/0/None fallback."""
    provider = StaticFXRateProvider({})  # deliberately empty
    stack = build_paper_stack(
        starting_cash=1_000_000,
        base_currency=Currency.KRW,
        fx_provider=provider,
        risk_kwargs={"max_base_exposure": 10_000_000},
    )
    stack.ledger.positions[("US", "AAPL")] = Position(quantity=25, avg_cost=200.0)

    # 1) the ledger conversion itself raises - it does not silently return 1.0/0
    with pytest.raises(MissingFXRateError):
        stack.ledger.exposure_in(Currency.KRW, provider)

    # 2) end to end: order is not approved and the reason is the FX failure
    res = stack.submit_trade_proposal(mkprop(Market.KR, "005930", Side.BUY, 1), 100)
    print(f"V-02 end-to-end -> accepted={res.accepted} stage={res.stage} reason={res.reason}")
    assert res.accepted is False
    assert res.stage == "risk"
    assert "fx_unavailable" in res.reason


# ============================================================ V-03
def test_v03_partial_fill_then_cancel_reservation_exact():
    """Cash 10,000. BUY 10 @ 100 -> reserve 1,000. Fill 4. Cancel.
    Filled 4 hits real cash/basis; only the remaining 6 units of reservation
    are released; final cash_reserved == 0; no double release."""
    stack = build_paper_stack(starting_cash=10_000)
    res = stack.submit_trade_proposal(mkprop(Market.KR, "005930", Side.BUY, 10), 100)
    assert res.accepted
    assert stack.ledger.cash[Currency.KRW] == pytest.approx(10_000)
    assert stack.ledger.reserved_cash[Currency.KRW] == pytest.approx(1_000)
    assert stack.ledger.available_cash(Currency.KRW) == pytest.approx(9_000)

    stack.settle(stack.broker.simulate_fill(res.order_id, 100, 4))
    assert stack.ledger.cash[Currency.KRW] == pytest.approx(10_000 - 400)      # 4 * 100
    assert stack.ledger.reserved_cash[Currency.KRW] == pytest.approx(600)      # 6 * 100
    assert stack.ledger.available_cash(Currency.KRW) == pytest.approx(9_000)
    assert stack.ledger.get_position(Market.KR, "005930").quantity == pytest.approx(4)
    assert stack.ledger.get_position(Market.KR, "005930").avg_cost == pytest.approx(100)

    stack.cancel_order(res.order_id)
    order = stack.broker.orders[res.order_id]
    print(
        f"V-03 after cancel -> status={order.status.value} "
        f"cash={stack.ledger.cash[Currency.KRW]} "
        f"reserved={stack.ledger.reserved_cash[Currency.KRW]} "
        f"available={stack.ledger.available_cash(Currency.KRW)} "
        f"released={order.reservation.cash_released}/{order.reservation.cash_reserved}"
    )
    assert order.status is OrderStatus.CANCELLED
    assert stack.ledger.cash[Currency.KRW] == pytest.approx(9_600)             # only 4 filled
    assert stack.ledger.reserved_cash[Currency.KRW] == pytest.approx(0.0)
    assert stack.ledger.available_cash(Currency.KRW) == pytest.approx(9_600)
    # total released exactly equals total reserved - no double release
    assert order.reservation.cash_released == pytest.approx(order.reservation.cash_reserved)


# ============================================================ V-04  (the critical one)
def test_v04a_reservation_upper_bounds_actual_cost_same_price_fill():
    """Prevention: commission 1% + slippage 1%, fill at the reserve-time
    reference price. The reservation must be an upper bound on the actual cost,
    so cash_available stays >= 0 and no breach is recorded.

    Pre-fix the reservation was computed on the pre-slippage notional and
    under-covered by notional*slip*commission (= 1.0 here), pushing
    cash_available to -1.0 with nothing noticing.
    """
    stack = build_paper_stack(starting_cash=1_000_000, commission_rate=0.01, slippage_bps=100)
    res = stack.submit_trade_proposal(mkprop(Market.KR, "005930", Side.BUY, 100), 100)
    assert res.accepted
    reserved = stack.ledger.reserved_cash[Currency.KRW]

    fill = stack.broker.simulate_fill(res.order_id, 100)
    stack.settle(fill)
    actual_cost = fill.gross + fill.total_cost
    avail = stack.ledger.available_cash(Currency.KRW)
    print(
        f"V-04a reserved={reserved} actual_cost={actual_cost} "
        f"cash_available={avail} breaches={stack.ledger.reservation_breaches}"
    )
    assert actual_cost <= reserved + 1e-6          # reservation is an upper bound
    assert avail >= -1e-6                           # cash_available never negative
    assert stack.ledger.reservation_breaches == []
    assert stack.ledger.get_position(Market.KR, "005930").quantity == pytest.approx(100)


def test_v04b_overrun_beyond_reservation_is_flagged_and_fails_closed():
    """Detection: fill at a reference price far above the one used at reserve
    time (a gap the reservation cannot anticipate). The real fill is kept, the
    breach is recorded, and the Risk gate blocks the next order (fail closed).
    """
    stack = build_paper_stack(starting_cash=10_201, commission_rate=0.01, slippage_bps=100)
    res = stack.submit_trade_proposal(mkprop(Market.KR, "005930", Side.BUY, 100), 100)
    assert res.accepted
    reserved = stack.ledger.reserved_cash[Currency.KRW]

    fill = stack.broker.simulate_fill(res.order_id, 200)  # 2x the reserve-time reference
    outcome = stack.settle(fill)
    actual_cost = fill.gross + fill.total_cost
    avail = stack.ledger.available_cash(Currency.KRW)
    breaches = stack.ledger.reservation_breaches
    print(
        f"V-04b outcome={outcome} reserved={reserved} actual_cost={actual_cost} "
        f"cash_total={stack.ledger.cash[Currency.KRW]} cash_available={avail} breaches={breaches}"
    )

    # 1) the real fill is NOT discarded
    assert outcome is FillOutcome.APPLIED
    assert stack.ledger.get_position(Market.KR, "005930").quantity == pytest.approx(100)
    assert stack.ledger.fees[Currency.KRW] == pytest.approx(fill.commission)
    # 2) the cost is not hidden - cash reflects the true outflow
    assert stack.ledger.cash[Currency.KRW] == pytest.approx(10_201 - actual_cost)
    # 3) the breach is recorded with the shortfall
    assert len(breaches) == 1
    assert breaches[0]["shortfall"] == pytest.approx(-avail)
    assert breaches[0]["overrun"] > 0
    # 4) Risk fails closed on the next order until reconciled
    nxt = stack.submit_trade_proposal(mkprop(Market.KR, "005930", Side.BUY, 1), 100)
    print(f"V-04b next order -> accepted={nxt.accepted} stage={nxt.stage} reason={nxt.reason}")
    assert nxt.accepted is False
    assert "reservation_breach" in nxt.reason


# ============================================================ V-05
def test_v05_slippage_not_double_counted():
    """Same trade with slippage 0 vs slippage 0.5%.
    The only PnL difference must come from fill_price (and fees on it);
    slippage_estimate is never a separate cash deduction."""
    def run(slippage_bps):
        stack = build_paper_stack(starting_cash=1_000_000, commission_rate=0.001,
                                  slippage_bps=slippage_bps)
        buy = stack.submit_trade_proposal(mkprop(Market.KR, "005930", Side.BUY, 10), 100)
        bf = stack.broker.simulate_fill(buy.order_id, 100)
        cash_before = 1_000_000
        stack.settle(bf)
        cash_after_buy = stack.ledger.cash[Currency.KRW]
        # cash out on BUY == quantity*fill_price + commission ONLY
        assert cash_after_buy == pytest.approx(cash_before - (bf.quantity * bf.price + bf.commission))
        sell = stack.submit_trade_proposal(mkprop(Market.KR, "005930", Side.SELL, 10), 110)
        sf = stack.broker.simulate_fill(sell.order_id, 110)
        stack.settle(sf)
        return bf, sf, stack.ledger.realized_pnl[Currency.KRW]

    b0, s0, pnl0 = run(0)
    b1, s1, pnl1 = run(50)

    # reconstruct the pnl delta purely from price + commission differences
    buy_price_delta = (b1.price - b0.price) * 10          # paid more per share
    sell_price_delta = (s1.price - s0.price) * 10         # received less per share
    commission_delta = (b1.commission - b0.commission) + (s1.commission - s0.commission)
    expected_pnl_delta = -buy_price_delta + sell_price_delta - commission_delta

    print(
        f"V-05 pnl0={pnl0:.6f} pnl1={pnl1:.6f} delta={pnl1 - pnl0:.6f} "
        f"expected_from_price_only={expected_pnl_delta:.6f} "
        f"slippage_estimate b1={b1.slippage_estimate} s1={s1.slippage_estimate}"
    )
    assert (pnl1 - pnl0) == pytest.approx(expected_pnl_delta)
    assert b1.slippage_estimate > 0 and s1.slippage_estimate > 0  # recorded, not charged
