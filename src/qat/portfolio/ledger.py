"""Central Portfolio Ledger (Core System Design v0.1, sections 9-12; FIX-01/03).

The single internal accounting reference. It does not trust a broker account;
the reconciliation engine compares this ledger against broker state separately.

Accounting conventions (fixed by FIX-03):
  * BUY average cost basis INCLUDES acquisition costs:
        acquisition_cost = quantity*price + commission + tax + exchange_fee + fx_cost
    so the per-share ``avg_cost`` already carries the buy-side transaction costs.
  * SELL realized PnL subtracts ONLY the disposal (sell-side) costs:
        realized = proceeds - cost_basis_sold - sell_commission - sell_tax
                   - sell_exchange_fee - sell_fx_cost
    The buy-side costs are never subtracted again (no double counting).
  * Slippage is already inside ``fill.price``. ``slippage_estimate`` is recorded
    for analysis only and is never charged as a second cash expense.

Multi-currency (FIX-01): cash, realized PnL, fees, taxes and FX cost are tracked
per currency. Base-currency figures are produced only via the ``*_in`` methods,
which take an explicit ``FXRateProvider`` and raise (fail closed) on a missing
rate rather than assuming one.

Reservations (FIX-02): ``reserve_* / release_*`` and the ``*_available`` views are
owned by :class:`qat.execution.settlement.SettlementService`. ``apply_fill`` only
moves *total* cash / position; it never touches the reserved amounts.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field

from qat.core.fx import FXError, InvalidFXRateError, MissingFXRateError
from qat.core.models import Currency, Fill, Market, Position, Side, currency_for

_EPS = 1e-9


class LedgerError(RuntimeError):
    """Raised on an accounting rule violation (e.g. INV-05 short sale)."""


def _to_currency(value) -> Currency:
    return value if isinstance(value, Currency) else Currency(value)


@dataclass
class Valuation:
    """Strict mark-to-market snapshot (Batch #2.0).

    Per-currency figures are always produced. ``total_base`` is ``None`` when any
    held position lacks a mark or any non-base currency lacks a valid FX rate -
    a total is never synthesised from a fallback price or an assumed rate.
    ``missing_marks`` (price not available) and ``missing_fx`` (rate not
    available) are reported separately.
    """

    base_currency: Currency
    by_currency: dict = field(default_factory=dict)
    positions: list = field(default_factory=list)
    missing_marks: list = field(default_factory=list)
    missing_fx: list = field(default_factory=list)
    total_base: float | None = None

    @property
    def complete(self) -> bool:
        return not self.missing_marks and not self.missing_fx


class PortfolioLedger:
    def __init__(self, starting_cash, base_currency: Currency | str = Currency.KRW) -> None:
        self.base_currency = _to_currency(base_currency)
        self.cash: dict[Currency, float] = defaultdict(float)
        if isinstance(starting_cash, (int, float)):
            self.cash[self.base_currency] = float(starting_cash)
        else:
            for key, amount in dict(starting_cash).items():
                self.cash[_to_currency(key)] += float(amount)

        self.reserved_cash: dict[Currency, float] = defaultdict(float)
        self.positions: dict[tuple[str, str], Position] = {}
        self.realized_pnl: dict[Currency, float] = defaultdict(float)
        self.fees: dict[Currency, float] = defaultdict(float)
        self.taxes: dict[Currency, float] = defaultdict(float)
        self.fx_costs: dict[Currency, float] = defaultdict(float)
        self.processed_fill_ids: set[str] = set()
        # FIX (V-04): a real fill whose cost exceeded its reservation and drove
        # available cash negative. The fill is kept (it is external reality); this
        # list is the fail-closed signal for the Risk gate / reconciliation.
        self.reservation_breaches: list[dict] = []
        # breaches cleared only after a clean reconciliation + named approver
        self.resolved_breaches: list[dict] = []
        # Batch #2.2: accounting-integrity signals the Risk gate reads before it relaxes
        # any limit for an exposure-reducing order.
        self.integrity_issues: list[dict] = []
        self.resolved_integrity_issues: list[dict] = []
        # latest reconciliation outcome, written by qat.portfolio.reconciliation.reconcile()
        self.last_reconciliation: dict | None = None
        # source/received timestamps of the latest broker snapshot (the Risk gate recomputes its AGE at decision time)
        self.last_snapshot_meta: dict | None = None

    # ------------------------------------------------------------------ keys
    @staticmethod
    def _key(market, symbol: str) -> tuple[str, str]:
        market_value = market.value if isinstance(market, Market) else str(market)
        return (market_value, symbol)

    def get_position(self, market, symbol: str) -> Position:
        return self.positions.get(self._key(market, symbol), Position())

    # ------------------------------------------------------------ availability
    def available_cash(self, currency) -> float:
        ccy = _to_currency(currency)
        return self.cash[ccy] - self.reserved_cash[ccy]

    def available_quantity(self, market, symbol: str) -> float:
        return self.get_position(market, symbol).available_quantity

    # ----------------------------------------------------------- reservations
    def reserve_cash(self, currency, amount: float) -> None:
        ccy = _to_currency(currency)
        amount = float(amount)
        if amount < 0:
            raise LedgerError("reservation amount must be >= 0")
        if amount - self.available_cash(ccy) > _EPS:
            raise LedgerError(
                f"insufficient available cash to reserve {amount:.2f} {ccy.value} "
                f"(available {self.available_cash(ccy):.2f})"
            )
        self.reserved_cash[ccy] += amount

    def release_cash(self, currency, amount: float) -> None:
        ccy = _to_currency(currency)
        self.reserved_cash[ccy] = max(0.0, self.reserved_cash[ccy] - float(amount))

    def reserve_position(self, market, symbol: str, quantity: float) -> None:
        position = self.positions.setdefault(self._key(market, symbol), Position())
        if float(quantity) - position.available_quantity > _EPS:
            raise LedgerError(
                f"insufficient available position to reserve {quantity} "
                f"(available {position.available_quantity})"
            )
        position.reserved_quantity += float(quantity)

    def release_position(self, market, symbol: str, quantity: float) -> None:
        position = self.positions.get(self._key(market, symbol))
        if position is not None:
            position.reserved_quantity = max(0.0, position.reserved_quantity - float(quantity))

    # ------------------------------------------------------------------ fills
    def has_applied(self, fill_id: str) -> bool:
        return fill_id in self.processed_fill_ids

    def record_reservation_breach(self, currency, shortfall: float, **detail) -> dict:
        # kind: a financial overrun (actual cost > reservation). Anything else a caller
        # might record is NOT treated as a plain overrun by the Risk gate.
        entry = {"kind": "reservation_overrun", "currency": _to_currency(currency).value,
                 "shortfall": float(shortfall), **detail}
        self.reservation_breaches.append(entry)
        return entry

    def resolve_reservation_breaches(self, recon_result, *, approver: str, note: str) -> list[dict]:
        """Clear outstanding breaches. Requires a *clean* reconciliation result and
        a named approver; cleared entries are kept in ``resolved_breaches``.
        This is the only way to lift the V-04 Risk block - no UI path calls it.
        """

        if not approver or not str(approver).strip():
            raise LedgerError("breach resolution requires a named approver")
        if recon_result is None or not getattr(recon_result, "ok", False):
            raise LedgerError("breach resolution requires a clean reconciliation result")
        cleared = [
            {**entry, "approver": str(approver), "note": str(note),
             "recon_reasons": list(getattr(recon_result, "reasons", []))}
            for entry in self.reservation_breaches
        ]
        self.resolved_breaches.extend(cleared)
        self.reservation_breaches.clear()
        return cleared

    # ------------------------------------------------ accounting integrity (Batch #2.2)
    def record_integrity_issue(self, kind: str, **detail) -> dict:
        entry = {"kind": kind, **detail}
        self.integrity_issues.append(entry)
        return entry

    def record_reconciliation(self, ok: bool, reasons) -> None:
        self.last_reconciliation = {"ok": bool(ok), "reasons": list(reasons)}

    def record_snapshot_meta(self, snapshot_ts, received_ts, *, complete: bool = True) -> None:
        self.last_snapshot_meta = {"snapshot_ts": snapshot_ts, "received_ts": received_ts, "complete": bool(complete)}

    def resolve_integrity_issues(self, recon_result, *, approver: str, note: str) -> list[dict]:
        """Clear recorded settlement-integrity issues. Same guard as breach resolution:
        a clean reconciliation result and a named approver."""

        if not approver or not str(approver).strip():
            raise LedgerError("integrity resolution requires a named approver")
        if recon_result is None or not getattr(recon_result, "ok", False):
            raise LedgerError("integrity resolution requires a clean reconciliation result")
        cleared = [{**e, "approver": str(approver), "note": str(note)} for e in self.integrity_issues]
        self.resolved_integrity_issues.extend(cleared)
        self.integrity_issues.clear()
        return cleared

    def integrity_problems(self) -> list[str]:
        """Internal-consistency check of the books. Empty list = internally consistent.

        Deliberately NOT a problem: a reservation overrun (``reservation_breaches``) or
        negative available cash - those are financial-limit breaches that the Risk
        gate handles separately. Problems here mean the ledger itself cannot be trusted.
        """

        def bad(value) -> bool:
            return not isinstance(value, (int, float)) or not math.isfinite(value)

        problems: list[str] = []
        for name in ("cash", "reserved_cash", "realized_pnl", "fees", "taxes", "fx_costs"):
            for ccy, value in getattr(self, name).items():
                if bad(value):
                    problems.append(f"non_finite:{name}:{_to_currency(ccy).value}")
        for ccy, value in self.reserved_cash.items():
            if not bad(value) and value < -_EPS:
                problems.append(f"negative_reserved_cash:{_to_currency(ccy).value}")
        for (market_value, symbol), position in self.positions.items():
            label = f"{market_value}:{symbol}"
            if bad(position.quantity) or bad(position.avg_cost) or bad(position.reserved_quantity):
                problems.append(f"non_finite:position:{label}")
            elif position.quantity < -_EPS:
                problems.append(f"negative_position:{label}")
            elif position.reserved_quantity < -_EPS or position.reserved_quantity > position.quantity + _EPS:
                problems.append(f"reserved_quantity_inconsistent:{label}")
        for breach in self.reservation_breaches:
            for key in ("shortfall", "actual_cost", "reservation_allocated", "overrun"):
                if key in breach and bad(breach[key]):
                    problems.append(f"non_finite:breach:{key}")
        for issue in self.integrity_issues:
            problems.append(f"settlement:{issue.get('kind', 'unknown')}")
        return problems

    def apply_fill(self, fill: Fill) -> bool:
        """Apply a fill to the books. Idempotent (INV-04): a fill_id already seen
        is ignored and ``False`` is returned. Returns ``True`` when applied.
        A rejected fill (LedgerError) leaves every balance untouched.
        """

        if fill.fill_id in self.processed_fill_ids:
            return False

        ccy = _to_currency(fill.currency)
        key = self._key(fill.market, fill.symbol)
        position = self.positions.get(key, Position())
        gross = fill.gross
        costs = fill.total_cost

        # Validate BEFORE any mutation (Batch #2.0 E-3: fees/taxes used to change
        # before the INV-05 raise).
        if fill.side is Side.SELL and fill.quantity - position.quantity > _EPS:  # INV-05
            raise LedgerError(
                "SELL exceeds owned quantity (short selling disabled in Phase 0)"
            )

        self.fees[ccy] += fill.commission + fill.exchange_fee
        self.taxes[ccy] += fill.tax
        self.fx_costs[ccy] += fill.fx_cost

        if fill.side is Side.BUY:
            acquisition_cost = gross + costs  # FIX-03: costs enter the cost basis
            new_qty = position.quantity + fill.quantity
            new_avg = ((position.quantity * position.avg_cost) + acquisition_cost) / new_qty
            self.cash[ccy] -= acquisition_cost
            self.positions[key] = Position(new_qty, new_avg, position.reserved_quantity)
        else:
            cost_basis_sold = position.avg_cost * fill.quantity
            # FIX-03: only the sell-side costs are subtracted here; buy-side costs
            # were already folded into avg_cost.
            self.realized_pnl[ccy] += gross - cost_basis_sold - costs
            self.cash[ccy] += gross - costs
            remaining = position.quantity - fill.quantity
            self.positions[key] = Position(
                remaining,
                position.avg_cost if remaining > _EPS else 0.0,
                position.reserved_quantity,  # settlement releases reservations, not apply_fill
            )

        self.processed_fill_ids.add(fill.fill_id)
        return True

    # -------------------------------------------------------------- reporting
    def _dict_rate(self, rates: dict, ccy: Currency) -> float:
        """Rate ``ccy -> base_currency`` from a plain dict. Base is 1.0; anything
        else must be present, finite and positive (D-011 / D-014). Batch #2.0
        B-1/B-2 removed the previous ``rates.get(ccy, 1.0)`` fallback."""

        if ccy == self.base_currency:
            return 1.0
        if ccy not in rates:
            raise MissingFXRateError((ccy, self.base_currency))
        value = rates[ccy]
        if not math.isfinite(value) or value <= 0:
            raise InvalidFXRateError((ccy, self.base_currency))
        return value

    def realized_pnl_total(self, fx_rates: dict | None = None) -> float:
        """Sum in ``base_currency`` with a plain ``{currency: rate_to_base}`` dict.
        Raises ``MissingFXRateError`` for a non-zero non-base currency without a
        rate. Prefer :meth:`realized_pnl_in` with an explicit provider.
        """

        rates = {_to_currency(k): float(v) for k, v in (fx_rates or {}).items()}
        return sum(
            value * self._dict_rate(rates, ccy)
            for ccy, value in self.realized_pnl.items()
            if abs(value) > _EPS or ccy == self.base_currency
        )

    def realized_pnl_in(self, base_currency, fx_provider) -> float:
        base = _to_currency(base_currency)
        return sum(
            value * fx_provider.rate(ccy, base) for ccy, value in self.realized_pnl.items()
        )

    def unrealized_pnl(self, mark_prices: dict) -> dict[Currency, float]:
        out: dict[Currency, float] = defaultdict(float)
        for (market_value, symbol), position in self.positions.items():
            if position.quantity <= _EPS:
                continue
            mark = mark_prices.get((market_value, symbol))
            if mark is None:
                mark = mark_prices.get(symbol)
            if mark is None:
                continue
            ccy = currency_for(Market(market_value), symbol)
            out[ccy] += (float(mark) - position.avg_cost) * position.quantity
        return dict(out)

    def equity(self, fx_rates: dict | None = None, mark_prices: dict | None = None) -> float:
        """Legacy convenience. FX is strict (no 1.0 fallback); a missing mark still
        falls back to ``avg_cost`` - use :meth:`valuation` where a missing price
        must be surfaced instead."""

        rates = {_to_currency(k): float(v) for k, v in (fx_rates or {}).items()}
        marks = mark_prices or {}
        total = sum(
            value * self._dict_rate(rates, ccy)
            for ccy, value in self.cash.items()
            if abs(value) > _EPS or ccy == self.base_currency
        )
        for (market_value, symbol), position in self.positions.items():
            if position.quantity <= _EPS:
                continue
            ccy = currency_for(Market(market_value), symbol)
            price = marks.get((market_value, symbol)) or marks.get(symbol) or position.avg_cost
            total += position.quantity * float(price) * self._dict_rate(rates, ccy)
        return total

    def valuation(self, mark_prices: dict | None, fx_provider=None, base_currency=None) -> Valuation:
        """Strict valuation for research and the UI (Batch #2.0 B-3).

        ``mark_prices`` maps ``(market_value, symbol)`` -> price. A held position
        without a finite positive mark is listed in ``missing_marks`` and its
        currency's ``positions_value`` / ``unrealized`` / ``equity`` become
        ``None``; it is never valued at avg_cost.
        """

        base = _to_currency(base_currency or self.base_currency)
        marks = mark_prices or {}
        val = Valuation(base_currency=base)
        currencies = set(self.cash) | set(self.reserved_cash) | set(self.realized_pnl)
        rows = []
        for (market_value, symbol), position in sorted(self.positions.items()):
            if abs(position.quantity) <= _EPS and position.reserved_quantity <= _EPS:
                continue
            ccy = currency_for(Market(market_value), symbol)
            currencies.add(ccy)
            mark = marks.get((market_value, symbol))
            ok = isinstance(mark, (int, float)) and math.isfinite(mark) and mark > 0
            if not ok and abs(position.quantity) > _EPS:
                val.missing_marks.append((market_value, symbol))
            rows.append({
                "market": market_value, "symbol": symbol, "currency": ccy.value,
                "quantity": position.quantity, "reserved_quantity": position.reserved_quantity,
                "available_quantity": position.available_quantity, "avg_cost": position.avg_cost,
                "mark": float(mark) if ok else None,
                "market_value": position.quantity * float(mark) if ok else None,
                "unrealized": (float(mark) - position.avg_cost) * position.quantity if ok else None,
            })
        val.positions = rows
        for ccy in sorted(currencies, key=lambda c: c.value):
            pos_rows = [r for r in rows if r["currency"] == ccy.value]
            mark_missing = any(r["mark"] is None and abs(r["quantity"]) > _EPS for r in pos_rows)
            positions_value = sum(r["market_value"] or 0.0 for r in pos_rows)
            unrealized = sum(r["unrealized"] or 0.0 for r in pos_rows)
            cash = self.cash.get(ccy, 0.0)
            val.by_currency[ccy] = {
                "cash": cash,
                "reserved": self.reserved_cash.get(ccy, 0.0),
                "available": cash - self.reserved_cash.get(ccy, 0.0),
                "positions_value": None if mark_missing else positions_value,
                "unrealized": None if mark_missing else unrealized,
                "realized": self.realized_pnl.get(ccy, 0.0),
                "fees": self.fees.get(ccy, 0.0),
                "taxes": self.taxes.get(ccy, 0.0),
                "fx_costs": self.fx_costs.get(ccy, 0.0),
                "equity": None if mark_missing else cash + positions_value,
            }
        total = 0.0
        for ccy, row in val.by_currency.items():
            amount = row["equity"]
            held = any(r["currency"] == ccy.value and abs(r["quantity"]) > _EPS for r in rows)
            if not held and (amount is None or abs(amount) <= _EPS) and ccy != base:
                continue
            # FX availability is checked independently of mark availability so
            # both gaps are reported.
            try:
                if ccy == base:
                    rate = 1.0
                elif fx_provider is None:
                    raise MissingFXRateError((ccy, base))
                else:
                    rate = fx_provider.rate(ccy, base)
            except FXError:
                val.missing_fx.append(ccy.value)
                continue
            if amount is not None:
                total += amount * rate
        val.total_base = total if val.complete else None
        return val

    def equity_in(self, base_currency, fx_provider, mark_prices: dict | None = None) -> float:
        """Total account value converted to ``base_currency``. Raises FXError
        (via the provider) on a missing rate - callers fail closed.
        """

        base = _to_currency(base_currency)
        marks = mark_prices or {}
        total = sum(
            value * fx_provider.rate(ccy, base) for ccy, value in self.cash.items()
        )
        for (market_value, symbol), position in self.positions.items():
            if abs(position.quantity) <= _EPS:
                continue
            pccy = currency_for(Market(market_value), symbol)
            price = marks.get((market_value, symbol)) or marks.get(symbol) or position.avg_cost
            total += position.quantity * float(price) * fx_provider.rate(pccy, base)
        return total

    def exposure_in(self, base_currency, fx_provider, mark_prices: dict | None = None) -> float:
        """Sum of absolute position market values, converted to ``base_currency``."""

        base = _to_currency(base_currency)
        marks = mark_prices or {}
        total = 0.0
        for (market_value, symbol), position in self.positions.items():
            if abs(position.quantity) <= _EPS:
                continue
            pccy = currency_for(Market(market_value), symbol)
            price = marks.get((market_value, symbol)) or marks.get(symbol) or position.avg_cost
            total += abs(position.quantity * float(price)) * fx_provider.rate(pccy, base)
        return total
