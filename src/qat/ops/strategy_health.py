"""Strategy Health - OPERATIONAL status of a strategy / proposal source. Never performance.

  HEALTHY   running normally (including "no trades": a quiet strategy is not a broken one)
  DEGRADED  something operational needs attention (stale data, an error streak starting, a rejection concentration, a missing heartbeat)
  UNHEALTHY an operational failure (error streak at the limit, unexpected state carry-over)
  UNKNOWN   not enough information (never initialized / nothing observed yet)

Inputs are operational events only: initialization, data timestamps, evaluation heartbeats / errors, signal timestamps, proposal outcomes and
state-carry-over signals. The monitor only OBSERVES: it never changes a strategy parameter and never replaces or disables a strategy
(design 12.2: no automatic retraining / model replacement). Profit, loss or drawdown are deliberately not inputs.
Mapping to the design doc's colour scale: HEALTHY ~ GREEN, DEGRADED ~ YELLOW/ORANGE, UNHEALTHY ~ RED/BLACK, UNKNOWN ~ not assessed.
"""

from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import datetime

HEALTHY, DEGRADED, UNHEALTHY, UNKNOWN = "HEALTHY", "DEGRADED", "UNHEALTHY", "UNKNOWN"
_RANK = {HEALTHY: 0, DEGRADED: 1, UNHEALTHY: 2}


@dataclass
class HealthReport:
    strategy_id: str
    status: str
    reasons: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"strategy_id": self.strategy_id, "status": self.status, "reasons": list(self.reasons), "notes": list(self.notes), "metrics": dict(self.metrics),
                "scope": "operational health only; performance is not assessed"}


class StrategyHealthMonitor:
    def __init__(self, strategy_id: str, *, thresholds: dict, now_fn) -> None:
        self.strategy_id = strategy_id
        self.t = dict(thresholds)
        self._now = now_fn
        self.initialized = False
        self.last_data_ts: datetime | None = None
        self.last_heartbeat_ts: datetime | None = None
        self.last_signal_ts: datetime | None = None
        self.evaluations = 0
        self.signals = 0
        self.consecutive_errors = 0
        self.total_errors = 0
        self.exception_count = 0
        self.proposal_failures = 0
        self.consecutive_proposal_failures = 0
        self.state_carry_over = False
        self.proposals = 0
        self.rejections: deque = deque(maxlen=int(self.t["rejection_window"]))
        self.last_error: str | None = None

    # ---------------------------------------------------------------- events (observation only)
    def mark_initialized(self) -> None:
        self.initialized = True

    def record_data(self, ts: datetime) -> None:
        self.last_data_ts = ts

    def record_evaluation(self, *, ok: bool = True, error: str | None = None, exception: bool = False) -> None:
        self.evaluations += 1
        self.last_heartbeat_ts = self._now()
        if ok:
            self.consecutive_errors = 0
        else:
            self.consecutive_errors += 1
            self.total_errors += 1
            self.last_error = (error or "evaluation_error")[:200]
            if exception:
                self.exception_count += 1

    def record_signal(self, ts: datetime | None = None) -> None:
        self.signals += 1
        self.last_signal_ts = ts or self._now()

    def record_proposal(self, *, accepted: bool | None, reason: str | None = None, generation_failed: bool = False) -> None:
        if generation_failed:
            self.proposal_failures += 1
            self.consecutive_proposal_failures += 1
            return
        self.consecutive_proposal_failures = 0
        self.proposals += 1
        if accepted is False:
            self.rejections.append(":".join((reason or "unknown").split(":")[:3]))
        elif accepted:
            self.rejections.append(None)

    def record_state_carry_over(self, detected: bool = True) -> None:
        self.state_carry_over = bool(detected)

    # ---------------------------------------------------------------- evaluation
    def evaluate(self) -> HealthReport:
        now = self._now()
        r = HealthReport(self.strategy_id, HEALTHY)
        t = self.t
        r.metrics = {"evaluations": self.evaluations, "signals": self.signals, "proposals": self.proposals, "consecutive_errors": self.consecutive_errors,
                     "total_errors": self.total_errors, "exception_count": self.exception_count, "proposal_generation_failures": self.proposal_failures,
                     "last_signal_ts": self.last_signal_ts.isoformat() if self.last_signal_ts else None,
                     "last_data_ts": self.last_data_ts.isoformat() if self.last_data_ts else None,
                     "last_heartbeat_ts": self.last_heartbeat_ts.isoformat() if self.last_heartbeat_ts else None}

        def raise_to(status: str, reason: str) -> None:
            r.reasons.append(reason)
            if _RANK[status] > _RANK[r.status]:
                r.status = status

        if not self.initialized:
            r.status = UNKNOWN
            r.reasons.append("not_initialized")
            return r
        if self.state_carry_over:
            raise_to(UNHEALTHY, "unexpected_state_carry_over")
        if self.consecutive_errors >= t["max_consecutive_errors"]:
            raise_to(UNHEALTHY, f"consecutive_evaluation_errors:{self.consecutive_errors}>={t['max_consecutive_errors']}")
        elif self.consecutive_errors > 0:
            raise_to(DEGRADED, f"evaluation_errors:{self.consecutive_errors}")
        if self.exception_count > 0:
            raise_to(DEGRADED, f"exceptions:{self.exception_count}")
        if self.consecutive_proposal_failures >= max(2, t["max_consecutive_errors"] // 2 + 1):
            raise_to(DEGRADED, f"proposal_generation_failures:{self.consecutive_proposal_failures}")
        if self.last_data_ts is None and self.evaluations > 0:
            raise_to(DEGRADED, "no_data_observed")
        elif self.last_data_ts is not None and (now - self.last_data_ts).total_seconds() > t["max_data_age_seconds"]:
            raise_to(DEGRADED, f"data_stale:{(now - self.last_data_ts).total_seconds():.0f}s>{t['max_data_age_seconds']:.0f}s")
        if self.last_heartbeat_ts is not None and (now - self.last_heartbeat_ts).total_seconds() > t["max_heartbeat_age_seconds"]:
            raise_to(DEGRADED, "evaluation_heartbeat_missing")
        counts = Counter(x for x in self.rejections if x)
        if counts and len(self.rejections) >= t["min_rejections_for_concentration"]:
            reason, n = counts.most_common(1)[0]
            if n / len(self.rejections) >= t["rejection_concentration"]:
                raise_to(DEGRADED, f"rejection_concentration:{reason}:{n}/{len(self.rejections)}")
        if self.evaluations == 0:
            r.status = UNKNOWN if r.status == HEALTHY else r.status
            r.reasons.append("no_evaluation_observed_yet")
        elif self.signals == 0:
            r.notes.append("no signals/trades so far: not a failure (a quiet strategy is healthy)")
        return r
