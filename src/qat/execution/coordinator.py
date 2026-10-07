"""Global Order Coordinator (Core System Design v0.1, section 25).

Every broker order passes through here. A strategy decides "BUY AAPL"; it never
picks a venue. The coordinator/router chooses the broker and tracks open orders
across venues so conflicts (opposing / self-trade) can be detected.
"""

from __future__ import annotations

from qat.core.models import Order, Side, TradeProposal


class GlobalOrderCoordinator:
    def __init__(self, brokers: dict, default_broker: str | None = None) -> None:
        if not brokers:
            raise ValueError("at least one broker must be registered")
        self.brokers = dict(brokers)
        self.default_broker = default_broker or next(iter(self.brokers))
        if self.default_broker not in self.brokers:
            raise ValueError(f"default_broker {self.default_broker!r} is not registered")
        self.open_orders: dict[str, Order] = {}

    def select_broker(self, proposal: TradeProposal) -> str:
        # Phase 0: single-venue routing. Extension point for IBKR / KIS / Kiwoom /
        # Upbit / Bithumb (section 33) once multiple adapters exist.
        return self.default_broker

    def register(self, order: Order) -> None:
        self.open_orders[order.order_id] = order

    def refresh(self) -> None:
        for order_id in [oid for oid, order in self.open_orders.items() if order.is_terminal]:
            self.open_orders.pop(order_id, None)

    def open_orders_for(self, market, symbol: str) -> list[Order]:
        self.refresh()
        market_value = market.value if hasattr(market, "value") else market
        return [
            order
            for order in self.open_orders.values()
            if order.market.value == market_value and order.symbol == symbol
        ]

    def has_open_order(self, market, symbol: str, side: Side) -> bool:
        return any(order.side is side for order in self.open_orders_for(market, symbol))
