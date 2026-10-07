"""Recovery controls: persisted recovery state, restart assessment, and the explicit Emergency Flatten operation.

Principles (frozen policy: the Kill Switch does NOT imply automatic liquidation):
* flatten is NEVER automatic: it needs an explicit operator action (operator name, reason, the literal confirmation ``FLATTEN``)
* it only REDUCES exposure (SELL of an existing long, never more than held, never a flip) - classified from the server's Ledger
* it refuses to guess when the books cannot be trusted (integrity problems, reconciliation mismatch, non-finite values, a non-overrun
  breach): the state becomes ``RECOVERY_REQUIRED`` and a human must intervene; nothing is liquidated on a guess
* it is idempotent per session (a retry returns the recorded result and never creates a second set of orders)
* the Kill Switch stays engaged afterwards; the flatten uses its own authorization path, not the general order pipeline
* every step is audited and the state is persisted atomically so a restart can tell SAFE_RECOVERY from MANUAL_INTERVENTION_REQUIRED
"""

from __future__ import annotations

import datetime as dt
import math
import pathlib

from qat.core.exposure import reduces_exposure
from qat.core.models import Market, OrderType, Side, TradeProposal
from qat.core.recovery import RecoveryAuthorization, issue_recovery_authorization
from qat.ops.atomic import StateCorrupt, atomic_write_json, read_json_strict

NORMAL, RECOVERY_REQUIRED, FLATTEN_SUBMITTED, FLATTENED = "NORMAL", "RECOVERY_REQUIRED", "FLATTEN_SUBMITTED", "FLATTENED"
CONFIRM_TOKEN = "FLATTEN"
_EPS = 1e-9


class RecoveryError(RuntimeError):
    """The flatten was refused (nothing was sold). ``state`` carries the resulting recovery state."""

    def __init__(self, message: str, *, state: str = RECOVERY_REQUIRED, blockers: list[str] | None = None) -> None:
        super().__init__(message)
        self.state = state
        self.blockers = list(blockers or [])


def _utc_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


# ------------------------------------------------------------------ persisted state
class RecoveryStateStore:
    def __init__(self, path: pathlib.Path) -> None:
        self.path = pathlib.Path(path)

    def load(self) -> dict:
        data = read_json_strict(self.path, default=None)
        return data if data is not None else {"status": NORMAL, "session_uid": None, "history": [], "flatten": None}

    def save(self, data: dict) -> None:
        data["updated_utc"] = _utc_iso()
        atomic_write_json(self.path, data)

    def transition(self, status: str, **fields) -> dict:
        data = self.load()
        data["status"] = status
        data.update(fields)
        data["history"] = [*data.get("history", []), {"status": status, "utc": _utc_iso(), **{k: v for k, v in fields.items() if k in ("note", "operator", "reason")}}][-50:]
        self.save(data)
        return data


class SafetyStateStore:
    """A small persisted snapshot of the safety-relevant state of the running session. It is NOT a session restore (a Paper replay session
    restarts from scratch); it lets the next start tell whether the previous run ended in a state a human must look at."""

    def __init__(self, path: pathlib.Path) -> None:
        self.path = pathlib.Path(path)

    def update(self, *, session_uid: str, ledger, kill_switch: bool, pending_orders: list[str], recovery_status: str, clean_shutdown: bool = False) -> dict:
        recon = getattr(ledger, "last_reconciliation", None)
        snap = {"session_uid": session_uid, "updated_utc": _utc_iso(), "kill_switch": bool(kill_switch),
                "integrity_problems": ledger.integrity_problems(), "reservation_breaches": len(ledger.reservation_breaches),
                "reconciliation": recon, "pending_orders": list(pending_orders), "recovery_status": recovery_status,
                "open_positions": sum(1 for p in ledger.positions.values() if abs(p.quantity) > _EPS), "clean_shutdown": bool(clean_shutdown)}
        atomic_write_json(self.path, snap)
        return snap

    def mark_clean_shutdown(self) -> None:
        data = read_json_strict(self.path, default=None)
        if data is not None:
            data["clean_shutdown"] = True
            data["updated_utc"] = _utc_iso()
            atomic_write_json(self.path, data)

    def load(self):
        return read_json_strict(self.path, default=None)


def assess_restart(*, safety: SafetyStateStore, recovery: RecoveryStateStore, latches_active: list[dict], audit_verify: dict) -> dict:
    """After a (re)start: SAFE_RECOVERY or MANUAL_INTERVENTION_REQUIRED, with every reason. Never a silent reset."""

    reasons: list[str] = []
    notes: list[str] = []
    try:
        prev = safety.load()
    except StateCorrupt as exc:
        prev = None
        reasons.append(f"safety_state_corrupt:{exc}")
    try:
        rec = recovery.load()
    except StateCorrupt as exc:
        rec = {"status": NORMAL}
        reasons.append(f"recovery_state_corrupt:{exc}")
    if not audit_verify.get("ok", False):
        reasons.append("audit_log_corrupt:" + ";".join(audit_verify.get("errors", [])[:2]))
    if latches_active:
        reasons.append("safety_latch_active:" + ",".join(sorted({str(x.get('kind')) for x in latches_active})))
    if rec.get("status") in (RECOVERY_REQUIRED, FLATTEN_SUBMITTED):
        reasons.append(f"recovery_state:{rec['status']}")
    if prev is None and not reasons:
        return {"state": "FIRST_START", "reasons": [], "notes": ["no previous run recorded"], "previous": None}
    if prev is not None:
        if prev.get("integrity_problems"):
            reasons.append("previous_run_integrity_problems:" + ",".join(prev["integrity_problems"][:3]))
        if prev.get("reservation_breaches"):
            reasons.append(f"previous_run_reservation_breaches:{prev['reservation_breaches']}")
        recon = prev.get("reconciliation")
        if recon is not None and not recon.get("ok", True):
            reasons.append("previous_run_reconciliation_mismatch")
        if prev.get("pending_orders") and not prev.get("clean_shutdown"):
            reasons.append(f"order_state_unknown_after_restart:{len(prev['pending_orders'])}")
        if prev.get("kill_switch"):
            reasons.append("previous_run_kill_switch_engaged")
        if not prev.get("clean_shutdown"):
            notes.append("previous run did not shut down cleanly; the Paper replay session is NOT restored (it restarts from scratch)")
    return {"state": "MANUAL_INTERVENTION_REQUIRED" if reasons else "SAFE_RECOVERY", "reasons": reasons, "notes": notes, "previous": prev}


# ------------------------------------------------------------------ explicit emergency flatten
class RecoveryController:
    def __init__(self, stack, state: RecoveryStateStore, *, session_id: str, session_uid: str, audit_store=None) -> None:
        self.stack = stack
        self.state = state
        self.session_id = session_id
        self.session_uid = session_uid
        self.audit_store = audit_store
        self._seq = 0

    # authorization is minted here and nowhere else
    def _authorize(self, operator: str, reason: str, recovery_id: str) -> RecoveryAuthorization:
        return issue_recovery_authorization(operator, reason, recovery_id)

    def _event(self, event_type: str, **fields) -> None:
        if self.audit_store is None:
            return
        self._seq += 1
        self.audit_store.append({"event_id": f"{self.session_uid}:recovery:{self._seq:04d}", "event_type": event_type, "timestamp": _utc_iso(),
                                 "session_id": self.session_id, "session_uid": self.session_uid, "actor": fields.pop("operator", "system"), "source": "recovery_controller",
                                 "recovery_action": fields.pop("recovery_action", event_type), **fields})

    def assess(self) -> dict:
        """Blockers that forbid acting on the books (empty list = the books may be trusted)."""

        ledger = self.stack.ledger
        blockers: list[str] = []
        problems = ledger.integrity_problems()
        blockers += [f"accounting:{p}" for p in problems[:5]]
        recon = getattr(ledger, "last_reconciliation", None)
        if recon is not None and not recon.get("ok", True):
            blockers.append("reconciliation_mismatch")
        if recon is None and getattr(self.stack.risk, "require_reconciliation", False):
            blockers.append("reconciliation_not_performed")
        if any(b.get("kind") != "reservation_overrun" for b in ledger.reservation_breaches):
            blockers.append("reservation_breach_not_an_overrun")
        for key, pos in ledger.positions.items():
            if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in (pos.quantity, pos.avg_cost, pos.reserved_quantity)):
                blockers.append(f"position_state_unknown:{key[0]}:{key[1]}")
        positions = [{"market": k[0], "symbol": k[1], "quantity": p.quantity} for k, p in ledger.positions.items() if isinstance(p.quantity, (int, float)) and abs(p.quantity) > _EPS]
        return {"blockers": blockers, "flatten_allowed": not blockers, "positions": positions, "status": self.state.load().get("status", NORMAL)}

    def emergency_flatten(self, *, operator: str, reason: str, confirm: str, reference_price_fn, pending_order_ids: list[str]) -> dict:
        if not str(operator or "").strip() or not str(reason or "").strip():
            raise RecoveryError("operator and reason are required", state=self.state.load().get("status", NORMAL))
        if confirm != CONFIRM_TOKEN:
            raise RecoveryError(f"confirmation must be the literal {CONFIRM_TOKEN!r}", state=self.state.load().get("status", NORMAL))
        current = self.state.load()
        flat = current.get("flatten")
        if flat and flat.get("session_uid") == self.session_uid and flat.get("orders"):
            return {**flat, "idempotent_replay": True, "state": current["status"]}
        recovery_id = f"flatten-{self.session_uid}"
        self._event("recovery_requested", operator=operator, reason=reason, recovery_id=recovery_id, recovery_action="emergency_flatten")
        assessment = self.assess()
        if assessment["blockers"]:
            self.state.transition(RECOVERY_REQUIRED, note="flatten refused: " + ",".join(assessment["blockers"][:3]), operator=operator, reason=reason, session_uid=self.session_uid)
            self._event("recovery_blocked", operator=operator, blockers=assessment["blockers"], recovery_action="emergency_flatten")
            raise RecoveryError("recovery_required: the books cannot be trusted; nothing was liquidated (manual intervention required): " + ",".join(assessment["blockers"][:3]),
                                state=RECOVERY_REQUIRED, blockers=assessment["blockers"])
        auth = self._authorize(operator, reason, recovery_id)
        cancelled = []
        for order_id in list(pending_order_ids):
            order = self.stack.broker.orders.get(order_id)
            if order is not None and not order.is_terminal:
                self.stack.cancel_order(order_id)
                cancelled.append(order_id)
                self._event("recovery_order_cancelled", operator=operator, order_id=order_id, recovery_action="emergency_flatten")
        ledger = self.stack.ledger
        proposals = []
        now = self.stack.orchestrator._now() if self.stack.orchestrator._now else dt.datetime.now(dt.timezone.utc)
        for i, ((market_value, symbol), pos) in enumerate(sorted(ledger.positions.items()), 1):
            if pos.quantity <= _EPS:
                continue
            price = reference_price_fn(market_value, symbol)
            if price is None or not math.isfinite(price) or price <= 0:
                self.state.transition(RECOVERY_REQUIRED, note=f"no reference price for {market_value}:{symbol}", operator=operator, session_uid=self.session_uid)
                raise RecoveryError(f"recovery_required: no usable reference price for {market_value}:{symbol}; nothing was liquidated", blockers=["reference_price_unavailable"])
            proposal = TradeProposal(market=Market(market_value), symbol=symbol, side=Side.SELL, quantity=pos.available_quantity, order_type=OrderType.MARKET,
                                     limit_price=None, strategy_id="recovery-flatten", reason_code="emergency_flatten", expected_gross_return=0.0, confidence=0.5,
                                     created_at=now, signal_timestamp=now, proposal_id=f"{self.session_id}-RECOVERY-{recovery_id}-{i}",
                                     feature_snapshot_id=f"recovery@{self.session_uid}")
            if proposal.quantity <= _EPS or not reduces_exposure(ledger, proposal):
                self.state.transition(RECOVERY_REQUIRED, note=f"position {market_value}:{symbol} cannot be reduced legally", operator=operator, session_uid=self.session_uid)
                raise RecoveryError(f"recovery_required: {market_value}:{symbol} cannot be reduced by a legal exposure-reducing order", blockers=["not_reducible"])
            proposals.append((proposal, price))
        orders, refused = [], []
        for proposal, price in proposals:
            result = self.stack.orchestrator.submit_recovery_order(proposal, price, auth)
            if result.accepted:
                orders.append({"order_id": result.order_id, "symbol": proposal.symbol, "quantity": proposal.quantity, "proposal_id": proposal.proposal_id})
            else:
                refused.append({"symbol": proposal.symbol, "reason": result.reason})
        record = {"session_uid": self.session_uid, "recovery_id": recovery_id, "operator": operator, "reason": reason, "requested_utc": _utc_iso(),
                  "cancelled_orders": cancelled, "orders": orders, "refused": refused}
        status = FLATTEN_SUBMITTED if orders or not proposals else RECOVERY_REQUIRED
        if refused and not orders:
            status = RECOVERY_REQUIRED
        if not proposals:
            status = FLATTENED  # nothing to reduce
        self.state.transition(status, flatten=record, session_uid=self.session_uid, operator=operator, reason=reason, note="emergency flatten operator action")
        self._event("recovery_flatten_submitted", operator=operator, orders=orders, refused=refused, cancelled=cancelled, recovery_action="emergency_flatten", state=status)
        return {**record, "state": status, "kill_switch_unchanged": True}

    def refresh(self) -> str:
        """After fills: FLATTEN_SUBMITTED -> FLATTENED once no position remains."""

        data = self.state.load()
        if data.get("status") == FLATTEN_SUBMITTED and not any(abs(p.quantity) > _EPS for p in self.stack.ledger.positions.values()):
            self.state.transition(FLATTENED, note="all positions are flat", session_uid=self.session_uid)
            self._event("recovery_flatten_completed", recovery_action="emergency_flatten", state=FLATTENED)
            return FLATTENED
        return data.get("status", NORMAL)
