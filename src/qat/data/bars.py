"""OHLCV bar + dataset metadata contract (Batch #2 Historical Data Engine).

Timestamp semantics (fixed here, recorded in every manifest):
  * ``Bar.ts`` is the bar OPEN time, timezone-aware, normalised to UTC.
  * ``Bar.close_time = ts + timeframe``. A bar's OHLC is only known after its
    close time; the backtest never lets a signal see a bar before that.
  * Source files may label bars by open or by close (``timestamp_label``) and
    may carry naive timestamps interpreted in ``DatasetMeta.timezone``.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone

from qat.core.models import DomainError, Market, currency_for

_TIMEFRAME_RE = re.compile(r"^(\d+)([mhd])$")
_UNITS = {"m": "minutes", "h": "hours", "d": "days"}
_OFFSET_RE = re.compile(r"^([+-])(\d{2}):?(\d{2})$")


class DataError(ValueError):
    """Invalid dataset metadata or unreadable data."""


class DataRejected(RuntimeError):
    """A dataset failed validation / admission; research on it is BLOCKED (domain rejection,
    surfaced as HTTP 4xx by the UI - never a 500)."""


class DataBlocked(RuntimeError):
    """A data path cannot run in this environment (e.g. optional dependency)."""


def parse_timeframe(value: str) -> timedelta:
    match = _TIMEFRAME_RE.match(str(value).strip())
    if not match:
        raise DataError(f"unsupported timeframe {value!r} (use e.g. 1m, 5m, 1h, 1d)")
    amount = int(match.group(1))
    if amount <= 0:
        raise DataError(f"timeframe must be positive: {value!r}")
    return timedelta(**{_UNITS[match.group(2)]: amount})


@dataclass(frozen=True)
class Bar:
    ts: datetime  # bar open time, UTC
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class DatasetMeta:
    market: str
    symbol: str
    timeframe: str
    timezone: str  # "UTC", fixed offset "+09:00", or an IANA name (needs tz database)
    timestamp_label: str = "open"  # "open" | "close"
    source: str = "LOCAL_FILE"  # e.g. SYNTHETIC, LOCAL_FILE, VENDOR:<name>
    synthetic: bool = False
    description: str = ""
    extra: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        try:
            market = Market(self.market)
        except ValueError as exc:
            raise DataError(f"unknown market {self.market!r}") from exc
        if not self.symbol:
            raise DataError("symbol is required")
        # Audit fix (D6): market/symbol consistency (design D-006: KR/US plain ticker,
        # CRYPTO BASE/QUOTE) - an inconsistent pair is an input error, not a dataset.
        if market is not Market.CRYPTO and "/" in self.symbol:
            raise DataError(f"{market.value} symbol must be a plain ticker, got {self.symbol!r}")
        try:
            currency_for(market, self.symbol)  # validates BASE/QUOTE for crypto
        except DomainError as exc:
            raise DataError(str(exc)) from exc
        parse_timeframe(self.timeframe)
        if self.timestamp_label not in ("open", "close"):
            raise DataError("timestamp_label must be 'open' or 'close'")
        if not self.timezone:
            raise DataError("timezone is required (e.g. 'UTC' or '+09:00')")
        if self.synthetic and not str(self.source).upper().startswith("SYNTHETIC"):
            raise DataError("synthetic datasets must declare source SYNTHETIC")
        if not self.synthetic and str(self.source).upper().startswith("SYNTHETIC"):
            raise DataError("source SYNTHETIC requires synthetic=true (would be shown as real data)")

    @property
    def market_enum(self) -> Market:
        return Market(self.market)

    @property
    def currency(self) -> str:
        return currency_for(self.market_enum, self.symbol).value

    @property
    def duration(self) -> timedelta:
        return parse_timeframe(self.timeframe)

    def to_dict(self) -> dict:
        return asdict(self)


def resolve_timezone(name: str):
    text = str(name).strip()
    if text.upper() in ("UTC", "Z", "+00:00"):
        return timezone.utc
    match = _OFFSET_RE.match(text)
    if match:
        sign = 1 if match.group(1) == "+" else -1
        try:
            return timezone(sign * timedelta(hours=int(match.group(2)), minutes=int(match.group(3))))
        except ValueError as exc:  # offset outside (-24h, 24h)
            raise DataError(f"invalid timezone offset {text!r}") from exc
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(text)
    except Exception as exc:  # noqa: BLE001
        raise DataBlocked(
            f"timezone {text!r} needs an IANA tz database that is not available here; "
            "use 'UTC' or a fixed offset such as '+09:00'"
        ) from exc


def utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)
