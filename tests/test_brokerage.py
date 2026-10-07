"""Broker adapter boundary (contract + fake adapter): idempotency, UNKNOWN-is-never-terminal, duplicate / inconsistent fills, reconnect, stale snapshot."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from conftest import make_proposal as mkprop

from qat.app import build_paper_stack
from qat.brokerage.boundary import BrokerBoundary
from qat.brokerage.contract import BrokerOrderState, BrokerTimeout
from qat.brokerage.fake import FakeBrokerAdapter
from qat.core.models import Market, OrderStatus, Side

def _now():
    return datetime.now(timezone.utc)


NOW = _now()


def _stack(cash=1_000_000):
    stack = build_paper_stack(starting_cash=cash, now_fn=_now)
    adapter = FakeBrokerAdapter(now_fn=_now)
    boundary = BrokerBoundary(adapter, stack.settlement, stack.ledger, name=stack.broker.name, now_fn=_now)
    stack.coordinator.brokers[stack.broker.name] = boundary
    return stack, adapter, boundary


def _buy(stack, qty=10, price=100):
    res = stack.submit_trade_proposal(mkprop(Market.KR, "005930", Side.BUY, qty), price)
    assert res.accepted, res.reason
    return res.order


def test_submit_is_idempotent_by_client_order_id():
    stack, adapter, boundary = _stack()
    order = _buy(stack)
    assert order.status is OrderStatus.SUBMITTED
    again = adapter.submit_order({}, order.order_id)
    assert again.reason == "idempotent_replay"
    assert len(adapter.orders) == 1


def test_timeout_leaves_order_unknown_not_terminal_and_keeps_reservation():
    stack, adapter, boundary = _stack()
    adapter.inject("submit_order", "timeout", accepted=True)
    order = _buy(stack)
    assert order.status is OrderStatus.SUBMITTED and not order.is_terminal
    assert order.order_id in boundary.unknown_orders
    assert order.reservation.cash_remaining > 0
    assert any(i["kind"] == "broker_unknown_order_state" for i in stack.ledger.integrity_issues)
    # Risk stops opening new exposure while an order state is unknown
    res = stack.submit_trade_proposal(mkprop(Market.KR, "005930", Side.BUY, 1), 100)
    assert not res.accepted and res.stage == "submission" and "broker_state_unverified" in res.reason


def test_unknown_status_stays_unknown_until_definitive_answer():
    stack, adapter, boundary = _stack()
    adapter.inject("submit_order", "timeout", accepted=False)
    order = _buy(stack)
    adapter.inject("order_status", "unknown_state")
    assert boundary.resolve_unknown(order.order_id) == "UNKNOWN"
    assert not order.is_terminal and order.order_id in boundary.unknown_orders
    # the broker never saw it; the same client_order_id retried is idempotent and clears UNKNOWN
    assert boundary.retry_submit(order.order_id) == "ACCEPTED"
    assert order.order_id not in boundary.unknown_orders and len(adapter.orders) == 1


def test_broker_reject_is_definitive_and_releases_reservation():
    stack, adapter, boundary = _stack()
    adapter.inject("submit_order", "reject", reason="no")
    res = stack.submit_trade_proposal(mkprop(Market.KR, "005930", Side.BUY, 10), 100)
    assert not res.accepted and res.stage == "submission"
    assert res.order.status is OrderStatus.REJECTED
    assert stack.ledger.reserved_cash and sum(stack.ledger.reserved_cash.values()) == 0


def test_connection_lost_during_submit_is_unknown():
    stack, adapter, boundary = _stack()
    adapter.inject("submit_order", "connection_lost")
    order = _buy(stack)
    assert order.order_id in boundary.unknown_orders and boundary.connected is False


def test_duplicate_fill_callback_applied_once():
    stack, adapter, boundary = _stack()
    order = _buy(stack, qty=10)
    adapter.report_fill(order.order_id, 4, 100, side="BUY", duplicate=True, broker_fill_id="F1")
    counts = boundary.process_fills()
    assert counts["applied"] == 1 and counts["duplicate"] == 1
    assert order.filled_quantity == 4 and order.status is OrderStatus.PARTIALLY_FILLED
    assert stack.ledger.positions[(Market.KR.value, "005930")].quantity == 4
    # an out-of-band re-delivery later is still ignored
    adapter.report_fill(order.order_id, 4, 100, side="BUY", broker_fill_id="F1")
    assert boundary.process_fills()["duplicate"] == 1
    assert stack.ledger.positions[(Market.KR.value, "005930")].quantity == 4


def test_partial_then_full_fill_completes_order():
    stack, adapter, boundary = _stack()
    order = _buy(stack, qty=10)
    adapter.report_fill(order.order_id, 4, 100, side="BUY")
    adapter.report_fill(order.order_id, 6, 101, side="BUY")
    boundary.process_fills()
    assert order.status is OrderStatus.FILLED and order.filled_quantity == 10


@pytest.mark.parametrize("qty,price,side", [(11, 100, "BUY"), (4, 100, "SELL"), (-1, 100, "BUY"), (4, float("nan"), "BUY"), (4, 0, "BUY")])
def test_inconsistent_fill_not_applied_and_flags_integrity(qty, price, side):
    stack, adapter, boundary = _stack()
    order = _buy(stack, qty=10)
    adapter.report_fill(order.order_id, qty, price, side=side)
    counts = boundary.process_fills()
    assert counts["inconsistent"] == 1 and counts["applied"] == 0
    assert order.filled_quantity == 0
    assert any(i["kind"] == "broker_inconsistent_fill" for i in stack.ledger.integrity_issues)
    assert boundary.needs_reconciliation


def test_fill_for_unknown_order_is_inconsistent():
    stack, adapter, boundary = _stack()
    order = _buy(stack)
    adapter.orders["ghost"] = {"broker_order_id": "B-x", "state": BrokerOrderState.ACCEPTED, "view": {}, "filled": 0.0}
    adapter.report_fill("ghost", 1, 100, side="BUY")
    assert boundary.process_fills()["inconsistent"] == 1
    assert order.filled_quantity == 0


def test_fill_after_cancel_is_inconsistent():
    stack, adapter, boundary = _stack()
    order = _buy(stack)
    stack.cancel_order(order.order_id)
    adapter.orders[order.order_id]["state"] = BrokerOrderState.CANCELLED
    adapter.report_fill(order.order_id, 1, 100, side="BUY")
    assert boundary.process_fills()["inconsistent"] == 1


def test_cancel_timeout_keeps_reservation_and_is_not_terminal():
    stack, adapter, boundary = _stack()
    order = _buy(stack)
    adapter.inject("cancel_order", "timeout")
    with pytest.raises(BrokerTimeout):
        stack.cancel_order(order.order_id)
    assert order.status is OrderStatus.CANCEL_PENDING and not order.is_terminal
    assert order.reservation.cash_remaining > 0
    assert order.order_id in boundary.unknown_orders


def test_cancel_success_releases_reservation():
    stack, adapter, boundary = _stack()
    order = _buy(stack)
    stack.cancel_order(order.order_id)
    assert order.status is OrderStatus.CANCELLED
    assert sum(stack.ledger.reserved_cash.values()) == 0


def test_reconnect_invalidates_reconciliation_until_fresh_snapshot_matches():
    stack, adapter, boundary = _stack(cash=1_000_000)
    from qat.core.models import Currency
    adapter._cash = {Currency.KRW: 1_000_000}
    recon, fresh = boundary.refresh_snapshot(max_age_seconds=30)
    assert recon.ok
    boundary.reconnect()
    assert stack.ledger.last_reconciliation["ok"] is False and boundary.needs_reconciliation
    recon, fresh = boundary.refresh_snapshot(max_age_seconds=30)
    assert recon.ok and not boundary.needs_reconciliation


def test_stale_snapshot_never_attests():
    stack, adapter, boundary = _stack(cash=1_000_000)
    from qat.core.models import Currency
    adapter._cash = {Currency.KRW: 1_000_000}
    adapter.snapshot_age_seconds = 3600
    recon, fresh = boundary.refresh_snapshot(max_age_seconds=30)
    assert not recon.ok and "snapshot_stale" in recon.reasons
    assert not (stack.ledger.last_reconciliation or {}).get("ok", False)


def test_unknown_age_snapshot_never_attests():
    stack, adapter, boundary = _stack(cash=1_000_000)
    from qat.core.models import Currency
    adapter._cash = {Currency.KRW: 1_000_000}
    adapter.snapshot_timestamp_override = None
    recon, fresh = boundary.refresh_snapshot(max_age_seconds=30)
    assert not recon.ok and fresh.status.value == "UNKNOWN"


def test_snapshot_fetch_failure_fails_closed():
    stack, adapter, boundary = _stack()
    adapter.connected = False
    recon, fresh = boundary.refresh_snapshot(max_age_seconds=30)
    assert recon is None and stack.ledger.last_reconciliation["ok"] is False and boundary.needs_reconciliation


def test_adapter_has_no_risk_or_policy_surface():
    forbidden = {"evaluate", "approve", "risk", "compliance", "set_kill_switch"}
    assert not forbidden & set(dir(FakeBrokerAdapter))
    assert not forbidden & {n for n in dir(BrokerBoundary) if not n.startswith("_")}
