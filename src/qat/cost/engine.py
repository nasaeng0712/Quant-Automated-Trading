"""Cost Engine + Expected Net Alpha gate (Core System Design v0.1, sections 18-19).

Net PnL, not gross (principle P4). Cost parameters are configuration, never
hard-coded business truth: the defaults below are *placeholder examples* and must
be calibrated per broker/market before any live use.
"""

from __future__ import annotations

from dataclasses import dataclass

from qat.core.exposure import reduces_exposure
from qat.core.gate import GateDecision, GateStatus
from qat.core.models import Market, Side, TradeProposal


@dataclass(frozen=True)
class CostModel:
    commission_rate: float = 0.0
    tax_rate_sell: float = 0.0
    half_spread_bps: float = 0.0
    slippage_bps: float = 0.0
    fx_cost_bps: float = 0.0


@dataclass(frozen=True)
class CostEstimate:
    commission: float
    tax: float
    spread: float
    slippage: float
    fx: float

    @property
    def total(self) -> float:
        return self.commission + self.tax + self.spread + self.slippage + self.fx


# PLACEHOLDER ONLY - not authoritative. Calibrate before live (P4, sections 12/18).
DEFAULT_COST_MODELS: dict[Market, CostModel] = {
    Market.KR: CostModel(commission_rate=0.00015, tax_rate_sell=0.0018, half_spread_bps=5, slippage_bps=2, fx_cost_bps=0),
    Market.US: CostModel(commission_rate=0.0005, tax_rate_sell=0.0, half_spread_bps=3, slippage_bps=2, fx_cost_bps=25),
    Market.CRYPTO: CostModel(commission_rate=0.0005, tax_rate_sell=0.0, half_spread_bps=8, slippage_bps=5, fx_cost_bps=0),
}

ZERO_COST_MODELS: dict[Market, CostModel] = {m: CostModel() for m in Market}


class CostEngine:
    """Per-market cost adapter (section 18)."""

    def __init__(self, models: dict[Market, CostModel] | None = None) -> None:
        self.models: dict[Market, CostModel] = dict(DEFAULT_COST_MODELS)
        if models:
            self.models.update(models)

    def model_for(self, market: Market) -> CostModel:
        return self.models.get(market, CostModel())

    def estimate(self, market: Market, side: Side, notional: float) -> CostEstimate:
        model = self.model_for(market)
        notional = abs(float(notional))
        return CostEstimate(
            commission=notional * model.commission_rate,
            tax=notional * model.tax_rate_sell if side is Side.SELL else 0.0,
            spread=notional * model.half_spread_bps / 1e4,
            slippage=notional * model.slippage_bps / 1e4,
            fx=notional * model.fx_cost_bps / 1e4,
        )


class NetAlphaGate:
    """Expected Net Alpha gate (section 19). ``NO TRADE`` when expected net edge
    does not clear the per-strategy threshold.
    """

    def __init__(self, cost_engine: CostEngine, min_net_alpha_bps: float = 0.0, ledger=None) -> None:
        self.cost_engine = cost_engine
        self.min_net_alpha = float(min_net_alpha_bps) / 1e4
        # Authoritative position source for the OD-01 exemption. Without a ledger
        # no proposal can be shown to reduce exposure, so nothing is exempt.
        self.ledger = ledger

    def reduces_exposure(self, proposal: TradeProposal) -> bool:
        """OD-01: delegates to the shared authoritative classification
        (``qat.core.exposure.reduces_exposure``) so Net Alpha and Risk agree."""

        return reduces_exposure(self.ledger, proposal)

    def evaluate(self, proposal: TradeProposal, reference_price: float | None) -> GateDecision:
        if reference_price is None or reference_price <= 0:
            return GateDecision(GateStatus.UNKNOWN, ("missing_reference_price",))
        notional = proposal.quantity * reference_price
        if notional <= 0:
            return GateDecision(GateStatus.UNKNOWN, ("non_positive_notional",))

        estimate = self.cost_engine.estimate(proposal.market, proposal.side, notional)
        cost_fraction = estimate.total / notional
        net = float(proposal.expected_gross_return) - cost_fraction
        if self.reduces_exposure(proposal):
            # Only the Net Alpha threshold is waived. Validation, execution cost,
            # Risk, Compliance, Integrity, Settlement and the Kill Switch all still run.
            return GateDecision(
                GateStatus.PASS,
                ("net_alpha_exempt:exposure_reducing",
                 f"expected_net_alpha={net:.6f}", f"cost_fraction={cost_fraction:.6f}"),
            )
        if net < self.min_net_alpha:
            return GateDecision(
                GateStatus.BLOCK,
                (
                    f"net_alpha_below_threshold:net={net:.6f}:min={self.min_net_alpha:.6f}"
                    f":cost_fraction={cost_fraction:.6f}",
                ),
            )
        return GateDecision(GateStatus.PASS, (f"expected_net_alpha={net:.6f}",))
