"""Global Risk Engine v0.1 (Core System Design v0.1, sections 20-21; FIX-01).

Risk outranks strategy. Output is PASS / BLOCK / UNKNOWN; anything unclear about
safety fails closed (P2). Limit *numbers* are supplied by configuration and
default to "no limit".

Multi-currency (FIX-01): every portfolio-level check (base exposure, daily loss,
drawdown, exposure ratios) is evaluated in ``base_currency`` using an explicit
``FXRateProvider``. If a required rate is missing or invalid the gate returns
``UNKNOWN`` (fail closed) - it never guesses a rate. Per-currency liquidity
checks (cash for a BUY, shares for a SELL) need no conversion.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

from qat.core.exposure import reduces_exposure
from qat.core.fx import FXError, StaticFXRateProvider
from qat.core.gate import GateDecision, GateStatus
from qat.core.models import Currency, Market, Side, TradeProposal, currency_for
from qat.ops import market_rules
from qat.ops.snapshot import Freshness, evaluate_freshness

_EPS = 1e-6


class RiskGate:
    def __init__(
        self,
        ledger=None,
        *,
        base_currency: Currency | str = Currency.KRW,
        fx_provider=None,
        kill_switch: bool = False,
        max_order_notional: float | None = None,
        max_symbol_exposure: float | None = None,
        max_market_exposure: float | None = None,
        max_total_exposure: float | None = None,
        max_base_exposure: float | None = None,
        daily_loss_limit: float | None = None,
        max_drawdown: float | None = None,
        mark_prices_fn=None,
        require_reconciliation: bool = False,
        snapshot_policy: str = "paper_replay",
        max_snapshot_age_seconds: float | None = None,
        future_tolerance_seconds: float = 5.0,
        now_fn=None,
        market_context_fn=None,
        min_bar_volume: float | None = None,
        max_spread_bps: float | None = None,
        volatility_shock_range_pct: float | None = None,
    ) -> None:
        if snapshot_policy not in ("paper_replay", "broker_connected"):
            raise ValueError("snapshot_policy must be 'paper_replay' or 'broker_connected'")
        self.ledger = ledger
        self.base_currency = Currency(base_currency)
        self.fx_provider = fx_provider or StaticFXRateProvider({})
        self.kill_switch = kill_switch
        self.max_order_notional = max_order_notional
        self.max_symbol_exposure = max_symbol_exposure
        self.max_market_exposure = max_market_exposure
        self.max_total_exposure = max_total_exposure
        self.max_base_exposure = max_base_exposure
        self.daily_loss_limit = daily_loss_limit
        self.max_drawdown = max_drawdown
        # Batch #2.2: when True a reservation breach never lets an exit through until a
        # reconciliation has actually been recorded. Paper replay has no broker account to
        # reconcile against (False); any broker-connected mode must set True.
        self.require_reconciliation = require_reconciliation
        # optional callable -> {(market_value, symbol): price}; when set, equity and
        # exposure use point-in-time marks instead of avg_cost (Batch #2.0 B).
        self.mark_prices_fn = mark_prices_fn
        self._peak_equity: float | None = None
        self._day_start_realized_base: float = 0.0
        # Stale-snapshot policy: "paper_replay" is the EXPLICIT contract for modes without a broker account; "broker_connected" is
        # fail-closed on a STALE / UNKNOWN-age authoritative snapshot (it implies require_reconciliation).
        self.snapshot_policy = snapshot_policy
        self.max_snapshot_age_seconds = max_snapshot_age_seconds
        self.future_tolerance_seconds = future_tolerance_seconds
        self._now = now_fn or (lambda: datetime.now(timezone.utc))
        if snapshot_policy == "broker_connected":
            self.require_reconciliation = True
        # R-07 / R-08 / R-09 (thresholds None == NOT_CONFIGURED); the context comes from the server, never from the client
        self.market_context_fn = market_context_fn
        self.min_bar_volume = min_bar_volume
        self.max_spread_bps = max_spread_bps
        self.volatility_shock_range_pct = volatility_shock_range_pct

    # --- session / risk-state hooks (R-05, R-06) -------------------------
    def trip_kill_switch(self, on: bool = True) -> None:
        self.kill_switch = on

    def start_new_day(self) -> None:
        if self.ledger is None:
            return
        try:
            self._day_start_realized_base = self.ledger.realized_pnl_in(
                self.base_currency, self.fx_provider
            )
        except FXError:
            self._day_start_realized_base = 0.0

    def observe_equity(self, equity_base: float) -> None:
        self._peak_equity = (
            equity_base if self._peak_equity is None else max(self._peak_equity, equity_base)
        )

    # --- helpers -----------------------------------------------------------
    def _marks(self) -> dict | None:
        return self.mark_prices_fn() if self.mark_prices_fn is not None else None

    def _to_base(self, amount: float, currency: Currency) -> float:
        return amount * self.fx_provider.rate(currency, self.base_currency)

    def _breach_decision(self, reducing: bool, problems: list[str], count: int) -> GateDecision | None:
        """Reservation breach (design 5.3) vs exposure-reducing orders (Batch #2.2).

        A breach is a *financial* overrun. It blocks every risk-increasing order, but it
        alone does not trap an exposure-reducing one. The exit is allowed only when the
        books themselves are trustworthy; otherwise BLOCK/UNKNOWN is kept.
        Returns the blocking decision, or ``None`` when the exit may proceed.
        """

        ledger = self.ledger
        if not reducing:
            return GateDecision(GateStatus.BLOCK, (f"reservation_breach:{count}",))
        if any(b.get("kind") != "reservation_overrun" for b in ledger.reservation_breaches):
            return GateDecision(GateStatus.BLOCK, (f"reservation_breach:{count}:not_an_overrun",))
        if problems:
            return GateDecision(
                GateStatus.BLOCK,
                (f"reservation_breach:{count}:accounting_untrusted:{','.join(problems[:3])}",),
            )
        recon = getattr(ledger, "last_reconciliation", None)
        if recon is not None and not recon["ok"]:
            return GateDecision(GateStatus.BLOCK, (f"reservation_breach:{count}:reconciliation_mismatch",))
        if recon is None and self.require_reconciliation:
            return GateDecision(GateStatus.UNKNOWN, (f"reservation_breach:{count}:reconciliation_not_performed",))
        try:  # authoritative valuation must be possible (FX / finite)
            equity = ledger.equity_in(self.base_currency, self.fx_provider, self._marks())
        except FXError as exc:
            return GateDecision(GateStatus.UNKNOWN,
                                (f"reservation_breach:{count}:fx_unavailable:{type(exc).__name__}",))
        if not math.isfinite(equity):
            return GateDecision(GateStatus.BLOCK, (f"reservation_breach:{count}:accounting_untrusted:non_finite_equity",))
        return None

    def _snapshot_decision(self, reducing: bool, problems: list[str], notes: list[str]) -> GateDecision | None:
        """Broker-connected fail-closed freshness policy. STALE -> BLOCK, unknown age / no snapshot -> UNKNOWN for every risk-increasing order.
        An exposure-reducing order passes only on books that are internally consistent AND whose last FRESH reconciliation matched - the same
        accounting-integrity test the frozen exit hierarchy uses; otherwise reduction is blocked too."""

        if self.snapshot_policy != "broker_connected":
            return None
        ledger = self.ledger
        if ledger is None or self.max_snapshot_age_seconds is None:
            return GateDecision(GateStatus.UNKNOWN, ("snapshot_policy_misconfigured",))
        meta = getattr(ledger, "last_snapshot_meta", None)
        recon = getattr(ledger, "last_reconciliation", None)
        if meta is None:
            fr_status, fr_reason = Freshness.UNKNOWN, "no_snapshot"
        else:
            fr = evaluate_freshness(meta.get("snapshot_ts"), meta.get("received_ts"), self._now(), self.max_snapshot_age_seconds,
                                    future_tolerance_seconds=self.future_tolerance_seconds)
            fr_status, fr_reason = fr.status, fr.reason
        if fr_status is Freshness.FRESH:
            if recon is None:
                return GateDecision(GateStatus.UNKNOWN, ("reconciliation_not_performed",))
            if not recon["ok"]:
                return GateDecision(GateStatus.BLOCK, ("reconciliation_mismatch",))
            return None
        books_trusted = not problems and recon is not None and recon["ok"] and getattr(ledger, "last_snapshot_meta", None) is not None
        if reducing and books_trusted:
            notes.append(f"exit_allowed:snapshot_{fr_status.value.lower()}")
            return None
        status = GateStatus.BLOCK if fr_status is Freshness.STALE else GateStatus.UNKNOWN
        label = "stale_snapshot" if fr_status is Freshness.STALE else "snapshot_freshness_unknown"
        return GateDecision(status, (f"{label}:{fr_reason}" + ("" if not reducing else ":accounting_untrusted"),))

    def _market_rule_decision(self, proposal: TradeProposal, relax: bool, notes: list[str]) -> GateDecision | None:
        """R-07 / R-08 / R-09 on the server-supplied bar context. See ``qat.ops.market_rules`` for the exact semantics."""

        if self.min_bar_volume is None and self.max_spread_bps is None and self.volatility_shock_range_pct is None:
            return None
        ctx = self.market_context_fn(proposal) if self.market_context_fn is not None else None
        checks = (("R-07", market_rules.liquidity_shortage(ctx, self.min_bar_volume)),
                  ("R-08", market_rules.abnormal_spread(ctx, self.max_spread_bps)),
                  ("R-09", market_rules.volatility_shock(ctx, self.volatility_shock_range_pct)))
        for rule, (status, reason) in checks:
            if status == market_rules.PASS:
                continue
            if relax:
                notes.append(f"exit_allowed:{rule}:{reason}")
                continue
            return GateDecision(GateStatus.BLOCK if status == market_rules.BLOCK else GateStatus.UNKNOWN, (f"{rule}:{reason}",))
        return None

    # --- evaluation ------------------------------------------------------------
    def evaluate(self, proposal: TradeProposal, reference_price: float | None) -> GateDecision:
        # R-10 Kill Switch: top-level hard stop for EVERY order, exposure-reducing or not.
        # (It does not imply liquidation; an emergency flatten would be a separate design.)
        if self.kill_switch:
            return GateDecision(GateStatus.BLOCK, ("kill_switch",))

        ledger = self.ledger
        # Batch #2.2: authoritative, ledger-owned classification (shared with Net Alpha).
        # Nothing on the proposal can claim it. ``relax`` additionally requires that the
        # books are internally consistent - never relax a limit on untrustworthy accounting.
        reducing = reduces_exposure(ledger, proposal)
        problems = ledger.integrity_problems() if ledger is not None else []
        relax = reducing and not problems
        notes: list[str] = []

        # stale-snapshot policy (broker-connected mode only; paper replay uses the explicit paper_replay contract)
        snapshot = self._snapshot_decision(reducing, problems, notes)
        if snapshot is not None:
            return snapshot

        # FIX (V-04): a recorded reservation breach means a prior fill outran its
        # reservation. Risk-increasing orders are blocked until reconciliation clears it;
        # an exposure-reducing order is handled by ``_breach_decision``.
        if ledger is not None and getattr(ledger, "reservation_breaches", None):
            count = len(ledger.reservation_breaches)
            blocked = self._breach_decision(reducing, problems, count)
            if blocked is not None:
                return blocked
            notes.append(f"exit_allowed:reservation_overrun:{count}")
        if reference_price is None or reference_price <= 0:
            return GateDecision(GateStatus.UNKNOWN, ("missing_reference_price",))

        market_decision = self._market_rule_decision(proposal, relax, notes)
        if market_decision is not None:
            return market_decision

        notional = proposal.quantity * reference_price  # in the order's currency
        if self.max_order_notional is not None and notional > self.max_order_notional:
            return GateDecision(
                GateStatus.BLOCK,
                (f"max_order_notional:{notional:.2f}>{self.max_order_notional:.2f}",),
            )

        if ledger is None:
            return GateDecision(GateStatus.PASS)

        currency = proposal.currency

        try:
            portfolio_decision = self._portfolio_checks(proposal, reference_price, notional, currency,
                                                        relax, notes)
        except FXError as exc:
            return GateDecision(GateStatus.UNKNOWN, (f"fx_unavailable:{type(exc).__name__}",))
        if portfolio_decision is not None:
            return portfolio_decision

        # per-currency liquidity (no FX conversion needed)
        if proposal.side is Side.BUY:
            available = ledger.available_cash(currency)
            if notional - available > _EPS:
                return GateDecision(
                    GateStatus.BLOCK,
                    (f"insufficient_cash:need={notional:.2f}:have={available:.2f}",),
                )
        else:
            have = ledger.available_quantity(proposal.market, proposal.symbol)
            if proposal.quantity - have > 1e-9:
                return GateDecision(
                    GateStatus.BLOCK,
                    (f"insufficient_position:need={proposal.quantity}:have={have}",),
                )

        return GateDecision(GateStatus.PASS, tuple(notes))

    def _needs_portfolio_checks(self) -> bool:
        return any(
            cap is not None
            for cap in (
                self.max_base_exposure,
                self.daily_loss_limit,
                self.max_drawdown,
                self.max_symbol_exposure,
                self.max_market_exposure,
                self.max_total_exposure,
            )
        )

    def _portfolio_checks(
        self,
        proposal: TradeProposal,
        reference_price: float,
        notional: float,
        currency: Currency,
        relax: bool = False,
        notes: list[str] | None = None,
    ) -> GateDecision | None:
        """``relax`` = the order is exposure-reducing AND the books are consistent. It lifts
        only the three "limit already breached" blocks below (absolute exposure, daily
        loss, drawdown) - each is still *computed*, so missing FX or a missing drawdown
        baseline keeps its UNKNOWN meaning. It never touches order-size limits, the
        Kill Switch, liquidity/position checks or any other gate."""

        notes = notes if notes is not None else []
        if not self._needs_portfolio_checks():
            return None  # no portfolio-level limit configured -> no FX conversion needed

        ledger = self.ledger
        base = self.base_currency
        provider = self.fx_provider
        is_buy = proposal.side is Side.BUY

        # R-03/R-04 absolute base-currency exposure cap (FIX-01)
        if self.max_base_exposure is not None:
            incoming_base = self._to_base(notional, currency) if is_buy else 0.0
            exposure_base = ledger.exposure_in(base, provider, self._marks())
            if exposure_base + incoming_base > self.max_base_exposure:
                if relax:  # a reducing SELL can only lower exposure
                    notes.append("exit_allowed:max_base_exposure")
                else:
                    return GateDecision(
                        GateStatus.BLOCK,
                        (
                            f"max_base_exposure:{exposure_base + incoming_base:.2f}"
                            f">{self.max_base_exposure:.2f}",
                        ),
                    )

        # R-05 daily loss limit (base currency)
        if self.daily_loss_limit is not None:
            realized_base = ledger.realized_pnl_in(base, provider)
            loss = self._day_start_realized_base - realized_base
            if not math.isfinite(loss):  # NaN would compare False and pass silently
                return GateDecision(GateStatus.UNKNOWN, ("daily_loss_not_finite",))
            if loss >= self.daily_loss_limit:
                if relax:
                    notes.append("exit_allowed:daily_loss_limit")
                else:
                    return GateDecision(
                        GateStatus.BLOCK,
                        (f"daily_loss_limit:{loss:.2f}>={self.daily_loss_limit:.2f}",),
                    )

        # R-06 drawdown (base currency)
        # Audit fix (D4): a configured limit with no equity baseline used to be
        # skipped silently (PASS). Without a peak the drawdown is unknown -> UNKNOWN.
        if self.max_drawdown is not None and not (self._peak_equity and self._peak_equity > 0):
            return GateDecision(GateStatus.UNKNOWN, ("drawdown_baseline_unknown",))
        if self.max_drawdown is not None:
            equity_now = ledger.equity_in(base, provider, self._marks())
            drawdown = (self._peak_equity - equity_now) / self._peak_equity
            if not math.isfinite(drawdown):  # NaN would compare False and pass silently
                return GateDecision(GateStatus.UNKNOWN, ("drawdown_not_finite",))
            if drawdown >= self.max_drawdown:
                if relax:
                    notes.append("exit_allowed:max_drawdown")
                else:
                    return GateDecision(
                        GateStatus.BLOCK,
                        (f"max_drawdown:{drawdown:.4f}>={self.max_drawdown:.4f}",),
                    )

        # R-02 / R-03 / R-04 ratio-of-equity exposure caps (base currency)
        ratio_caps = (
            self.max_symbol_exposure,
            self.max_market_exposure,
            self.max_total_exposure,
        )
        if is_buy and any(cap is not None for cap in ratio_caps):
            equity = max(ledger.equity_in(base, provider, self._marks()), _EPS)
            incoming_base = self._to_base(notional, currency)

            if self.max_symbol_exposure is not None:
                position = ledger.get_position(proposal.market, proposal.symbol)
                symbol_base = (
                    self._to_base(abs(position.quantity * reference_price), currency)
                    + incoming_base
                )
                if symbol_base / equity > self.max_symbol_exposure:
                    return GateDecision(
                        GateStatus.BLOCK,
                        (f"max_symbol_exposure:{symbol_base / equity:.4f}"
                         f">{self.max_symbol_exposure:.4f}",),
                    )

            if self.max_market_exposure is not None:
                market_base = incoming_base
                for (market_key, symbol), position in ledger.positions.items():
                    if market_key != proposal.market.value:
                        continue
                    if symbol == proposal.symbol:
                        market_base += self._to_base(
                            abs(position.quantity * reference_price), currency
                        )
                    else:
                        pccy = currency_for(Market(market_key), symbol)
                        market_base += self._to_base(
                            abs(position.quantity * position.avg_cost), pccy
                        )
                if market_base / equity > self.max_market_exposure:
                    return GateDecision(
                        GateStatus.BLOCK,
                        (f"max_market_exposure:{market_base / equity:.4f}"
                         f">{self.max_market_exposure:.4f}",),
                    )

            if self.max_total_exposure is not None:
                total_base = incoming_base + ledger.exposure_in(base, provider, self._marks())
                if total_base / equity > self.max_total_exposure:
                    return GateDecision(
                        GateStatus.BLOCK,
                        (f"max_total_exposure:{total_base / equity:.4f}"
                         f">{self.max_total_exposure:.4f}",),
                    )

        return None
