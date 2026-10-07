"""Invariant, property, determinism and boundary tests (Core System Design v0.1,
sections 7, 36, 37, 48)."""

from __future__ import annotations

import random

import pytest

from conftest import FIXED_NOW, make_proposal as mkprop

from qat.app import build_paper_stack
from qat.cost.engine import CostModel
from qat.core.models import (
    Currency,
    DomainError,
    Fill,
    InvalidStateTransition,
    Market,
    Order,
    OrderStatus,
    Side,
    currency_for,
)
from qat.execution.router import ExecutionRouter, LiveExecutionDisabled
from qat.live.broker_stub import LiveBrokerStub, LiveTradingDisabled
from qat.paper.broker import BrokerError
from qat.portfolio.ledger import PortfolioLedger


# INV-03 --------------------------------------------------------------------
def test_inv03_total_filled_never_exceeds_order_quantity():
    rnd = random.Random(2026)
    for _ in range(150):
        qty = rnd.randint(1, 50)
        stack = build_paper_stack(starting_cash=10_000_000)
        res = stack.submit_trade_proposal(mkprop(Market.KR, "005930", Side.BUY, qty), 100)
        assert res.accepted
        remaining = qty
        while remaining > 0:
            take = rnd.randint(1, remaining)
            stack.broker.simulate_fill(res.order_id, 100, take)
            remaining -= take
            assert stack.broker.orders[res.order_id].filled_quantity <= qty + 1e-9
        assert stack.broker.orders[res.order_id].status is OrderStatus.FILLED
        with pytest.raises(BrokerError):
            stack.broker.simulate_fill(res.order_id, 100, 1)


# INV-04 ------------------------------------------------------------------------
def test_inv04_fill_idempotency_direct():
    ledger = PortfolioLedger(1_000_000)
    fill = Fill(
        order_id="o1",
        market=Market.KR,
        symbol="005930",
        side=Side.BUY,
        quantity=5,
        price=100,
        currency=Currency.KRW,
    )
    assert ledger.apply_fill(fill) is True
    assert ledger.apply_fill(fill) is False
    assert ledger.get_position(Market.KR, "005930").quantity == pytest.approx(5)
    assert ledger.cash[Currency.KRW] == pytest.approx(1_000_000 - 500)


# section 6 / 7 -------------------------------------------------------------------
def test_order_state_machine_rejects_illegal_transitions():
    order = Order(proposal=mkprop(Market.KR, "005930", Side.BUY, 1))
    assert order.status is OrderStatus.CREATED
    with pytest.raises(InvalidStateTransition):
        order.transition_to(OrderStatus.FILLED)
    order.transition_to(OrderStatus.VALIDATED)
    order.transition_to(OrderStatus.APPROVED)
    order.transition_to(OrderStatus.SUBMITTED)
    order.transition_to(OrderStatus.PARTIALLY_FILLED)
    order.transition_to(OrderStatus.FILLED)
    assert order.is_terminal
    with pytest.raises(InvalidStateTransition):
        order.transition_to(OrderStatus.CANCELLED)


# section 3 -------------------------------------------------------------------
def test_crypto_symbol_currency_parsing():
    assert currency_for(Market.CRYPTO, "BTC/KRW") is Currency.KRW
    assert currency_for(Market.CRYPTO, "ETH/USDT") is Currency.USDT
    with pytest.raises(DomainError):
        currency_for(Market.CRYPTO, "BTCKRW")


# section 10 ------------------------------------------------------------------
def test_multi_currency_ledger_separation():
    stack = build_paper_stack(
        starting_cash={Currency.KRW: 1_000_000, Currency.USD: 10_000},
        base_currency=Currency.KRW,
    )
    kr = stack.submit_trade_proposal(mkprop(Market.KR, "005930", Side.BUY, 10), 100)
    stack.settle(stack.broker.simulate_fill(kr.order_id, 100))
    us = stack.submit_trade_proposal(mkprop(Market.US, "AAPL", Side.BUY, 5), 200)
    stack.settle(stack.broker.simulate_fill(us.order_id, 200))

    assert stack.ledger.cash[Currency.KRW] == pytest.approx(1_000_000 - 1000)
    assert stack.ledger.cash[Currency.USD] == pytest.approx(10_000 - 1000)
    assert stack.ledger.get_position(Market.KR, "005930").quantity == pytest.approx(10)
    assert stack.ledger.get_position(Market.US, "AAPL").quantity == pytest.approx(5)


# section 11 ------------------------------------------------------------------
def test_average_cost_updates_on_buy_only():
    ledger = PortfolioLedger(1_000_000)

    def buy(qty, price):
        ledger.apply_fill(
            Fill(order_id="o", market=Market.KR, symbol="005930", side=Side.BUY,
                 quantity=qty, price=price, currency=Currency.KRW)
        )

    buy(10, 100)
    buy(10, 120)
    pos = ledger.get_position(Market.KR, "005930")
    assert pos.avg_cost == pytest.approx((10 * 100 + 10 * 120) / 20)

    ledger.apply_fill(
        Fill(order_id="o", market=Market.KR, symbol="005930", side=Side.SELL,
             quantity=5, price=200, currency=Currency.KRW)
    )
    pos = ledger.get_position(Market.KR, "005930")
    assert pos.quantity == pytest.approx(15)
    assert pos.avg_cost == pytest.approx(110)  # unchanged by SELL


# section 19 ------------------------------------------------------------------
def test_net_alpha_blocks_insufficient_edge():
    stack = build_paper_stack(
        starting_cash=1_000_000,
        cost_models={Market.KR: CostModel(commission_rate=0.001, half_spread_bps=10)},
    )
    res = stack.submit_trade_proposal(
        mkprop(Market.KR, "005930", Side.BUY, 10, expected_gross_return=0.0), 100
    )
    assert not res.accepted
    assert res.stage == "net_alpha"


def test_net_alpha_passes_sufficient_edge():
    stack = build_paper_stack(
        starting_cash=1_000_000,
        cost_models={Market.KR: CostModel(commission_rate=0.001, half_spread_bps=10)},
    )
    res = stack.submit_trade_proposal(
        mkprop(Market.KR, "005930", Side.BUY, 10, expected_gross_return=0.05), 100
    )
    assert res.accepted


# section 35 ------------------------------------------------------------------
def test_audit_log_records_pipeline_decisions():
    stack = build_paper_stack(starting_cash=1_000_000)
    stack.submit_trade_proposal(mkprop(Market.KR, "005930", Side.BUY, 1), 100)
    stages = {rec.stage for rec in stack.audit.records}
    assert {
        "proposal_received",
        "validator_decision",
        "net_alpha_decision",
        "risk_decision",
        "compliance_decision",
        "integrity_decision",
        "order_submitted",
    } <= stages


# section 31 / 48 -----------------------------------------------------------------
def test_live_mode_routing_is_disabled():
    router = ExecutionRouter(mode="LIVE")
    with pytest.raises(LiveExecutionDisabled):
        router.route(Order(proposal=mkprop(Market.KR, "005930", Side.BUY, 1)), coordinator=None)


def test_live_mode_execution_blocked_end_to_end():
    stack = build_paper_stack(mode="LIVE", starting_cash=1_000_000)
    res = stack.submit_trade_proposal(mkprop(Market.KR, "005930", Side.BUY, 1), 100)
    assert not res.accepted
    assert "submission:ERROR" in res.reason
    assert "Live execution" in res.reason


def test_live_broker_stub_cannot_be_used():
    with pytest.raises(LiveTradingDisabled):
        LiveBrokerStub()


# section 36 ------------------------------------------------------------------
def _deterministic_run() -> list:
    stack = build_paper_stack(
        starting_cash=1_000_000,
        commission_rate=0.001,
        tax_rate_sell=0.0018,
        slippage_bps=7,
        now_fn=lambda: FIXED_NOW,
    )
    trace: list = []

    buy = stack.submit_trade_proposal(
        mkprop(Market.KR, "005930", Side.BUY, 10, signal_timestamp=FIXED_NOW), 100
    )
    for qty in (4, 6):
        fill = stack.broker.simulate_fill(buy.order_id, 100, qty)
        stack.settle(fill)
        trace.append((round(fill.price, 9), round(fill.commission, 9), fill.timestamp.isoformat()))

    sell = stack.submit_trade_proposal(
        mkprop(Market.KR, "005930", Side.SELL, 10, signal_timestamp=FIXED_NOW), 110
    )
    fill = stack.broker.simulate_fill(sell.order_id, 110)
    stack.settle(fill)
    trace.append((round(fill.price, 9), round(fill.tax, 9)))
    trace.append(round(stack.ledger.realized_pnl[Currency.KRW], 9))
    trace.append(round(stack.ledger.cash[Currency.KRW], 9))
    return trace


def test_deterministic_paper_mode_is_reproducible():
    assert _deterministic_run() == _deterministic_run()


# section 12 / 18 - numbers come from config, not code -------------------------
def test_stack_builds_from_settings_file():
    pytest.importorskip("yaml", reason="PyYAML required to read config/settings.yaml")
    from qat.app import build_paper_stack_from_settings

    stack = build_paper_stack_from_settings(starting_cash=1_000_000)
    # placeholder KR cost model from settings.yaml has a non-zero commission
    assert stack.net_alpha_gate.cost_engine.model_for(Market.KR).commission_rate > 0
    res = stack.submit_trade_proposal(
        mkprop(Market.KR, "005930", Side.BUY, 10, expected_gross_return=0.05), 100
    )
    assert res.accepted
