"""Broker/account snapshot freshness (stale snapshot policy).

Three timestamps are kept apart: the SOURCE timestamp of the snapshot (when the broker says it is true), the RECEIVED timestamp (when QAT got it)
and ``now`` (when the decision is made). Freshness is the age of the source timestamp at decision time:

  FRESH    age <= configured maximum
  STALE    age >  configured maximum
  UNKNOWN  anything that prevents a trustworthy age: missing / naive / non-finite / future source timestamp, missing received timestamp,
           received before it was produced, invalid maximum

``UNKNOWN`` is never treated as FRESH. A stale (or unknown-age) snapshot can neither attest a reconciliation nor overwrite a previous one.
Paper replay has no broker account: it uses the explicit ``paper_replay`` policy of the RiskGate, never a silent freshness bypass.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum


class Freshness(str, Enum):
    FRESH = "FRESH"
    STALE = "STALE"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class FreshnessResult:
    status: Freshness
    age_seconds: float | None
    received_lag_seconds: float | None
    max_age_seconds: float | None
    reason: str

    def to_dict(self) -> dict:
        d = asdict(self)
        d["status"] = self.status.value
        return d


def _aware(value) -> bool:
    return isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None


def evaluate_freshness(snapshot_ts, received_ts, now, max_age_seconds, *, future_tolerance_seconds: float = 5.0) -> FreshnessResult:
    def unknown(reason, age=None, lag=None):
        return FreshnessResult(Freshness.UNKNOWN, age, lag, max_age_seconds if _finite(max_age_seconds) else None, reason)

    if not _finite(max_age_seconds) or max_age_seconds <= 0:
        return unknown("max_age_invalid")
    if not _finite(future_tolerance_seconds) or future_tolerance_seconds < 0:
        return unknown("future_tolerance_invalid")
    if not _aware(now):
        return unknown("now_invalid")
    if snapshot_ts is None:
        return unknown("snapshot_timestamp_missing")
    if not _aware(snapshot_ts):
        return unknown("snapshot_timestamp_invalid")
    if received_ts is None:
        return unknown("received_timestamp_missing")
    if not _aware(received_ts):
        return unknown("received_timestamp_invalid")
    now_utc, snap_utc, recv_utc = (x.astimezone(timezone.utc) for x in (now, snapshot_ts, received_ts))
    age = (now_utc - snap_utc).total_seconds()
    lag = (recv_utc - snap_utc).total_seconds()
    if age < -future_tolerance_seconds:
        return unknown("snapshot_timestamp_in_future", age, lag)
    if lag < -future_tolerance_seconds:
        return unknown("received_before_snapshot_timestamp", age, lag)
    if (now_utc - recv_utc).total_seconds() < -future_tolerance_seconds:
        return unknown("received_timestamp_in_future", age, lag)
    age = max(age, 0.0)
    if age > max_age_seconds:
        return FreshnessResult(Freshness.STALE, age, lag, max_age_seconds, f"age {age:.1f}s > max {max_age_seconds:.1f}s")
    return FreshnessResult(Freshness.FRESH, age, lag, max_age_seconds, "within the configured maximum age")


def _finite(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def reconcile_with_snapshot(ledger, *, broker_cash: dict, broker_positions: dict, snapshot_ts, received_ts, now, max_age_seconds: float,
                            complete: bool = True, tol: float = 1e-6, future_tolerance_seconds: float = 5.0):
    """Record the snapshot metadata, then reconcile ONLY if the snapshot is FRESH.

    A STALE / UNKNOWN snapshot cannot attest the books: it is recorded (so the Risk gate sees its age) but the previous reconciliation result
    is left untouched and the returned result is ``ok=False`` with an explicit reason. Returns ``(ReconResult, FreshnessResult)``.
    """

    from qat.portfolio.reconciliation import ReconResult, reconcile

    ledger.record_snapshot_meta(snapshot_ts, received_ts, complete=complete)
    fr = evaluate_freshness(snapshot_ts, received_ts, now, max_age_seconds, future_tolerance_seconds=future_tolerance_seconds)
    if fr.status is not Freshness.FRESH:
        reason = "snapshot_stale" if fr.status is Freshness.STALE else f"snapshot_freshness_unknown:{fr.reason}"
        return ReconResult(ok=False, reasons=[reason]), fr
    return reconcile(ledger, broker_cash=broker_cash, broker_positions=broker_positions, tol=tol, complete=complete), fr
