"""Batch #2 Independent Audit - reproductions, regressions and invariants.

Sections D1..D10 are defects that were reproduced on the pre-audit code (see
docs/QAT_검증_이력.md section 12) and are now fixed. The remaining sections pin
audit areas where no defect was found (accounting invariants, pipeline order,
kill switch, cost stress, manifests) and self-test the leakage detectors so they
are known to catch a real look-ahead / contamination mutation (non-vacuous).
"""

from __future__ import annotations

import hashlib
import json
import math
import pathlib
import random
import threading
import urllib.error
import urllib.request
from dataclasses import replace

import pytest

from conftest import make_proposal as mk

from qat.app import build_paper_stack
from qat.core.fx import StaticFXRateProvider
from qat.core.models import Currency, Market, Position, Side, currency_for
from qat.data.bars import Bar, DataError, DatasetMeta
from qat.data.loader import load_dataset
from qat.portfolio.ledger import PortfolioLedger
from qat.portfolio.reconciliation import reconcile
from qat.research import backtest as bt_module
from qat.research import walkforward as wf_module
from qat.research.backtest import BacktestConfig, run_backtest
from qat.research.store import load_run
from qat.research.strategies import History, make_strategy
from qat.research.walkforward import (
    LockboxAlreadyUsed,
    WalkForwardConfig,
    evaluate_lockbox,
    run_cost_stress,
    run_walkforward,
)
from qat.risk.gate import RiskGate
from qat.ui.server import make_server
from qat.ui.service import QATService, ServiceError

ROOT = pathlib.Path(__file__).resolve().parents[1]
KR = "data/fixtures/SYN_KR1_1d.csv"
US = "data/fixtures/SYN_US1_1d.csv"
KR_PATH = ROOT / KR
FIXTURE = {"alpha_mode": "fixture", "fixture_expected_return": 0.01}
BASE_YAML = (ROOT / "config" / "settings.yaml").read_text(encoding="utf-8")
HEADER = "timestamp,open,high,low,close,volume\n"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("QAT_RESULTS_DIR", str(tmp_path / "results"))
    monkeypatch.setenv("QAT_STATE_DIR", str(tmp_path / "state"))


def settings_file(tmp_path, *replacements, name="s.yaml"):
    text = BASE_YAML
    for old, new in replacements:
        assert old in text, old
        text = text.replace(old, new)
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return str(path)


NO_FX = (
    "    - { from: USD, to: KRW, rate: 1350.0 }\n    - { from: USDT, to: KRW, rate: 1350.0 }\n",
    "",
)


def write_dataset(tmp_path, body, meta, name="d.csv"):
    path = tmp_path / name
    path.write_text(HEADER + body, encoding="utf-8")
    (tmp_path / (name + ".meta.json")).write_text(json.dumps(meta), encoding="utf-8")
    return path


CRYPTO_META = {"market": "CRYPTO", "symbol": "T/KRW", "timeframe": "1h", "timezone": "UTC",
               "timestamp_label": "open", "source": "TEST", "synthetic": False}


# ============================================================ D1 reconciliation non-finite
@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_d1_non_finite_broker_cash_is_a_mismatch(bad):
    ledger = PortfolioLedger(1000)
    res = reconcile(ledger, broker_cash={Currency.KRW: bad}, broker_positions={})
    assert not res.ok and any("non_finite" in r for r in res.reasons)


@pytest.mark.parametrize("bad", [math.nan, math.inf])
def test_d1_non_finite_broker_position_is_a_mismatch(bad):
    ledger = PortfolioLedger(1000)
    ledger.positions[("KR", "X")] = Position(5, 100)
    res = reconcile(ledger, broker_cash={Currency.KRW: 1000}, broker_positions={("KR", "X"): bad})
    assert not res.ok and any("non_finite" in r for r in res.reasons)


@pytest.mark.parametrize("tol", [math.nan, -1.0, math.inf])
def test_d1_invalid_tolerance_rejected(tol):
    with pytest.raises(ValueError):
        reconcile(PortfolioLedger(1000), broker_cash={Currency.KRW: 1000}, broker_positions={}, tol=tol)


def test_reconciliation_case_matrix():
    ledger = PortfolioLedger({Currency.KRW: 1000, Currency.USD: 10})
    ledger.positions[("KR", "A")] = Position(5, 100)
    ok = reconcile(ledger, broker_cash={Currency.KRW: 1000, Currency.USD: 10},
                   broker_positions={("KR", "A"): 5})
    assert ok.ok
    assert not reconcile(ledger, broker_cash={}, broker_positions={}).ok  # empty
    assert not reconcile(ledger, broker_cash={Currency.KRW: 1000, Currency.USD: 10},
                         broker_positions={("KR", "A"): 5}, complete=False).ok  # incomplete
    assert not reconcile(ledger, broker_cash={Currency.KRW: 1000, Currency.USD: 10},
                         broker_positions={("KR", "A"): 5, ("KR", "B"): 1}).ok  # broker-only key
    assert not reconcile(ledger, broker_cash={Currency.KRW: 1000, Currency.USD: 10},
                         broker_positions={}).ok  # ledger-only key
    assert not reconcile(ledger, broker_cash={Currency.KRW: 1001, Currency.USD: 10},
                         broker_positions={("KR", "A"): 5}).ok  # cash
    assert not reconcile(ledger, broker_cash={Currency.KRW: 1000, Currency.USD: 10},
                         broker_positions={("KR", "A"): 4}).ok  # quantity


# ============================================================ D2 API payload robustness
def _session(svc=None, dataset=KR, start_bar=60):
    svc = svc or QATService()
    svc.start_session({"dataset_id": dataset, "start_bar": start_bar})
    return svc


@pytest.mark.parametrize("body", [
    {"start_bar": math.inf}, {"start_bar": -math.inf}, {"start_bar": math.nan}, {"start_bar": True},
    {"start_bar": 1.5}, {"initial_cash": math.inf}, {"initial_cash": math.nan}, {"initial_cash": 1e15},
    {"initial_cash": True}, {"initial_cash": "abc"},
])
def test_d2_session_payloads_are_rejected_cleanly(body):
    with pytest.raises(ServiceError):
        QATService().start_session({"dataset_id": KR, **body})


@pytest.mark.parametrize("bars", [math.inf, math.nan, True, 0, 251, 1.5, "x"])
def test_d2_advance_payloads_are_rejected_cleanly(bars):
    svc = _session()
    with pytest.raises(ServiceError):
        svc.advance({"bars": bars})


@pytest.mark.parametrize("field", ["train_bars", "test_bars", "step_bars", "lockbox_bars"])
def test_d2_walkforward_int_fields_reject_infinity(field):
    with pytest.raises(ServiceError):
        QATService().run_walkforward({"dataset_id": KR, "strategy": "ma_trend", field: math.inf})


@pytest.mark.parametrize("field,value", [("quantity", True), ("quantity", False),
                                         ("expected_gross_return", True), ("limit_price", True)])
def test_d2_boolean_is_not_a_number(field, value):
    svc = _session()
    body = {"side": "BUY", "quantity": 1, "expected_gross_return": 0.05, "client_request_id": "b", **{field: value}}
    if field == "limit_price":
        body["order_type"] = "LIMIT"
    with pytest.raises(ServiceError):
        svc.submit_proposal(body)
    assert svc.session.stack.broker.orders == {}


def test_d2_research_money_fields_are_bounded():
    svc = QATService()
    for extra in ({"initial_cash": 1e15}, {"initial_cash": math.inf}, {"cost_multiplier": math.nan},
                  {"position_fraction": True}, {"multipliers": [1, math.inf]}, {"multipliers": [-1]},
                  {"multipliers": "1,2"}):
        with pytest.raises(ServiceError):
            svc.run_stress({"dataset_id": KR, "strategy": "ma_trend", **extra})


@pytest.fixture
def http():
    server = make_server("127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}", server
    server.shutdown()
    server.server_close()


def _req(url, method="GET", raw=None, headers=None):
    hdrs = {"Content-Type": "application/json", "X-QAT-Client": "ui", **(headers or {})}
    req = urllib.request.Request(url, data=raw, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def test_d2_http_json_infinity_literal_is_400_not_500(http):
    base, _ = http
    status, _ = _req(base + "/api/paper/session", "POST", b'{"dataset_id": "%s", "start_bar": Infinity}' % KR.encode())
    assert status == 400
    _req(base + "/api/paper/session", "POST", json.dumps({"dataset_id": KR}).encode())
    for raw in (b'{"bars": Infinity}', b'{"bars": NaN}'):
        assert _req(base + "/api/paper/advance", "POST", raw)[0] == 400
    body = b'{"side":"BUY","quantity":NaN,"client_request_id":"n1"}'
    assert _req(base + "/api/paper/proposals", "POST", body)[0] == 400
    body = b'{"side":"BUY","quantity":Infinity,"client_request_id":"n2"}'
    assert _req(base + "/api/paper/proposals", "POST", body)[0] == 400


# ============================================================ D3 strategy parameter validation
@pytest.mark.parametrize("name,params", [
    ("ma_trend", {"fast": 0}), ("ma_trend", {"slow": 0}), ("ma_trend", {"fast": -5}),
    ("ma_trend", {"fast": math.nan}), ("ma_trend", {"fast": "abc"}), ("ma_trend", {"fast": 1.5}),
    ("ma_trend", {"fast": True}), ("ma_trend", {"horizon": 0}), ("ma_trend", {"min_samples": 0}),
    ("breakout", {"entry_lookback": 0}), ("breakout", {"exit_lookback": -1}),
    ("mean_reversion", {"window": 0}), ("mean_reversion", {"entry_z": 0}),
    ("mean_reversion", {"entry_z": math.inf}), ("mean_reversion", {"exit_z": math.nan}),
    ("ma_trend", {"alpha_mode": "fixture", "fixture_expected_return": math.nan}),
])
def test_d3_invalid_strategy_params_raise(name, params):
    with pytest.raises(ValueError):
        make_strategy(name, **params)
    with pytest.raises(ServiceError):
        QATService().run_backtest({"dataset_id": KR, "strategy": name, "params": params})


def test_d3_integral_float_is_normalised():
    strat = make_strategy("ma_trend", fast=10.0, slow=30.0)
    assert strat.params["fast"] == 10 and isinstance(strat.params["fast"], int)


# ============================================================ D4 drawdown limit must not be inert
def test_d4_risk_gate_drawdown_without_baseline_is_unknown():
    gate = RiskGate(PortfolioLedger(1000), max_drawdown=0.01)
    decision = gate.evaluate(mk(Market.KR, "X", Side.BUY, 1), 10)
    assert decision.status.value == "UNKNOWN" and "drawdown_baseline_unknown" in decision.reasons
    gate.observe_equity(1000)
    assert gate.evaluate(mk(Market.KR, "X", Side.BUY, 1), 10).ok


def test_d4_ui_session_enforces_configured_drawdown(tmp_path):
    path = settings_file(tmp_path, ("max_drawdown: null", "max_drawdown: 0.01"))
    svc = _session(QATService(path), start_bar=100)
    first = svc.submit_proposal({"side": "BUY", "quantity": 100, "expected_gross_return": 0.05,
                                 "client_request_id": "d1"})
    assert first["accepted"], first["reason"]
    for _ in range(60):
        svc.advance({"bars": 1})
        dd = svc.overview()["session"]["drawdown"]
        if dd and dd > 0.02:
            break
    assert dd > 0.02
    blocked = svc.submit_proposal({"side": "BUY", "quantity": 1, "expected_gross_return": 0.05,
                                   "client_request_id": "d2"})
    assert not blocked["accepted"] and "max_drawdown" in blocked["reason"]
    # UI and backend agree about the configured limit
    rule = next(r for r in svc.risk()["risk_rules"] if r["id"] == "R-06")
    assert rule["value"] == svc.session.stack.risk.max_drawdown == 0.01


def test_d4_backtest_with_missing_fx_and_drawdown_limit_fails_closed(tmp_path):
    path = settings_file(tmp_path, NO_FX, ("rates:", "rates: []\n    #"),
                         ("max_drawdown: null", "max_drawdown: 0.01"))
    res = run_backtest(load_dataset(ROOT / US), make_strategy("ma_trend", **FIXTURE),
                       BacktestConfig(initial_cash=10_000, settings_path=path))
    assert res.metrics["fills"] == 0, "configured limit silently ignored (pre-audit: 27 fills)"
    assert res.rejections and all(r["stage"] == "risk" and "UNKNOWN" in r["reason"] for r in res.rejections)


def test_d4_backtest_drawdown_limit_is_enforced_in_base_currency(tmp_path):
    path = settings_file(tmp_path, ("max_drawdown: null", "max_drawdown: 0.05"))
    res = run_backtest(load_dataset(KR_PATH), make_strategy("ma_trend", **FIXTURE),
                       BacktestConfig(settings_path=path))
    assert any("max_drawdown" in r["reason"] for r in res.rejections)


# ============================================================ D5 daily loss resets each day
def test_d5_ui_daily_loss_limit_is_per_day(tmp_path):
    path = settings_file(tmp_path, ("daily_loss_limit: null", "daily_loss_limit: 5000"))
    svc = _session(QATService(path), start_bar=100)
    assert svc.submit_proposal({"side": "BUY", "quantity": 100, "expected_gross_return": 0.05,
                                "client_request_id": "b1"})["accepted"]
    svc.advance({"bars": 1})
    s = svc.session
    for _ in range(60):
        pos = s.stack.ledger.get_position("KR", "SYNKR1")
        if s.marks[s.key] < pos.avg_cost * 0.97:
            break
        svc.advance({"bars": 1})
    assert svc.submit_proposal({"side": "SELL", "quantity": 100, "expected_gross_return": 0.05,
                                "client_request_id": "s1"})["accepted"]
    svc.advance({"bars": 1})
    assert s.stack.ledger.realized_pnl["KRW"] < -5000
    same_day = svc.submit_proposal({"side": "BUY", "quantity": 1, "expected_gross_return": 0.05,
                                    "client_request_id": "b2"})
    assert not same_day["accepted"] and "daily_loss_limit" in same_day["reason"]
    svc.advance({"bars": 1})  # next trading day: the loss baseline resets (as in the backtest)
    next_day = svc.submit_proposal({"side": "BUY", "quantity": 1, "expected_gross_return": 0.05,
                                    "client_request_id": "b3"})
    assert next_day["accepted"], next_day["reason"]


# ============================================================ D6 data engine input errors
def test_d6_overlapping_bars_fail(tmp_path):
    body = "2024-01-01T00:00:00,1,1,1,1,1\n2024-01-01T00:30:00,1,1,1,1,1\n2024-01-01T01:00:00,1,1,1,1,1\n"
    ds = load_dataset(write_dataset(tmp_path, body, CRYPTO_META))
    assert ds.validation.status == "FAIL" and not ds.usable
    assert any(i["code"] == "overlapping_bars" for i in ds.validation.issues)


def test_d6_daily_bars_tolerate_dst_hour(tmp_path):
    meta = {**CRYPTO_META, "timeframe": "1d", "market": "US", "symbol": "TST"}
    body = ("2024-03-09T00:00:00-05:00,1,1,1,1,1\n2024-03-10T00:00:00-05:00,1,1,1,1,1\n"
            "2024-03-11T00:00:00-04:00,1,1,1,1,1\n")
    ds = load_dataset(write_dataset(tmp_path, body, meta))
    assert not any(i["code"] == "overlapping_bars" for i in ds.validation.issues)


@pytest.mark.parametrize("market,symbol", [("KR", "005930/KRW"), ("US", "AAPL/USD"),
                                           ("CRYPTO", "BTCKRW"), ("CRYPTO", "BTC/EUR"), ("KR", "")])
def test_d6_inconsistent_market_metadata_rejected(market, symbol):
    with pytest.raises(DataError):
        DatasetMeta(market, symbol, "1d", "UTC")


def test_d6_synthetic_source_must_be_flagged_synthetic():
    with pytest.raises(DataError):
        DatasetMeta("KR", "A", "1d", "UTC", source="SYNTHETIC", synthetic=False)
    with pytest.raises(DataError):
        DatasetMeta("KR", "A", "1d", "UTC", source="LOCAL_FILE", synthetic=True)


def test_d6_malformed_files_raise_data_error(tmp_path):
    good = "2024-01-01T00:00:00,1,1,1,1,1\n"
    for name, meta_tz in (("tz.csv", "+25:00"),):
        path = write_dataset(tmp_path, good, {**CRYPTO_META, "timezone": meta_tz}, name)
        with pytest.raises(DataError):
            load_dataset(path)
    binary = tmp_path / "bin.csv"
    binary.write_bytes(b"\xff\xfe\x00garbage\x80")
    (tmp_path / "bin.csv.meta.json").write_text(json.dumps(CRYPTO_META))
    with pytest.raises(DataError):
        load_dataset(binary)
    empty = tmp_path / "empty.csv"
    empty.write_bytes(b"")
    (tmp_path / "empty.csv.meta.json").write_text(json.dumps(CRYPTO_META))
    with pytest.raises(DataError):
        load_dataset(empty)
    bad_meta = write_dataset(tmp_path, good, CRYPTO_META, "bm.csv")
    (tmp_path / "bm.csv.meta.json").write_text("{not json")
    with pytest.raises(DataError):
        load_dataset(bad_meta)
    (tmp_path / "bm.csv.meta.json").write_text("[1, 2]")
    with pytest.raises(DataError):
        load_dataset(bad_meta)
    no_cols = tmp_path / "nc.csv"
    no_cols.write_text("timestamp,open,close\n2024-01-01,1,1\n")
    (tmp_path / "nc.csv.meta.json").write_text(json.dumps(CRYPTO_META))
    with pytest.raises(DataError):
        load_dataset(no_cols)


def test_data_version_is_deterministic_and_content_addressed(tmp_path):
    copy = tmp_path / "copy.csv"
    copy.write_bytes(KR_PATH.read_bytes())
    (tmp_path / "copy.csv.meta.json").write_bytes(KR_PATH.with_name(KR_PATH.name + ".meta.json").read_bytes())
    a, b, c = load_dataset(KR_PATH), load_dataset(KR_PATH), load_dataset(copy)
    assert a.data_version == b.data_version == c.data_version
    assert a.sha256 == hashlib.sha256(KR_PATH.read_bytes()).hexdigest() == c.sha256
    copy.write_bytes(copy.read_bytes().replace(b"\n", b"\r\n"))  # same numbers, different bytes
    assert load_dataset(copy).data_version != a.data_version


# ============================================================ D7 strategy state reuse
def _scaled(ds, mult):
    bars = [Bar(b.ts, b.open * mult, b.high * mult, b.low * mult, b.close * mult, b.volume) for b in ds.bars]
    return replace(ds, bars=bars, data_version=f"x{mult}")


@pytest.mark.parametrize("name", ["ma_trend", "breakout", "mean_reversion"])
def test_d7_reused_strategy_instance_carries_no_state(name):
    ds = load_dataset(KR_PATH)
    other = _scaled(ds, 2)
    cfg = BacktestConfig()
    reused = make_strategy(name, **FIXTURE)
    run_backtest(ds, reused, replace(cfg, trade_start=10, trade_end=300))
    again = run_backtest(other, reused, replace(cfg, trade_start=320, trade_end=745))
    fresh = run_backtest(other, make_strategy(name, **FIXTURE), replace(cfg, trade_start=320, trade_end=745))
    assert again.fills == fresh.fills and again.orders == fresh.orders and again.equity == fresh.equity
    assert fresh.orders, "non-vacuous: the compared window must contain orders"


# ============================================================ D8 lockbox is one-shot
def _wf(ds, lockbox):
    return run_walkforward(ds, "ma_trend", WalkForwardConfig(train_bars=250, test_bars=100, lockbox_bars=lockbox,
                                                             grid={"fast": [10], "slow": [30]}), FIXTURE)


def test_d8_overlapping_lockbox_range_counts_as_reuse(tmp_path):
    ds = load_dataset(KR_PATH)
    first = evaluate_lockbox(ds, _wf(ds, 100)["manifest"]["run_id"])
    assert first["independent"] is True
    smaller = _wf(ds, 50)["manifest"]["run_id"]  # bars 700-750 lie inside the used 650-750
    with pytest.raises(LockboxAlreadyUsed):
        evaluate_lockbox(ds, smaller)
    assert evaluate_lockbox(ds, smaller, acknowledge_reuse=True)["independent"] is False
    registry = json.loads((tmp_path / "results" / "lockbox_registry.json").read_text(encoding="utf-8"))
    assert sum(len(v) for v in registry.values()) == 2  # persisted on disk -> survives a restart


def test_d8_disjoint_lockbox_is_independent():
    ds = load_dataset(KR_PATH)
    wf_a, wf_b = _wf(ds, 50), _wf(ds, 100)
    assert evaluate_lockbox(ds, wf_a["manifest"]["run_id"])["independent"] is True  # 700-750
    # 650-750 overlaps 700-750 -> refused
    with pytest.raises(LockboxAlreadyUsed):
        evaluate_lockbox(ds, wf_b["manifest"]["run_id"])


# ============================================================ D9 UI request ids
def test_d9_proposal_ids_never_collide_after_request_cache_fills():
    svc = _session()
    reasons = []
    for i in range(503):
        reasons.append(svc.submit_proposal({"side": "BUY", "quantity": 1, "expected_gross_return": 0.05,
                                            "client_request_id": f"q{i}"})["reason"])
    assert not any("duplicate_proposal" in r for r in reasons)
    orders_before = len(svc.session.stack.broker.orders)
    replay = svc.submit_proposal({"side": "BUY", "quantity": 1, "expected_gross_return": 0.05,
                                  "client_request_id": "q0"})
    assert replay["idempotent_replay"] is True
    assert len(svc.session.stack.broker.orders) == orders_before


def test_d9_evicted_request_id_is_never_re_executed():
    svc = _session()
    svc.session.requests = {}  # simulate eviction of every cached payload
    first = svc.submit_proposal({"side": "BUY", "quantity": 1, "expected_gross_return": 0.05, "client_request_id": "e1"})
    assert first["accepted"]
    svc.session.requests.clear()
    again = svc.submit_proposal({"side": "BUY", "quantity": 1, "expected_gross_return": 0.05, "client_request_id": "e1"})
    assert again["idempotent_replay"] is True and again["accepted"] is None
    assert len(svc.session.stack.broker.orders) == 1


# ============================================================ D10 Host / Origin
def test_d10_non_loopback_host_and_origin_are_refused(http):
    base, server = http
    assert _req(base + "/api/status")[0] == 200
    assert _req(base + "/api/status", headers={"Host": "localhost:8765"})[0] == 200
    assert _req(base + "/api/status", headers={"Host": "evil.example"})[0] == 403
    assert _req(base + "/", headers={"Host": "evil.example:8765"})[0] == 403
    body = json.dumps({"reason": "x"}).encode()
    assert _req(base + "/api/paper/kill-switch", "POST", body, {"Host": "evil.example"})[0] == 403
    assert _req(base + "/api/paper/kill-switch", "POST", body, {"Origin": "http://evil.example"})[0] == 403
    assert _req(base + "/api/paper/kill-switch", "POST", body, {"Origin": "null"})[0] == 403
    assert server.qat_service.latches.active() == []  # forged requests changed nothing
    assert _req(base + "/api/paper/kill-switch", "POST", body, {"Origin": "http://localhost:8765"})[0] == 200


# ============================================================ accounting invariants (no defect found)
def test_accounting_invariants_hold_on_random_multicurrency_runs():
    rnd = random.Random(2026)
    sym = {"KR": "005930", "US": "AAPL"}
    for trial in range(25):
        fx = StaticFXRateProvider({(Currency.USD, Currency.KRW): 1300.0})
        stack = build_paper_stack(
            starting_cash={Currency.KRW: 5_000_000, Currency.USD: 5_000}, base_currency=Currency.KRW,
            fx_provider=fx, commission_rate=0.001, tax_rate_sell=0.002, slippage_bps=9,
            reservation_execution_buffer=0.02)
        init = {Currency.KRW: 5_000_000.0, Currency.USD: 5_000.0}
        price = {"KR": 1000.0, "US": 100.0}
        marks, flow, fees = {}, {c: 0.0 for c in init}, {c: 0.0 for c in init}
        for step in range(25):
            key = rnd.choice(["KR", "US"])
            market = Market(key)
            ccy = currency_for(market, sym[key])
            price[key] *= 1 + rnd.uniform(-0.03, 0.03)
            marks[(key, sym[key])] = price[key]
            side, qty = rnd.choice([Side.BUY, Side.SELL]), rnd.randint(1, 30)
            res = stack.submit_trade_proposal(
                mk(market, sym[key], side, qty, strategy_id=f"s{step}", expected_gross_return=0.05), price[key])
            if not res.accepted:
                continue
            roll = rnd.random()
            if roll < 0.15:
                stack.cancel_order(res.order_id)
                continue
            part = rnd.randint(1, qty) if roll < 0.5 and qty > 1 else None
            fill = stack.broker.simulate_fill(res.order_id, price[key] * (1 + rnd.uniform(-0.002, 0.002)), part)
            stack.settle(fill)
            flow[ccy] += (-fill.gross if side is Side.BUY else fill.gross) - fill.total_cost
            fees[ccy] += fill.commission + fill.exchange_fee
        val = stack.ledger.valuation(marks, fx)
        ledger = stack.ledger
        for ccy in init:
            row = val.by_currency[ccy]
            pos_value = sum(p["market_value"] or 0 for p in val.positions if p["currency"] == ccy.value)
            assert ledger.cash[ccy] == pytest.approx(init[ccy] + flow[ccy], abs=1e-6)          # cash flow
            assert row["equity"] == pytest.approx(ledger.cash[ccy] + pos_value, abs=1e-6)       # cash + positions
            assert row["equity"] - init[ccy] == pytest.approx(row["realized"] + row["unrealized"], abs=1e-6)
            assert ledger.fees[ccy] == pytest.approx(fees[ccy], abs=1e-6)
            open_reserved = sum(o.reservation.cash_remaining for o in stack.broker.orders.values()
                                if o.reservation and o.reservation.kind == "CASH"
                                and o.reservation.currency == ccy and not o.is_terminal)
            assert ledger.reserved_cash[ccy] == pytest.approx(open_reserved, abs=1e-6)
        assert val.total_base == pytest.approx(
            sum(val.by_currency[c]["equity"] * fx.rate(c, Currency.KRW) for c in init), abs=1e-6)


def test_concurrent_settlement_of_one_fill_applies_once():
    for _ in range(60):
        stack = build_paper_stack(starting_cash=1_000_000)
        res = stack.submit_trade_proposal(mk(Market.KR, "005930", Side.BUY, 10), 100)
        fill = stack.broker.simulate_fill(res.order_id, 100)
        outcomes = []
        threads = [threading.Thread(target=lambda: outcomes.append(stack.settle(fill))) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert sum(o.value == "APPLIED" for o in outcomes) == 1
        assert stack.ledger.get_position(Market.KR, "005930").quantity == pytest.approx(10)


# ============================================================ pipeline order / no bypass
def test_pipeline_stops_at_first_non_pass_and_later_gates_do_not_run():
    stack = build_paper_stack(starting_cash=1_000, risk_kwargs={"max_order_notional": 10})
    res = stack.submit_trade_proposal(mk(Market.KR, "005930", Side.BUY, 100), 100)
    assert not res.accepted and res.stage == "risk"
    stages = [r.stage for r in stack.audit.records]
    assert stages == ["proposal_received", "validator_decision", "net_alpha_decision", "risk_decision"]
    assert stack.broker.orders == {} and stack.ledger.reserved_cash[Currency.KRW] == 0
    assert stack.compliance._seen_proposals == set()  # later gates never saw it
    ok = build_paper_stack(starting_cash=1_000_000)
    ok.submit_trade_proposal(mk(Market.KR, "005930", Side.BUY, 1), 100)
    order = [r.stage for r in ok.audit.records if r.stage.endswith("_decision")]
    assert order == ["validator_decision", "net_alpha_decision", "risk_decision",
                     "compliance_decision", "integrity_decision"]


def test_kill_switch_blocks_every_order_variant_and_the_same_proposal_twice():
    stack = build_paper_stack(starting_cash=1_000_000, risk_kwargs={"kill_switch": True})
    stack.ledger.positions[("KR", "005930")] = Position(quantity=10, avg_cost=100.0)
    from qat.core.models import OrderType
    variants = [
        mk(Market.KR, "005930", Side.BUY, 1),
        mk(Market.KR, "005930", Side.SELL, 1),
        mk(Market.KR, "005930", Side.BUY, 1, order_type=OrderType.LIMIT, limit_price=100),
    ]
    for proposal in variants:
        res = stack.submit_trade_proposal(proposal, 100)
        assert not res.accepted and "kill_switch" in res.reason
    assert stack.broker.orders == {}


def test_resubmitting_the_same_proposal_object_is_blocked():
    stack = build_paper_stack(starting_cash=1_000_000)
    proposal = mk(Market.KR, "005930", Side.BUY, 1)
    assert stack.submit_trade_proposal(proposal, 100).accepted
    again = stack.submit_trade_proposal(proposal, 100)
    assert not again.accepted and "duplicate_proposal" in again.reason


# ============================================================ UI vs backend state
def test_ui_always_on_rules_are_not_reported_as_unconfigured():
    svc = _session()
    rules = {r["id"]: r for r in svc.risk()["integrity_rules"]}
    for rid in ("MI-02", "MI-03"):
        assert rules[rid]["implemented"] is True and "configured" not in rules[rid] and "value" not in rules[rid]
    # SPEC CHANGE (QAT v1 final completion): the five previously unimplemented rules now exist; unset thresholds are NOT_CONFIGURED, not hidden
    assert not [r for r in svc.risk()["risk_rules"] if not r["implemented"]]
    assert not [r for r in svc.risk()["integrity_rules"] if not r["implemented"]]
    assert all(r["configured"] is False for r in svc.risk()["risk_rules"] if r["id"] in ("R-07", "R-08", "R-09"))
    assert all(r["configured"] is False for r in svc.risk()["integrity_rules"] if r["id"] in ("MI-05", "MI-06"))


def test_ui_reflects_backend_safety_state_across_restart():
    svc = _session()
    svc.engage_kill_switch({"reason": "audit"})
    assert svc.risk()["kill_switch"] is True and svc.overview()["session"]["kill_switch"] is True
    assert svc.status()["latches"]
    fresh = _session(QATService())
    assert fresh.risk()["kill_switch"] is True  # new session after "restart" is still latched
    assert not fresh.submit_proposal({"side": "BUY", "quantity": 1, "expected_gross_return": 0.05,
                                      "client_request_id": "k"})["accepted"]


def test_http_views_without_session_are_409_not_500(http):
    base, _ = http
    for path in ("/api/portfolio", "/api/orders"):
        assert _req(base + path)[0] == 409
    assert _req(base + "/api/paper/advance", "POST", b"{}")[0] == 409
    assert _req(base + "/api/overview")[0] == 200


def test_duplicate_intent_with_fresh_request_ids_is_blocked_server_side():
    svc = _session()
    results = []

    def go(i):
        results.append(svc.submit_proposal({"side": "BUY", "quantity": 1, "expected_gross_return": 0.05,
                                            "client_request_id": f"c{i}"}))

    threads = [threading.Thread(target=go, args=(i,)) for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(r["accepted"] for r in results) == 1
    assert len(svc.session.stack.broker.orders) == 1


# ============================================================ cost stress is real execution cost
def test_cost_stress_scales_each_execution_cost_component():
    ds = load_dataset(KR_PATH)
    runs = {}
    for mult in (1.0, 2.0):
        res = run_backtest(ds, make_strategy("ma_trend", **FIXTURE), BacktestConfig(cost_multiplier=mult))
        runs[mult] = res
    base, stressed = runs[1.0], runs[2.0]
    assert stressed.cost_models["KR"]["commission_rate"] == pytest.approx(2 * base.cost_models["KR"]["commission_rate"])
    for res, mult in ((base, 1.0), (stressed, 2.0)):
        model = res.cost_models["KR"]
        for fill in res.fills:
            gross = fill["quantity"] * fill["price"]
            assert fill["commission"] / gross == pytest.approx(model["commission_rate"])        # commission
            bps = (fill["price"] / fill["reference_price"] - 1) * 1e4
            expected = (model["half_spread_bps"] + model["slippage_bps"]) * (1 if fill["side"] == "BUY" else -1)
            assert bps == pytest.approx(expected)                                               # spread + slippage
            if fill["side"] == "SELL":
                assert fill["tax"] / gross == pytest.approx(model["tax_rate_sell"])             # tax
    assert stressed.metrics["commission"] > base.metrics["commission"]
    assert stressed.metrics["tax"] > base.metrics["tax"]
    assert stressed.metrics["slippage_estimate"] > base.metrics["slippage_estimate"]
    out = run_cost_stress(ds, "ma_trend", FIXTURE, BacktestConfig(), (1.0, 2.0), save=False)
    rows = out["metrics"]["rows"]
    assert rows[1]["explicit_costs"] > rows[0]["explicit_costs"] and rows[1]["net_return"] < rows[0]["net_return"]


# ============================================================ results: manifest <-> input, immutability
def test_manifest_hash_matches_input_and_stored_results_are_immutable(tmp_path):
    svc = QATService()
    out = svc.run_backtest({"dataset_id": KR, "strategy": "ma_trend", "params": FIXTURE})
    run_dir = tmp_path / "results" / "runs" / out["run_id"]
    snapshot = {p.name: p.read_bytes() for p in run_dir.iterdir()}
    detail = load_run(out["run_id"], include_audit=True)
    again = svc.run_detail(out["run_id"])
    assert detail["manifest"]["data"]["sha256"] == hashlib.sha256(KR_PATH.read_bytes()).hexdigest()
    assert detail["manifest"]["data"]["data_version"] == load_dataset(KR_PATH).data_version
    assert again["manifest"] == detail["manifest"] and again["result"] == detail["result"]
    assert {p.name: p.read_bytes() for p in run_dir.iterdir()} == snapshot  # reads never write


# ============================================================ leakage detectors are non-vacuous
def _perturb_future(ds, k, mult):
    bars = list(ds.bars)
    for i in range(k + 1, len(bars)):
        b = bars[i]
        bars[i] = Bar(b.ts, b.open * mult, b.high * mult, b.low * mult, b.close * mult, b.volume)
    return replace(ds, bars=bars, data_version=f"perturbed-{k}-{mult}")


_DECISION_FIELDS = ("signal_bar", "side", "quantity", "reference_price", "expected_gross_return",
                    "alpha_source", "reservation", "order_type")


def _decision_view(res, k):
    """Everything a strategy/gate decided at or before bar k. Order execution fields
    (status, avg fill price) are excluded: an order signalled at bar k legitimately
    fills at open(k+1), which is exactly the bar the perturbation changes."""

    return {
        "orders": [{f: o[f] for f in _DECISION_FIELDS} for o in res.orders if o["signal_bar"] <= k],
        "rejections": [r for r in res.rejections if r["bar"] <= k],
        "fills": [f for f in res.fills if f["bar"] <= k],
        "equity": [p for p in res.equity if p["bar"] <= k],
    }


def decisions_depend_on_future(ds, name, ks=(300, 500)) -> bool:
    cfg = BacktestConfig()
    for k in ks:
        base = run_backtest(ds, make_strategy(name, **FIXTURE), cfg)
        assert _decision_view(base, k)["orders"], "vacuous comparison window"
        for mult in (5.0, 0.2):
            alt = run_backtest(_perturb_future(ds, k, mult), make_strategy(name, **FIXTURE), cfg)
            if _decision_view(base, k) != _decision_view(alt, k):
                return True
    return False


@pytest.mark.parametrize("name", ["ma_trend", "breakout", "mean_reversion"])
def test_leakage_detector_is_clean_on_real_engine_and_catches_a_one_bar_leak(name, monkeypatch):
    ds = load_dataset(KR_PATH)
    assert decisions_depend_on_future(ds, name) is False

    class LeakyHistory(History):  # mutation: strategy sees bar t+1
        def __init__(self, bars, end):
            super().__init__(bars, min(end + 1, len(bars)))

    monkeypatch.setattr(bt_module, "History", LeakyHistory)
    assert decisions_depend_on_future(ds, name) is True


def calibration_depends_on_test_window(ds) -> bool:
    cfg = WalkForwardConfig(train_bars=250, test_bars=100, grid={"fast": [5, 10], "slow": [30, 50]})
    base = run_walkforward(ds, "ma_trend", cfg, FIXTURE, save=False)["result"]["folds"][0]
    assert any(c["trades_closed"] > 0 for c in base["calibration"]), "vacuous: no trades in train window"
    for mult in (5.0, 0.2):
        alt = run_walkforward(_perturb_future(ds, base["test"][0] - 1, mult), "ma_trend", cfg, FIXTURE,
                              save=False)["result"]["folds"][0]
        if alt["calibration"] != base["calibration"] or alt["chosen_params"] != base["chosen_params"]:
            return True
    return False


def test_train_test_contamination_detector_is_clean_and_catches_a_widened_window(monkeypatch):
    ds = load_dataset(KR_PATH)
    assert calibration_depends_on_test_window(ds) is False
    original = wf_module._calibrate

    def leaky(dataset, name, candidates, base, train):
        return original(dataset, name, candidates, base, [train[0], train[1] + 50])

    monkeypatch.setattr(wf_module, "_calibrate", leaky)
    assert calibration_depends_on_test_window(ds) is True


def test_oos_fold_never_sees_bars_after_its_window():
    ds = load_dataset(KR_PATH)
    cfg = WalkForwardConfig(train_bars=250, test_bars=100, lockbox_bars=100, grid={"fast": [10], "slow": [30]})
    base = run_walkforward(ds, "ma_trend", cfg, FIXTURE, save=False)["result"]["folds"]
    cut = base[0]["test"][1]
    alt = run_walkforward(_perturb_future(ds, cut - 1, 5.0), "ma_trend", cfg, FIXTURE, save=False)["result"]["folds"]
    assert base[0]["oos_metrics"] == alt[0]["oos_metrics"]  # bars >= window end are invisible to fold 0
    assert base[0]["calibration"] == alt[0]["calibration"]
