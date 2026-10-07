"""Reconciliation engine (Core System Design v0.1, section 34).

Compares the internal ledger against a broker snapshot. Any mismatch beyond
tolerance is reported so the caller can BLOCK / trip the kill switch. Phase 0
ships the comparison only; there is no live broker to pull a snapshot from yet.

Batch #2.0 (D-1..D-4): the comparison covers the UNION of internal and broker
keys - a cash balance or position that exists only in the ledger is a mismatch,
an empty snapshot is never a match, and a snapshot flagged incomplete is not ok.
This module only compares; clearing breaches or a kill switch is a separate,
approved action (``PortfolioLedger.resolve_reservation_breaches``).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from qat.core.models import Currency, Market
from qat.portfolio.ledger import PortfolioLedger


@dataclass
class ReconResult:
    ok: bool
    cash_diffs: dict = field(default_factory=dict)
    position_diffs: dict = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)


def _market_value(market) -> str:
    return market.value if isinstance(market, Market) else str(market)


def reconcile(
    ledger: PortfolioLedger,
    *,
    broker_cash: dict,
    broker_positions: dict,
    tol: float = 1e-6,
    complete: bool = True,
) -> ReconResult:
    """``broker_cash`` maps currency -> balance. ``broker_positions`` maps
    ``(market, symbol)`` -> quantity. ``complete=False`` marks a snapshot the
    caller knows to be partial; the result is then never ok.
    """

    cash_diffs: dict = {}
    position_diffs: dict = {}
    reasons: list[str] = []

    # Audit fix (D1): every comparison below is ``abs(diff) > tol``, which is False
    # for NaN - a non-finite balance or tolerance would read as a MATCH.
    if not (isinstance(tol, (int, float)) and math.isfinite(tol) and tol >= 0):
        raise ValueError("tol must be a finite number >= 0")

    if not complete:
        reasons.append("snapshot_incomplete")
    if not broker_cash and not broker_positions:
        reasons.append("snapshot_empty")

    broker_cash_n = {
        (c if isinstance(c, Currency) else Currency(c)): float(v) for c, v in broker_cash.items()
    }
    for ccy, value in broker_cash_n.items():
        if not math.isfinite(value):
            cash_diffs[ccy.value] = {"internal": ledger.cash.get(ccy, 0.0), "broker": value}
            reasons.append(f"cash_mismatch:{ccy.value}:non_finite_broker_value")
    currencies = set(broker_cash_n) | {c for c, v in ledger.cash.items() if abs(v) > tol}
    for ccy in sorted(currencies, key=lambda c: c.value):
        internal = ledger.cash.get(ccy, 0.0)
        if ccy in broker_cash_n and not math.isfinite(broker_cash_n[ccy]):
            continue  # already reported above
        if not math.isfinite(internal):
            cash_diffs[ccy.value] = {"internal": internal, "broker": broker_cash_n.get(ccy)}
            reasons.append(f"cash_mismatch:{ccy.value}:non_finite_internal_value")
            continue
        if ccy not in broker_cash_n:
            cash_diffs[ccy.value] = {"internal": internal, "broker": None}
            reasons.append(f"cash_mismatch:{ccy.value}:missing_in_broker")
            continue
        if abs(internal - broker_cash_n[ccy]) > tol:
            cash_diffs[ccy.value] = {"internal": internal, "broker": broker_cash_n[ccy]}
            reasons.append(f"cash_mismatch:{ccy.value}")

    broker_pos_n = {
        (_market_value(m), s): float(q) for (m, s), q in broker_positions.items()
    }
    keys = set(broker_pos_n) | {k for k, p in ledger.positions.items() if abs(p.quantity) > tol}
    for market, symbol in sorted(keys):
        internal = ledger.get_position(market, symbol).quantity
        broker_qty = broker_pos_n.get((market, symbol))
        if not math.isfinite(internal) or (broker_qty is not None and not math.isfinite(broker_qty)):
            position_diffs[f"{market}:{symbol}"] = {"internal": internal, "broker": broker_qty}
            reasons.append(f"position_mismatch:{market}:{symbol}:non_finite_value")
        elif broker_qty is None:
            position_diffs[f"{market}:{symbol}"] = {"internal": internal, "broker": None}
            reasons.append(f"position_mismatch:{market}:{symbol}:missing_in_broker")
        elif abs(internal - broker_qty) > tol:
            position_diffs[f"{market}:{symbol}"] = {"internal": internal, "broker": broker_qty}
            reasons.append(f"position_mismatch:{market}:{symbol}")

    ok = not cash_diffs and not position_diffs and not reasons
    # The Risk gate reads this (Batch #2.2): a real comparison is never invisible to it.
    ledger.record_reconciliation(ok, reasons)
    return ReconResult(ok=ok, cash_diffs=cash_diffs, position_diffs=position_diffs, reasons=reasons)
