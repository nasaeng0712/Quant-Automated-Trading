"""Execution Orchestrator - the single execution path (Core System Design v0.1,
sections 2, 25, 31, 32).

``submit_trade_proposal`` is the ONLY way a proposal becomes a submitted order.
The pipeline is fixed and linear:

    proposal
      -> ProposalValidator
      -> NetAlphaGate           (Expected Net Alpha, section 19)
      -> RiskGate               (section 20)
      -> ComplianceGate         (section 22)
      -> MarketIntegrityEngine  (section 24)
      -> Order APPROVED
      -> ExecutionRouter        (section 31; LIVE disabled in Phase 0)
      -> GlobalOrderCoordinator (section 25)
      -> Broker.submit_order

Anything that is not ``PASS`` stops the pipeline. There is no alternate method
that reaches a broker.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from qat.core.exposure import reduces_exposure
from qat.core.gate import GateDecision, GateStatus
from qat.core.models import Order, OrderStatus, TradeProposal
from qat.core.recovery import RecoveryAuthorization


@dataclass
class ExecutionResult:
    accepted: bool
    reason: str
    order_id: str | None = None
    order: Order | None = None
    stage: str | None = None


class ExecutionOrchestrator:
    def __init__(
        self,
        *,
        validator,
        net_alpha_gate,
        risk,
        compliance,
        integrity,
        coordinator,
        router,
        settlement,
        ledger=None,
        audit=None,
        now_fn=None,
        id_fn=None,
    ) -> None:
        self._validator = validator
        self._net_alpha_gate = net_alpha_gate
        self._risk = risk
        self._compliance = compliance
        self._integrity = integrity
        self._coordinator = coordinator
        self._router = router
        self._settlement = settlement
        self._ledger = ledger
        self._audit = audit
        self._now = now_fn
        self._id_fn = id_fn
        # Batch #2.0 G-2: one proposal at a time through gates + reservation so
        # concurrent callers (UI double-click, threads) cannot interleave the
        # check-then-reserve sequence.
        self._lock = threading.RLock()

    def _log(self, stage: str, **payload) -> None:
        if self._audit is not None:
            self._audit.record(stage, **payload)

    def submit_trade_proposal(
        self, proposal: TradeProposal, reference_price: float | None
    ) -> ExecutionResult:
        with self._lock:
            return self._submit(proposal, reference_price)

    def _new_order(self, proposal: TradeProposal) -> Order:
        kwargs = {}
        if self._id_fn is not None:
            kwargs["order_id"] = self._id_fn("O")
        if self._now is not None:
            kwargs["created_at"] = self._now()
        return Order(proposal=proposal, **kwargs)

    def _submit(self, proposal: TradeProposal, reference_price: float | None) -> ExecutionResult:
        self._log(
            "proposal_received",
            proposal_id=proposal.proposal_id,
            strategy_id=proposal.strategy_id,
            market=proposal.market.value,
            symbol=proposal.symbol,
            side=proposal.side.value,
            quantity=proposal.quantity,
            order_type=proposal.order_type.value,
            reference_price=reference_price,
        )

        stages = (
            ("validator", lambda: self._validator.validate(proposal, reference_price)),
            ("net_alpha", lambda: self._net_alpha_gate.evaluate(proposal, reference_price)),
            ("risk", lambda: self._risk.evaluate(proposal, reference_price)),
            ("compliance", lambda: self._compliance.evaluate(proposal)),
            ("integrity", lambda: self._integrity.evaluate(proposal, reference_price)),
        )

        for stage, run in stages:
            decision: GateDecision = run()
            self._log(
                f"{stage}_decision",
                status=decision.status.value,
                reasons=list(decision.reasons),
            )
            if decision.status is not GateStatus.PASS:
                return ExecutionResult(
                    accepted=False,
                    reason=f"{stage}:{decision.describe()}",
                    stage=stage,
                )

        return self._place_order(proposal, reference_price)

    def submit_recovery_order(self, proposal: TradeProposal, reference_price: float | None, authorization) -> ExecutionResult:
        """The ONLY alternative to the gated pipeline: an explicit, operator-authorized emergency-flatten order (never automatic).

        It does not bypass accounting integrity or the exposure rule: the order must be a strictly exposure-reducing order by the
        Ledger-authoritative classification, the books must be internally consistent, the proposal must validate and Compliance must pass.
        The Kill Switch (Risk) and the frequency/duplicate MI rules are not consulted - a flatten is the recovery operation FROM those states -
        and the Kill Switch is left engaged afterwards."""

        with self._lock:
            if not isinstance(authorization, RecoveryAuthorization) or not authorization.is_valid():
                raise PermissionError("emergency flatten needs a valid operator authorization issued by the recovery controller")
            self._log("recovery_order_received", proposal_id=proposal.proposal_id, recovery_action="emergency_flatten", operator=authorization.operator,
                      recovery_id=authorization.recovery_id, market=proposal.market.value, symbol=proposal.symbol, side=proposal.side.value,
                      quantity=proposal.quantity, reference_price=reference_price)

            def refuse(stage: str, reasons) -> ExecutionResult:
                self._log("recovery_order_refused", proposal_id=proposal.proposal_id, recovery_action="emergency_flatten", refused_at=stage, reasons=list(reasons))
                return ExecutionResult(accepted=False, reason=f"recovery_{stage}:{','.join(reasons)}", stage=f"recovery_{stage}")

            decision = self._validator.validate(proposal, reference_price)
            if decision.status is not GateStatus.PASS:
                return refuse("validator", decision.reasons)
            ledger = self._ledger
            problems = ledger.integrity_problems() if ledger is not None else ["no_ledger"]
            if problems:
                return refuse("accounting_untrusted", problems[:3])
            if not reduces_exposure(ledger, proposal):
                return refuse("not_exposure_reducing", ("flatten orders must strictly reduce an existing position",))
            decision = self._compliance.evaluate(proposal)
            if decision.status is not GateStatus.PASS:
                return refuse("compliance", decision.reasons)
            return self._place_order(proposal, reference_price)

    def _place_order(self, proposal: TradeProposal, reference_price: float | None) -> ExecutionResult:
        order = self._new_order(proposal)
        order.transition_to(OrderStatus.VALIDATED)
        order.transition_to(OrderStatus.APPROVED)

        # FIX-02: an APPROVED order reserves the cash / position it needs so a
        # concurrent order cannot double-spend the same balance.
        try:
            reservation = self._settlement.reserve(order, reference_price)
        except Exception as exc:  # noqa: BLE001 - reservation shortfall blocks the order
            order.transition_to(OrderStatus.REJECTED)
            self._log("reservation_failed", reasons=[repr(exc)])
            return ExecutionResult(
                accepted=False,
                reason=f"reservation:BLOCK:{exc}",
                order=order,
                stage="reservation",
            )
        self._log(
            "order_reserved",
            order_id=order.order_id,
            kind=reservation.kind,
            cash_reserved=reservation.cash_reserved,
            quantity_reserved=reservation.quantity_reserved,
        )

        try:
            broker = self._router.route(order, self._coordinator)
            self._coordinator.register(order)
            broker.submit_order(order)
        except Exception as exc:  # noqa: BLE001 - surface any submission failure as a blocked result
            self._settlement.release(order)
            if order.can_transition_to(OrderStatus.ERROR):
                order.transition_to(OrderStatus.ERROR)
            self._log("submission_failed", reasons=[repr(exc)])
            return ExecutionResult(
                accepted=False,
                reason=f"submission:ERROR:{exc}",
                order=order,
                stage="submission",
            )

        self._log(
            "order_submitted",
            order_id=order.order_id,
            broker=order.broker,
            status=order.status.value,
        )
        return ExecutionResult(
            accepted=True,
            reason="accepted",
            order_id=order.order_id,
            order=order,
            stage="submitted",
        )


class StrategyGateway:
    """The entire API surface a strategy is allowed to touch (section 32).

    It exposes ``submit_trade_proposal`` and nothing else - no broker handle, no
    ledger handle, no ``submit_order``.
    """

    __slots__ = ("_orchestrator",)

    def __init__(self, orchestrator: ExecutionOrchestrator) -> None:
        self._orchestrator = orchestrator

    def submit_trade_proposal(
        self, proposal: TradeProposal, reference_price: float | None
    ) -> ExecutionResult:
        return self._orchestrator.submit_trade_proposal(proposal, reference_price)
