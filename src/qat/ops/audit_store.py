"""Durable, append-only, hash-chained audit store (JSON Lines).

* append-oriented: records are only ever added (``ab`` + fsync); nothing rewrites history
* deterministic serialization: canonical JSON (sorted keys, no whitespace, no NaN) - the same event always produces the same bytes
* tamper evidence: every record carries ``seq``, ``prev_hash`` and ``hash = sha256(prev_hash + canonical(body))``; ``verify`` detects a malformed
  line, a truncated final record, a broken chain, a changed record, a seq gap and a duplicated ``event_id``
* idempotency: appending an ``event_id`` again with the SAME content is a no-op that returns the stored record; different content is a conflict
* restart-safe: the chain is re-verified when the store is opened; a corrupt file puts the store in a FAULT state that refuses appends (the
  server then refuses to trade: fail-closed) - it is never silently repaired or truncated
* secrets are never stored: credential-looking keys / ``serviceKey=`` style values are redacted before serialization

Single-writer assumption (one server process); multi-process appends are unsupported and documented as such.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import pathlib
import re
import threading
from datetime import date, datetime, timezone

GENESIS = "0" * 64
_SECRET_KEY = re.compile(r"(?i)(secret|password|passwd|token|api[_-]?key|service[_-]?key|credential|authorization)")
_SECRET_VALUE = re.compile(r"(?i)((?:service_?key|api_?key|token|secret|password)=)([^&\s\"']+)")
REDACTED = "<REDACTED>"


class AuditCorrupt(RuntimeError):
    """The audit file failed verification (malformed / truncated / chain broken). The store refuses further appends."""


class AuditConflict(RuntimeError):
    """The same event_id was appended with different content."""


def scrub(obj):
    """Recursively redact credential-looking keys/values and make the object JSON-safe and deterministic."""

    if isinstance(obj, dict):
        return {str(k): (REDACTED if _SECRET_KEY.search(str(k)) else scrub(v)) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [scrub(v) for v in obj]
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else ("nan" if math.isnan(obj) else ("inf" if obj > 0 else "-inf"))
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, (str, int, bool)) or obj is None:
        return _SECRET_VALUE.sub(lambda m: m.group(1) + REDACTED, obj) if isinstance(obj, str) else obj
    if hasattr(obj, "value"):  # enums
        return scrub(obj.value)
    return scrub(repr(obj))


def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _hash(prev_hash: str, body: dict) -> str:
    return hashlib.sha256((prev_hash + canonical(body)).encode("utf-8")).hexdigest()


def _utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class DurableAuditStore:
    REQUIRED = ("seq", "prev_hash", "hash", "event_id", "event_type")

    def __init__(self, path, *, max_page_size: int = 500) -> None:
        self.path = pathlib.Path(path)
        self.max_page_size = int(max_page_size)
        self._lock = threading.RLock()
        self._seq = 0
        self._last_hash = GENESIS
        self._content: dict[str, str] = {}
        self._loaded = False
        self.fault: str | None = None

    # ------------------------------------------------------------------ verification
    def verify(self) -> dict:
        """Full re-verification of the file. Never modifies it."""

        errors: list[str] = []
        records = 0
        last_hash = GENESIS
        seen: set[str] = set()
        if not self.path.exists():
            return {"ok": True, "records": 0, "errors": [], "last_hash": GENESIS, "exists": False}
        raw = self.path.read_bytes()
        if raw and not raw.endswith(b"\n"):
            errors.append("truncated_final_record")
        for lineno, line in enumerate(raw.decode("utf-8", errors="replace").split("\n"), 1):
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                errors.append(f"line {lineno}: malformed_json")
                continue
            if not isinstance(rec, dict) or any(k not in rec for k in self.REQUIRED):
                errors.append(f"line {lineno}: missing_required_fields")
                continue
            records += 1
            if rec["seq"] != records:
                errors.append(f"line {lineno}: seq_gap(expected {records}, found {rec['seq']})")
            if rec["prev_hash"] != last_hash:
                errors.append(f"line {lineno}: chain_broken")
            body = {k: v for k, v in rec.items() if k != "hash"}
            if _hash(rec["prev_hash"], body) != rec["hash"]:
                errors.append(f"line {lineno}: record_tampered")
            if rec["event_id"] in seen:
                errors.append(f"line {lineno}: duplicate_event_id")
            seen.add(rec["event_id"])
            last_hash = rec["hash"]
        return {"ok": not errors, "records": records, "errors": errors[:20], "last_hash": last_hash, "exists": True}

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        report = self.verify()
        if not report["ok"]:
            self.fault = "; ".join(report["errors"][:3])
            raise AuditCorrupt(self.fault)
        self._seq, self._last_hash = report["records"], report["last_hash"]
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if line:
                    rec = json.loads(line)
                    self._content[rec["event_id"]] = rec.get("content_hash", "")
        self._loaded = True

    # ------------------------------------------------------------------ append / read
    @property
    def healthy(self) -> bool:
        return self.fault is None

    def append(self, event: dict) -> dict:
        """Append one event. ``event`` needs ``event_id`` and ``event_type``; everything is scrubbed and serialized canonically."""

        if not isinstance(event.get("event_id"), str) or not event["event_id"] or not isinstance(event.get("event_type"), str) or not event["event_type"]:
            raise ValueError("an audit event needs event_id and event_type")
        with self._lock:
            if self.fault is not None:
                raise AuditCorrupt(self.fault)
            self._ensure_loaded()
            clean = scrub(event)
            content_hash = hashlib.sha256(canonical(clean).encode("utf-8")).hexdigest()
            if clean["event_id"] in self._content:
                if self._content[clean["event_id"]] == content_hash:
                    return self.get(clean["event_id"])  # idempotent replay
                raise AuditConflict(f"event_id {clean['event_id']!r} already stored with different content")
            body = {"seq": self._seq + 1, "prev_hash": self._last_hash, "recorded_utc": _utc_iso(), "content_hash": content_hash, **clean}
            record = {**body, "hash": _hash(self._last_hash, body)}
            line = (canonical(record) + "\n").encode("utf-8")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            try:
                with self.path.open("ab") as handle:
                    handle.write(line)
                    handle.flush()
                    os.fsync(handle.fileno())
            except OSError as exc:
                self.fault = f"write_failed:{type(exc).__name__}"
                raise AuditCorrupt(self.fault) from exc
            self._seq, self._last_hash = record["seq"], record["hash"]
            self._content[clean["event_id"]] = content_hash
            return record

    def get(self, event_id: str) -> dict | None:
        for rec in self._iter():
            if rec["event_id"] == event_id:
                return rec
        return None

    def _iter(self):
        if not self.path.exists():
            return
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if line:
                yield json.loads(line)

    def read(self, *, offset: int = 0, limit: int = 100, session_uid: str | None = None, event_type: str | None = None, descending: bool = True) -> dict:
        """Read-only paginated view (the UI/API route uses this; it never accepts a path)."""

        limit = max(1, min(int(limit), self.max_page_size))
        offset = max(0, int(offset))
        rows = [r for r in self._iter() if (session_uid is None or r.get("session_uid") == session_uid) and (event_type is None or r["event_type"] == event_type)]
        total = len(rows)
        if descending:
            rows = rows[::-1]
        return {"total": total, "offset": offset, "limit": limit, "events": rows[offset:offset + limit]}

    def summary(self) -> dict:
        """Aggregate evidence for readiness evaluators (read-only)."""

        sessions: dict[str, dict] = {}
        types: dict[str, int] = {}
        for rec in self._iter():
            types[rec["event_type"]] = types.get(rec["event_type"], 0) + 1
            uid = rec.get("session_uid")
            if uid:
                s = sessions.setdefault(uid, {"events": 0, "applied_fills": 0})
                s["events"] += 1
                if rec["event_type"] == "fill_settled" and (rec.get("settlement_result") or {}).get("outcome") == "APPLIED":
                    s["applied_fills"] += 1
        return {"event_types": types, "sessions": sessions}

    def last_events_by_order(self) -> dict[str, dict]:
        """order_id -> last audit record that mentions it (used by restart recovery to find non-terminal orders)."""

        last: dict[str, dict] = {}
        for rec in self._iter():
            oid = rec.get("order_id")
            if oid:
                last[f"{rec.get('session_uid')}:{oid}"] = rec
        return last


class DurableAuditSink:
    """Adapter from the in-memory ``AuditLog`` sink callback to the durable store. Never raises into the trading pipeline: a write failure puts
    the store in FAULT and the server refuses to continue (checked before every action), instead of failing half-way through an order."""

    def __init__(self, store: DurableAuditStore, *, session_id: str, session_uid: str, actor: str = "paper-session", source: str = "paper_session") -> None:
        self.store, self.session_id, self.session_uid, self.actor, self.source = store, session_id, session_uid, actor, source
        self._n = 0
        self._proposal_id: str | None = None
        self.errors = 0

    def __call__(self, record) -> None:
        self._n += 1
        payload = dict(record.payload)
        stage = record.stage
        if stage == "proposal_received":
            self._proposal_id = payload.get("proposal_id")
        event = {"event_id": f"{self.session_uid}:{self._n:06d}", "event_type": stage, "timestamp": record.timestamp.isoformat(), "session_id": self.session_id,
                 "session_uid": self.session_uid, "actor": self.actor, "source": self.source, "proposal_id": payload.get("proposal_id") or self._proposal_id,
                 "order_id": payload.get("order_id"), "payload": payload}
        if stage.endswith("_decision"):
            event["gate"] = {"stage": stage[: -len("_decision")], "status": payload.get("status"), "reasons": payload.get("reasons", [])}
        if stage in ("order_reserved", "order_submitted", "submission_failed", "reservation_failed"):
            event["state_transition"] = {"order_id": payload.get("order_id"), "event": stage, "status": payload.get("status")}
        if stage == "fill_settled":
            event["settlement_result"] = {"outcome": payload.get("outcome"), "fill_id": payload.get("fill_id"), "order_id": payload.get("order_id")}
        if stage in ("reservation_breach", "reservation_released"):
            event["risk_state"] = {"event": stage}
        try:
            self.store.append(event)
        except Exception:  # noqa: BLE001 - the store records its own FAULT; the server checks health before the next action
            self.errors += 1
            if self.store.fault is None:
                self.store.fault = "sink_write_failed"
