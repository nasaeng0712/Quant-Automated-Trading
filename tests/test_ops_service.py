"""QAT v1 final completion at the service / HTTP level: startup self-check, durable audit, restart assessment, Emergency Flatten + recovery state
machine, operations health, Paper graduation / Live readiness, security of the new routes, and failure injection. Real objects, no mocks of the core."""

from __future__ import annotations

import json
import pathlib
import threading
import urllib.error
import urllib.request

import pytest
import yaml

from qat.core.models import Currency
from qat.ops.graduation import evaluate_live_readiness, evaluate_paper_graduation
from qat.ops.health import build_health
from qat.ops.startup import run_startup_check
from qat.ui.server import make_server
from qat.ui.service import ConflictError, QATService, SafetyLatches, ServiceError, acknowledge_recovery

KR = "data/fixtures/SYN_KR1_1d.csv"
OPS_DEFAULT = pathlib.Path("config/ops.yaml")


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("QAT_RESULTS_DIR", str(tmp_path / "results"))
    monkeypatch.setenv("QAT_STATE_DIR", str(tmp_path / "state"))


def _no_research():
    return {"ok": True, "detail": "stubbed for speed"}


def _svc(**kw):
    kw.setdefault("research_check", _no_research)
    return QATService(**kw)


def _session(svc=None, **kw):
    svc = svc or _svc()
    svc.start_session({"dataset_id": KR, "initial_cash": 10_000_000, "start_bar": 60, **kw})
    return svc


def _buy(svc, rid, qty=10, **extra):
    return svc.submit_proposal({"side": "BUY", "quantity": qty, "order_type": "MARKET", "expected_gross_return": 0.05, "client_request_id": rid, **extra})


def _holding(svc, qty=10):
    assert _buy(svc, "seed", qty)["accepted"]
    svc.advance({"bars": 1})
    return svc.session.stack.ledger


def _ops_file(tmp_path, **sections):
    raw = yaml.safe_load(OPS_DEFAULT.read_text(encoding="utf-8"))
    for key, value in sections.items():
        if isinstance(value, dict) and isinstance(raw.get(key), dict):
            raw[key].update(value)
        else:
            raw[key] = value
    path = tmp_path / "ops_test.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return str(path)


def _state(tmp_path):
    return tmp_path / "state"


# ======================================================================= startup self-check
def test_startup_passes_with_default_config_and_has_no_blockers():
    svc = _svc()
    assert svc.startup["status"] in ("PASS", "WARN") and not svc.startup["blocked"]
    ids = {c["id"] for c in svc.startup["checks"]}
    assert {"ops_config", "settings", "live_config", "state_dir_writable", "latches", "audit_chain", "broker_requirements"} <= ids


def test_startup_blocks_on_invalid_ops_config_and_trading_is_refused(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("version: 1\nmode: paper\nsnapshot: {policy: bogus}\n", encoding="utf-8")
    svc = _svc(ops_config_path=str(bad))
    assert svc.startup["status"] == "BLOCK" and "ops_config" in svc.startup["blocked"]
    with pytest.raises(ConflictError, match="fail-closed"):
        svc.start_session({"dataset_id": KR})


def test_startup_blocks_unsupported_live_config(tmp_path):
    svc = _svc(ops_config_path=_ops_file(tmp_path, mode="live"))
    assert svc.startup["status"] == "BLOCK" and "live_config" in svc.startup["blocked"]
    with pytest.raises(ConflictError):
        svc.start_session({"dataset_id": KR})
    assert svc.operations()["live_readiness"]["status"] == "BLOCKED"


def test_startup_blocks_broker_connected_without_a_broker(tmp_path):
    svc = _svc(ops_config_path=_ops_file(tmp_path, snapshot={"policy": "broker_connected", "max_age_seconds": 30}))
    assert "broker_requirements" in svc.startup["blocked"]


def test_startup_blocks_when_state_dir_is_not_writable(tmp_path):
    blocker = tmp_path / "file_not_dir"
    blocker.write_text("x", encoding="utf-8")
    res = run_startup_check(state_dir=blocker, settings={"project": {"mode": "paper"}}, latch_status=lambda: {"ok": True, "active": []},
                            audit_store=None)
    assert res["status"] == "BLOCK" and "state_dir_writable" in res["blocked"] and "audit_chain" in res["blocked"]


@pytest.mark.parametrize("name", ["recovery_state.json", "safety_state.json"])
def test_startup_blocks_on_corrupt_persisted_state(tmp_path, name):
    _state(tmp_path).mkdir(parents=True, exist_ok=True)
    (_state(tmp_path) / name).write_text("{broken", encoding="utf-8")
    svc = _svc()
    assert svc.startup["status"] == "BLOCK" and f"state:{name}" in svc.startup["blocked"]
    with pytest.raises(ConflictError):
        svc.start_session({"dataset_id": KR})
    assert svc.restart["state"] == "MANUAL_INTERVENTION_REQUIRED"  # never a silent reset


def test_malformed_audit_record_blocks_startup_and_withholds_events(tmp_path):
    svc = _session()
    _buy(svc, "a1")
    log = _state(tmp_path) / "audit_log.jsonl"
    lines = log.read_text(encoding="utf-8").splitlines()
    lines[2] = lines[2][:15] + "!!garbage"
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    restarted = _svc()
    assert restarted.startup["status"] == "BLOCK" and "audit_chain" in restarted.startup["blocked"]
    view = restarted.audit_view({})
    assert view["verify"]["ok"] is False and view["events"] == [] and "withheld" in view["error"]
    with pytest.raises(ConflictError):
        restarted.start_session({"dataset_id": KR})
    assert restarted.operations()["health"]["overall"] == "BLOCKED"


def test_corrupt_latch_file_is_quarantined_not_reset(tmp_path):
    _state(tmp_path).mkdir(parents=True, exist_ok=True)
    (_state(tmp_path) / "safety_latches.json").write_text("{nope", encoding="utf-8")
    latches = SafetyLatches()
    active = latches.active()
    assert [a["kind"] for a in active] == ["STATE_CORRUPT"]
    assert list(_state(tmp_path).glob("safety_latches.json.corrupt-*"))
    assert latches.status()["ok"] is False
    svc = _svc()
    assert svc.startup["status"] == "BLOCK" and "latches" in svc.startup["blocked"]
    with pytest.raises(ConflictError):
        svc.start_session({"dataset_id": KR})
    latches.clear("STATE_CORRUPT-1", "op", "reviewed")
    assert _svc().startup["status"] != "BLOCK"


def test_wrong_shape_latch_file_is_also_quarantined(tmp_path):
    _state(tmp_path).mkdir(parents=True, exist_ok=True)
    (_state(tmp_path) / "safety_latches.json").write_text('{"active": "x", "cleared": []}', encoding="utf-8")
    assert SafetyLatches().active()[0]["kind"] == "STATE_CORRUPT"


def test_latch_writes_are_atomic_and_leave_no_temp_files(tmp_path):
    latches = SafetyLatches()
    latches.engage("KILL_SWITCH", "x")
    latches.clear("KILL_SWITCH-1", "op", "n")
    assert not list(_state(tmp_path).glob("*.tmp"))
    assert json.loads((_state(tmp_path) / "safety_latches.json").read_text(encoding="utf-8"))["cleared"]


# ======================================================================= durable audit through the service
def test_service_audit_is_durable_paginated_and_survives_restart(tmp_path):
    svc = _session()
    for i in range(3):
        _buy(svc, f"p{i}", 1)
    first = svc.audit_view({"limit": "5", "offset": "0"})
    assert first["total"] > 5 and len(first["events"]) == 5 and first["verify"]["ok"] and first["durable"]
    seqs = [e["seq"] for e in first["events"]]
    assert seqs == sorted(seqs, reverse=True)
    uid = svc.session.session_uid
    svc.close()
    after = _svc()
    again = after.audit_view({"session_uid": uid, "limit": "500", "order": "asc"})
    assert again["total"] >= first["total"] and [e["seq"] for e in again["events"]] == sorted(e["seq"] for e in again["events"])
    assert after.audit_view({"event_type": "session_started"})["total"] >= 1
    kinds = {e["event_type"] for e in after.audit_view({"limit": "500"})["events"]}
    assert {"proposal_received", "session_started", "service_stopped", "service_started"} <= kinds


@pytest.mark.parametrize("params", [{"path": "../../x"}, {"limit": "0"}, {"limit": "100000"}, {"offset": "-1"}, {"limit": "abc"},
                                    {"session_uid": "../etc"}, {"event_type": "a b"}, {"order": "sideways"}, {"file": "x"}])
def test_audit_view_rejects_unsafe_or_unknown_parameters(params):
    with pytest.raises(ServiceError):
        _svc().audit_view(params)


def test_audit_view_never_exposes_secrets_or_paths(tmp_path):
    svc = _session()
    svc.audit_store.append({"event_id": "sec-1", "event_type": "probe", "timestamp": "t", "serviceKey": "TOPSECRET99", "detail": "x?serviceKey=TOPSECRET99"})
    text = json.dumps(svc.audit_view({"limit": "500"}))
    assert "TOPSECRET99" not in text and str(tmp_path) not in text and svc.audit_view({})["path_shown"] is False


def test_kill_switch_works_even_when_the_audit_log_is_faulted():
    svc = _session()
    svc.audit_store.fault = "forced"
    out = svc.engage_kill_switch({"reason": "test"})
    assert out["engaged"] and svc.session.stack.risk.kill_switch
    with pytest.raises(ConflictError, match="FAULT"):
        _buy(svc, "x")
    assert svc.operations()["health"]["overall"] == "BLOCKED"


# ======================================================================= restart assessment / persistence
def test_first_start_then_safe_recovery_after_a_clean_shutdown():
    svc = _session()
    assert svc.restart["state"] == "FIRST_START"
    svc.close()
    assert _svc().restart["state"] == "SAFE_RECOVERY"


def test_restart_after_crash_with_pending_order_requires_manual_intervention():
    svc = _session()
    assert _buy(svc, "o1")["accepted"]  # pending, never filled, no clean shutdown
    restarted = _svc()
    assert restarted.restart["state"] == "MANUAL_INTERVENTION_REQUIRED"
    assert any(r.startswith("order_state_unknown_after_restart") for r in restarted.restart["reasons"])
    assert restarted.operations()["health"]["components"][0]["status"] in ("BLOCKED", "DEGRADED", "UNKNOWN")
    recovery = restarted.recovery_view()
    assert recovery["restart"]["acknowledged"] is False and recovery["session"] is None if "session" in recovery else True


def test_kill_switch_latch_persists_across_restart_and_new_sessions_run_latched():
    svc = _session()
    svc.engage_kill_switch({"reason": "drill"})
    svc.close()
    restarted = _svc()
    assert restarted.restart["state"] == "MANUAL_INTERVENTION_REQUIRED"
    assert any("kill_switch" in r or "safety_latch" in r for r in restarted.restart["reasons"])
    restarted.start_session({"dataset_id": KR})
    assert restarted.session.stack.risk.kill_switch is True
    res = _buy(restarted, "k1")
    assert res["accepted"] is False and "kill_switch" in res["reason"]


def test_safety_state_is_persisted_atomically(tmp_path):
    svc = _session()
    _buy(svc, "s1")
    data = json.loads((_state(tmp_path) / "safety_state.json").read_text(encoding="utf-8"))
    assert data["session_uid"] == svc.session.session_uid and data["pending_orders"] and data["clean_shutdown"] is False
    svc.close()
    assert json.loads((_state(tmp_path) / "safety_state.json").read_text(encoding="utf-8"))["clean_shutdown"] is True
    assert not list(_state(tmp_path).glob("*.tmp"))


# ======================================================================= emergency flatten + recovery state machine
def test_flatten_requires_operator_reason_and_literal_confirmation():
    svc = _session()
    _holding(svc)
    for payload in ({"operator": "", "reason": "r", "confirm": "FLATTEN"}, {"operator": "o", "reason": "", "confirm": "FLATTEN"},
                    {"operator": "o", "reason": "r", "confirm": "flatten"}, {"operator": "o", "reason": "r"}):
        with pytest.raises(ConflictError):
            svc.emergency_flatten(payload)
    assert svc.session.stack.ledger.positions and svc.recovery_view()["state"] == "NORMAL"


def test_flatten_rejects_client_supplied_decisions():
    svc = _session()
    _holding(svc)
    for extra in ({"quantity": 5}, {"price": 1}, {"side": "BUY"}, {"approved": True}, {"status": "FILLED"}, {"symbol": "X"}):
        with pytest.raises(ServiceError):
            svc.emergency_flatten({"operator": "o", "reason": "r", "confirm": "FLATTEN", **extra})
    assert svc.session.stack.ledger.positions[(svc.session.meta.market, svc.session.meta.symbol)].quantity == 10


def test_flatten_requires_a_session():
    with pytest.raises(ConflictError):
        _svc().emergency_flatten({"operator": "o", "reason": "r", "confirm": "FLATTEN"})


def test_emergency_flatten_end_to_end_reduces_to_zero_keeps_kill_switch_and_is_audited():
    svc = _session()
    ledger = _holding(svc, 10)
    key = (svc.session.meta.market, svc.session.meta.symbol)
    svc.engage_kill_switch({"reason": "incident"})
    assert _buy(svc, "blocked")["accepted"] is False  # the Kill Switch still blocks the NORMAL pipeline, and does not liquidate on its own
    assert ledger.positions[key].quantity == 10
    view = svc.recovery_view()
    assert view["flatten_available"] and view["state"] == "NORMAL"
    out = svc.emergency_flatten({"operator": "alice", "reason": "incident #1", "confirm": "FLATTEN"})
    assert out["state"] == "FLATTEN_SUBMITTED" and out["kill_switch"] is True and len(out["orders"]) == 1
    assert out["orders"][0]["quantity"] == 10  # never more than held: no flip
    assert svc.session.stack.risk.kill_switch is True
    svc.advance({"bars": 1})
    assert ledger.positions[key].quantity == pytest.approx(0)
    assert svc.recovery_view()["state"] == "FLATTENED"
    assert svc.session.stack.risk.kill_switch is True  # a flatten never releases the Kill Switch
    assert _buy(svc, "after")["accepted"] is False
    types = [e["event_type"] for e in svc.audit_view({"limit": "500", "order": "asc"})["events"]]
    for needed in ("recovery_requested", "recovery_order_received", "recovery_flatten_submitted", "recovery_flatten_completed"):
        assert needed in types
    ev = [e for e in svc.audit_view({"limit": "500"})["events"] if e["event_type"] == "recovery_flatten_submitted"][0]
    assert ev["actor"] == "alice"
    assert not any(p.quantity < 0 for p in ledger.positions.values())


def test_flatten_is_idempotent_a_retry_creates_no_second_set_of_orders():
    svc = _session()
    _holding(svc, 10)
    first = svc.emergency_flatten({"operator": "o", "reason": "r", "confirm": "FLATTEN"})
    again = svc.emergency_flatten({"operator": "o", "reason": "r", "confirm": "FLATTEN"})
    assert again.get("idempotent_replay") is True and again["orders"] == first["orders"]
    sells = [o for o in svc.session.stack.broker.orders.values() if o.side.value == "SELL"]
    assert len(sells) == 1


def test_flatten_with_nothing_to_flatten_is_flattened_without_orders():
    svc = _session()
    out = svc.emergency_flatten({"operator": "o", "reason": "r", "confirm": "FLATTEN"})
    assert out["state"] == "FLATTENED" and out["orders"] == []


def test_flatten_refuses_and_requires_recovery_when_accounting_is_untrusted():
    svc = _session()
    ledger = _holding(svc, 10)
    ledger.record_integrity_issue("fill_order_mismatch", order_id="x")  # settlement integrity problem
    assert svc.recovery_view()["flatten_available"] is False
    with pytest.raises(ConflictError, match="recovery_required"):
        svc.emergency_flatten({"operator": "o", "reason": "r", "confirm": "FLATTEN"})
    assert svc.recovery_view()["state"] == "RECOVERY_REQUIRED"
    assert ledger.positions[(svc.session.meta.market, svc.session.meta.symbol)].quantity == 10  # nothing liquidated on a guess
    assert not [o for o in svc.session.stack.broker.orders.values() if o.side.value == "SELL"]
    # RECOVERY_REQUIRED persists: no new risk, no silent reset, even across a restart
    with pytest.raises(ConflictError, match="RECOVERY_REQUIRED"):
        _buy(svc, "n1")
    restarted = _svc()
    assert restarted.recovery_view()["state"] == "RECOVERY_REQUIRED"
    with pytest.raises(ConflictError, match="RECOVERY_REQUIRED"):
        restarted.start_session({"dataset_id": KR})
    assert acknowledge_recovery("op", "books reviewed offline")["to"] == "NORMAL"
    restarted2 = _svc()
    restarted2.start_session({"dataset_id": KR})
    assert restarted2.recovery_view()["state"] == "NORMAL"


def test_flatten_refuses_on_reconciliation_mismatch():
    svc = _session()
    ledger = _holding(svc, 10)
    ledger.record_reconciliation(False, ["cash_mismatch"])
    with pytest.raises(ConflictError, match="reconciliation_mismatch"):
        svc.emergency_flatten({"operator": "o", "reason": "r", "confirm": "FLATTEN"})
    assert svc.recovery_view()["state"] == "RECOVERY_REQUIRED"


def test_flatten_refuses_non_overrun_breach_and_never_creates_orders():
    svc = _session()
    ledger = _holding(svc, 10)
    ledger.reservation_breaches.append({"kind": "something_else"})
    with pytest.raises(ConflictError):
        svc.emergency_flatten({"operator": "o", "reason": "r", "confirm": "FLATTEN"})
    assert not [o for o in svc.session.stack.broker.orders.values() if o.side.value == "SELL"]


def test_flatten_after_a_plain_overrun_breach_is_allowed_on_trusted_books():
    svc = _session()
    ledger = _holding(svc, 10)
    ledger.record_reservation_breach(Currency.KRW, 5.0, order_id="x")
    out = svc.emergency_flatten({"operator": "o", "reason": "r", "confirm": "FLATTEN"})
    assert out["state"] == "FLATTEN_SUBMITTED" and out["orders"]


def test_recovery_order_path_rejects_forged_authorization_and_buys():
    from qat.core.models import Market, OrderType, Side, TradeProposal
    from qat.core.recovery import RecoveryAuthorization
    svc = _session()
    _holding(svc, 10)
    stack = svc.session.stack
    prop = TradeProposal(market=svc.session.meta.market_enum, symbol=svc.session.meta.symbol, side=Side.SELL, quantity=1, order_type=OrderType.MARKET,
                         strategy_id="x", reason_code="x", expected_gross_return=0.0, confidence=0.5, created_at=svc.session.clock.now(),
                         signal_timestamp=svc.session.clock.now())
    with pytest.raises(PermissionError):
        stack.orchestrator.submit_recovery_order(prop, 100.0, "FLATTEN")
    with pytest.raises(PermissionError):
        stack.orchestrator.submit_recovery_order(prop, 100.0, RecoveryAuthorization("o", "r", "id", "guessed-token"))  # forged: token never issued
    # a genuine authorization still cannot be used to INCREASE exposure
    auth = svc.session.recovery._authorize("op", "r", "rid")
    buy = TradeProposal(market=Market(svc.session.meta.market), symbol=svc.session.meta.symbol, side=Side.BUY, quantity=1, order_type=OrderType.MARKET,
                        strategy_id="x", reason_code="x", expected_gross_return=0.0, confidence=0.5, created_at=svc.session.clock.now(),
                        signal_timestamp=svc.session.clock.now())
    res = stack.orchestrator.submit_recovery_order(buy, 100.0, auth)
    assert res.accepted is False and "not_exposure_reducing" in res.reason
    oversize = TradeProposal(market=Market(svc.session.meta.market), symbol=svc.session.meta.symbol, side=Side.SELL, quantity=11, order_type=OrderType.MARKET,
                             strategy_id="x", reason_code="x", expected_gross_return=0.0, confidence=0.5, created_at=svc.session.clock.now(),
                             signal_timestamp=svc.session.clock.now())
    assert stack.orchestrator.submit_recovery_order(oversize, 100.0, auth).accepted is False  # a flip / short is refused


def test_recovery_order_path_refuses_untrusted_books():
    from qat.core.models import OrderType, Side, TradeProposal
    svc = _session()
    ledger = _holding(svc, 10)
    auth = svc.session.recovery._authorize("op", "r", "rid")
    ledger.record_integrity_issue("fill_order_mismatch")
    prop = TradeProposal(market=svc.session.meta.market_enum, symbol=svc.session.meta.symbol, side=Side.SELL, quantity=1, order_type=OrderType.MARKET,
                         strategy_id="x", reason_code="x", expected_gross_return=0.0, confidence=0.5, created_at=svc.session.clock.now(),
                         signal_timestamp=svc.session.clock.now())
    res = svc.session.stack.orchestrator.submit_recovery_order(prop, 100.0, auth)
    assert res.accepted is False and "accounting_untrusted" in res.reason


def test_flatten_cancels_pending_orders_first_and_releases_their_reservation():
    svc = _session()
    _holding(svc, 10)
    assert _buy(svc, "pend", 5)["accepted"]
    pending = list(svc.session.pending)
    svc.emergency_flatten({"operator": "o", "reason": "r", "confirm": "FLATTEN"})
    for oid in pending:
        assert svc.session.stack.broker.orders[oid].status.value == "CANCELLED"
    assert sum(svc.session.stack.ledger.reserved_cash.values()) == pytest.approx(0)


def test_flatten_when_audit_is_faulted_fails_closed():
    svc = _session()
    _holding(svc, 10)
    svc.audit_store.fault = "forced"
    with pytest.raises(ConflictError):
        svc.emergency_flatten({"operator": "o", "reason": "r", "confirm": "FLATTEN"})
    assert not [o for o in svc.session.stack.broker.orders.values() if o.side.value == "SELL"]


def test_new_session_after_completed_flatten_returns_to_normal():
    svc = _session()
    _holding(svc, 10)
    svc.emergency_flatten({"operator": "o", "reason": "r", "confirm": "FLATTEN"})
    svc.advance({"bars": 1})
    assert svc.recovery_view()["state"] == "FLATTENED"
    svc.start_session({"dataset_id": KR})
    assert svc.recovery_view()["state"] == "NORMAL"


def test_recovery_flatten_in_flight_blocks_new_risk_and_restart_requires_ack():
    svc = _session()
    _holding(svc, 10)
    svc.emergency_flatten({"operator": "o", "reason": "r", "confirm": "FLATTEN"})  # FLATTEN_SUBMITTED, fills not yet applied
    with pytest.raises(ConflictError, match="FLATTEN_SUBMITTED"):
        _buy(svc, "n")
    restarted = _svc()  # restart mid-state
    assert restarted.recovery_view()["state"] == "FLATTEN_SUBMITTED"
    assert restarted.restart["state"] == "MANUAL_INTERVENTION_REQUIRED"
    with pytest.raises(ConflictError):
        restarted.start_session({"dataset_id": KR})


# ======================================================================= service-level R-07..R-09 / MI-05 / MI-06 wiring
def test_configured_ops_rules_reach_the_live_session_and_the_risk_view(tmp_path):
    ops = _ops_file(tmp_path, market_rules={"min_bar_volume": 1e18, "max_participation_rate": 0.5, "max_cancels_per_window": 1})
    svc = _session(_svc(ops_config_path=ops))
    res = _buy(svc, "r7")
    assert res["accepted"] is False and "R-07" in res["reason"]
    rules = {r["id"]: r for r in svc.risk()["risk_rules"] + svc.risk()["integrity_rules"]}
    assert rules["R-07"]["configured"] is True and rules["R-08"]["configured"] is False and rules["R-08"]["supported_by_current_data"] is False
    assert rules["MI-05"]["configured"] and rules["MI-06"]["configured"]


def test_mi06_participation_applies_in_a_service_session(tmp_path):
    ops = _ops_file(tmp_path, market_rules={"max_participation_rate": 1e-12})
    svc = _session(_svc(ops_config_path=ops))
    res = _buy(svc, "p1")
    assert res["accepted"] is False and "MI-06" in res["reason"]


# ======================================================================= strategy health through the service
def test_strategy_health_is_observed_by_the_session_and_no_trade_is_not_a_failure():
    svc = _session()
    assert svc.status()["strategy_health"] == "UNKNOWN"
    svc.advance({"bars": 2})
    st = svc.status()["strategy_health"]
    assert st == "HEALTHY"  # evaluations happen, no trades: still healthy
    sh = svc.operations()["strategy_health"]
    assert sh["scope"].startswith("operational health only") and any("no signals" in n for n in sh["notes"])


def test_strategy_health_rejection_concentration_degrades_but_never_changes_trading():
    svc = _session()
    for i in range(12):
        _buy(svc, f"rej{i}", 10_000_000)  # always rejected the same way
    sh = svc.operations()["strategy_health"]
    assert sh["status"] == "DEGRADED" and any(r.startswith("rejection_concentration") for r in sh["reasons"])
    assert _buy(svc, "fine", 1)["accepted"] is True  # observation only


# ======================================================================= operations / graduation / live readiness
def test_operations_view_is_honest_about_every_component():
    svc = _session()
    ops = svc.operations()
    comps = {c["id"]: c for c in ops["health"]["components"]}
    assert {"data", "ledger", "reconciliation", "risk", "compliance", "integrity", "strategy_health", "audit", "broker", "research", "paper", "live"} <= set(comps)
    assert comps["live"]["status"] == "BLOCKED" and comps["broker"]["status"] == "NOT_CONFIGURED"
    assert comps["reconciliation"]["status"] == "UNKNOWN"  # paper replay has no broker account: never PASS
    assert ops["health"]["overall"] != "PASS"
    assert ops["live_readiness"]["status"] == "BLOCKED" and ops["paper_graduation"]["status"] in ("NOT_READY", "UNKNOWN", "BLOCKED")
    assert ops["paper_graduation"]["status"] != "GRADUATED"
    assert "UNKNOWN" in ops["completion_scope"] and "NOT PROVEN" in ops["completion_scope"]


def test_operations_critical_components_sort_first_and_kill_switch_blocks_risk():
    svc = _session()
    svc.engage_kill_switch({"reason": "t"})
    health = svc.operations()["health"]
    assert health["overall"] == "BLOCKED"
    deciding = [c for c in health["components"] if not c["informational"]]
    rank = {"BLOCKED": 0, "DEGRADED": 1, "UNKNOWN": 2, "NOT_CONFIGURED": 3, "PASS": 4}
    assert [rank[c["status"]] for c in deciding] == sorted(rank[c["status"]] for c in deciding)
    assert health["components"][: len(deciding)] == deciding  # informational items (Live BLOCKED by design) never push real problems down


def test_operations_without_a_session_is_unknown_not_pass():
    comps = {c["id"]: c for c in _svc().operations()["health"]["components"]}
    assert comps["data"]["status"] == "UNKNOWN" and comps["ledger"]["status"] == "UNKNOWN"


_GOOD = dict(criteria={"min_completed_sessions": 2, "min_applied_fills": 3, "require_recovery_drill": True},
             audit_summary={"sessions": {"a": {"applied_fills": 2}, "b": {"applied_fills": 2}}, "event_types": {"recovery_flatten_completed": 1}},
             audit_ok=True, startup_status="PASS", latches_active=[], integrity_problems=[], strategy_health="HEALTHY", research_label="VALIDATED",
             costs_calibrated=True, broker_reconciliation_evidence=True)


def test_graduation_graduated_only_when_every_gate_passes():
    assert evaluate_paper_graduation(**_GOOD)["status"] == "GRADUATED"


@pytest.mark.parametrize("change,expected", [
    ({"startup_status": "BLOCK"}, "BLOCKED"), ({"audit_ok": False}, "BLOCKED"), ({"latches_active": [{"kind": "KILL_SWITCH"}]}, "BLOCKED"),
    ({"integrity_problems": ["x"]}, "BLOCKED"),
    ({"audit_summary": {"sessions": {}, "event_types": {"recovery_flatten_completed": 1}}}, "NOT_READY"),
    ({"audit_summary": {"sessions": {"a": {"applied_fills": 2}, "b": {"applied_fills": 2}}, "event_types": {}}}, "NOT_READY"),
    ({"audit_summary": {"sessions": {"a": {"applied_fills": 9}}, "event_types": {"recovery_flatten_completed": 1}}}, "NOT_READY"),  # only G2 (sessions) fails
    ({"audit_summary": {"sessions": {"a": {"applied_fills": 1}, "b": {"applied_fills": 1}}, "event_types": {"recovery_flatten_completed": 1}}}, "NOT_READY"),  # only G3 (fills) fails
    ({"research_label": "DEVELOPMENT_SELECTED_CANDIDATE — NOT_HOLDOUT_ELIGIBLE"}, "NOT_READY"),
    ({"strategy_health": "UNHEALTHY"}, "NOT_READY"), ({"costs_calibrated": False}, "UNKNOWN"), ({"broker_reconciliation_evidence": False}, "UNKNOWN"),
    ({"strategy_health": None}, "UNKNOWN"), ({"research_label": None}, "UNKNOWN"),
])
def test_graduation_each_gate_can_deny(change, expected):
    assert evaluate_paper_graduation(**{**_GOOD, **change})["status"] == expected


def test_graduation_blocked_beats_not_ready_and_unknown_does_not_graduate():
    res = evaluate_paper_graduation(**{**_GOOD, "audit_ok": False, "research_label": "NOT_HOLDOUT_ELIGIBLE", "costs_calibrated": False})
    assert res["status"] == "BLOCKED"
    assert "not alpha" in res["scope"]


def test_graduation_real_system_applies_protocol_v2_not_holdout_eligible_honestly(tmp_path):
    svc = _svc()
    res = svc.operations()["paper_graduation"]
    g5 = [g for g in res["gates"] if g["id"] == "G5"][0]
    assert g5["status"] == "FAIL" and "NOT_HOLDOUT_ELIGIBLE" in g5["detail"]
    assert res["status"] == "NOT_READY"


def test_live_readiness_is_always_blocked_whatever_the_inputs():
    best = evaluate_live_readiness(paper_graduation_status="GRADUATED", broker_configured=True, costs_calibrated=True, risk_limits_configured=True,
                                   profitability="PROVEN")
    assert best["status"] == "BLOCKED" and best["structural_refusals_confirmed"] is True
    assert evaluate_live_readiness(paper_graduation_status="GRADUATED", requested_by_ui=True)["status"] == "BLOCKED"


def test_live_readiness_stays_blocked_even_if_the_probes_were_to_succeed(monkeypatch):
    import qat.ops.graduation as grad
    monkeypatch.setattr(grad, "_probe_live_refusals", lambda: ["live_broker_stub_constructible", "router_live_route_succeeded"])
    res = evaluate_live_readiness(paper_graduation_status="GRADUATED", broker_configured=True, costs_calibrated=True, risk_limits_configured=True,
                                  profitability="PROVEN")
    assert res["status"] == "BLOCKED" and res["structural_refusals_confirmed"] is False


def test_no_http_route_can_enable_live(tmp_path):
    server = make_server("127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        for path in ("/api/live", "/api/live/enable", "/api/paper/live", "/api/recovery/live", "/api/operations/live"):
            assert _req(base + path, "POST", {"enable": True})[0] == 404
        assert json.loads(_req(base + "/api/operations")[2])["live_readiness"]["status"] == "BLOCKED"
        assert json.loads(_req(base + "/api/status")[2])["live"]["status"] == "BLOCKED"
    finally:
        server.shutdown()
        server.server_close()


def test_health_summary_precedence_unit():
    base = {"session": None, "reconciliation": {"status": "PASS"}, "risk": {}, "compliance": {"universe_configured": True}, "integrity": {},
            "strategy_health": {"status": "HEALTHY", "reasons": []}, "audit": {"ok": True, "healthy": True, "records": 1}, "broker": {"configured": True, "connected": True},
            "startup": {"status": "PASS", "checks": []}, "recovery": {"status": "NORMAL"}, "research_label": "x", "paper_graduation": {"status": "NOT_READY"},
            "live": {"status": "BLOCKED", "blockers": ["a"]}}
    session = {"data": {"dataset_validation": "PASS", "synthetic": False, "missing_marks": [], "missing_fx": []}, "ledger": {"integrity_problems": [], "reservation_breaches": 0}}
    ok = build_health({**base, "session": session})
    assert ok["overall"] == "PASS"  # Live BLOCKED / Paper NOT_READY are informational
    assert build_health({**base, "session": session, "audit": {"ok": False, "errors": ["x"]}})["overall"] == "BLOCKED"
    assert build_health({**base, "session": session, "risk": {"kill_switch": True}})["overall"] == "BLOCKED"
    assert build_health({**base, "session": session, "recovery": {"status": "RECOVERY_REQUIRED"}})["overall"] == "BLOCKED"
    assert build_health({**base, "session": session, "broker": {"configured": False}})["overall"] == "DEGRADED"  # NOT_CONFIGURED never PASS
    assert build_health({**base, "session": session, "reconciliation": {"status": "UNKNOWN"}})["overall"] == "UNKNOWN"
    bad = {**session, "ledger": {"integrity_problems": ["settlement:x"], "reservation_breaches": 0}}
    assert build_health({**base, "session": bad})["overall"] == "BLOCKED"


# ======================================================================= HTTP: new routes + security
@pytest.fixture
def http():
    server = make_server("127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}", server
    server.shutdown()
    server.server_close()


def _req(url, method="GET", body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    hdrs = {"Content-Type": "application/json", "X-QAT-Client": "ui", **(headers or {})}
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def test_http_new_routes_end_to_end(http):
    base, server = http
    assert _req(base + "/api/paper/session", "POST", {"dataset_id": KR, "start_bar": 60})[0] == 200
    assert _req(base + "/api/paper/proposals", "POST", {"side": "BUY", "quantity": 10, "expected_gross_return": 0.05, "client_request_id": "h1"})[0] == 200
    assert _req(base + "/api/paper/advance", "POST", {"bars": 1})[0] == 200
    audit = json.loads(_req(base + "/api/audit?limit=5&order=desc")[2])
    assert audit["verify"]["ok"] and len(audit["events"]) == 5
    rec = json.loads(_req(base + "/api/recovery")[2])
    assert rec["flatten_available"] is True and rec["confirm_token"] == "FLATTEN"
    status, _, body = _req(base + "/api/recovery/flatten", "POST", {"operator": "alice", "reason": "drill", "confirm": "FLATTEN"})
    assert status == 200 and json.loads(body)["state"] == "FLATTEN_SUBMITTED"
    ops = json.loads(_req(base + "/api/operations")[2])
    assert ops["live_readiness"]["status"] == "BLOCKED" and ops["recovery_status"] == "FLATTEN_SUBMITTED"
    ledger = server.qat_service.session.stack.ledger
    assert ledger.positions  # not yet filled: the flatten fills at the next replay bar


def test_http_new_routes_enforce_the_same_trust_boundary(http):
    base, _ = http
    body = {"operator": "o", "reason": "r", "confirm": "FLATTEN"}
    assert _req(base + "/api/recovery/flatten", "POST", body, {"X-QAT-Client": ""})[0] == 403
    assert _req(base + "/api/recovery/flatten", "POST", body, {"Content-Type": "text/plain"})[0] == 415
    assert _req(base + "/api/recovery/flatten", "POST", body, {"Host": "evil.example"})[0] == 403
    assert _req(base + "/api/recovery/flatten", "POST", body, {"Origin": "http://evil.example"})[0] == 403
    for path in ("/api/operations", "/api/audit", "/api/recovery"):
        assert _req(base + path, "GET", None, {"Host": "evil.example"})[0] == 403
    assert _req(base + "/api/audit", "POST", {})[0] == 404 and _req(base + "/api/audit", "DELETE")[0] == 405
    assert _req(base + "/api/recovery/flatten", "POST", {**body, "quantity": 1})[0] in (400, 409)


def test_http_audit_rejects_path_like_and_repeated_parameters(http):
    base, _ = http
    assert _req(base + "/api/audit?path=C:/Windows/win.ini")[0] == 400
    assert _req(base + "/api/audit?file=../../etc/passwd")[0] == 400
    assert _req(base + "/api/audit?limit=5&limit=6")[0] == 400
    assert _req(base + "/api/audit?limit=999999")[0] == 400
    assert _req(base + "/api/audit?session_uid=../x")[0] == 400


def test_http_forged_client_state_is_ignored_or_rejected(http):
    base, _ = http
    _req(base + "/api/paper/session", "POST", {"dataset_id": KR, "start_bar": 60})
    for forged in ({"flatten_available": True}, {"state": "NORMAL"}, {"kill_switch": False}, {"authorization": "x"}):
        status, _, _ = _req(base + "/api/recovery/flatten", "POST", {"operator": "o", "reason": "r", "confirm": "FLATTEN", **forged})
        assert status == 400
    assert _req(base + "/api/paper/proposals", "POST", {"side": "BUY", "quantity": 1, "client_request_id": "z", "recovery": True})[0] == 400


def test_new_modules_and_routes_contain_no_secret_material():
    root = pathlib.Path("src/qat")
    needles = ("serviceKey=", "api_key=", "BEGIN PRIVATE KEY")
    for path in list(root.glob("ops/*.py")) + list(root.glob("brokerage/*.py")) + [root / "ui" / "service.py", root / "ui" / "server.py"]:
        text = path.read_text(encoding="utf-8")
        # the audit scrubber legitimately names the patterns it redacts
        if path.name == "audit_store.py":
            continue
        assert not any(n in text for n in needles), path


# ======================================================================= E2E Paper scenarios
def test_e2e_normal_flow_rejection_and_restart_persistence(tmp_path):
    svc = _session()
    ok = _buy(svc, "n1", 10)
    assert ok["accepted"]
    svc.advance({"bars": 1})
    assert svc.session.stack.ledger.positions
    rejected = _buy(svc, "n2", 10**9)
    assert rejected["accepted"] is False and rejected["stage"] in ("risk", "validator", "net_alpha", "integrity", "compliance")
    uid = svc.session.session_uid
    svc.close()
    restarted = _svc()
    events = restarted.audit_view({"session_uid": uid, "limit": "500"})["events"]
    gates = [e["gate"]["status"] for e in events if e.get("gate")]
    assert "PASS" in gates and any(g != "PASS" for g in gates)
    assert any(e["event_type"] == "fill_settled" for e in events)
    assert restarted.restart["state"] == "SAFE_RECOVERY"
    assert restarted.audit_view({})["verify"]["ok"]


def test_e2e_risk_exit_hierarchy_holds_through_the_service(tmp_path):
    ledger_svc = _session()
    _holding(ledger_svc, 10)
    ledger_svc.session.stack.risk.max_total_exposure = 1e-9  # absurdly tight (fraction of equity): any increase is blocked, a reducing SELL is not trapped
    assert _buy(ledger_svc, "more", 10)["accepted"] is False
    sell = ledger_svc.submit_proposal({"side": "SELL", "quantity": 5, "order_type": "MARKET", "expected_gross_return": 0.0, "client_request_id": "exit1"})
    assert sell["accepted"] is True
    ledger_svc.engage_kill_switch({"reason": "x"})
    sell2 = ledger_svc.submit_proposal({"side": "SELL", "quantity": 1, "order_type": "MARKET", "expected_gross_return": 0.0, "client_request_id": "exit2"})
    assert sell2["accepted"] is False and "kill_switch" in sell2["reason"]


def test_e2e_state_persistence_failure_fails_closed(tmp_path, monkeypatch):
    svc = _session()
    import qat.ops.atomic as atomic
    monkeypatch.setattr(atomic.os, "replace", lambda *a: (_ for _ in ()).throw(OSError("disk")))
    _buy(svc, "pf")  # the action itself completes; the persistence failure is detected
    monkeypatch.undo()
    assert svc.state_fault
    with pytest.raises(ConflictError, match="state persistence fault"):
        _buy(svc, "pf2")
    assert svc.operations()["persistence"]["state_fault"]


def test_ops_endpoint_documents_single_process_limitation():
    ops = _svc().operations()
    assert "single process" in ops["persistence"]["process_model"] and "NOT supported" in ops["persistence"]["process_model"]
