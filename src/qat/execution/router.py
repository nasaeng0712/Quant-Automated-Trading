"""Execution Router (Core System Design v0.1, sections 31-32).

Decides *where* an approved order runs. Modes: PAPER, SHADOW, LIVE.
LIVE is hard-disabled in Phase 0 (section 48) - routing in LIVE mode raises.
"""

from __future__ import annotations

from qat.core.models import Order


class LiveExecutionDisabled(RuntimeError):
    """Raised whenever something tries to route an order to live execution."""


class ExecutionRouter:
    VALID_MODES = ("PAPER", "SHADOW", "LIVE")

    def __init__(self, mode: str = "PAPER") -> None:
        normalized = mode.upper()
        if normalized not in self.VALID_MODES:
            raise ValueError(f"invalid execution mode: {mode!r}")
        self.mode = normalized

    def route(self, order: Order, coordinator):
        if self.mode == "LIVE":
            raise LiveExecutionDisabled("Live execution is not available in Phase 0")
        broker_name = coordinator.select_broker(order.proposal)
        broker = coordinator.brokers.get(broker_name)
        if broker is None:
            raise RuntimeError(f"broker not available: {broker_name!r}")
        return broker
