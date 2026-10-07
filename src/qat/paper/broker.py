"""Paper Broker - Level 1 "Simple" (Core System Design v0.1, sections 13-17).

Goal: execute virtual orders under conditions roughly similar to a real broker.
It does NOT claim to reproduce real fills. Simplifications (section 13/14),
also documented in ``docs/QAT_통합_설계_운영.md``:

  * price-point fill against a caller-supplied reference price; no order book,
    no queue position, no market depth
  * fixed basis-point slippage; no latency model, no variable/liquidity slippage
  * fills are triggered explicitly by ``simulate_fill``; the broker does not
    advance time or decide partial fills on its own
  * ``cancel_order`` always succeeds immediately - no cancel/fill race in paper
  * commission / tax / exchange-fee are flat rates on notional
  * with per-market ``cost_models`` (settings-driven stacks, Batch #2.0) a
    MARKET order's price moves by ``half_spread_bps + slippage_bps`` (it crosses
    half the spread and slips); LIMIT orders are clamped to the limit as before.
    A market without a configured cost model is refused (no silent zero cost).

Determinism (acceptance criterion 8): with a fixed ``now_fn`` and fixed slippage,
identical inputs produce identical fills.

The broker does not touch the ledger. It returns :class:`Fill` objects; the
caller applies them via ``PortfolioLedger.apply_fill`` (which is idempotent).
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from qat.core.models import (
    Fill,
    Market,
    is_finite_number,
    InvalidStateTransition,
    Order,
    OrderStatus,
    OrderType,
    Side,
    currency_for,
)

_EPS = 1e-9


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class BrokerError(RuntimeError):
    """Raised on an illegal paper-broker operation."""


class PaperBroker:
    def __init__(
        self,
        *,
        commission_rate: float = 0.0,
        tax_rate_sell: float = 0.0,
        slippage_bps: float = 0.0,
        exchange_fee_rate: float = 0.0,
        name: str = "PAPER",
        now_fn=_utcnow,
        cost_models: dict | None = None,
        id_fn=None,
    ) -> None:
        self.commission_rate = float(commission_rate)
        self.tax_rate_sell = float(tax_rate_sell)
        self.slippage_bps = float(slippage_bps)
        self.exchange_fee_rate = float(exchange_fee_rate)
        self.name = name
        self._now = now_fn
        self._id_fn = id_fn
        # per-market CostModel (qat.cost.engine) from settings; None -> scalar rates
        self.cost_models = dict(cost_models) if cost_models is not None else None
        self.orders: dict[str, Order] = {}

    def rates_for(self, market) -> dict:
        """Rates charged for ``market``. Settings-driven brokers read the market's
        CostModel; a market missing from it raises (fail closed)."""

        if self.cost_models is None:
            return {
                "commission_rate": self.commission_rate,
                "exchange_fee_rate": self.exchange_fee_rate,
                "tax_rate_sell": self.tax_rate_sell,
                "slippage_bps": self.slippage_bps,
            }
        model = self.cost_models.get(Market(market))
        if model is None:
            raise BrokerError(f"no cost model configured for market {Market(market).value}")
        return {
            "commission_rate": model.commission_rate,
            "exchange_fee_rate": self.exchange_fee_rate,
            "tax_rate_sell": model.tax_rate_sell,
            "slippage_bps": model.half_spread_bps + model.slippage_bps,
        }

    def _new_id(self, prefix: str) -> str:
        return self._id_fn(prefix) if self._id_fn is not None else str(uuid4())

    def submit_order(self, order: Order) -> Order:
        # No bypass: an order only reaches the broker after the orchestrator has
        # driven it CREATED -> VALIDATED -> APPROVED.
        if order.status is not OrderStatus.APPROVED:
            raise InvalidStateTransition(
                f"PaperBroker.submit_order requires an APPROVED order, got {order.status.value}"
            )
        self.rates_for(order.market)  # unknown market cost model -> refuse before SUBMITTED
        order.transition_to(OrderStatus.SUBMITTED)
        order.broker = self.name
        self.orders[order.order_id] = order
        return order

    def cancel_order(self, order_id: str) -> Order:
        order = self.orders[order_id]
        if order.is_terminal:
            raise BrokerError(f"cannot cancel order in terminal state {order.status.value}")
        if order.status is not OrderStatus.CANCEL_PENDING:
            order.transition_to(OrderStatus.CANCEL_PENDING)
        order.transition_to(OrderStatus.CANCELLED)
        return order

    def simulate_fill(
        self,
        order_id: str,
        reference_price: float,
        quantity: float | None = None,
        broker_fill_id: str | None = None,
    ) -> Fill | None:
        """Attempt a fill. Returns the :class:`Fill`, or ``None`` when a LIMIT
        order's price condition is not met (the order stays SUBMITTED / OPEN -
        that is not a rejection, it just has not traded yet, FIX-05).
        """

        order = self.orders[order_id]

        # INV-01 (FILLED cannot refill) / INV-02 (CANCELLED cannot fill)
        if order.status in (
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED,
            OrderStatus.EXPIRED,
            OrderStatus.ERROR,
        ):
            raise BrokerError(f"order {order_id} is not fillable in state {order.status.value}")
        if not is_finite_number(reference_price) or reference_price <= 0:
            raise BrokerError("reference_price must be a finite number > 0")

        remaining = order.remaining_quantity
        qty = remaining if quantity is None else float(quantity)
        if not is_finite_number(qty) or qty <= 0 or qty - remaining > _EPS:  # INV-03
            raise BrokerError(f"invalid fill quantity {qty} (remaining {remaining})")

        # FIX-05: LIMIT price gating against the reference price.
        if order.order_type is OrderType.LIMIT:
            limit = order.limit_price
            if order.side is Side.BUY and reference_price > limit:
                return None
            if order.side is Side.SELL and reference_price < limit:
                return None

        rates = self.rates_for(order.market)
        slip = rates["slippage_bps"] / 1e4
        if order.side is Side.BUY:
            price = reference_price * (1.0 + slip)
        else:
            price = reference_price * (1.0 - slip)

        # FIX-05: a LIMIT order never fills worse than its limit, even with slippage.
        if order.order_type is OrderType.LIMIT:
            if order.side is Side.BUY:
                price = min(price, order.limit_price)
            else:
                price = max(price, order.limit_price)

        gross = qty * price
        commission = gross * rates["commission_rate"]
        exchange_fee = gross * rates["exchange_fee_rate"]
        tax = gross * rates["tax_rate_sell"] if order.side is Side.SELL else 0.0
        slippage_estimate = abs(price - reference_price) * qty

        fill = Fill(
            order_id=order.order_id,
            market=order.market,
            symbol=order.symbol,
            side=order.side,
            quantity=qty,
            price=price,
            currency=currency_for(order.market, order.symbol),
            commission=commission,
            tax=tax,
            exchange_fee=exchange_fee,
            fx_cost=0.0,
            slippage_estimate=slippage_estimate,
            broker_fill_id=broker_fill_id or self._new_id("BF"),
            timestamp=self._now(),
            fill_id=self._new_id("F"),
        )

        previous_notional = (order.avg_fill_price or 0.0) * order.filled_quantity
        order.filled_quantity += qty
        order.avg_fill_price = (previous_notional + price * qty) / order.filled_quantity
        order.fills.append(fill)

        if abs(order.filled_quantity - order.quantity) <= _EPS:
            order.transition_to(OrderStatus.FILLED)
        else:
            order.transition_to(OrderStatus.PARTIALLY_FILLED)
        return fill
