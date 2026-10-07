"""Domain model layer (Core System Design v0.1, sections 3-8).

Everything downstream communicates through these objects. A ``TradeProposal`` is
*not* an order: it is a request that must clear every gate before an ``Order`` is
created. The ``Order`` state machine (section 6) and the state invariants
(section 7) are enforced here so no other module can shortcut them.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from uuid import uuid4

_EPS = 1e-9


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class DomainError(ValueError):
    """Raised when a domain object is constructed with invalid data."""


def is_finite_number(value) -> bool:
    """True for a real, finite int/float (bool excluded). NaN / +-inf are False:
    comparisons such as ``nan <= 0`` are False and would otherwise slip past
    positivity checks (Batch #2.0 F-series)."""

    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


class InvalidStateTransition(RuntimeError):
    """Raised when an Order is asked to move to a state that is not reachable."""


class Market(str, Enum):
    KR = "KR"
    US = "US"
    CRYPTO = "CRYPTO"


class Currency(str, Enum):
    KRW = "KRW"
    USD = "USD"
    USDT = "USDT"  # common crypto quote currency; extend as needed (section 3.2)


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class OrderStatus(str, Enum):
    CREATED = "CREATED"
    VALIDATED = "VALIDATED"
    APPROVED = "APPROVED"
    SUBMITTED = "SUBMITTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCEL_PENDING = "CANCEL_PENDING"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    ERROR = "ERROR"


TERMINAL_STATUSES: frozenset[OrderStatus] = frozenset(
    {
        OrderStatus.FILLED,
        OrderStatus.CANCELLED,
        OrderStatus.REJECTED,
        OrderStatus.EXPIRED,
        OrderStatus.ERROR,
    }
)

# Allowed forward transitions (section 6). Anything not listed is rejected.
_ALLOWED_TRANSITIONS: dict[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.CREATED: frozenset({OrderStatus.VALIDATED, OrderStatus.REJECTED, OrderStatus.ERROR}),
    OrderStatus.VALIDATED: frozenset({OrderStatus.APPROVED, OrderStatus.REJECTED, OrderStatus.ERROR}),
    OrderStatus.APPROVED: frozenset({OrderStatus.SUBMITTED, OrderStatus.REJECTED, OrderStatus.ERROR}),
    OrderStatus.SUBMITTED: frozenset(
        {
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.CANCEL_PENDING,
            OrderStatus.REJECTED,
            OrderStatus.EXPIRED,
            OrderStatus.ERROR,
        }
    ),
    OrderStatus.PARTIALLY_FILLED: frozenset(
        {
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.CANCEL_PENDING,
            OrderStatus.EXPIRED,
            OrderStatus.ERROR,
        }
    ),
    # section 7 / INV-02: if the broker reports a fill while a cancel is pending,
    # the real broker state wins and reconciliation follows.
    OrderStatus.CANCEL_PENDING: frozenset(
        {
            OrderStatus.CANCELLED,
            OrderStatus.FILLED,
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.ERROR,
        }
    ),
    OrderStatus.FILLED: frozenset(),
    OrderStatus.CANCELLED: frozenset(),
    OrderStatus.REJECTED: frozenset(),
    OrderStatus.EXPIRED: frozenset(),
    OrderStatus.ERROR: frozenset(),
}


def currency_for(market: Market, symbol: str) -> Currency:
    """Settlement/quote currency for a market + symbol.

    KR -> KRW, US -> USD. Crypto symbols must be ``BASE/QUOTE`` (e.g. ``BTC/KRW``)
    and the quote leg is the cash currency that moves.
    """

    if market is Market.KR:
        return Currency.KRW
    if market is Market.US:
        return Currency.USD
    if "/" not in symbol:
        raise DomainError(f"crypto symbol must be BASE/QUOTE, got {symbol!r}")
    quote = symbol.split("/", 1)[1].strip().upper()
    try:
        return Currency(quote)
    except ValueError as exc:  # pragma: no cover - defensive
        raise DomainError(f"unsupported crypto quote currency: {quote!r}") from exc


@dataclass(frozen=True)
class TradeProposal:
    """The only trade-request object a strategy may produce (section 4).

    ``TradeProposal != Order``. Mandatory business fields carry defaults so the
    object stays constructible, but :class:`qat.core.validator.ProposalValidator`
    rejects proposals that leave them unset before the pipeline continues.
    """

    market: Market
    symbol: str
    side: Side
    quantity: float
    order_type: OrderType = OrderType.MARKET
    limit_price: float | None = None
    strategy_id: str = "unassigned"
    reason_code: str = ""
    confidence: float = 0.5
    expected_gross_return: float = 0.0  # fraction of notional, e.g. 0.004 == 40 bps
    expected_holding_period_seconds: int = 0
    created_at: datetime = field(default_factory=_utcnow)
    signal_timestamp: datetime | None = None
    model_version: str = "n/a"
    strategy_version: str = "n/a"
    config_version: str = "n/a"
    feature_snapshot_id: str | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    target_position: float | None = None
    proposal_id: str = field(default_factory=lambda: str(uuid4()))

    def __post_init__(self) -> None:
        if not self.symbol:
            raise DomainError("symbol is required")
        for name in ("quantity", "confidence", "expected_gross_return"):
            if not is_finite_number(getattr(self, name)):
                raise DomainError(f"{name} must be a finite number")
        for name in ("limit_price", "stop_loss", "take_profit", "target_position"):
            value = getattr(self, name)
            if value is not None and not is_finite_number(value):
                raise DomainError(f"{name} must be a finite number")
        if self.quantity <= 0:
            raise DomainError("quantity must be > 0")
        if self.order_type is OrderType.LIMIT and (self.limit_price is None or self.limit_price <= 0):
            raise DomainError("LIMIT order requires a positive limit_price")
        if self.order_type is OrderType.MARKET and self.limit_price is not None:
            raise DomainError("MARKET order must not carry a limit_price")
        if not (0.0 <= self.confidence <= 1.0):
            raise DomainError("confidence must be within [0, 1]")
        if self.expected_holding_period_seconds < 0:
            raise DomainError("expected_holding_period_seconds must be >= 0")
        object.__setattr__(self, "created_at", _as_utc(self.created_at))
        if self.signal_timestamp is None:
            object.__setattr__(self, "signal_timestamp", self.created_at)
        else:
            object.__setattr__(self, "signal_timestamp", _as_utc(self.signal_timestamp))

    @property
    def currency(self) -> Currency:
        return currency_for(self.market, self.symbol)

    @property
    def notional_hint(self) -> float | None:
        price = self.limit_price
        return None if price is None else price * self.quantity


@dataclass
class OrderReservation:
    """Cash / position an APPROVED order holds against the ledger (FIX-02).

    ``kind`` is ``"CASH"`` (BUY) or ``"POSITION"`` (SELL). The settlement service
    is the only writer; it decrements ``*_released`` as fills land and releases
    the remainder on a terminal non-filled state.
    """

    kind: str
    currency: "Currency | None" = None
    cash_reserved: float = 0.0
    cash_released: float = 0.0
    quantity_reserved: float = 0.0
    quantity_released: float = 0.0

    @property
    def cash_remaining(self) -> float:
        return max(0.0, self.cash_reserved - self.cash_released)

    @property
    def quantity_remaining(self) -> float:
        return max(0.0, self.quantity_reserved - self.quantity_released)


@dataclass
class Order:
    """A proposal that has cleared every gate (sections 5-7)."""

    proposal: TradeProposal
    broker: str = "PAPER"
    account_id_alias: str = "PAPER-DEFAULT"
    order_id: str = field(default_factory=lambda: str(uuid4()))
    status: OrderStatus = OrderStatus.CREATED
    filled_quantity: float = 0.0
    avg_fill_price: float | None = None
    created_at: datetime = field(default_factory=_utcnow)
    fills: list["Fill"] = field(default_factory=list)
    status_history: list[OrderStatus] = field(default_factory=list)
    reservation: "OrderReservation | None" = None

    # --- passthrough accessors so callers never reach into .proposal for identity
    @property
    def proposal_id(self) -> str:
        return self.proposal.proposal_id

    @property
    def market(self) -> Market:
        return self.proposal.market

    @property
    def symbol(self) -> str:
        return self.proposal.symbol

    @property
    def side(self) -> Side:
        return self.proposal.side

    @property
    def quantity(self) -> float:
        return self.proposal.quantity

    @property
    def order_type(self) -> OrderType:
        return self.proposal.order_type

    @property
    def limit_price(self) -> float | None:
        return self.proposal.limit_price

    @property
    def currency(self) -> Currency:
        return self.proposal.currency

    @property
    def remaining_quantity(self) -> float:
        return self.quantity - self.filled_quantity

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    def can_transition_to(self, new_status: OrderStatus) -> bool:
        return new_status in _ALLOWED_TRANSITIONS[self.status]

    def transition_to(self, new_status: OrderStatus) -> None:
        if new_status not in _ALLOWED_TRANSITIONS[self.status]:
            raise InvalidStateTransition(
                f"illegal order transition {self.status.value} -> {new_status.value}"
            )
        self.status_history.append(self.status)
        self.status = new_status


@dataclass(frozen=True)
class Fill:
    """A real or simulated execution event (section 8)."""

    order_id: str
    market: Market
    symbol: str
    side: Side
    quantity: float
    price: float
    currency: Currency
    commission: float = 0.0
    tax: float = 0.0
    exchange_fee: float = 0.0
    fx_cost: float = 0.0
    slippage_estimate: float = 0.0
    broker_fill_id: str | None = None
    timestamp: datetime = field(default_factory=_utcnow)
    fill_id: str = field(default_factory=lambda: str(uuid4()))

    def __post_init__(self) -> None:
        for name in ("quantity", "price", "commission", "tax", "exchange_fee", "fx_cost",
                     "slippage_estimate"):
            if not is_finite_number(getattr(self, name)):
                raise DomainError(f"fill {name} must be a finite number")
        if self.quantity <= 0 or self.price <= 0:
            raise DomainError("fill quantity and price must be > 0")
        for name in ("commission", "tax", "exchange_fee", "fx_cost"):
            if getattr(self, name) < 0:
                raise DomainError(f"fill {name} must be >= 0")
        object.__setattr__(self, "timestamp", _as_utc(self.timestamp))
        if self.broker_fill_id is None:
            object.__setattr__(self, "broker_fill_id", self.fill_id)

    @property
    def gross(self) -> float:
        return self.quantity * self.price

    @property
    def total_cost(self) -> float:
        return self.commission + self.tax + self.exchange_fee + self.fx_cost


@dataclass
class Position:
    quantity: float = 0.0
    avg_cost: float = 0.0
    reserved_quantity: float = 0.0

    @property
    def available_quantity(self) -> float:
        return self.quantity - self.reserved_quantity
