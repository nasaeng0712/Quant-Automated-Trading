"""Audit logging (Core System Design v0.1, section 35).

Append-only record of every decision in the pipeline. In Phase 0 this is an
in-memory list with an optional sink callback; a durable sink is a later phase.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class AuditRecord:
    stage: str
    payload: dict
    timestamp: datetime = field(default_factory=_utcnow)


class AuditLog:
    def __init__(self, sink=None, now_fn=None) -> None:
        self.records: list[AuditRecord] = []
        self._sink = sink
        self._now = now_fn or _utcnow

    def record(self, stage: str, **payload: Any) -> AuditRecord:
        rec = AuditRecord(stage=stage, payload=dict(payload), timestamp=self._now())
        self.records.append(rec)
        if self._sink is not None:
            self._sink(rec)
        return rec

    def by_stage(self, stage: str) -> list[AuditRecord]:
        return [rec for rec in self.records if rec.stage == stage]

    def dump(self) -> list[dict]:
        return [
            {"stage": rec.stage, "timestamp": rec.timestamp.isoformat(), **rec.payload}
            for rec in self.records
        ]
