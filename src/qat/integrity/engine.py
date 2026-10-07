"""Market Integrity Engine (Core System Design v0.1, section 24).

A separate hard-gate layer from compliance. Phase 0 covers a subset:
  MI-01 duplicate order           - same intent repeated inside a short window
  MI-02 / MI-03 opposing / self-trade - an open opposite-side order on the symbol
  MI-04 excessive order frequency - too many orders inside the rolling window
  MI-07 price deviation           - LIMIT price too far from the reference
  MI-08 abnormal repetition       - too many same-direction orders on a symbol

  MI-05 cancel/replace pattern    - too many server-side cancels on a symbol inside the window (Batch: final completion)
  MI-06 liquidity participation   - order quantity above a share of the reference-bar volume (UNKNOWN when volume is unknown)

MI-05/MI-06 thresholds default to None (NOT_CONFIGURED, inert); see ``qat.ops.market_rules`` for the exact semantics.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime, timezone

from qat.core.gate import GateDecision, GateStatus
from qat.core.models import OrderType, Side, TradeProposal
from qat.ops import market_rules


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class MarketIntegrityEngine:
    def __init__(
        self,
        *,
        coordinator=None,
        window_seconds: float = 60.0,
        duplicate_window_seconds: float = 5.0,
        max_orders_per_window: int | None = None,
        max_same_direction_repeats: int | None = None,
        price_deviation_limit: float | None = None,
        now_fn=_utcnow,
        max_cancels_per_window: float | None = None,
        cancel_window_seconds: float = 300.0,
        max_participation_rate: float | None = None,
        market_context_fn=None,
    ) -> None:
        self.coordinator = coordinator
        self.window_seconds = window_seconds
        self.duplicate_window_seconds = duplicate_window_seconds
        self.max_orders_per_window = max_orders_per_window
        self.max_same_direction_repeats = max_same_direction_repeats
        self.price_deviation_limit = price_deviation_limit
        self._now = now_fn
        # each event: (timestamp, fingerprint, market_value, symbol, side_value)
        self._events: deque = deque()
        self.max_cancels_per_window = max_cancels_per_window
        self.cancel_window_seconds = cancel_window_seconds
        self.max_participation_rate = max_participation_rate
        self.market_context_fn = market_context_fn
        self._cancels: deque = deque()  # (epoch seconds, market_value, symbol) of server-side cancels (MI-05)

    def record_cancel(self, market, symbol: str) -> None:
        """Called by the server whenever it cancels an order; the client cannot influence this history."""

        market_value = market.value if hasattr(market, "value") else str(market)
        self._cancels.append((self._now().timestamp(), market_value, symbol))

    @staticmethod
    def _fingerprint(proposal: TradeProposal) -> tuple:
        return (
            proposal.strategy_id,
            proposal.market.value,
            proposal.symbol,
            proposal.side.value,
            round(proposal.quantity, 10),
            proposal.order_type.value,
        )

    def _prune(self, now: datetime) -> None:
        while self._events and (now - self._events[0][0]).total_seconds() > self.window_seconds:
            self._events.popleft()

    def evaluate(self, proposal: TradeProposal, reference_price: float | None = None) -> GateDecision:
        now = self._now()
        self._prune(now)
        fingerprint = self._fingerprint(proposal)

        # MI-01 duplicate order
        for timestamp, event_fp, *_ in self._events:
            if event_fp == fingerprint and (now - timestamp).total_seconds() <= self.duplicate_window_seconds:
                return GateDecision(GateStatus.BLOCK, ("duplicate_order",))

        # MI-02 / MI-03 opposing order / self-trade risk
        if self.coordinator is not None:
            opposite = Side.SELL if proposal.side is Side.BUY else Side.BUY
            if self.coordinator.has_open_order(proposal.market, proposal.symbol, opposite):
                return GateDecision(GateStatus.BLOCK, ("opposing_order",))

        # MI-04 excessive order frequency
        if self.max_orders_per_window is not None:
            recent = sum(
                1 for timestamp, *_ in self._events
                if (now - timestamp).total_seconds() <= self.window_seconds
            )
            if recent + 1 > self.max_orders_per_window:
                return GateDecision(
                    GateStatus.BLOCK,
                    (f"excessive_order_frequency:{recent + 1}>{self.max_orders_per_window}",),
                )

        # MI-08 abnormal repetition (same market/symbol/side)
        if self.max_same_direction_repeats is not None:
            same = sum(
                1 for _ts, _fp, market_value, symbol, side_value in self._events
                if market_value == proposal.market.value
                and symbol == proposal.symbol
                and side_value == proposal.side.value
            )
            if same + 1 > self.max_same_direction_repeats:
                return GateDecision(GateStatus.BLOCK, (f"abnormal_repetition:{same + 1}",))

        # MI-07 price deviation
        if (
            self.price_deviation_limit is not None
            and reference_price
            and reference_price > 0
            and proposal.order_type is OrderType.LIMIT
            and proposal.limit_price
        ):
            deviation = abs(proposal.limit_price - reference_price) / reference_price
            if deviation > self.price_deviation_limit:
                return GateDecision(
                    GateStatus.BLOCK,
                    (f"price_deviation:{deviation:.4f}>{self.price_deviation_limit:.4f}",),
                )

        # MI-05 cancel/replace pattern (server-side cancel history for this market/symbol)
        if self.max_cancels_per_window is not None:
            stamp = now.timestamp()
            while self._cancels and stamp - self._cancels[0][0] > self.cancel_window_seconds:
                self._cancels.popleft()
            times = [t for t, m, s in self._cancels if m == proposal.market.value and s == proposal.symbol]
            status, reason = market_rules.cancel_replace_pattern(times, stamp, self.cancel_window_seconds, self.max_cancels_per_window)
            if status == market_rules.BLOCK:
                return GateDecision(GateStatus.BLOCK, (f"MI-05:{reason}",))

        # MI-06 liquidity participation (server-supplied bar volume; unknown volume is UNKNOWN, never a pass)
        if self.max_participation_rate is not None:
            ctx = self.market_context_fn(proposal) if self.market_context_fn is not None else None
            status, reason = market_rules.liquidity_participation(ctx, proposal.quantity, self.max_participation_rate)
            if status != market_rules.PASS:
                return GateDecision(GateStatus.BLOCK if status == market_rules.BLOCK else GateStatus.UNKNOWN, (f"MI-06:{reason}",))

        self._events.append(
            (now, fingerprint, proposal.market.value, proposal.symbol, proposal.side.value)
        )
        return GateDecision(GateStatus.PASS)
