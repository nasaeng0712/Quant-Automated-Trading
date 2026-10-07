"""QAT v1 final completion: stale-snapshot policy, R-07/R-08/R-09, MI-05/MI-06, durable audit store, strategy health, atomic state."""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone

import pytest

from conftest import make_proposal as mkprop

from qat.app import build_paper_stack
from qat.core.models import Currency, Market, Side
from qat.ops import market_rules as mr
from qat.ops.atomic import StateCorrupt, atomic_write_json, read_json_strict
from qat.ops.audit_store import AuditConflict, AuditCorrupt, DurableAuditSink, DurableAuditStore, scrub
from qat.ops.config import OpsConfigError, load_ops_config, validate_ops_config
from qat.ops.snapshot import Freshness, evaluate_freshness, reconcile_with_snapshot
from qat.ops.strategy_health import StrategyHealthMonitor

UTC = timezone.utc


def _now():
    return datetime.now(UTC)


# ======================================================================= freshness
@pytest.mark.parametrize("age,expected", [(0, Freshness.FRESH), (30, Freshness.FRESH), (31, Freshness.STALE), (3600, Freshness.STALE)])
def test_freshness_by_source_timestamp_age(age, expected):
    now = _now()
    res = evaluate_freshness(now - timedelta(seconds=age), now, now, 30)
    assert res.status is expected


def test_received_time_does_not_hide_a_stale_source_timestamp():
    now = _now()
    res = evaluate_freshness(now - timedelta(seconds=600), now, now, 30)  # just received, but the data is old
    assert res.status is Freshness.STALE


@pytest.mark.parametrize("snap,recv,mx", [
    (None, "now", 30), ("naive", "now", 30), ("future", "now", 30), ("old", None, 30), ("old", "before", 30), ("old", "now", 0), ("old", "now", float("nan")),
    ("old", "now", None), ("old", "now", True),
])
def test_freshness_unknown_is_never_fresh(snap, recv, mx):
    now = _now()
    snaps = {None: None, "naive": datetime.now(), "future": now + timedelta(seconds=600), "old": now - timedelta(seconds=1)}
    recvs = {"now": now, None: None, "before": now - timedelta(seconds=100)}
    res = evaluate_freshness(snaps[snap], recvs[recv], now, mx)
    assert res.status is Freshness.UNKNOWN, res


def test_stale_snapshot_cannot_attest_or_overwrite_reconciliation():
    stack = build_paper_stack(starting_cash=1_000_000)
    now = _now()
    ok, _ = reconcile_with_snapshot(stack.ledger, broker_cash={Currency.KRW: 1_000_000}, broker_positions={}, snapshot_ts=now, received_ts=now, now=now, max_age_seconds=30)
    assert ok.ok and stack.ledger.last_reconciliation["ok"] is True
    stale, fr = reconcile_with_snapshot(stack.ledger, broker_cash={Currency.KRW: 1_000_000}, broker_positions={}, snapshot_ts=now - timedelta(hours=1),
                                        received_ts=now, now=now, max_age_seconds=30)
    assert not stale.ok and fr.status is Freshness.STALE and "snapshot_stale" in stale.reasons
    assert stack.ledger.last_reconciliation["ok"] is True  # the earlier FRESH result is untouched, never replaced by a stale "match"


def _connected_stack(**risk):
    return build_paper_stack(starting_cash=1_000_000, risk_kwargs={"snapshot_policy": "broker_connected", "max_snapshot_age_seconds": 30, **risk})


def _snap(stack, age, *, cash=1_000_000, positions=None):
    now = _now()
    return reconcile_with_snapshot(stack.ledger, broker_cash={Currency.KRW: cash}, broker_positions=positions or {}, snapshot_ts=now - timedelta(seconds=age),
                                   received_ts=now, now=now, max_age_seconds=30)


def test_broker_connected_without_any_snapshot_is_unknown_for_new_risk():
    stack = _connected_stack()
    res = stack.submit_trade_proposal(mkprop(Market.KR, "005930", Side.BUY, 1), 100)
    assert not res.accepted and res.stage == "risk" and "snapshot_freshness_unknown" in res.reason


def test_broker_connected_fresh_matching_snapshot_allows_trading():
    stack = _connected_stack()
    _snap(stack, 1)
    assert stack.submit_trade_proposal(mkprop(Market.KR, "005930", Side.BUY, 1), 100).accepted


def test_broker_connected_stale_snapshot_blocks_new_risk():
    stack = _connected_stack()
    _snap(stack, 1)
    stack.ledger.record_snapshot_meta(_now() - timedelta(hours=1), _now())  # a later, stale snapshot arrives
    res = stack.submit_trade_proposal(mkprop(Market.KR, "005930", Side.BUY, 1), 100)
    assert not res.accepted and "stale_snapshot" in res.reason


def test_broker_connected_snapshot_from_the_future_is_unknown():
    stack = _connected_stack()
    now = _now()
    stack.ledger.record_snapshot_meta(now + timedelta(hours=1), now)
    stack.ledger.record_reconciliation(True, [])
    res = stack.submit_trade_proposal(mkprop(Market.KR, "005930", Side.BUY, 1), 100)
    assert not res.accepted and "snapshot_freshness_unknown" in res.reason


def test_fresh_snapshot_with_mismatch_blocks():
    stack = _connected_stack()
    _snap(stack, 1, cash=999_000)
    res = stack.submit_trade_proposal(mkprop(Market.KR, "005930", Side.BUY, 1), 100)
    assert not res.accepted and "reconciliation_mismatch" in res.reason


def _with_position(stack, qty=10):
    from qat.core.models import Fill
    stack.ledger.apply_fill(Fill(order_id="seed", market=Market.KR, symbol="005930", side=Side.BUY, quantity=qty, price=100, currency=Currency.KRW, fill_id="seed-1"))


def test_stale_snapshot_still_allows_a_reducing_exit_only_on_trusted_books():
    stack = _connected_stack()
    _with_position(stack)
    cash = stack.ledger.cash[Currency.KRW]
    _snap(stack, 1, cash=cash, positions={(Market.KR.value, "005930"): 10})
    stack.ledger.record_snapshot_meta(_now() - timedelta(hours=1), _now())  # stale, but the last FRESH reconciliation matched and books are consistent
    assert stack.submit_trade_proposal(mkprop(Market.KR, "005930", Side.SELL, 5), 100).accepted
    assert not stack.submit_trade_proposal(mkprop(Market.KR, "005930", Side.BUY, 1), 100).accepted  # risk-increasing stays blocked


def test_stale_snapshot_blocks_even_a_reducing_exit_when_accounting_is_untrusted():
    stack = _connected_stack()
    _with_position(stack)
    _snap(stack, 1, cash=stack.ledger.cash[Currency.KRW], positions={(Market.KR.value, "005930"): 10})
    stack.ledger.record_snapshot_meta(_now() - timedelta(hours=1), _now())
    stack.ledger.record_integrity_issue("fill_order_mismatch")
    res = stack.submit_trade_proposal(mkprop(Market.KR, "005930", Side.SELL, 5), 100)
    assert not res.accepted and "stale_snapshot" in res.reason and "accounting_untrusted" in res.reason


def test_kill_switch_still_blocks_everything_under_broker_connected():
    stack = _connected_stack(kill_switch=True)
    _with_position(stack)
    res = stack.submit_trade_proposal(mkprop(Market.KR, "005930", Side.SELL, 5), 100)
    assert not res.accepted and "kill_switch" in res.reason


# ======================================================================= R-07 / R-08 / R-09 / MI-05 / MI-06
CTX = mr.MarketContext(volume=1000, high=110, low=100, close=105, spread_bps=None, source="t")


def test_market_rule_functions_pure_semantics():
    assert mr.liquidity_shortage(CTX, None) == (mr.PASS, "not_configured")
    assert mr.liquidity_shortage(None, 10)[0] == mr.UNKNOWN
    assert mr.liquidity_shortage(CTX, 5000)[0] == mr.BLOCK
    assert mr.liquidity_shortage(mr.MarketContext(volume=0), 0)[0] == mr.BLOCK
    assert mr.liquidity_shortage(mr.MarketContext(volume=float("nan")), 1)[0] == mr.UNKNOWN
    assert mr.abnormal_spread(CTX, 50)[0] == mr.UNKNOWN  # OHLCV has no quotes: never a PASS
    assert mr.abnormal_spread(mr.MarketContext(spread_bps=80), 50)[0] == mr.BLOCK
    assert mr.abnormal_spread(mr.MarketContext(spread_bps=10), 50)[0] == mr.PASS
    assert mr.volatility_shock(CTX, 0.05)[0] == mr.BLOCK  # (110-100)/105 = 9.5%
    assert mr.volatility_shock(CTX, 0.2)[0] == mr.PASS
    assert mr.volatility_shock(mr.MarketContext(high=1, low=2, close=1), 0.2)[0] == mr.UNKNOWN
    assert mr.liquidity_participation(CTX, 500, 0.1)[0] == mr.BLOCK
    assert mr.liquidity_participation(CTX, 50, 0.1)[0] == mr.PASS
    assert mr.liquidity_participation(None, 50, 0.1)[0] == mr.UNKNOWN
    assert mr.cancel_replace_pattern([1, 2, 3], 4, 10, 2)[0] == mr.BLOCK
    assert mr.cancel_replace_pattern([1, 2, 3], 100, 10, 2)[0] == mr.PASS
    assert mr.cancel_replace_pattern([1, 2, 3], 4, 10, None)[1] == "not_configured"


def _mkt_stack(ctx=CTX, **kw):
    return build_paper_stack(starting_cash=1_000_000, risk_kwargs={"market_context_fn": lambda p: ctx, **kw.pop("risk", {})}, integrity_kwargs=kw.pop("integrity", None))


def test_r07_blocks_new_risk_on_illiquid_bar_but_not_an_exit():
    stack = _mkt_stack(risk={"min_bar_volume": 5000})
    res = stack.submit_trade_proposal(mkprop(Market.KR, "005930", Side.BUY, 1), 100)
    assert not res.accepted and "R-07" in res.reason
    _with_position(stack)
    assert stack.submit_trade_proposal(mkprop(Market.KR, "005930", Side.SELL, 5), 100).accepted  # frozen exit hierarchy: exits not trapped


def test_r08_unknown_when_no_quotes_and_configured():
    stack = _mkt_stack(risk={"max_spread_bps": 50})
    res = stack.submit_trade_proposal(mkprop(Market.KR, "005930", Side.BUY, 1), 100)
    assert not res.accepted and "R-08" in res.reason and "no_quote_data" in res.reason


def test_r09_blocks_volatility_shock():
    stack = _mkt_stack(risk={"volatility_shock_range_pct": 0.05})
    res = stack.submit_trade_proposal(mkprop(Market.KR, "005930", Side.BUY, 1), 100)
    assert not res.accepted and "R-09" in res.reason


def test_unconfigured_market_rules_are_inert():
    stack = _mkt_stack()
    assert stack.submit_trade_proposal(mkprop(Market.KR, "005930", Side.BUY, 1), 100).accepted


def test_r07_missing_context_is_unknown_not_pass():
    stack = build_paper_stack(starting_cash=1_000_000, risk_kwargs={"min_bar_volume": 10})
    res = stack.submit_trade_proposal(mkprop(Market.KR, "005930", Side.BUY, 1), 100)
    assert not res.accepted and "R-07" in res.reason and "unavailable" in res.reason


def test_kill_switch_beats_market_rule_relaxation():
    stack = _mkt_stack(risk={"min_bar_volume": 5000, "kill_switch": True})
    _with_position(stack)
    assert not stack.submit_trade_proposal(mkprop(Market.KR, "005930", Side.SELL, 5), 100).accepted


def test_mi06_participation_blocks_oversized_orders():
    stack = _mkt_stack(integrity={"max_participation_rate": 0.01, "market_context_fn": lambda p: CTX})
    res = stack.submit_trade_proposal(mkprop(Market.KR, "005930", Side.BUY, 50), 100)  # 50/1000 = 5% > 1%
    assert not res.accepted and "MI-06" in res.reason
    assert stack.submit_trade_proposal(mkprop(Market.KR, "005930", Side.BUY, 5), 100).accepted


def test_mi06_unknown_volume_is_unknown():
    stack = _mkt_stack(integrity={"max_participation_rate": 0.01})
    res = stack.submit_trade_proposal(mkprop(Market.KR, "005930", Side.BUY, 1), 100)
    assert not res.accepted and "MI-06" in res.reason


def test_mi05_cancel_replace_pattern_blocks_after_too_many_cancels():
    stack = _mkt_stack(integrity={"max_cancels_per_window": 2, "cancel_window_seconds": 600})
    for _ in range(3):
        res = stack.submit_trade_proposal(mkprop(Market.KR, "005930", Side.BUY, 1), 100)
        if not res.accepted:
            break
        stack.cancel_order(res.order_id)
        stack.integrity._events.clear()  # isolate MI-05 from the duplicate / frequency rules
    res = stack.submit_trade_proposal(mkprop(Market.KR, "005930", Side.BUY, 1), 100)
    assert not res.accepted and "MI-05" in res.reason


# ======================================================================= ops config
def test_ops_config_defaults_load_and_unset_rules_are_not_configured():
    cfg = load_ops_config()
    assert cfg["mode"] == "paper" and cfg["snapshot"]["policy"] == "paper_replay"
    assert all(v is None for k, v in cfg["market_rules"].items() if k not in ("cancel_window_seconds",))


@pytest.mark.parametrize("mutate", [
    lambda r: r.update(version=2), lambda r: r.update(mode="demo"), lambda r: r["snapshot"].update(policy="bogus"),
    lambda r: r["snapshot"].update(policy="broker_connected", max_age_seconds=None), lambda r: r["snapshot"].update(policy="broker_connected", max_age_seconds=-1),
    lambda r: r["snapshot"].update(future_tolerance_seconds=-1), lambda r: r["market_rules"].update(min_bar_volume=float("nan")),
    lambda r: r["market_rules"].update(max_participation_rate=2), lambda r: r["market_rules"].update(volatility_shock_range_pct=True),
    lambda r: r["strategy_health"].update(rejection_concentration=1.5), lambda r: r["audit"].update(max_page_size=0),
    lambda r: r["paper_graduation"].update(min_completed_sessions=0),
])
def test_ops_config_rejects_invalid_values(mutate):
    import yaml
    raw = yaml.safe_load(open("config/ops.yaml", encoding="utf-8"))
    mutate(raw)
    with pytest.raises(OpsConfigError):
        validate_ops_config(raw)


# ======================================================================= atomic state
def test_atomic_write_and_corrupt_detection(tmp_path):
    path = tmp_path / "s.json"
    assert read_json_strict(path, default={"x": 1}) == {"x": 1}
    atomic_write_json(path, {"a": 1})
    assert read_json_strict(path) == {"a": 1}
    assert not list(tmp_path.glob("*.tmp"))
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(StateCorrupt):
        read_json_strict(path)
    path.write_text("[1,2]", encoding="utf-8")
    with pytest.raises(StateCorrupt):
        read_json_strict(path)


def test_atomic_write_failure_keeps_the_previous_file(tmp_path, monkeypatch):
    path = tmp_path / "s.json"
    atomic_write_json(path, {"v": 1})
    import qat.ops.atomic as atomic
    monkeypatch.setattr(atomic.os, "replace", lambda *a: (_ for _ in ()).throw(OSError("disk")))
    with pytest.raises(OSError):
        atomic_write_json(path, {"v": 2})
    assert json.loads(path.read_text(encoding="utf-8")) == {"v": 1}
    assert not list(tmp_path.glob("*.tmp"))


# ======================================================================= durable audit
def _ev(i, **kw):
    return {"event_id": f"e{i}", "event_type": kw.pop("event_type", "t"), "timestamp": "2026-01-01T00:00:00+00:00", **kw}


def test_audit_store_roundtrip_chain_and_restart(tmp_path):
    store = DurableAuditStore(tmp_path / "a.jsonl")
    for i in range(5):
        store.append(_ev(i, session_uid="S1"))
    assert store.verify()["ok"] and store.verify()["records"] == 5
    again = DurableAuditStore(tmp_path / "a.jsonl")  # restart: readable, appends continue the chain
    assert again.read(limit=10)["total"] == 5
    again.append(_ev(5))
    assert DurableAuditStore(tmp_path / "a.jsonl").verify()["records"] == 6
    page = again.read(offset=1, limit=2, descending=False)
    assert [r["seq"] for r in page["events"]] == [2, 3]


def test_audit_store_idempotent_and_conflicting_event_ids(tmp_path):
    store = DurableAuditStore(tmp_path / "a.jsonl")
    first = store.append(_ev(1, x=1))
    assert store.append(_ev(1, x=1))["hash"] == first["hash"] and store.verify()["records"] == 1
    with pytest.raises(AuditConflict):
        store.append(_ev(1, x=2))


def test_audit_store_requires_event_identity(tmp_path):
    store = DurableAuditStore(tmp_path / "a.jsonl")
    with pytest.raises(ValueError):
        store.append({"event_type": "t"})


@pytest.mark.parametrize("damage", ["malformed", "truncated", "tamper", "delete_line", "duplicate_line", "bad_chain"])
def test_audit_store_detects_corruption(tmp_path, damage):
    path = tmp_path / "a.jsonl"
    store = DurableAuditStore(path)
    for i in range(4):
        store.append(_ev(i, v=i))
    lines = path.read_text(encoding="utf-8").splitlines()
    if damage == "malformed":
        lines[1] = lines[1][:20] + "garbage"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    elif damage == "truncated":
        path.write_text("\n".join(lines) + "\n" + lines[0][:30], encoding="utf-8")
    elif damage == "tamper":
        path.write_text("\n".join(lines).replace('"v":2', '"v":9') + "\n", encoding="utf-8")
    elif damage == "delete_line":
        del lines[1]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    elif damage == "duplicate_line":
        lines.insert(2, lines[1])
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    elif damage == "bad_chain":
        rec = json.loads(lines[2])
        rec["prev_hash"] = "f" * 64
        lines[2] = json.dumps(rec, sort_keys=True, separators=(",", ":"))
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    report = DurableAuditStore(path).verify()
    assert report["ok"] is False and report["errors"]
    with pytest.raises(AuditCorrupt):
        DurableAuditStore(path).append(_ev(99))  # a corrupt log refuses further appends


def test_audit_store_write_failure_enters_fault_and_refuses(tmp_path, monkeypatch):
    store = DurableAuditStore(tmp_path / "a.jsonl")
    store.append(_ev(1))
    import qat.ops.audit_store as mod
    monkeypatch.setattr(mod.os, "fsync", lambda fd: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(AuditCorrupt):
        store.append(_ev(2))
    assert store.healthy is False
    monkeypatch.undo()
    with pytest.raises(AuditCorrupt):
        store.append(_ev(3))  # stays refused


def test_audit_scrubs_secrets_and_never_stores_them(tmp_path):
    store = DurableAuditStore(tmp_path / "a.jsonl")
    secret = "SUPERSECRETVALUE123"
    store.append(_ev(1, serviceKey=secret, nested={"api_key": secret, "ok": "fine", "url": f"https://x/y?serviceKey={secret}&a=1"}))
    raw = (tmp_path / "a.jsonl").read_text(encoding="utf-8")
    assert secret not in raw and "<REDACTED>" in raw and "fine" in raw
    assert scrub({"password": "x"}) == {"password": "<REDACTED>"}


def test_audit_sink_persists_pipeline_events_and_never_raises(tmp_path):
    store = DurableAuditStore(tmp_path / "a.jsonl")
    sink = DurableAuditSink(store, session_id="S1", session_uid="S1-x")
    stack = build_paper_stack(starting_cash=1_000_000, audit_sink=sink)
    res = stack.submit_trade_proposal(mkprop(Market.KR, "005930", Side.BUY, 1), 100)
    assert res.accepted
    types = {r["event_type"] for r in store.read(limit=500)["events"]}
    assert {"proposal_received", "order_submitted"} <= types
    assert any(r.get("gate") for r in store.read(limit=500)["events"])
    store.fault = "forced"  # a broken store must not break the order pipeline
    res2 = stack.submit_trade_proposal(mkprop(Market.KR, "005930", Side.BUY, 2), 100)
    assert res2.accepted is not None and sink.errors >= 1


def test_audit_deterministic_serialization(tmp_path):
    a, b = DurableAuditStore(tmp_path / "a.jsonl"), DurableAuditStore(tmp_path / "b.jsonl")
    for store in (a, b):
        store.append({"event_id": "x", "event_type": "t", "b": 1, "a": [1, 2]})
    la = json.loads((tmp_path / "a.jsonl").read_text(encoding="utf-8"))
    lb = json.loads((tmp_path / "b.jsonl").read_text(encoding="utf-8"))
    assert la["content_hash"] == lb["content_hash"]


# ======================================================================= strategy health
T = {"max_consecutive_errors": 3, "max_data_age_seconds": 100, "max_heartbeat_age_seconds": 100, "rejection_window": 10, "rejection_concentration": 0.8,
     "min_rejections_for_concentration": 5}


class Clock:
    def __init__(self):
        self.t = datetime(2026, 1, 1, tzinfo=UTC)

    def now(self):
        return self.t


def _mon():
    clock = Clock()
    return StrategyHealthMonitor("s", thresholds=T, now_fn=clock.now), clock


def test_health_unknown_until_initialized_and_observed():
    m, _ = _mon()
    assert m.evaluate().status == "UNKNOWN"
    m.mark_initialized()
    assert m.evaluate().status == "UNKNOWN"  # initialized but nothing observed yet


def test_health_no_trades_is_not_a_failure():
    m, c = _mon()
    m.mark_initialized()
    m.record_data(c.now())
    for _ in range(5):
        m.record_evaluation(ok=True)
    rep = m.evaluate()
    assert rep.status == "HEALTHY" and any("no signals" in n for n in rep.notes)


def test_health_error_streak_degrades_then_fails_and_recovers():
    m, c = _mon()
    m.mark_initialized()
    m.record_data(c.now())
    m.record_evaluation(ok=False, error="boom")
    assert m.evaluate().status == "DEGRADED"
    m.record_evaluation(ok=False)
    m.record_evaluation(ok=False)
    assert m.evaluate().status == "UNHEALTHY"
    m.record_evaluation(ok=True)
    assert m.evaluate().status == "HEALTHY"


def test_health_stale_data_and_missing_heartbeat_degrade():
    m, c = _mon()
    m.mark_initialized()
    m.record_data(c.now())
    m.record_evaluation(ok=True)
    c.t += timedelta(seconds=500)
    rep = m.evaluate()
    assert rep.status == "DEGRADED" and any(r.startswith("data_stale") for r in rep.reasons) and "evaluation_heartbeat_missing" in rep.reasons


def test_health_rejection_concentration_and_carry_over():
    m, c = _mon()
    m.mark_initialized()
    m.record_data(c.now())
    m.record_evaluation(ok=True)
    for _ in range(8):
        m.record_proposal(accepted=False, reason="risk:BLOCK:max_order_notional")
    assert any(r.startswith("rejection_concentration") for r in m.evaluate().reasons)
    m.record_state_carry_over(True)
    assert m.evaluate().status == "UNHEALTHY"


def test_health_never_uses_performance_and_never_acts():
    m, _ = _mon()
    assert not hasattr(m, "record_pnl") and not hasattr(m, "disable") and not hasattr(m, "retrain")
    assert math.isfinite(1.0)


# ======================================================================= research store: atomic + corrupt-state detection
def test_research_registry_is_atomic_and_a_corrupt_registry_is_never_read_as_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("QAT_RESULTS_DIR", str(tmp_path / "results"))
    from qat.research.store import registry_read, registry_write, results_root
    assert registry_read("oos_registry.json") == {}
    registry_write("oos_registry.json", {"a": 1})
    assert registry_read("oos_registry.json") == {"a": 1}
    assert not list(results_root().glob("*.tmp"))
    (results_root() / "oos_registry.json").write_text("{truncated", encoding="utf-8")
    with pytest.raises(StateCorrupt):
        registry_read("oos_registry.json")  # would otherwise silently reset OOS accounting
