"""Broker boundary: wraps a ``BrokerAdapter`` behind the broker interface the ExecutionRouter / GlobalOrderCoordinator already use
(``name``, ``orders``, ``submit_order(order)``, ``cancel_order(order_id)``).

The boundary is a TRANSLATOR and a WITNESS. It makes no Risk / Compliance / Market Integrity decision (those happened before an order could reach
it) and everything the adapter says is external evidence that is validated here and then checked again by Ledger reconciliation:

* Idempotency: the Order's id is the ``client_order_id``; a retried submit can never create a second broker order.
* UNKNOWN is never terminal: a timeout / lost connection during submit or an UNKNOWN status leaves the order SUBMITTED and flagged unknown, KEEPS the
  reservation, and records a settlement-integrity issue (Risk stops opening exposure; flatten refuses) until reconciliation proves the truth.
* Fills are deduplicated by ``broker_fill_id`` (callbacks are not exactly-once), validated (known order, side, finite positive qty/price, cumulative
  quantity <= order quantity) and only then settled through ``SettlementService.apply_fill``. An inconsistent fill is NOT applied and records an
  integrity issue.
* A reconnect (or a stale / unknown-age snapshot) never attests anything: it forces reconciliation to be re-established from a FRESH snapshot.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

from qat.brokerage.contract import (
    BrokerConnectionLost, BrokerFault, BrokerOrderState, BrokerRejected, BrokerTimeout, InconsistentFill,
)
from qat.core.exposure import reduces_exposure
from qat.core.models import Fill, Order, OrderStatus, Side, currency_for
from qat.ops.snapshot import reconcile_with_snapshot

_EPS = 1e-9


class BrokerBlocked(RuntimeError):
    """The boundary refuses to open new exposure while the broker state is unverified (the orchestrator releases the reservation)."""


def _utcnow():
    return datetime.now(timezone.utc)


def _order_view(order: Order) -> dict:
    return {"market": order.market.value, "symbol": order.symbol, "side": order.side.value, "quantity": order.quantity,
            "order_type": order.order_type.value, "limit_price": getattr(order, "limit_price", None)}


class BrokerBoundary:
    def __init__(self, adapter, settlement, ledger, *, name: str | None = None, now_fn=_utcnow, audit_fn=None) -> None:
        self.adapter = adapter
        self._settlement = settlement
        self._ledger = ledger
        self.name = name or getattr(adapter, "name", "BROKER")
        self._now = now_fn
        self._audit = audit_fn
        self.orders: dict[str, Order] = {}
        self.unknown_orders: dict[str, str] = {}  # order_id -> reason; never terminal
        self.seen_fill_ids: set[str] = set()
        self.duplicate_fill_count = 0
        self.inconsistent_fill_count = 0
        self.needs_reconciliation = False
        self.connected = True

    # ------------------------------------------------------------------ helpers
    def _log(self, event: str, **payload) -> None:
        if self._audit is not None:
            try:
                self._audit(event, **payload)
            except Exception:  # noqa: BLE001 - audit trouble must not corrupt order handling
                pass

    def _mark_unknown(self, order: Order, reason: str) -> None:
        if order.order_id not in self.unknown_orders:
            self.unknown_orders[order.order_id] = reason
            self._ledger.record_integrity_issue("broker_unknown_order_state", order_id=order.order_id, detail=reason)
        self.needs_reconciliation = True
        self._log("broker_order_unknown", order_id=order.order_id, reason=reason)

    # ------------------------------------------------------------------ submit / cancel
    def submit_order(self, order: Order) -> Order:
        if order.status is not OrderStatus.APPROVED:
            raise RuntimeError(f"BrokerBoundary.submit_order requires an APPROVED order, got {order.status.value}")
        if (self.unknown_orders or self.needs_reconciliation or not self.connected) and not reduces_exposure(self._ledger, order.proposal):
            # No new exposure while an order state is unknown, the connection was lost, or reconciliation is not re-established.
            raise BrokerBlocked("broker_state_unverified:" + ",".join(
                n for n, v in (("unknown_orders", self.unknown_orders), ("needs_reconciliation", self.needs_reconciliation),
                               ("disconnected", not self.connected)) if v))
        order.broker = self.name
        self.orders[order.order_id] = order
        try:
            ack = self.adapter.submit_order(_order_view(order), order.order_id)
        except BrokerRejected as exc:
            order.transition_to(OrderStatus.REJECTED)  # definitive: the broker said no -> orchestrator releases the reservation
            self._log("broker_submit_rejected", order_id=order.order_id, reason=str(exc))
            raise
        except (BrokerTimeout, BrokerConnectionLost) as exc:
            # The broker may or may not hold the order. Never assume either: SUBMITTED + UNKNOWN, reservation kept.
            order.transition_to(OrderStatus.SUBMITTED)
            self._mark_unknown(order, f"{exc.kind}_during_submit")
            if isinstance(exc, BrokerConnectionLost):
                self.connected = False
            return order
        except BrokerFault as exc:
            order.transition_to(OrderStatus.SUBMITTED)
            self._mark_unknown(order, f"{exc.kind}_during_submit")
            return order
        if ack.state is BrokerOrderState.REJECTED:
            order.transition_to(OrderStatus.REJECTED)
            raise BrokerRejected(ack.reason or "rejected")
        order.transition_to(OrderStatus.SUBMITTED)
        if ack.state is BrokerOrderState.UNKNOWN:
            self._mark_unknown(order, ack.reason or "unknown_state_in_ack")
        self._log("broker_submit_ack", order_id=order.order_id, broker_order_id=ack.broker_order_id, state=ack.state.value)
        return order

    def retry_submit(self, order_id: str) -> str:
        """Retry an UNKNOWN submission with the SAME client_order_id (idempotent). Returns the resulting state name."""

        order = self.orders[order_id]
        if order_id not in self.unknown_orders:
            return "not_unknown"
        try:
            ack = self.adapter.submit_order(_order_view(order), order.order_id)
        except BrokerRejected:
            self._finish_unknown(order, OrderStatus.REJECTED)
            return "REJECTED"
        except BrokerFault as exc:
            self.unknown_orders[order_id] = f"{exc.kind}_during_retry"
            return "UNKNOWN"
        return self._apply_status(order, ack.state, ack.reason)

    def resolve_unknown(self, order_id: str) -> str:
        """Ask the broker for the order's state. UNKNOWN stays UNKNOWN; only definitive answers change anything."""

        order = self.orders[order_id]
        try:
            ack = self.adapter.order_status(order.order_id)
        except BrokerFault as exc:
            self.unknown_orders[order_id] = f"{exc.kind}_during_status"
            return "UNKNOWN"
        return self._apply_status(order, ack.state, ack.reason)

    def _apply_status(self, order: Order, state: BrokerOrderState, reason: str) -> str:
        if state is BrokerOrderState.UNKNOWN:
            self.unknown_orders[order.order_id] = reason or "unknown"
            return "UNKNOWN"
        if state is BrokerOrderState.REJECTED:
            self._finish_unknown(order, OrderStatus.REJECTED)
            return "REJECTED"
        if state is BrokerOrderState.CANCELLED:
            if not order.is_terminal:
                self._cancel_local(order)
            self.unknown_orders.pop(order.order_id, None)
            return "CANCELLED"
        # ACCEPTED / PARTIALLY_FILLED / FILLED: the order exists at the broker; fills arrive through process_fills
        self.unknown_orders.pop(order.order_id, None)
        return state.value

    def _finish_unknown(self, order: Order, status: OrderStatus) -> None:
        if order.can_transition_to(status):
            order.transition_to(status)
        self._settlement.release(order)
        self.unknown_orders.pop(order.order_id, None)

    def _cancel_local(self, order: Order) -> None:
        if order.status is not OrderStatus.CANCEL_PENDING and order.can_transition_to(OrderStatus.CANCEL_PENDING):
            order.transition_to(OrderStatus.CANCEL_PENDING)
        if order.can_transition_to(OrderStatus.CANCELLED):
            order.transition_to(OrderStatus.CANCELLED)
        self._settlement.release(order)

    def cancel_order(self, order_id: str) -> Order:
        order = self.orders[order_id]
        if order.is_terminal:
            raise RuntimeError(f"cannot cancel order in terminal state {order.status.value}")
        if order.status is not OrderStatus.CANCEL_PENDING:
            order.transition_to(OrderStatus.CANCEL_PENDING)
        try:
            ack = self.adapter.cancel_order(order.order_id)
        except BrokerRejected:
            self._mark_unknown(order, "cancel_rejected_unknown_order")
            raise BrokerTimeout("cancel unconfirmed: broker does not know the order") from None
        except BrokerFault as exc:
            self._mark_unknown(order, f"{exc.kind}_during_cancel")
            raise  # still CANCEL_PENDING (not terminal); SettlementService.cancel therefore does NOT release the reservation
        if ack.state is BrokerOrderState.CANCELLED:
            if order.can_transition_to(OrderStatus.CANCELLED):
                order.transition_to(OrderStatus.CANCELLED)
            self.unknown_orders.pop(order.order_id, None)
        elif ack.state is BrokerOrderState.UNKNOWN:
            self._mark_unknown(order, ack.reason or "unknown_after_cancel")
            raise BrokerTimeout("cancel unconfirmed: unknown order state")
        # FILLED / other: the order is not cancelled; the caller must not release - surface it
        else:
            raise RuntimeError(f"cancel not effective: broker state {ack.state.value}")
        return order

    # ------------------------------------------------------------------ fills
    def process_fills(self) -> dict:
        """Pull fill reports, dedupe, validate, settle. Returns counts. Never raises on a bad report."""

        counts = {"applied": 0, "duplicate": 0, "inconsistent": 0, "blocked": 0}
        try:
            reports = self.adapter.fills()
        except BrokerFault as exc:
            self.connected = False if isinstance(exc, BrokerConnectionLost) else self.connected
            self.needs_reconciliation = True
            return {**counts, "fault": exc.kind}
        for report in reports:
            if report.broker_fill_id in self.seen_fill_ids:
                self.duplicate_fill_count += 1
                counts["duplicate"] += 1
                self._log("broker_fill_duplicate", broker_fill_id=report.broker_fill_id)
                continue
            try:
                order, fill = self._validate_fill(report)
            except InconsistentFill as exc:
                self.seen_fill_ids.add(report.broker_fill_id)  # a replay of a bad report is still bad, not "new"
                self.inconsistent_fill_count += 1
                counts["inconsistent"] += 1
                self._ledger.record_integrity_issue("broker_inconsistent_fill", broker_fill_id=report.broker_fill_id, detail=str(exc))
                self._log("broker_fill_inconsistent", broker_fill_id=report.broker_fill_id, reason=str(exc))
                self.needs_reconciliation = True
                continue
            self.seen_fill_ids.add(report.broker_fill_id)
            self._apply_valid_fill(order, fill, counts)
        return counts

    def _validate_fill(self, report):
        order = self.orders.get(report.client_order_id)
        if order is None:
            raise InconsistentFill("fill for an order this system never submitted")
        try:
            side = Side(report.side)
        except ValueError as exc:
            raise InconsistentFill(f"invalid side {report.side!r}") from exc
        if side is not order.side:
            raise InconsistentFill("fill side differs from order side")
        for label, value in (("quantity", report.quantity), ("price", report.price)):
            if not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise InconsistentFill(f"fill {label} must be a finite number > 0")
        for label in ("commission", "tax", "exchange_fee"):
            value = getattr(report, label)
            if not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise InconsistentFill(f"fill {label} must be a finite number >= 0")
        if report.quantity - order.remaining_quantity > _EPS:
            raise InconsistentFill("fill quantity exceeds the order's remaining quantity")
        if order.status in (OrderStatus.REJECTED, OrderStatus.CANCELLED, OrderStatus.ERROR, OrderStatus.EXPIRED, OrderStatus.FILLED):
            raise InconsistentFill(f"fill for an order already {order.status.value}")
        fill = Fill(order_id=order.order_id, market=order.market, symbol=order.symbol, side=side, quantity=float(report.quantity),
                    price=float(report.price), currency=currency_for(order.market, order.symbol), commission=float(report.commission),
                    tax=float(report.tax), exchange_fee=float(report.exchange_fee), broker_fill_id=report.broker_fill_id,
                    timestamp=report.timestamp, fill_id=f"BF:{report.broker_fill_id}")
        return order, fill

    def _apply_valid_fill(self, order: Order, fill: Fill, counts: dict) -> None:
        previous = (order.avg_fill_price or 0.0) * order.filled_quantity
        order.filled_quantity += fill.quantity
        order.avg_fill_price = (previous + fill.price * fill.quantity) / order.filled_quantity
        order.fills.append(fill)
        target = OrderStatus.FILLED if abs(order.filled_quantity - order.quantity) <= _EPS else OrderStatus.PARTIALLY_FILLED
        if order.can_transition_to(target):
            order.transition_to(target)
        self.unknown_orders.pop(order.order_id, None)  # a fill proves the order exists
        outcome = self._settlement.apply_fill(order, fill)
        key = "applied" if outcome.value == "APPLIED" else "blocked"
        counts[key] += 1

    # ------------------------------------------------------------------ snapshot / reconnect
    def refresh_snapshot(self, *, max_age_seconds: float, tol: float = 1e-6, future_tolerance_seconds: float = 5.0):
        """Reconcile the Ledger against a FRESH broker snapshot. A stale / unknown-age snapshot never attests."""

        try:
            snap = self.adapter.account_snapshot()
        except BrokerFault as exc:
            self.needs_reconciliation = True
            self._ledger.record_reconciliation(False, [f"snapshot_unavailable:{exc.kind}"])
            return None, None
        recon, fresh = reconcile_with_snapshot(
            self._ledger, broker_cash=snap.cash, broker_positions=snap.positions, snapshot_ts=snap.snapshot_ts, received_ts=snap.received_ts,
            now=self._now(), max_age_seconds=max_age_seconds, complete=snap.complete, tol=tol, future_tolerance_seconds=future_tolerance_seconds)
        if recon is not None and recon.ok and not self.unknown_orders:
            self.needs_reconciliation = False
        return recon, fresh

    def reconnect(self) -> dict:
        """Reconnect never restores trust by itself: reconciliation is invalidated until a fresh snapshot matches, unknown orders are re-queried."""

        info = self.adapter.reconnect()
        self.connected = True
        self.needs_reconciliation = True
        self._ledger.record_reconciliation(False, ["reconnect_requires_reconciliation"])
        resolved = {oid: self.resolve_unknown(oid) for oid in list(self.unknown_orders)}
        self._log("broker_reconnect", open_orders=info.get("open_orders", []), resolved=resolved)
        return {"reconnected": True, "resolved": resolved, "unknown_remaining": sorted(self.unknown_orders)}

    def status(self) -> dict:
        return {"adapter": self.name, "connected": self.connected, "unknown_orders": dict(self.unknown_orders),
                "needs_reconciliation": self.needs_reconciliation, "duplicate_fills_ignored": self.duplicate_fill_count,
                "inconsistent_fills": self.inconsistent_fill_count}
