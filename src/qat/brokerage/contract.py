"""Broker adapter CONTRACT (interface only - no SDK, no credentials, no network).

A future real adapter implements ``BrokerAdapter`` and nothing else: it moves orders and reports facts. It makes NO Risk / Compliance / Market
Integrity decision (the Core is the authoritative policy engine) and everything it reports is EXTERNAL EVIDENCE that the boundary
(``qat.brokerage.boundary``) validates and the Ledger / Reconciliation then verify.

Required capabilities: account snapshot, positions, cash/balance, submit order (idempotent by ``client_order_id``), cancel order, order status,
fills, reconnect/recover, and SOURCE timestamps on everything.

Failure vocabulary (``BrokerFault`` subclasses / ``BrokerOrderState.UNKNOWN``): timeout, connection lost, rejected, partial fill, unknown order
state, duplicate response, stale snapshot, inconsistent fill, reconnect. ``UNKNOWN`` is a first-class state - it is never silently turned into a
terminal state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Protocol


class BrokerOrderState(str, Enum):
    ACCEPTED = "ACCEPTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"


TERMINAL_BROKER_STATES = frozenset({BrokerOrderState.FILLED, BrokerOrderState.CANCELLED, BrokerOrderState.REJECTED})


class BrokerFault(RuntimeError):
    kind = "broker_fault"


class BrokerTimeout(BrokerFault):
    kind = "timeout"


class BrokerConnectionLost(BrokerFault):
    kind = "connection_lost"


class BrokerRejected(BrokerFault):
    kind = "rejected"


class DuplicateResponse(BrokerFault):
    kind = "duplicate_response"


class StaleSnapshotError(BrokerFault):
    kind = "stale_snapshot"


class InconsistentFill(BrokerFault):
    kind = "inconsistent_fill"


@dataclass(frozen=True)
class BrokerSnapshot:
    cash: dict  # currency -> balance
    positions: dict  # (market, symbol) -> quantity
    snapshot_ts: datetime | None  # SOURCE time: when the broker says this is true
    received_ts: datetime | None  # when QAT received it
    complete: bool = True
    source: str = ""


@dataclass(frozen=True)
class BrokerAck:
    client_order_id: str
    broker_order_id: str | None
    state: BrokerOrderState
    received_ts: datetime | None = None
    reason: str = ""


@dataclass(frozen=True)
class BrokerFillReport:
    broker_fill_id: str
    client_order_id: str
    broker_order_id: str | None
    side: str
    quantity: float
    price: float
    timestamp: datetime
    commission: float = 0.0
    tax: float = 0.0
    exchange_fee: float = 0.0
    extra: dict = field(default_factory=dict)


class BrokerAdapter(Protocol):
    """Everything a broker connection must provide. All methods may raise ``BrokerFault`` subclasses."""

    name: str

    def account_snapshot(self) -> BrokerSnapshot: ...

    def positions(self) -> dict: ...

    def cash(self) -> dict: ...

    def submit_order(self, order_view: dict, client_order_id: str) -> BrokerAck:
        """MUST be idempotent: the same ``client_order_id`` never creates a second order."""

    def cancel_order(self, client_order_id: str) -> BrokerAck: ...

    def order_status(self, client_order_id: str) -> BrokerAck: ...

    def fills(self) -> list[BrokerFillReport]:
        """Fill reports not yet acknowledged; the same fill may be delivered more than once (callbacks are not exactly-once)."""

    def reconnect(self) -> dict: ...
