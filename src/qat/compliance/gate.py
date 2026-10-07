"""Compliance Engine (Core System Design v0.1, sections 22-23).

This is a software safety filter, NOT a legal-judgement AI and NOT proof of legal
compliance. Market-specific rules and broker API terms must be re-verified before
live deployment.

Fail-closed behaviour (P2 / T-018): in LIVE mode an ``UNKNOWN`` outcome is
coerced to ``BLOCK``. In PAPER/SHADOW mode it stays ``UNKNOWN`` (still not PASS,
so the orchestrator will not trade, but the distinction is visible for review).
"""

from __future__ import annotations

from datetime import datetime, timezone

from qat.core.gate import GateDecision, GateStatus
from qat.core.models import TradeProposal


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ComplianceGate:
    # OD-02 (Control Tower): without a tradable-universe allowlist nothing is allowed.
    # ``unrestricted_universe=True`` is an explicit, traceable opt-in; this class
    # default is False (fail-closed). Only unit-test scaffolding (tests/conftest.py)
    # overrides the default - every production entry point (UI, research, settings
    # builder) states its universe explicitly.
    DEFAULT_UNRESTRICTED_UNIVERSE = False

    def __init__(
        self,
        *,
        mode: str = "PAPER",
        halted_symbols=(),
        tradable_symbols=None,
        max_signal_age_seconds: float | None = None,
        enabled_markets=None,
        unrestricted_universe: bool | None = None,
        universe_label: str = "allowlist",
        now_fn=_utcnow,
    ) -> None:
        self.mode = mode.upper()
        # None == all markets enabled; otherwise a set of Market values from settings
        self.enabled_markets = (
            {getattr(m, "value", m) for m in enabled_markets} if enabled_markets is not None else None
        )
        self.halted_symbols = set(halted_symbols)
        self.tradable_symbols = set(tradable_symbols) if tradable_symbols is not None else None
        self.unrestricted_universe = (
            self.DEFAULT_UNRESTRICTED_UNIVERSE if unrestricted_universe is None else bool(unrestricted_universe)
        )
        self.universe_label = universe_label  # traceability: which universe admitted a symbol
        self.max_signal_age_seconds = max_signal_age_seconds
        self._now = now_fn
        self._seen_proposals: set[str] = set()

    def _finalize(self, status: GateStatus, reasons: tuple[str, ...]) -> GateDecision:
        if status is GateStatus.UNKNOWN and self.mode == "LIVE":
            return GateDecision(GateStatus.BLOCK, reasons + ("unknown_blocked_in_live",))
        return GateDecision(status, reasons)

    def evaluate(self, proposal: TradeProposal) -> GateDecision:
        if proposal.proposal_id in self._seen_proposals:
            return GateDecision(GateStatus.BLOCK, ("duplicate_proposal",))
        self._seen_proposals.add(proposal.proposal_id)

        if self.enabled_markets is not None and proposal.market.value not in self.enabled_markets:
            return GateDecision(GateStatus.BLOCK, (f"market_disabled:{proposal.market.value}",))

        if proposal.symbol in self.halted_symbols:
            return GateDecision(GateStatus.BLOCK, ("trading_halt",))

        if (
            self.max_signal_age_seconds is not None
            and proposal.signal_timestamp is not None
        ):
            age = (self._now() - proposal.signal_timestamp).total_seconds()
            if age > self.max_signal_age_seconds:
                return self._finalize(GateStatus.BLOCK, (f"stale_signal:{age:.1f}s",))

        reasons: list[str] = []
        status = GateStatus.PASS

        if self.tradable_symbols is not None:
            if proposal.symbol not in self.tradable_symbols:
                status = GateStatus.UNKNOWN
                reasons.append("symbol_not_verified")
            else:
                reasons.append(f"universe:{self.universe_label}")
        elif self.unrestricted_universe:
            reasons.append("universe:unrestricted_explicit")
        else:
            # No allowlist and no explicit opt-in: never a silent PASS.
            status = GateStatus.UNKNOWN
            reasons.append("tradable_universe_not_configured")

        return self._finalize(status, tuple(reasons))
