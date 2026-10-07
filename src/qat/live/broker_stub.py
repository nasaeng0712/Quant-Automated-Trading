"""Live broker placeholder (Core System Design v0.1, sections 33 and 48).

Phase 0 does not implement live trading. This stub exists only so the interface
shape is visible. Every entry point raises - it must never execute an order.
"""

from __future__ import annotations


class LiveTradingDisabled(RuntimeError):
    """Raised on any attempt to use a live broker in Phase 0."""


_MESSAGE = "Live broker trading is not implemented in Phase 0 (see design doc section 48)"


class LiveBrokerStub:
    def __init__(self, *args, **kwargs) -> None:
        raise LiveTradingDisabled(_MESSAGE)

    def submit_order(self, *args, **kwargs):  # pragma: no cover - unreachable
        raise LiveTradingDisabled(_MESSAGE)

    def cancel_order(self, *args, **kwargs):  # pragma: no cover - unreachable
        raise LiveTradingDisabled(_MESSAGE)

    def get_account(self, *args, **kwargs):  # pragma: no cover - unreachable
        raise LiveTradingDisabled(_MESSAGE)
