"""Shared gate vocabulary (Core System Design v0.1, principle P2 - Fail Closed).

Every pre-trade engine returns a :class:`GateDecision`. ``UNKNOWN`` means the
engine could not prove the trade is safe; the orchestrator treats anything that
is not ``PASS`` as "do not trade".
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class GateStatus(str, Enum):
    PASS = "PASS"
    BLOCK = "BLOCK"
    UNKNOWN = "UNKNOWN"


_SEVERITY = {GateStatus.PASS: 0, GateStatus.UNKNOWN: 1, GateStatus.BLOCK: 2}


@dataclass(frozen=True)
class GateDecision:
    status: GateStatus
    reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.reasons, tuple):
            object.__setattr__(self, "reasons", tuple(self.reasons))

    @property
    def ok(self) -> bool:
        return self.status is GateStatus.PASS

    def describe(self) -> str:
        return f"{self.status.value}:{','.join(self.reasons)}" if self.reasons else self.status.value


def worst(*decisions: GateDecision) -> GateStatus:
    """Combine decisions: BLOCK dominates UNKNOWN dominates PASS."""

    status = GateStatus.PASS
    for decision in decisions:
        if _SEVERITY[decision.status] > _SEVERITY[status]:
            status = decision.status
    return status
