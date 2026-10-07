"""Settlement service - reservation lifecycle + single fill-application path
(Core System Design v0.1, FIX-02 and FIX-04; Batch #2.0).

Reservation lifecycle
  APPROVED   -> reserve()      cash (BUY) or position (SELL) held on the ledger
  Fill       -> apply_fill()   post to ledger, then consume the matching slice
  FILLED     -> apply_fill()   release any residual reservation
  CANCELLED  -> release()      give back the remaining reservation
  REJECTED   -> (nothing reserved yet) / release()
  EXPIRED    -> release()
  ERROR      -> release() after reconciliation

Fill application outcomes
  APPLIED             posted to the ledger
  IGNORED_DUPLICATE   fill_id already processed - books untouched (INV-04)
  BLOCKED_TERMINAL    late fill on an already-FILLED order - books untouched
  BLOCKED_CANCELLED   late fill on a CANCELLED/REJECTED/EXPIRED/ERROR order
  REJECTED_MISMATCH   fill does not belong to the order (id/market/symbol/side/
                      currency) or would over-fill it - books untouched

Atomicity (Batch #2.0 E-4): every check runs before any mutation, the ledger
posting (itself validate-then-mutate) happens before the reservation is
consumed, so a failed posting never releases a reservation.
"""

from __future__ import annotations

from enum import Enum

from qat.core.models import (
    Fill,
    Order,
    OrderReservation,
    OrderStatus,
    OrderType,
    Side,
    TERMINAL_STATUSES,
)
from qat.portfolio.ledger import LedgerError, PortfolioLedger

_EPS = 1e-9
_BREACH_TOL = 1e-6

_BLOCKED_FOR_FILL = {
    OrderStatus.CANCELLED,
    OrderStatus.REJECTED,
    OrderStatus.EXPIRED,
    OrderStatus.ERROR,
}


class FillOutcome(str, Enum):
    APPLIED = "APPLIED"
    IGNORED_DUPLICATE = "IGNORED_DUPLICATE"
    BLOCKED_TERMINAL = "BLOCKED_TERMINAL"
    BLOCKED_CANCELLED = "BLOCKED_CANCELLED"
    REJECTED_MISMATCH = "REJECTED_MISMATCH"


def broker_rates(broker, market) -> dict:
    """Commission / exchange-fee / slippage rates the broker will charge for
    ``market``. Uses ``broker.rates_for`` when the broker has per-market cost
    models (settings-driven stacks), else its scalar attributes."""

    if broker is not None and hasattr(broker, "rates_for"):
        return broker.rates_for(market)
    return {
        "commission_rate": getattr(broker, "commission_rate", 0.0),
        "exchange_fee_rate": getattr(broker, "exchange_fee_rate", 0.0),
        "tax_rate_sell": getattr(broker, "tax_rate_sell", 0.0),
        "slippage_bps": getattr(broker, "slippage_bps", 0.0),
    }


class SettlementService:
    def __init__(
        self,
        ledger: PortfolioLedger,
        broker=None,
        *,
        execution_buffer_pct: float = 0.0,
        safety_buffer_pct: float = 0.0,
        audit=None,
    ) -> None:
        self.ledger = ledger
        self.broker = broker
        self.execution_buffer_pct = float(execution_buffer_pct)
        self.safety_buffer_pct = float(safety_buffer_pct)
        self.audit = audit
        self._applied_qty: dict[str, float] = {}

    def _log(self, stage: str, **payload) -> None:
        if self.audit is not None:
            self.audit.record(stage, **payload)

    # ------------------------------------------------------------- reserve
    def expected_buy_notional(self, order: Order, reference_price: float | None) -> float:
        if order.order_type is OrderType.LIMIT and order.limit_price:
            return order.quantity * order.limit_price
        if reference_price is None or reference_price <= 0:
            raise LedgerError("reference_price is required to reserve a MARKET buy")
        return order.quantity * reference_price * (1.0 + self.execution_buffer_pct)

    def reserve(self, order: Order, reference_price: float | None) -> OrderReservation:
        if order.side is Side.BUY:
            rates = broker_rates(self.broker, order.market)
            notional = self.expected_buy_notional(order, reference_price)
            # FIX (V-04): the broker charges commission / exchange fee on the
            # *slipped* gross, so the reservation must use the slipped notional as
            # the fee base - otherwise it under-covers by notional*slip*fee_rate
            # and a fill can drive cash_available negative. A LIMIT buy cannot
            # fill above its limit, so no slippage headroom is added there.
            fee_base = notional
            if order.order_type is not OrderType.LIMIT:
                fee_base = notional * (1.0 + rates["slippage_bps"] / 1e4)
            commission = fee_base * rates["commission_rate"]
            exchange_fee = fee_base * rates["exchange_fee_rate"]
            safety = fee_base * self.safety_buffer_pct
            amount = fee_base + commission + exchange_fee + safety
            self.ledger.reserve_cash(order.currency, amount)
            order.reservation = OrderReservation(
                kind="CASH", currency=order.currency, cash_reserved=amount
            )
        else:
            self.ledger.reserve_position(order.market, order.symbol, order.quantity)
            order.reservation = OrderReservation(
                kind="POSITION", quantity_reserved=order.quantity
            )
        return order.reservation

    def release(self, order: Order) -> None:
        reservation = order.reservation
        if reservation is None:
            return
        released = 0.0
        if reservation.kind == "CASH" and reservation.cash_remaining > _EPS:
            released = reservation.cash_remaining
            self.ledger.release_cash(reservation.currency, released)
            reservation.cash_released = reservation.cash_reserved
        elif reservation.kind == "POSITION" and reservation.quantity_remaining > _EPS:
            released = reservation.quantity_remaining
            self.ledger.release_position(order.market, order.symbol, released)
            reservation.quantity_released = reservation.quantity_reserved
        if released:
            self._log("reservation_released", order_id=order.order_id,
                      kind=reservation.kind, amount=released, status=order.status.value)

    # -------------------------------------------------------------- apply
    def _mismatch(self, order: Order, fill: Fill) -> str | None:
        if fill.order_id != order.order_id:
            return "order_id"
        if fill.market is not order.market:
            return "market"
        if fill.symbol != order.symbol:
            return "symbol"
        if fill.side is not order.side:
            return "side"
        if fill.currency is not order.currency:
            return "currency"
        applied = self._applied_qty.get(order.order_id, 0.0)
        if applied + fill.quantity - order.quantity > _EPS:
            return f"overfill:{applied + fill.quantity}>{order.quantity}"
        return None

    def apply_fill(self, order: Order, fill: Fill) -> FillOutcome:
        if fill is None:
            raise ValueError("no fill to apply")

        if self.ledger.has_applied(fill.fill_id):
            return self._outcome(order, fill, FillOutcome.IGNORED_DUPLICATE)

        known_fill = any(existing.fill_id == fill.fill_id for existing in order.fills)
        if order.status in TERMINAL_STATUSES and not known_fill:
            if order.status is OrderStatus.FILLED:
                return self._outcome(order, fill, FillOutcome.BLOCKED_TERMINAL)
            return self._outcome(order, fill, FillOutcome.BLOCKED_CANCELLED)
        if order.status in _BLOCKED_FOR_FILL:
            return self._outcome(order, fill, FillOutcome.BLOCKED_CANCELLED)

        mismatch = self._mismatch(order, fill)
        if mismatch is not None:
            # books untouched, but a fill that does not belong to its order is a settlement
            # integrity signal: the Risk gate will not relax limits while it is unresolved.
            self.ledger.record_integrity_issue("fill_order_mismatch", order_id=order.order_id,
                                               fill_id=fill.fill_id, detail=mismatch)
            return self._outcome(order, fill, FillOutcome.REJECTED_MISMATCH, mismatch=mismatch)

        # ledger first: it validates before mutating, so a LedgerError here
        # leaves both the books and the reservation untouched (E-4).
        self.ledger.apply_fill(fill)
        self._applied_qty[order.order_id] = self._applied_qty.get(order.order_id, 0.0) + fill.quantity
        allocated = self._consume_reservation(order, fill)

        # FIX (V-04) + Batch #2.0 C-1/C-2: the fill is real and stays on the
        # books. If its actual cost outran the reservation slice it consumed -
        # whether or not spare cash hides it - record a breach so the Risk gate
        # fails closed and reconciliation is required (design doc 5.3).
        if fill.side is Side.BUY and order.reservation is not None:
            available = self.ledger.available_cash(fill.currency)
            actual_cost = fill.gross + fill.total_cost
            overrun = actual_cost - allocated
            if overrun > _BREACH_TOL or available < -_EPS:
                entry = self.ledger.record_reservation_breach(
                    fill.currency,
                    shortfall=max(0.0, -available),
                    order_id=order.order_id,
                    fill_id=fill.fill_id,
                    actual_cost=actual_cost,
                    reservation_allocated=allocated,
                    overrun=max(0.0, overrun),
                    timestamp=fill.timestamp.isoformat(),
                )
                self._log("reservation_breach", **entry)

        return self._outcome(order, fill, FillOutcome.APPLIED)

    def _outcome(self, order: Order, fill: Fill, outcome: FillOutcome, **extra) -> FillOutcome:
        self._log(
            "fill_settled",
            outcome=outcome.value,
            order_id=order.order_id,
            fill_id=fill.fill_id,
            market=fill.market.value,
            symbol=fill.symbol,
            side=fill.side.value,
            quantity=fill.quantity,
            price=fill.price,
            currency=fill.currency.value,
            commission=fill.commission,
            tax=fill.tax,
            exchange_fee=fill.exchange_fee,
            fx_cost=fill.fx_cost,
            slippage_estimate=fill.slippage_estimate,
            realized_pnl_ccy=self.ledger.realized_pnl.get(fill.currency, 0.0),
            **extra,
        )
        return outcome

    def _consume_reservation(self, order: Order, fill: Fill) -> float:
        """Release the reservation slice this fill consumes. Returns the cash
        amount released (0.0 for a position reservation)."""

        reservation = order.reservation
        if reservation is None:
            return 0.0
        if reservation.kind == "CASH":
            if order.status is OrderStatus.FILLED:
                release = reservation.cash_remaining
            else:
                proportional = (
                    reservation.cash_reserved * (fill.quantity / order.quantity)
                    if order.quantity
                    else 0.0
                )
                release = min(proportional, reservation.cash_remaining)
            if release > _EPS:
                self.ledger.release_cash(reservation.currency, release)
                reservation.cash_released += release
            return release
        release_qty = min(fill.quantity, reservation.quantity_remaining)
        if order.status is OrderStatus.FILLED:
            release_qty = reservation.quantity_remaining
        if release_qty > _EPS:
            self.ledger.release_position(order.market, order.symbol, release_qty)
            reservation.quantity_released += release_qty
        return 0.0

    # -------------------------------------------------------------- cancel
    def cancel(self, order: Order, broker) -> Order:
        broker.cancel_order(order.order_id)
        self.release(order)
        return order
