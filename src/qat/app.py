"""Wiring helper: assemble the full Phase 0 paper stack in one call.

This is the reference assembly of the fixed pipeline. Tests and any future
strategy runner build the stack here so there is exactly one wiring definition.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from qat.audit.log import AuditLog
from qat.compliance.gate import ComplianceGate
from qat.cost.engine import CostEngine, NetAlphaGate, ZERO_COST_MODELS
from qat.core.fx import StaticFXRateProvider
from qat.core.models import Currency, Fill
from qat.core.orchestrator import ExecutionOrchestrator, ExecutionResult, StrategyGateway
from qat.core.validator import ProposalValidator
from qat.execution.coordinator import GlobalOrderCoordinator
from qat.execution.router import ExecutionRouter
from qat.execution.settlement import FillOutcome, SettlementService
from qat.integrity.engine import MarketIntegrityEngine
from qat.paper.broker import PaperBroker
from qat.portfolio.ledger import PortfolioLedger
from qat.risk.gate import RiskGate


@dataclass
class PaperStack:
    gateway: StrategyGateway
    orchestrator: ExecutionOrchestrator
    ledger: PortfolioLedger
    broker: PaperBroker
    coordinator: GlobalOrderCoordinator
    router: ExecutionRouter
    settlement: SettlementService
    validator: ProposalValidator
    net_alpha_gate: NetAlphaGate
    risk: RiskGate
    compliance: ComplianceGate
    integrity: MarketIntegrityEngine
    audit: AuditLog
    fx_provider: object
    # non-fatal configuration notes (e.g. an ignored live_enabled toggle)
    config_warnings: list = field(default_factory=list)

    # convenience passthroughs for tests / runners (NOT part of the strategy surface)
    def submit_trade_proposal(self, proposal, reference_price) -> ExecutionResult:
        return self.gateway.submit_trade_proposal(proposal, reference_price)

    def settle(self, fill: Fill) -> FillOutcome:
        """Apply a fill through the settlement service: reservation decrement,
        terminal / duplicate guards, then ledger posting (FIX-02 / FIX-04)."""
        order = self._broker_for(fill.order_id).orders[fill.order_id]
        return self.settlement.apply_fill(order, fill)

    def _broker_for(self, order_id: str):
        """The registered broker (Paper broker or a broker boundary) that holds ``order_id``; the Paper broker when none does."""
        for candidate in self.coordinator.brokers.values():
            if order_id in candidate.orders:
                return candidate
        return self.broker

    def cancel_order(self, order_id: str):
        """Cancel via the broker and release the remaining reservation (FIX-02)."""
        broker = self._broker_for(order_id)
        order = broker.orders[order_id]
        result = self.settlement.cancel(order, broker)
        self.integrity.record_cancel(order.market, order.symbol)  # MI-05 history (server-side only)
        return result


def build_paper_stack(
    *,
    starting_cash=1_000_000,
    base_currency: Currency = Currency.KRW,
    fx_provider=None,
    mode: str = "PAPER",
    commission_rate: float = 0.0,
    tax_rate_sell: float = 0.0,
    slippage_bps: float = 0.0,
    exchange_fee_rate: float = 0.0,
    cost_models: dict | None = None,
    min_net_alpha_bps: float = 0.0,
    reservation_execution_buffer: float = 0.0,
    reservation_safety_buffer: float = 0.0,
    validator_kwargs: dict | None = None,
    risk_kwargs: dict | None = None,
    compliance_kwargs: dict | None = None,
    integrity_kwargs: dict | None = None,
    now_fn=None,
    broker_cost_models: dict | None = None,
    enabled_markets=None,
    id_fn=None,
    audit_sink=None,
) -> PaperStack:
    """Build a fully wired paper stack.

    By default cost models are ZERO so pipeline tests are not implicitly blocked
    by the Net Alpha gate, and the FX provider is empty (same-currency only), so
    a cross-currency portfolio check without a configured rate fails closed.

    ``broker_cost_models`` (per-market CostModel) makes the Paper Broker charge
    the same configured commission / tax / half-spread+slippage that the Gate
    estimates; ``id_fn(prefix)`` and ``now_fn`` make identifiers and timestamps
    deterministic for research runs.
    """

    audit = AuditLog(sink=audit_sink, now_fn=now_fn)
    ledger = PortfolioLedger(starting_cash, base_currency=base_currency)
    fx = fx_provider or StaticFXRateProvider({})

    broker_kwargs = dict(
        commission_rate=commission_rate,
        tax_rate_sell=tax_rate_sell,
        slippage_bps=slippage_bps,
        exchange_fee_rate=exchange_fee_rate,
    )
    if now_fn is not None:
        broker_kwargs["now_fn"] = now_fn
    broker = PaperBroker(**broker_kwargs, cost_models=broker_cost_models, id_fn=id_fn)

    coordinator = GlobalOrderCoordinator({broker.name: broker}, default_broker=broker.name)
    router = ExecutionRouter(mode=mode)
    settlement = SettlementService(
        ledger,
        broker,
        execution_buffer_pct=reservation_execution_buffer,
        safety_buffer_pct=reservation_safety_buffer,
        audit=audit,
    )

    cost_engine = CostEngine(models=cost_models if cost_models is not None else dict(ZERO_COST_MODELS))
    net_alpha_gate = NetAlphaGate(cost_engine, min_net_alpha_bps=min_net_alpha_bps, ledger=ledger)

    validator = ProposalValidator(**_with_now(validator_kwargs, now_fn))
    risk_final = dict(risk_kwargs or {})
    if now_fn is not None:
        risk_final.setdefault("now_fn", now_fn)
    risk = RiskGate(ledger, base_currency=base_currency, fx_provider=fx, **risk_final)

    compliance_final = dict(compliance_kwargs or {})
    compliance_final.setdefault("mode", mode)
    if enabled_markets is not None:
        compliance_final.setdefault("enabled_markets", enabled_markets)
    compliance = ComplianceGate(**_with_now(compliance_final, now_fn))

    integrity_final = dict(integrity_kwargs or {})
    integrity_final.setdefault("coordinator", coordinator)
    integrity = MarketIntegrityEngine(**_with_now(integrity_final, now_fn))

    orchestrator = ExecutionOrchestrator(
        validator=validator,
        net_alpha_gate=net_alpha_gate,
        risk=risk,
        compliance=compliance,
        integrity=integrity,
        coordinator=coordinator,
        router=router,
        settlement=settlement,
        ledger=ledger,
        audit=audit,
        now_fn=now_fn,
        id_fn=id_fn,
    )
    gateway = StrategyGateway(orchestrator)

    return PaperStack(
        gateway=gateway,
        orchestrator=orchestrator,
        ledger=ledger,
        broker=broker,
        coordinator=coordinator,
        router=router,
        settlement=settlement,
        validator=validator,
        net_alpha_gate=net_alpha_gate,
        risk=risk,
        compliance=compliance,
        integrity=integrity,
        audit=audit,
        fx_provider=fx,
    )


def _with_now(kwargs: dict | None, now_fn) -> dict:
    result = dict(kwargs or {})
    if now_fn is not None:
        result.setdefault("now_fn", now_fn)
    return result


def build_paper_stack_from_settings(settings_path=None, **overrides) -> PaperStack:
    """Build a paper stack whose cost / risk / net-alpha / FX values come from
    ``config/settings.yaml`` instead of code (design doc sections 12, 18, 57;
    FIX-01). Keyword ``overrides`` win over file values.

    Batch #2.0: the market cost models also drive the Paper Broker's actual
    charges and the reservation, ``project.mode`` selects the router/compliance
    mode (``live`` stays blocked by the router), ``markets`` flags disable
    markets in Compliance, and ``execution.live_enabled`` can never enable live
    execution (it is only reported as a warning).
    """

    from qat.config import (
        cost_models_from_settings,
        enabled_markets_from_settings,
        fx_provider_from_settings,
        load_settings,
    )

    settings = load_settings(settings_path)
    risk_cfg = {k: v for k, v in (settings.get("risk") or {}).items() if v is not None}
    net_alpha_cfg = settings.get("net_alpha") or {}
    cost_models = cost_models_from_settings(settings) or None
    mode = str((settings.get("project") or {}).get("mode", "paper")).upper()

    kwargs: dict = dict(
        base_currency=Currency(settings.get("base_currency", "KRW")),
        fx_provider=fx_provider_from_settings(settings),
        cost_models=cost_models,
        broker_cost_models=cost_models,
        min_net_alpha_bps=float(net_alpha_cfg.get("min_net_alpha_bps", 0.0)),
        risk_kwargs=risk_cfg or None,
        mode=mode,
        enabled_markets=enabled_markets_from_settings(settings),
    )
    kwargs.update(overrides)
    stack = build_paper_stack(**kwargs)
    if (settings.get("execution") or {}).get("live_enabled"):
        stack.config_warnings.append(
            "execution.live_enabled=true ignored: live execution is not available (D-013)"
        )
    return stack
