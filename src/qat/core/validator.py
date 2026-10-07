"""Proposal validator (Core System Design v0.1, sections 2 and 4).

First stage of the pipeline. Enforces the TradeProposal mandatory-field contract
and rejects structurally broken requests before any risk/compliance work runs.
Fail-closed: if a required field is missing the decision is BLOCK.
"""

from __future__ import annotations

from datetime import datetime, timezone

from qat.core.gate import GateDecision, GateStatus
from qat.core.models import OrderType, TradeProposal, is_finite_number


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ProposalValidator:
    def __init__(
        self,
        *,
        require_reason_code: bool = True,
        require_named_strategy: bool = True,
        max_future_skew_seconds: float = 5.0,
        now_fn=_utcnow,
    ) -> None:
        self.require_reason_code = require_reason_code
        self.require_named_strategy = require_named_strategy
        self.max_future_skew_seconds = max_future_skew_seconds
        self._now = now_fn

    def validate(self, proposal: TradeProposal, reference_price: float | None = None) -> GateDecision:
        reasons: list[str] = []

        if not proposal.symbol:
            reasons.append("missing_symbol")
        if proposal.quantity <= 0:
            reasons.append("non_positive_quantity")
        if self.require_named_strategy and (
            not proposal.strategy_id or proposal.strategy_id == "unassigned"
        ):
            reasons.append("missing_strategy_id")
        if self.require_reason_code and not proposal.reason_code:
            reasons.append("missing_reason_code")
        if not is_finite_number(proposal.quantity):
            reasons.append("non_finite_quantity")
        if not is_finite_number(proposal.expected_gross_return):
            reasons.append("non_finite_expected_gross_return")
        if not (0.0 <= proposal.confidence <= 1.0):
            reasons.append("confidence_out_of_range")
        if proposal.order_type is OrderType.LIMIT and (proposal.limit_price or 0) <= 0:
            reasons.append("limit_price_required_for_limit_order")
        if proposal.signal_timestamp is None:
            reasons.append("missing_signal_timestamp")
        else:
            skew = (proposal.signal_timestamp - self._now()).total_seconds()
            if skew > self.max_future_skew_seconds:
                reasons.append("signal_timestamp_in_future")

        if reasons:
            return GateDecision(GateStatus.BLOCK, tuple(reasons))
        if reference_price is not None and (
            not is_finite_number(reference_price) or reference_price <= 0
        ):
            return GateDecision(GateStatus.UNKNOWN, ("invalid_reference_price",))
        return GateDecision(GateStatus.PASS)
