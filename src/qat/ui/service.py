"""UI service layer (Batch #2 UI) - the only thing the HTTP server talks to.

Everything shown in the UI is read from the real objects: the paper session's
``PortfolioLedger`` / broker orders / audit log, the dataset validation reports
and the saved research runs. Nothing is mocked.

Paper session = server-side DATASET REPLAY. There is no real-time market data
feed (no Market Recorder yet), so a paper session replays a local dataset: the
reference price is the close of the current replay bar and accepted orders fill
at the next bar's open when the user advances the replay. The client can only
send a proposal's intent (side, quantity, order type, limit, claimed expected
return, request id); price, approval and status are always decided server-side
by the unchanged Gateway -> gates -> settlement pipeline.

Safety latches (``state/safety_latches.json``): engaging the kill switch or a
reservation breach writes a persistent latch. While any latch is active every
paper session - including a new session after reset or a server restart - runs
with the Risk kill switch engaged. Latches cannot be cleared from the UI; see
``python -m qat.ui clear-latch``. The latch file is written atomically; a corrupt
latch file is quarantined and replaced by a STATE_CORRUPT latch (never a silent reset).

Final completion: a durable hash-chained audit log (``state/audit_log.jsonl``), a persisted
recovery / safety state, a startup self-check that fails closed, the explicit operator-only
Emergency Flatten and the operations / audit / recovery views all live here. The server is
SINGLE-PROCESS: two server processes sharing one state directory are not supported.
"""

from __future__ import annotations

import json
import math
import os
import pathlib
import re
import secrets
import threading
import time
from dataclasses import asdict
from datetime import datetime, timezone

from qat.app import build_paper_stack
from qat.config import (
    cost_models_from_settings,
    enabled_markets_from_settings,
    fx_provider_from_settings,
    load_research_settings,
)
from qat.core.models import Currency, DomainError, OrderType, Side, TradeProposal
from qat.ops import market_rules as _mr
from qat.ops.atomic import StateCorrupt, atomic_write_json, read_json_strict
from qat.ops.audit_store import AuditConflict, AuditCorrupt, DurableAuditSink, DurableAuditStore
from qat.ops.config import OpsConfigError, load_ops_config, rule_states
from qat.ops.graduation import evaluate_live_readiness, evaluate_paper_graduation
from qat.ops.health import build_health
from qat.ops.recovery import RecoveryController, RecoveryError, RecoveryStateStore, SafetyStateStore, assess_restart
from qat.ops.startup import research_identity_check, run_startup_check
from qat.ops.strategy_health import StrategyHealthMonitor
from qat.data.bars import DataRejected
from qat.data.loader import discover_datasets, load_dataset
from qat.realdata.admission import check_admission, is_real, require_admitted, require_coverage
from qat.research.backtest import BacktestConfig, DeterministicIds, SimClock, run_backtest
from qat.research.manifest import PROJECT_ROOT, code_identity
from qat.research.store import list_runs, load_run, save_backtest
from qat.research.strategies import STRATEGIES, make_strategy
from qat.research.walkforward import WalkForwardConfig, run_cost_stress, run_walkforward

_EPS = 1e-9
DATA_DIRS = ("data/fixtures", "data/raw", "data/processed")
PROPOSAL_FIELDS = {"side", "quantity", "order_type", "limit_price", "expected_gross_return",
                   "reason_code", "client_request_id"}
MAX_REQUEST_CACHE = 500

# Batch #2.2 (Control Tower): how each Risk rule treats an EXPOSURE-REDUCING SELL
# (Ledger-derived, never client-claimed). Shown on the Risk screen.
EXIT_POLICY = [
    {"rule": "Kill Switch", "reducing_sell": "BLOCK",
     "note": "top-level hard stop; never triggers automatic liquidation. Flatten exists only as an explicit operator action (Recovery screen)"},
    {"rule": "Daily Loss", "reducing_sell": "ALLOWED", "note": "risk-increasing orders stay BLOCKED; UNKNOWN (FX/NaN) stays UNKNOWN"},
    {"rule": "Drawdown", "reducing_sell": "ALLOWED", "note": "unknown baseline / FX / non-finite stays UNKNOWN"},
    {"rule": "Absolute exposure limit", "reducing_sell": "ALLOWED", "note": "BUY / exposure increase stays BLOCKED"},
    {"rule": "Reservation breach", "reducing_sell": "CONDITIONAL",
     "note": "allowed only if it is a plain overrun AND the ledger is internally consistent AND reconciliation shows no mismatch AND valuation is possible"},
    {"rule": "Order size / notional", "reducing_sell": "BLOCK when over the limit",
     "note": "no automatic splitting; smaller reducing orders pass"},
    {"rule": "Compliance / Market Integrity / Settlement", "reducing_sell": "APPLY", "note": "never bypassed"},
    {"rule": "Net Alpha", "reducing_sell": "EXEMPT (OD-01)", "note": "threshold only"},
]


class ServiceError(ValueError):
    """A client error (HTTP 400) with a user-facing message."""


class ConflictError(RuntimeError):
    """A refused action (HTTP 409), e.g. reset while a safety latch is active."""


def _to_float(value, name: str) -> float:
    """Client number -> finite float. Rejects bool (``true`` is not a quantity),
    NaN/Infinity and non-numeric text (Audit fix D2)."""

    if isinstance(value, bool):
        raise ServiceError(f"{name} must be a number, not a boolean")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ServiceError(f"{name} must be a number, got {value!r}") from exc
    if not math.isfinite(number):
        raise ServiceError(f"{name} must be finite")
    return number


def _to_int(value, name: str) -> int:
    number = _to_float(value, name)
    if number != int(number):
        raise ServiceError(f"{name} must be an integer, got {value!r}")
    return int(number)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _state_dir() -> pathlib.Path:
    path = pathlib.Path(os.environ.get("QAT_STATE_DIR") or (PROJECT_ROOT / "state"))
    path.mkdir(parents=True, exist_ok=True)
    return path


# --------------------------------------------------------------------- latches
class SafetyLatches:
    def __init__(self) -> None:
        self.path = _state_dir() / "safety_latches.json"
        self._lock = threading.Lock()

    def _quarantine(self, exc: Exception) -> dict:
        """A latch file that cannot be trusted is moved aside and replaced by a STATE_CORRUPT latch: the kill switch stays engaged and an
        operator must clear it offline. It is never treated as "no latches"."""

        target = self.path.with_name(f"{self.path.name}.corrupt-{int(time.time() * 1000)}")
        try:
            os.replace(self.path, target)
        except OSError:
            pass
        entry = {"id": "STATE_CORRUPT-1", "kind": "STATE_CORRUPT",
                 "reason": f"safety latch file was unreadable and was quarantined ({type(exc).__name__}): kill switch stays engaged",
                 "engaged_utc": _now_iso(), "details": {"quarantined_as": target.name}}
        data = {"active": [entry], "cleared": []}
        atomic_write_json(self.path, data)
        return data

    def _read(self) -> dict:
        try:
            data = read_json_strict(self.path, default=None)
        except StateCorrupt as exc:
            return self._quarantine(exc)
        if data is None:
            return {"active": [], "cleared": []}
        if not isinstance(data.get("active"), list) or not isinstance(data.get("cleared"), list):
            return self._quarantine(StateCorrupt("latch file has the wrong shape"))
        return data

    def active(self) -> list[dict]:
        with self._lock:
            return list(self._read()["active"])

    def status(self) -> dict:
        """For the startup self-check: ``ok`` is False while a STATE_CORRUPT latch is active."""

        with self._lock:
            active = list(self._read()["active"])
        corrupt = [e for e in active if e.get("kind") == "STATE_CORRUPT"]
        return {"ok": not corrupt, "error": corrupt[0]["reason"] if corrupt else None, "active": active}

    def engage(self, kind: str, reason: str, details: dict | None = None) -> dict:
        with self._lock:
            data = self._read()
            entry = {"id": f"{kind}-{len(data['active']) + len(data['cleared']) + 1}", "kind": kind,
                     "reason": reason, "engaged_utc": _now_iso(), "details": details or {}}
            data["active"].append(entry)
            atomic_write_json(self.path, data)
            return entry

    def clear(self, latch_id: str, approver: str, note: str) -> dict:
        """Offline administrative action (CLI only). Requires approver + note."""

        if not approver.strip() or not note.strip():
            raise ServiceError("approver and note are required to clear a latch")
        with self._lock:
            data = self._read()
            match = [e for e in data["active"] if e["id"] == latch_id]
            if not match:
                raise ServiceError(f"no active latch {latch_id}")
            data["active"] = [e for e in data["active"] if e["id"] != latch_id]
            cleared = {**match[0], "cleared_utc": _now_iso(), "approver": approver, "note": note}
            data["cleared"].append(cleared)
            atomic_write_json(self.path, data)
            return cleared


# --------------------------------------------------------------- paper session
class PaperSession:
    def __init__(self, dataset, settings: dict, *, initial_cash: float, start_bar: int,
                 session_no: int, latched: bool, ops: dict, audit_store, recovery_store) -> None:
        if not dataset.usable:
            raise ConflictError("dataset failed validation - paper replay BLOCKED")
        try:  # Batch #3A: real datasets must be admitted (provenance + PASS validation); domain rejection -> 409
            require_admitted(dataset)
        except DataRejected as exc:
            raise ConflictError(str(exc)) from exc
        if not 0 <= start_bar < len(dataset.bars) - 1:
            raise ServiceError(f"start_bar must be within 0..{len(dataset.bars) - 2}")
        self.dataset = dataset
        self.meta = dataset.meta
        self.session_id = f"S{session_no}"
        self.session_uid = f"{self.session_id}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}-{secrets.token_hex(3)}"
        self.ops = ops
        self.currency = Currency(self.meta.currency)
        self.key = (self.meta.market, self.meta.symbol)
        self.initial_cash = float(initial_cash)
        self.t = start_bar
        self.start_bar = start_bar
        self.clock = SimClock(dataset.bars[start_bar].ts + self.meta.duration)
        self.marks = {self.key: dataset.bars[start_bar].close}
        self.fx = fx_provider_from_settings(settings)
        self.base = Currency(settings["base_currency"])
        counter = DeterministicIds()
        models = cost_models_from_settings(settings)
        risk_cfg = {k: v for k, v in (settings.get("risk") or {}).items() if v is not None}
        rules = ops["market_rules"]
        snap = ops["snapshot"]
        self.audit_sink = DurableAuditSink(audit_store, session_id=self.session_id, session_uid=self.session_uid, actor="ui-session")
        self.stack = build_paper_stack(
            starting_cash={self.currency: self.initial_cash},
            base_currency=self.base, fx_provider=self.fx, mode="PAPER",
            cost_models=models, broker_cost_models=models,
            min_net_alpha_bps=float((settings.get("net_alpha") or {}).get("min_net_alpha_bps", 0.0)),
            reservation_execution_buffer=0.02,
            risk_kwargs={**risk_cfg, "kill_switch": latched, "mark_prices_fn": lambda: dict(self.marks),
                         "snapshot_policy": snap["policy"], "max_snapshot_age_seconds": snap["max_age_seconds"],
                         "future_tolerance_seconds": snap["future_tolerance_seconds"],
                         "market_context_fn": self._market_context, "min_bar_volume": rules["min_bar_volume"],
                         "max_spread_bps": rules["max_spread_bps"],
                         "volatility_shock_range_pct": rules["volatility_shock_range_pct"]},
            integrity_kwargs={"max_cancels_per_window": rules["max_cancels_per_window"],
                              "cancel_window_seconds": rules["cancel_window_seconds"],
                              "max_participation_rate": rules["max_participation_rate"],
                              "market_context_fn": self._market_context},
            enabled_markets=enabled_markets_from_settings(settings),
            # OD-02: manual / Paper proposals are admitted only by the explicit
            # paper_universe allowlist from settings. Research datasets never add to
            # it; a missing allowlist means every proposal is UNKNOWN (fail-closed).
            compliance_kwargs={
                "tradable_symbols": (set(settings["paper_universe"])
                                     if settings.get("paper_universe") is not None else None),
                "unrestricted_universe": False,
                "universe_label": "paper_allowlist",
            },
            now_fn=self.clock.now,
            id_fn=lambda prefix: f"{self.session_id}-{counter(prefix)}",
            audit_sink=self.audit_sink,
        )
        self.created_utc = _now_iso()
        self.recovery = RecoveryController(self.stack, recovery_store, session_id=self.session_id, session_uid=self.session_uid,
                                           audit_store=audit_store)
        self.health = StrategyHealthMonitor("ui-manual", thresholds=ops["strategy_health"], now_fn=self.clock.now)
        self.health.mark_initialized()
        self.health.record_data(dataset.bars[start_bar].ts)
        # Audit fix (D4/D5): the Paper session must drive the same risk hooks the
        # backtest does, otherwise a configured drawdown / daily-loss limit is inert.
        self._prev_day = dataset.bars[start_bar].ts.date()
        self.stack.risk.start_new_day()
        self.pending: list[str] = []
        self.order_rows: dict[str, dict] = {}
        self.rejections: list[dict] = []
        self.fills: list[dict] = []
        self.events: list[dict] = []
        self.equity: list[dict] = []
        self.requests: dict[str, dict] = {}
        self._request_seq = 0  # Audit fix (D9): monotonic, never derived from cache size
        self._seen_request_ids: set[str] = set()
        self._record_equity()

    # ------------------------------------------------------------ helpers
    @property
    def bar(self):
        return self.dataset.bars[self.t]

    def _market_context(self, proposal=None):
        """R-07/R-08/R-09 + MI-06 input: the reference bar of the validated dataset. OHLCV bars carry no quotes -> spread_bps stays None
        (R-08 is UNKNOWN while configured: never a silent PASS)."""

        bar = self.bar
        return _mr.MarketContext(volume=bar.volume, high=bar.high, low=bar.low, close=bar.close, spread_bps=None,
                                 as_of=self.as_of(), source=str(self.meta.source))

    def as_of(self) -> str:
        return (self.bar.ts + self.meta.duration).isoformat()

    def valuation(self):
        return self.stack.ledger.valuation(self.marks, self.fx, self.base)

    def _record_equity(self) -> None:
        val = self.valuation()
        row = val.by_currency.get(self.currency, {})
        self.equity.append({"bar": self.t, "timestamp": self.as_of(), "equity": row.get("equity"),
                            "equity_base": val.total_base})
        if val.total_base is not None:
            self.stack.risk.observe_equity(val.total_base)

    def _order_view(self, order_id: str) -> dict:
        order = self.stack.broker.orders.get(order_id)
        row = dict(self.order_rows[order_id])
        if order is not None:
            res = order.reservation
            row.update({
                "status": order.status.value,
                "status_history": [s.value for s in order.status_history] + [order.status.value],
                "filled_quantity": order.filled_quantity,
                "remaining_quantity": order.remaining_quantity,
                "avg_fill_price": order.avg_fill_price,
                "reservation": asdict(res) if res else None,
                "reservation_remaining": (res.cash_remaining if res and res.kind == "CASH"
                                          else res.quantity_remaining if res else None),
            })
        return row

    # ------------------------------------------------------------ actions
    def submit(self, payload: dict) -> dict:
        unknown = set(payload) - PROPOSAL_FIELDS
        if unknown:
            raise ServiceError(f"fields not accepted from the client: {sorted(unknown)} "
                               "(price, approval and status are decided server-side)")
        request_id = str(payload.get("client_request_id") or "").strip()
        if not request_id or len(request_id) > 80:
            raise ServiceError("client_request_id (1..80 chars) is required for idempotency")
        if request_id in self._seen_request_ids:
            cached = self.requests.get(request_id)
            if cached is not None:
                return {**cached, "idempotent_replay": True}
            # payload evicted from the bounded cache: still never re-execute
            return {"accepted": None, "stage": None, "idempotent_replay": True,
                    "reason": "duplicate client_request_id (original result no longer cached)",
                    "decisions": [], "reference_price": self.bar.close, "as_of": self.as_of()}
        try:
            side = Side(str(payload.get("side", "")).upper())
            order_type = OrderType(str(payload.get("order_type", "MARKET")).upper())
            quantity = _to_float(payload.get("quantity"), "quantity")
            limit = payload.get("limit_price")
            limit = _to_float(limit, "limit_price") if limit not in (None, "") else None
            expected = _to_float(payload.get("expected_gross_return", 0.0), "expected_gross_return")
            proposal = TradeProposal(
                market=self.meta.market_enum, symbol=self.meta.symbol, side=side,
                quantity=quantity, order_type=order_type, limit_price=limit,
                strategy_id="ui-manual", reason_code=str(payload.get("reason_code") or "ui-manual")[:60],
                expected_gross_return=expected, confidence=0.5,
                created_at=self.clock.now(), signal_timestamp=self.clock.now(),
                proposal_id=f"{self.session_id}-UI-{self._request_seq + 1:05d}",
                feature_snapshot_id=f"{self.dataset.data_version}@bar{self.t}",
            )
        except (KeyError, TypeError, ValueError, DomainError) as exc:
            raise ServiceError(f"invalid proposal: {exc}") from exc

        self._request_seq += 1
        self._seen_request_ids.add(request_id)
        audit = self.stack.audit.records
        before = len(audit)
        self.health.record_signal(self.clock.now())
        try:
            result = self.stack.submit_trade_proposal(proposal, reference_price=self.bar.close)
        except Exception as exc:  # noqa: BLE001 - observed for health, then re-raised
            self.health.record_evaluation(ok=False, error=type(exc).__name__, exception=True)
            raise
        self.health.record_evaluation(ok=True)
        self.health.record_proposal(accepted=result.accepted, reason=None if result.accepted else ":".join(result.reason.split(":")[:3]))
        decisions = [
            {"stage": r.stage.replace("_decision", ""), "status": r.payload.get("status"),
             "reasons": r.payload.get("reasons", [])}
            for r in audit[before:] if r.stage.endswith("_decision")
        ]
        response = {"accepted": result.accepted, "stage": result.stage, "reason": result.reason,
                    "decisions": decisions, "reference_price": self.bar.close, "as_of": self.as_of(),
                    "expected_return_source": "USER_CLAIM (manual input, not an estimate)"}
        if result.accepted:
            self.pending.append(result.order_id)
            self.order_rows[result.order_id] = {
                "order_id": result.order_id, "proposal_id": proposal.proposal_id,
                "side": side.value, "quantity": quantity, "order_type": order_type.value,
                "limit_price": limit, "reference_price": self.bar.close, "submitted_as_of": self.as_of(),
                "client_request_id": request_id,
            }
            response["order"] = self._order_view(result.order_id)
        else:
            self.rejections.append({"as_of": self.as_of(), "side": side.value, "quantity": quantity,
                                    "stage": result.stage, "reason": result.reason,
                                    "decisions": decisions, "client_request_id": request_id})
        if len(self.requests) >= MAX_REQUEST_CACHE:
            self.requests.pop(next(iter(self.requests)))
        self.requests[request_id] = response
        return response

    def cancel(self, order_id: str) -> dict:
        order = self.stack.broker.orders.get(order_id)
        if order is None:
            raise ServiceError(f"unknown order {order_id}")
        if order.is_terminal:
            raise ConflictError(f"order already {order.status.value}")
        self.stack.cancel_order(order_id)
        self.pending = [o for o in self.pending if o != order_id]
        self.events.append({"as_of": self.as_of(), "event": "cancelled", "order_id": order_id})
        return self._order_view(order_id)

    def advance(self, bars: int = 1) -> dict:
        if not 1 <= bars <= 250:
            raise ServiceError("bars must be 1..250")
        advanced = 0
        breaches_before = len(self.stack.ledger.reservation_breaches)
        for _ in range(bars):
            if self.t >= len(self.dataset.bars) - 1:
                break
            self.t += 1
            bar = self.bar
            self.clock.set(bar.ts)
            if bar.ts.date() != self._prev_day:
                self.stack.risk.start_new_day()
                self._prev_day = bar.ts.date()
            self.marks[self.key] = bar.open
            self.health.record_data(bar.ts)
            self.health.record_evaluation(ok=True)
            for order_id in self.pending:
                fill = self.stack.broker.simulate_fill(order_id, bar.open)
                if fill is None:
                    self.stack.cancel_order(order_id)
                    self.events.append({"as_of": bar.ts.isoformat(), "event": "expired_unfilled",
                                        "order_id": order_id})
                    continue
                outcome = self.stack.settle(fill)
                self.fills.append({"fill_id": fill.fill_id, "order_id": order_id, "bar": self.t,
                                   "timestamp": fill.timestamp.isoformat(), "side": fill.side.value,
                                   "quantity": fill.quantity, "price": fill.price,
                                   "reference_price": bar.open, "commission": fill.commission,
                                   "tax": fill.tax, "exchange_fee": fill.exchange_fee,
                                   "slippage_estimate": fill.slippage_estimate, "outcome": outcome.value})
            self.pending = []
            self.clock.set(bar.ts + self.meta.duration)
            self.marks[self.key] = bar.close
            self._record_equity()
            advanced += 1
        new_breaches = self.stack.ledger.reservation_breaches[breaches_before:]
        return {"advanced": advanced, "as_of": self.as_of(), "bar": self.t,
                "end_of_data": self.t >= len(self.dataset.bars) - 1, "new_breaches": new_breaches}


# --------------------------------------------------------------------- service
RESEARCH_CLOSURE_PATH = PROJECT_ROOT / "artifacts" / "verification" / "protocol_v2" / "protocol_v2_closure.json"
_AUDIT_FILTER_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")


def _research_label() -> str | None:
    """Read-only: the recorded Protocol v2 closure label (historical development metadata; no 2026 value is involved)."""

    try:
        return json.loads(RESEARCH_CLOSURE_PATH.read_text(encoding="utf-8")).get("final_label")
    except (OSError, ValueError):
        return None


def acknowledge_recovery(approver: str, note: str) -> dict:
    """Offline administrative action (CLI only): an operator acknowledges a RECOVERY_REQUIRED / FLATTEN_SUBMITTED / MANUAL_INTERVENTION state.
    Requires a named approver and a note; the transition is recorded in the recovery history and the durable audit log. Never a silent reset."""

    if not str(approver or "").strip() or not str(note or "").strip():
        raise ServiceError("approver and note are required")
    state_dir = _state_dir()
    store = RecoveryStateStore(state_dir / "recovery_state.json")
    before = store.load()
    after = store.transition("NORMAL", note=note, operator=approver, reason=note,
                             restart_ack={"utc": _now_iso(), "approver": approver, "note": note, "from_status": before.get("status")})
    try:
        audit = DurableAuditStore(state_dir / "audit_log.jsonl")
        audit.append({"event_id": f"ack-{secrets.token_hex(6)}", "event_type": "recovery_acknowledged", "timestamp": _now_iso(), "session_id": None,
                      "session_uid": None, "actor": approver, "source": "cli", "from_status": before.get("status"), "note": note})
    except Exception:  # noqa: BLE001 - the recovery history above already holds the record
        pass
    return {"from": before.get("status"), "to": after["status"], "approver": approver}


class QATService:
    def __init__(self, settings_path: str | None = None, *, ops_config_path: str | None = None,
                 research_check=research_identity_check) -> None:
        self.settings_path = settings_path
        self._lock = threading.RLock()
        self._research_lock = threading.Lock()
        self.latches = SafetyLatches()
        self.settings_error: str | None = None
        try:
            self.settings = load_research_settings(settings_path)
        except Exception as exc:  # noqa: BLE001 - surfaced in the UI, trading disabled
            self.settings = None
            self.settings_error = f"{type(exc).__name__}: {exc}"
        self._datasets: dict[str, tuple[float, object]] = {}
        self._session_no = 0
        self.session: PaperSession | None = None
        # ---- final completion: operations state (single process; see the runbook)
        self.state_dir = _state_dir()
        self.ops_config_path = ops_config_path
        self.ops_error: str | None = None
        try:
            self.ops: dict | None = load_ops_config(ops_config_path)
        except OpsConfigError as exc:
            self.ops, self.ops_error = None, str(exc)
        self.audit_store = DurableAuditStore(self.state_dir / "audit_log.jsonl",
                                             max_page_size=self.ops["audit"]["max_page_size"] if self.ops else 500)
        self.recovery_store = RecoveryStateStore(self.state_dir / "recovery_state.json")
        self.safety_store = SafetyStateStore(self.state_dir / "safety_state.json")
        self.state_fault: str | None = None
        self.started_utc = _now_iso()
        self._boot_id = secrets.token_hex(4)
        self._event_seq = 0
        self._research_check = research_check
        self.startup = run_startup_check(
            state_dir=self.state_dir, ops_config_path=ops_config_path, settings=self.settings, settings_error=self.settings_error,
            latch_status=self.latches.status, audit_store=self.audit_store, research_check=research_check)
        self.restart = self._assess_restart()
        self._audit_event("service_started", startup=self.startup["status"], restart=self.restart["state"],
                          restart_reasons=self.restart["reasons"])

    # ------------------------------------------------------------ operations plumbing
    def _assess_restart(self) -> dict:
        try:
            verify = self.audit_store.verify()
        except OSError as exc:
            verify = {"ok": False, "errors": [f"audit_unreadable:{type(exc).__name__}"]}
        result = assess_restart(safety=self.safety_store, recovery=self.recovery_store, latches_active=self.latches.active(), audit_verify=verify)
        result.pop("previous", None)  # internal detail; the reasons already say what it contained
        return result

    def _audit_event(self, event_type: str, **fields) -> bool:
        """Service-level audit event. NEVER raises: the Kill Switch and the recovery paths must work even when the audit store is in FAULT."""

        try:
            self._event_seq += 1
            s = self.session
            self.audit_store.append({"event_id": f"svc-{self._boot_id}:{self._event_seq:05d}", "event_type": event_type,
                                     "timestamp": _now_iso(), "session_id": s.session_id if s else None,
                                     "session_uid": s.session_uid if s else None, "actor": fields.pop("actor", "system"),
                                     "source": "qat_service", **fields})
            return True
        except Exception:  # noqa: BLE001
            return False

    def _recovery_state(self) -> dict:
        try:
            return self.recovery_store.load()
        except StateCorrupt as exc:
            return {"status": "RECOVERY_REQUIRED", "error": f"recovery state corrupt: {exc}", "history": [], "flatten": None}

    def _operational_gate(self, action: str, *, opens_risk: bool = True) -> None:
        """Fail-closed preconditions for trading actions. The Kill Switch route does NOT use this: it must always work."""

        if self.startup["status"] == "BLOCK":
            raise ConflictError(f"startup self-check BLOCK ({', '.join(self.startup['blocked'])}): {action} refused (fail-closed)")
        if self.ops is None:
            raise ConflictError(f"ops configuration invalid ({self.ops_error}): {action} refused (fail-closed)")
        if not self.audit_store.healthy:
            raise ConflictError(f"durable audit log is in FAULT ({self.audit_store.fault}): {action} refused (fail-closed)")
        if self.state_fault:
            raise ConflictError(f"state persistence fault ({self.state_fault}): {action} refused (fail-closed)")
        if opens_risk:
            rec = self._recovery_state()
            if rec.get("status") in ("RECOVERY_REQUIRED", "FLATTEN_SUBMITTED"):
                raise ConflictError(f"recovery state {rec['status']}: {action} refused until an operator resolves it "
                                    "(offline: python -m qat.ui ack-recovery --approver NAME --note TEXT)")

    def _persist_safety(self, *, clean: bool = False) -> None:
        s = self.session
        if s is None:
            return
        try:
            self.safety_store.update(session_uid=s.session_uid, ledger=s.stack.ledger, kill_switch=s.stack.risk.kill_switch,
                                     pending_orders=list(s.pending), recovery_status=self._recovery_state().get("status", "NORMAL"),
                                     clean_shutdown=clean)
        except Exception as exc:  # noqa: BLE001 - fail closed on the next trading action
            self.state_fault = f"{type(exc).__name__}: {exc}"

    def close(self) -> None:
        """Clean shutdown marker: lets the next start tell a clean stop from a crash."""

        with self._lock:
            self._audit_event("service_stopped")
            try:
                self.safety_store.mark_clean_shutdown()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------ datasets
    def _dataset_ids(self) -> list[str]:
        roots = [PROJECT_ROOT / d for d in DATA_DIRS]
        return [p.relative_to(PROJECT_ROOT).as_posix() for p in discover_datasets(*roots)]

    def dataset(self, dataset_id: str):
        if dataset_id not in self._dataset_ids():
            raise ServiceError(f"unknown dataset {dataset_id!r}")
        path = PROJECT_ROOT / dataset_id
        mtime = path.stat().st_mtime
        cached = self._datasets.get(dataset_id)
        if cached is None or cached[0] != mtime:
            cached = (mtime, load_dataset(path))
            self._datasets[dataset_id] = cached
        return cached[1]

    def list_datasets(self) -> list[dict]:
        out = []
        for dataset_id in self._dataset_ids():
            try:
                ds = self.dataset(dataset_id)
                admission = check_admission(ds)
                out.append({"id": dataset_id, **ds.summary(), "bars": len(ds.bars), "real_data": is_real(ds.meta),
                            "admission": {"applicable": admission["applicable"], "admitted": admission["admitted"],
                                          "reasons": admission["reasons"]},
                            "issues": ds.validation.issues, "gap_summary": ds.validation.gap_summary,
                            "calendar_note": ds.validation.calendar_note})
            except Exception as exc:  # noqa: BLE001 - unreadable dataset shown as an error row
                out.append({"id": dataset_id, "error": f"{type(exc).__name__}: {exc}",
                            "validation_status": "FAIL"})
        return out

    # ------------------------------------------------------------ status
    def _require_settings(self) -> dict:
        if self.settings is None:
            raise ConflictError(f"research settings unavailable: {self.settings_error}")
        return self.settings

    def status(self) -> dict:
        s = self.session
        return {
            "app": "QAT", "server_time_utc": _now_iso(),
            "execution_mode": "PAPER (dataset replay)" if s else "NO SESSION",
            "live": {"status": "BLOCKED", "reason": "Live execution is not implemented (D-013); "
                     "ExecutionRouter LIVE raises and LiveBrokerStub refuses every call"},
            "shadow": "NOT_IMPLEMENTED (router mode exists; no market-connected shadow system)",
            "strategy_health": s.health.evaluate().status if s else "UNKNOWN",
            "startup": self.startup["status"], "recovery_status": self._recovery_state().get("status", "NORMAL"),
            "audit": {"healthy": self.audit_store.healthy, "fault": self.audit_store.fault},
            "net_alpha": "UNKNOWN",
            "settings_ok": self.settings is not None, "settings_error": self.settings_error,
            "latches": self.latches.active(),
            "session": None if s is None else {
                "id": s.session_id, "dataset_id": self._dataset_id_of(s.dataset),
                "symbol": s.meta.symbol, "market": s.meta.market, "currency": s.currency.value,
                "synthetic": s.meta.synthetic, "source": s.meta.source, "as_of": s.as_of(),
                "bar": s.t, "bars_total": len(s.dataset.bars), "created_utc": s.created_utc,
                "data_freshness": "HISTORICAL_REPLAY",
                "stale": True, "stale_reason": "historical replay - not live market data",
            },
        }

    def _dataset_id_of(self, dataset) -> str:
        try:
            return pathlib.Path(dataset.path).relative_to(PROJECT_ROOT).as_posix()
        except ValueError:
            return dataset.path

    # ------------------------------------------------------------ session
    def start_session(self, payload: dict) -> dict:
        with self._lock:
            settings = self._require_settings()
            self._operational_gate("start paper session")
            if self.session is not None and self.session.stack.ledger.reservation_breaches:
                raise ConflictError("current session has an unresolved reservation breach; "
                                    "a new session cannot replace it (design 5.3)")
            ds = self.dataset(str(payload.get("dataset_id", "")))
            cash = _to_float(payload.get("initial_cash", 10_000_000), "initial_cash")
            start_bar = _to_int(payload.get("start_bar", 60), "start_bar")
            if not (0 < cash < 1e15):
                raise ServiceError("initial_cash must be > 0 and < 1e15")
            self._session_no += 1
            new = PaperSession(ds, settings, initial_cash=cash, start_bar=start_bar, session_no=self._session_no,
                               latched=bool(self.latches.active()), ops=self.ops, audit_store=self.audit_store,
                               recovery_store=self.recovery_store)
            previous, self.session = self.session, new
            if self._recovery_state().get("status") == "FLATTENED":
                self.recovery_store.transition("NORMAL", note="new paper session started after a completed flatten", session_uid=new.session_uid)
            self._audit_event("session_started", dataset_id=self._dataset_id_of(ds), initial_cash=cash, start_bar=start_bar,
                              replaced_session=previous.session_uid if previous else None, latched=bool(self.latches.active()))
            self._persist_safety()
            return self.status()

    def _session(self) -> PaperSession:
        if self.session is None:
            raise ConflictError("no paper session - start one first")
        return self.session

    def submit_proposal(self, payload: dict) -> dict:
        with self._lock:
            session = self._session()
            self._operational_gate("submit proposal")
            out = session.submit(payload)
            self._persist_safety()
            return out

    def cancel_order(self, order_id: str) -> dict:
        with self._lock:
            session = self._session()
            self._operational_gate("cancel order", opens_risk=False)
            out = session.cancel(order_id)
            self._persist_safety()
            return out

    def advance(self, payload: dict) -> dict:
        with self._lock:
            session = self._session()
            self._operational_gate("advance replay", opens_risk=False)
            out = session.advance(_to_int(payload.get("bars", 1), "bars"))
            for breach in out["new_breaches"]:
                entry = self.latches.engage("RESERVATION_BREACH", "actual fill cost exceeded reservation",
                                            {"session": session.session_id, **breach})
                session.stack.risk.trip_kill_switch(True)
                self._audit_event("latch_engaged", latch=entry["id"], kind=entry["kind"], reason=entry["reason"])
            session.recovery.refresh()
            self._persist_safety()
            return out

    def engage_kill_switch(self, payload: dict) -> dict:
        with self._lock:
            reason = str(payload.get("reason") or "manual (UI)")[:200]
            entry = self.latches.engage("KILL_SWITCH", reason)
            if self.session is not None:
                self.session.stack.risk.trip_kill_switch(True)
            self._audit_event("kill_switch_engaged", latch=entry["id"], reason=reason, actor="operator")
            self._persist_safety()
            return {"engaged": True, "latch": entry}

    # ------------------------------------------------------------ recovery / operations / audit (final completion)
    def emergency_flatten(self, payload: dict) -> dict:
        """Operator-only Emergency Flatten (Paper session). Same trust boundary as every other POST (loopback Host/Origin, X-QAT-Client).
        Fields are exactly operator / reason / confirm; the server decides prices, quantities and classification from its own Ledger."""

        unknown = set(payload) - {"operator", "reason", "confirm"}
        if unknown:
            raise ServiceError(f"fields not accepted from the client: {sorted(unknown)}")
        with self._lock:
            session = self._session()
            self._operational_gate("emergency flatten", opens_risk=False)
            if self.ops is None:
                raise ConflictError("ops configuration invalid")
            try:
                result = session.recovery.emergency_flatten(
                    operator=str(payload.get("operator") or "").strip()[:80], reason=str(payload.get("reason") or "").strip()[:200],
                    confirm=str(payload.get("confirm") or ""),
                    reference_price_fn=lambda market, symbol: session.bar.close if (market, symbol) == session.key else None,
                    pending_order_ids=list(session.pending))
            except RecoveryError as exc:
                self._persist_safety()
                raise ConflictError(str(exc)) from exc
            except (AuditCorrupt, AuditConflict) as exc:
                raise ConflictError(f"audit log unavailable: {exc}") from exc
            session.pending = [oid for oid in session.pending if not session.stack.broker.orders[oid].is_terminal]
            for row in result.get("orders", []):
                oid = row["order_id"]
                if oid in session.order_rows or result.get("idempotent_replay"):
                    continue
                session.pending.append(oid)
                session.order_rows[oid] = {"order_id": oid, "proposal_id": row["proposal_id"], "side": "SELL", "quantity": row["quantity"],
                                           "order_type": "MARKET", "limit_price": None, "reference_price": session.bar.close,
                                           "submitted_as_of": session.as_of(), "client_request_id": f"recovery:{row['proposal_id']}",
                                           "origin": "emergency_flatten"}
            self._persist_safety()
            return {**result, "kill_switch": session.stack.risk.kill_switch,
                    "note": "orders fill at the next replay bar open; the Kill Switch stays engaged (a flatten never releases it)"}

    def recovery_view(self) -> dict:
        with self._lock:
            s = self.session
            state = self._recovery_state()
            assess = s.recovery.assess() if s else None
            ack = state.get("restart_ack") or {}
            restart = {**self.restart, "acknowledged": bool(ack.get("utc") and ack["utc"] >= self.started_utc),
                       "acknowledged_by": ack.get("approver") if ack.get("utc", "") >= self.started_utc else None}
            return {
                "state": state.get("status", "NORMAL"), "state_detail": {k: state.get(k) for k in ("history", "flatten", "updated_utc", "error")},
                "restart": restart, "assessment": assess,
                "flatten_available": bool(s and assess and assess["flatten_allowed"] and assess["positions"]),
                "kill_switch": bool(s and s.stack.risk.kill_switch), "latches": self.latches.active(),
                "policy": ["Kill Switch never triggers an automatic liquidation",
                           "Emergency Flatten needs an operator name, a reason and the literal confirmation FLATTEN",
                           "it only sells existing long positions (never more than held, never a flip) and only when the books are trustworthy",
                           "if accounting / reconciliation cannot be trusted the state becomes RECOVERY_REQUIRED and nothing is liquidated",
                           "recovery state is persisted; resolving RECOVERY_REQUIRED is an offline operator action (ack-recovery)"],
                "confirm_token": "FLATTEN",
            }

    def audit_view(self, params: dict) -> dict:
        """Read-only paginated view of the durable audit log. Whitelisted scalar filters only: no path, no free-form query."""

        allowed = {"offset", "limit", "session_uid", "event_type", "order"}
        unknown = set(params) - allowed
        if unknown:
            raise ServiceError(f"unknown audit query parameters: {sorted(unknown)}")
        offset = _to_int(params.get("offset", 0), "offset")
        limit = _to_int(params.get("limit", 50), "limit")
        if offset < 0 or not 1 <= limit <= self.audit_store.max_page_size:
            raise ServiceError(f"offset must be >= 0 and limit within 1..{self.audit_store.max_page_size}")
        for key in ("session_uid", "event_type"):
            value = params.get(key)
            if value is not None and not _AUDIT_FILTER_RE.match(str(value)):
                raise ServiceError(f"invalid {key}")
        order = params.get("order", "desc")
        if order not in ("asc", "desc"):
            raise ServiceError("order must be asc or desc")
        verify = self.audit_store.verify()
        base = {"verify": {k: verify[k] for k in ("ok", "records", "errors", "last_hash")}, "healthy": self.audit_store.healthy,
                "fault": self.audit_store.fault, "durable": True, "path_shown": False}
        if not verify["ok"]:
            return {**base, "total": 0, "offset": offset, "limit": limit, "events": [],
                    "error": "audit log failed verification: events are withheld until an operator investigates"}
        page = self.audit_store.read(offset=offset, limit=limit, session_uid=params.get("session_uid"),
                                     event_type=params.get("event_type"), descending=order == "desc")
        return {**base, **page}

    def _evidence(self) -> dict:
        s = self.session
        ops = self.ops
        verify = self.audit_store.verify()
        rec_state = self._recovery_state()
        states = rule_states(ops) if ops else {}
        risk_cfg = (self.settings or {}).get("risk") or {}
        keys = {"R-01": "max_order_notional", "R-02": "max_symbol_exposure", "R-03": "max_market_exposure", "R-04": "max_total_exposure",
                "R-05": "daily_loss_limit", "R-06": "max_drawdown"}
        unconfigured = [rid for rid, key in keys.items() if risk_cfg.get(key) is None] + [r for r in ("R-07", "R-08", "R-09") if not states.get(r)]
        ledger = s.stack.ledger if s else None
        recon = ledger.last_reconciliation if ledger is not None else None
        sh = s.health.evaluate().to_dict() if s else None
        problems = ledger.integrity_problems() if ledger is not None else []
        ack = rec_state.get("restart_ack") or {}
        restart = {**self.restart, "acknowledged": bool(ack.get("utc") and ack["utc"] >= self.started_utc)}
        research = _research_label()
        grad = evaluate_paper_graduation(
            criteria=ops["paper_graduation"] if ops else {"min_completed_sessions": 5, "min_applied_fills": 20, "require_recovery_drill": True},
            audit_summary=self.audit_store.summary() if verify["ok"] else None, audit_ok=bool(verify["ok"] and self.audit_store.healthy),
            startup_status=self.startup["status"], latches_active=self.latches.active(), integrity_problems=problems,
            strategy_health=sh["status"] if sh else None, research_label=research)
        live = evaluate_live_readiness(paper_graduation_status=grad["status"])
        return {
            "session": None if s is None else {
                "data": {"dataset_validation": s.dataset.validation.status, "synthetic": s.meta.synthetic, "missing_marks": s.valuation().missing_marks,
                         "missing_fx": s.valuation().missing_fx},
                "ledger": {"integrity_problems": problems, "reservation_breaches": len(ledger.reservation_breaches)}},
            "reconciliation": ({"status": "PASS" if recon["ok"] else "BLOCK", "reason": "latest recorded reconciliation", "reasons": recon["reasons"]}
                               if recon is not None else {"status": "UNKNOWN", "reason": "no broker account to reconcile against (paper replay)"}),
            "risk": {"kill_switch": bool(s and s.stack.risk.kill_switch), "latches": self.latches.active(), "unconfigured_rules": unconfigured},
            "compliance": {"universe_configured": bool((s.stack.compliance.tradable_symbols is not None) if s
                                                       else (self.settings or {}).get("paper_universe") is not None)},
            "integrity": {"unconfigured_rules": [r for r in ("MI-05", "MI-06") if not states.get(r)]},
            "strategy_health": sh, "audit": {**verify, "healthy": self.audit_store.healthy, "fault": self.audit_store.fault},
            "broker": {"configured": False}, "startup": self.startup, "recovery": {"status": rec_state.get("status", "NORMAL"), "restart": restart},
            "research_label": research, "paper_graduation": grad, "live": live, "rule_states": states,
        }

    def operations(self) -> dict:
        """System-wide operational health summary + readiness evaluators. Everything is derived server-side from the real objects."""

        with self._lock:
            ev = self._evidence()
            snap = (self.ops or {}).get("snapshot") or {}
            return {
                "health": build_health(ev), "startup": ev["startup"], "restart": ev["recovery"]["restart"], "recovery_status": ev["recovery"]["status"],
                "paper_graduation": ev["paper_graduation"], "live_readiness": ev["live"], "strategy_health": ev["strategy_health"],
                "snapshot_policy": {"policy": snap.get("policy"), "max_age_seconds": snap.get("max_age_seconds"),
                                    "note": ("paper replay: there is no broker account, so snapshot freshness is NOT_APPLICABLE and reconciliation stays UNKNOWN"
                                             if snap.get("policy") == "paper_replay" else "broker_connected: a STALE / UNKNOWN snapshot never attests; Risk fails closed")},
                "ops_rules": ev["rule_states"], "ops_error": self.ops_error,
                "persistence": {"state_dir": "state/ (QAT_STATE_DIR)", "writes": "atomic temp-file + fsync + replace",
                                "process_model": "single process only; multi-process sharing of one state directory is NOT supported",
                                "state_fault": self.state_fault},
                "completion_scope": "software / Paper / research-infrastructure completion only: Live BLOCKED, profitability UNKNOWN, alpha NOT PROVEN",
            }

    # ------------------------------------------------------------ views
    def overview(self) -> dict:
        with self._lock:
            status = self.status()
            s = self.session
            runs = list_runs()
            latest = runs[0] if runs else None
            if s is None:
                return {"status": status, "session": None, "latest_run": latest}
            val = s.valuation()
            row = val.by_currency.get(s.currency, {})
            eq = row.get("equity")
            values = [p["equity"] for p in s.equity if p["equity"] is not None]
            peak = max(values) if values else None
            dd = (peak - eq) / peak if (peak and eq is not None and peak > 0) else None
            block_counts: dict[str, int] = {}
            for r in s.rejections:
                label = ":".join(r["reason"].split(":")[:3])  # stage:STATUS:code
                block_counts[label] = block_counts.get(label, 0) + 1
            top = sorted(block_counts.items(), key=lambda kv: -kv[1])[:5]
            return {
                "status": status,
                "session": {
                    "currency": s.currency.value, "equity": eq, "initial_cash": s.initial_cash,
                    "net_pnl": None if eq is None else eq - s.initial_cash,
                    "net_return": None if eq is None else (eq - s.initial_cash) / s.initial_cash,
                    "realized": row.get("realized"), "unrealized": row.get("unrealized"),
                    "drawdown": dd, "equity_base": val.total_base, "base_currency": val.base_currency.value,
                    "missing_fx": val.missing_fx, "missing_marks": val.missing_marks,
                    "equity_curve": s.equity[-400:], "period_start": s.equity[0]["timestamp"],
                    "as_of": s.as_of(), "top_block_reasons": [{"reason": k, "count": v} for k, v in top],
                    "open_orders": len(s.pending), "fills": len(s.fills),
                    "reservation_breaches": len(s.stack.ledger.reservation_breaches),
                    "kill_switch": s.stack.risk.kill_switch,
                },
                "latest_run": latest,
            }

    def portfolio(self) -> dict:
        with self._lock:
            s = self._session()
            val = s.valuation()
            eq = val.by_currency.get(s.currency, {}).get("equity")
            positions = []
            for p in val.positions:
                positions.append({**p, "exposure": (p["market_value"] / eq) if (eq and p["market_value"] is not None) else None,
                                  "mark_as_of": s.as_of(), "mark_source": f"replay close ({s.meta.source})"})
            return {
                "as_of": s.as_of(), "base_currency": val.base_currency.value,
                "by_currency": {c.value: row for c, row in val.by_currency.items()},
                "positions": positions, "total_base": val.total_base,
                "missing_fx": val.missing_fx, "missing_marks": val.missing_marks,
                "fx_note": "total shown only when every non-base currency has an explicit rate (placeholder rates from settings)",
            }

    def orders(self) -> dict:
        with self._lock:
            s = self._session()
            audit = s.stack.audit.dump()
            return {
                "as_of": s.as_of(),
                "orders": [s._order_view(oid) for oid in reversed(list(s.order_rows))],
                "fills": list(reversed(s.fills)),
                "rejections": list(reversed(s.rejections)),
                "events": list(reversed(s.events)),
                "audit": audit[-200:][::-1],
                "audit_total": len(audit),
                "audit_note": "in-memory view of this session; the durable hash-chained audit log is on the Audit screen",
            }

    def risk(self) -> dict:
        with self._lock:
            settings = self.settings or {}
            risk_cfg = settings.get("risk") or {}
            s = self.session
            integrity = s.stack.integrity if s else None
            compliance = s.stack.compliance if s else None
            ops_rules = rule_states(self.ops) if self.ops else {}

            def cfg(key):
                value = risk_cfg.get(key)
                return {"configured": value is not None, "value": value}

            rules = [
                {"id": "R-01", "name": "주문금액 한도", "implemented": True, **cfg("max_order_notional")},
                {"id": "R-02", "name": "종목 노출 한도", "implemented": True, **cfg("max_symbol_exposure")},
                {"id": "R-03", "name": "시장 노출 한도", "implemented": True, **cfg("max_market_exposure")},
                {"id": "R-04", "name": "전체 노출 한도", "implemented": True, **cfg("max_total_exposure")},
                {"id": "R-05", "name": "일일 손실 한도", "implemented": True, **cfg("daily_loss_limit")},
                {"id": "R-06", "name": "Drawdown 한도", "implemented": True, **cfg("max_drawdown")},
                {"id": "R-07", "name": "유동성 부족 차단", "implemented": True, "configured": ops_rules.get("R-07", False),
                 "value": (self.ops or {}).get("market_rules", {}).get("min_bar_volume"), "supported_by_current_data": True,
                 "note": "reference-bar volume; NOT_CONFIGURED = inert (never a PASS)"},
                {"id": "R-08", "name": "비정상 Spread 차단", "implemented": True, "configured": ops_rules.get("R-08", False),
                 "value": (self.ops or {}).get("market_rules", {}).get("max_spread_bps"), "supported_by_current_data": False,
                 "note": "needs a quoted spread; OHLCV bars have none -> UNKNOWN while configured"},
                {"id": "R-09", "name": "변동성 충격 대응", "implemented": True, "configured": ops_rules.get("R-09", False),
                 "value": (self.ops or {}).get("market_rules", {}).get("volatility_shock_range_pct"), "supported_by_current_data": True,
                 "note": "reference-bar (high-low)/close"},
                {"id": "R-10", "name": "Kill Switch", "implemented": True, "configured": True,
                 "value": bool(s and s.stack.risk.kill_switch)},
            ]
            mi = [
                {"id": "MI-01", "name": "중복 주문", "implemented": True,
                 "value": f"{integrity.duplicate_window_seconds}s" if integrity else None},
                {"id": "MI-02", "name": "반대 주문", "implemented": True},
                {"id": "MI-03", "name": "Self-Trade 위험", "implemented": True},
                {"id": "MI-04", "name": "과도한 주문 빈도", "implemented": True,
                 "value": integrity.max_orders_per_window if integrity else None},
                {"id": "MI-05", "name": "Cancel/Replace 패턴", "implemented": True, "configured": ops_rules.get("MI-05", False),
                 "value": (self.ops or {}).get("market_rules", {}).get("max_cancels_per_window"), "supported_by_current_data": True,
                 "note": "server-side cancel history per symbol"},
                {"id": "MI-06", "name": "유동성 참여율", "implemented": True, "configured": ops_rules.get("MI-06", False),
                 "value": (self.ops or {}).get("market_rules", {}).get("max_participation_rate"), "supported_by_current_data": True,
                 "note": "order quantity / reference-bar volume"},
                {"id": "MI-07", "name": "가격 괴리", "implemented": True,
                 "value": integrity.price_deviation_limit if integrity else None},
                {"id": "MI-08", "name": "비정상 반복", "implemented": True,
                 "value": integrity.max_same_direction_repeats if integrity else None},
            ]
            val = s.valuation() if s else None
            last_decisions = []
            if s:
                for rec in reversed(s.stack.audit.records):
                    if rec.stage.endswith("_decision"):
                        last_decisions.append({"stage": rec.stage.replace("_decision", ""),
                                               "status": rec.payload.get("status"),
                                               "reasons": rec.payload.get("reasons", []),
                                               "timestamp": rec.timestamp.isoformat()})
                    if len(last_decisions) >= 5:
                        break
            return {
                "live": "BLOCKED",
                "kill_switch": bool(s and s.stack.risk.kill_switch),
                "latches": self.latches.active(),
                "latch_clear_policy": "UI cannot clear latches; offline: python -m qat.ui clear-latch --id <id> --approver <name> --note <text>",
                "reservation_breaches": list(s.stack.ledger.reservation_breaches) if s else [],
                "reconciliation": (
                    {"status": "PASS" if s.stack.ledger.last_reconciliation["ok"] else "BLOCK",
                     "reason": "latest recorded reconciliation",
                     "reasons": s.stack.ledger.last_reconciliation["reasons"]}
                    if s and s.stack.ledger.last_reconciliation is not None
                    else {"status": "UNKNOWN",
                          "reason": "no broker account to reconcile against (paper replay)"}),
                "accounting_integrity": (s.stack.ledger.integrity_problems() if s else []),
                "exit_policy": EXIT_POLICY,
                "compliance": {
                    "mode": compliance.mode if compliance else None,
                    "enabled_markets": sorted(compliance.enabled_markets) if compliance and compliance.enabled_markets else None,
                    "universe_configured": bool(compliance and compliance.tradable_symbols is not None),
                    "tradable_symbols": (
                        "NOT CONFIGURED - fail-closed: every manual / Paper proposal is UNKNOWN"
                        if not (compliance and compliance.tradable_symbols is not None)
                        else ", ".join(sorted(compliance.tradable_symbols)) or "(empty allowlist)"
                    ),
                    "universe_note": "manual / Paper allowlist only; research runs use their own validated dataset symbol",
                    "legal_note": "software PASS is not legal compliance; laws / exchange rules / broker terms not verified",
                },
                "risk_rules": rules, "integrity_rules": mi,
                "data": None if s is None else {
                    "dataset_validation": s.dataset.validation.status,
                    "synthetic": s.meta.synthetic, "freshness": "HISTORICAL_REPLAY",
                    "missing_marks": val.missing_marks, "missing_fx": val.missing_fx,
                },
                "last_decisions": last_decisions,
                "strategy_health": s.health.evaluate().status if s else "UNKNOWN",
            }

    def settings_view(self) -> dict:
        settings = self.settings or {}
        return {
            "settings_ok": self.settings is not None, "settings_error": self.settings_error,
            "meta": settings.get("_meta"),
            "placeholder_notice": "every cost / tax / FX / risk number is a PLACEHOLDER, not a calibrated value",
            "project": settings.get("project"), "markets": settings.get("markets"),
            "execution": settings.get("execution"), "base_currency": settings.get("base_currency"),
            "fx": settings.get("fx"), "costs": settings.get("costs"), "risk": settings.get("risk"),
            "net_alpha": settings.get("net_alpha"),
            "paper_universe": settings.get("paper_universe"),
            "policies": {
                "OD-01": "CLOSED - exposure-reducing SELL exempt from Net Alpha only",
                "OD-02": "CLOSED - manual/Paper allowlist required (fail-closed); research = validated dataset symbol",
                "OD-03": "CLOSED - reservation buffer 2% is a provisional policy value, not empirically validated",
                "risk_reducing_exit_hierarchy": "CLOSED - Kill Switch blocks everything; Daily Loss / Drawdown / absolute exposure / reservation overrun do not trap an exposure-reducing SELL (accounting must be trusted); order-size limits still apply",
            },
            "code": code_identity(),
            "strategies": {name: {"version": cls.version, "defaults": cls.default_params,
                                  "grid": cls.param_grid} for name, cls in STRATEGIES.items()},
            "unknowns": ["첫 Broker/Exchange", "Data Vendor", "실제 Risk Limit", "Capital Allocation",
                         "Paper 졸업 기준", "Initial Live Capital", "투자 Universe", "ML 모델/학습주기",
                         "지속 가능한 실제 Net Alpha"],
        }

    # ------------------------------------------------------------ research
    _RESEARCH_FIELDS = {"dataset_id", "strategy", "params", "initial_cash", "cost_multiplier",
                        "position_fraction", "reservation_buffer_pct", "train_bars", "test_bars",
                        "step_bars", "lockbox_bars", "multipliers", "requested_start", "requested_end"}

    def _research_inputs(self, payload: dict):
        unknown = set(payload) - self._RESEARCH_FIELDS
        if unknown:
            raise ServiceError(f"unknown fields: {sorted(unknown)}")
        self._require_settings()
        ds = self.dataset(str(payload.get("dataset_id", "")))
        period = [payload.get(k) for k in ("requested_start", "requested_end")]
        for value in period:
            if value is not None and not (isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value)):
                raise ServiceError("requested_start/requested_end must be YYYY-MM-DD")
        try:
            require_admitted(ds)
            require_coverage(ds, *period)  # explicit rejection: never a silent truncation/extension
        except DataRejected as exc:
            raise ConflictError(str(exc)) from exc
        name = str(payload.get("strategy", ""))
        if name not in STRATEGIES:
            raise ServiceError(f"unknown strategy {name!r}")
        params = payload.get("params") or {}
        if not isinstance(params, dict):
            raise ServiceError("params must be an object")
        cash = _to_float(payload.get("initial_cash", 10_000_000), "initial_cash")
        if not 0 < cash < 1e15:
            raise ServiceError("initial_cash must be > 0 and < 1e15")
        try:
            make_strategy(name, **params)
            base = BacktestConfig(
                initial_cash=cash,
                settings_path=self.settings_path,
                cost_multiplier=_to_float(payload.get("cost_multiplier", 1.0), "cost_multiplier"),
                position_fraction=_to_float(payload.get("position_fraction", 0.95), "position_fraction"),
                reservation_buffer_pct=_to_float(payload.get("reservation_buffer_pct", 0.02),
                                                 "reservation_buffer_pct"),
            )
        except (TypeError, ValueError) as exc:
            raise ServiceError(str(exc)) from exc
        return ds, name, params, base

    def run_backtest(self, payload: dict) -> dict:
        ds, name, params, base = self._research_inputs(payload)
        with self._research_lock:
            try:
                res = run_backtest(ds, make_strategy(name, **params), base)
            except DataRejected as exc:
                raise ConflictError(str(exc)) from exc
            manifest = save_backtest(res)
        return {"run_id": manifest["run_id"]}

    def run_walkforward(self, payload: dict) -> dict:
        ds, name, params, base = self._research_inputs(payload)
        train = _to_int(payload.get("train_bars", 250), "train_bars")
        test = _to_int(payload.get("test_bars", 60), "test_bars")
        step = _to_int(payload["step_bars"], "step_bars") if payload.get("step_bars") else None
        lockbox = _to_int(payload.get("lockbox_bars", 0), "lockbox_bars")
        try:
            cfg = WalkForwardConfig(train_bars=train, test_bars=test, step_bars=step,
                                    lockbox_bars=lockbox, base=base)
            cfg.folds(len(ds.bars))
        except (TypeError, ValueError) as exc:
            raise ServiceError(str(exc)) from exc
        with self._research_lock:
            try:
                out = run_walkforward(ds, name, cfg, params)
            except DataRejected as exc:
                raise ConflictError(str(exc)) from exc
        return {"run_id": out["manifest"]["run_id"]}

    def run_stress(self, payload: dict) -> dict:
        ds, name, params, base = self._research_inputs(payload)
        mults = payload.get("multipliers") or [1.0, 2.0, 3.0]
        if not isinstance(mults, list):
            raise ServiceError("multipliers must be a list of numbers")
        mults = tuple(_to_float(m, "multiplier") for m in mults)[:5]
        if any(m < 0 for m in mults):
            raise ServiceError("multipliers must be >= 0")
        with self._research_lock:
            try:
                out = run_cost_stress(ds, name, params, base, mults)
            except DataRejected as exc:
                raise ConflictError(str(exc)) from exc
        return {"run_id": out["manifest"]["run_id"]}

    def runs(self) -> list[dict]:
        return list_runs()

    def run_detail(self, run_id: str) -> dict:
        try:
            return load_run(run_id)
        except KeyError as exc:
            raise ServiceError(str(exc)) from exc
