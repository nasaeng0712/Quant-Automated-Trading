"""Authoritative exposure-reducing classification (OD-01, Batch #2.2).

One function, shared by the Net Alpha gate (threshold exemption) and the Risk gate
(limit relaxations), so the two can never disagree about what a "reducing" order is.

The decision is made ONLY from the Ledger (server/core-owned state) and the
proposal's side and quantity. Nothing the caller says about itself - no ``exit`` /
``reduce_only`` / ``risk_reducing`` flag, no reason code - is ever read.
"""

from __future__ import annotations

from qat.core.models import Side, TradeProposal

_EPS = 1e-9


def reduces_exposure(ledger, proposal: TradeProposal) -> bool:
    """True only when the proposal strictly moves the position toward zero and cannot
    cross it.

    Long position: SELL with quantity <= *available* (unreserved) quantity.
    (Signed rule kept general: a short position would be reduced by a BUY of at most
    its absolute size; shorts are not supported today.)
    A BUY on a long, a SELL with no position, a SELL larger than the position and a
    SELL on quantity already reserved by another order are never reducing
    (no flip, no new position, no exposure increase). Without a ledger nothing can be
    shown to reduce exposure.
    """

    if ledger is None:
        return False
    position = ledger.get_position(proposal.market, proposal.symbol)
    quantity = position.quantity
    if quantity > _EPS and proposal.side is Side.SELL:
        limit = position.available_quantity
    elif quantity < -_EPS and proposal.side is Side.BUY:
        limit = -quantity
    else:
        return False
    return proposal.quantity <= limit + _EPS
