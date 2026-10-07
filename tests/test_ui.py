"""Batch #2 UI - service layer + HTTP server (real ledger / gates, no mocks)."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

import pytest

from qat.core.models import Currency
from qat.ui.server import make_server
from qat.ui.service import ConflictError, QATService, SafetyLatches, ServiceError

KR = "data/fixtures/SYN_KR1_1d.csv"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("QAT_RESULTS_DIR", str(tmp_path / "results"))
    monkeypatch.setenv("QAT_STATE_DIR", str(tmp_path / "state"))


@pytest.fixture
def svc():
    service = QATService()
    service.start_session({"dataset_id": KR, "initial_cash": 10_000_000, "start_bar": 60})
    return service


def _buy(svc, rid, qty=10, exp=0.05, **extra):
    return svc.submit_proposal({"side": "BUY", "quantity": qty, "order_type": "MARKET",
                                "expected_gross_return": exp, "client_request_id": rid, **extra})


# ------------------------------------------------------------------ status
def test_status_is_honest_about_unimplemented_and_live():
    st = QATService().status()
    assert st["live"]["status"] == "BLOCKED"
    # SPEC CHANGE (QAT v1 final completion): Strategy Health is implemented; with no session nothing is observed -> UNKNOWN (not HEALTHY)
    assert st["strategy_health"] == "UNKNOWN"
    assert st["net_alpha"] == "UNKNOWN"
    assert st["execution_mode"] == "NO SESSION"


def test_ui_values_match_ledger_after_trade(svc):
    res = _buy(svc, "r1")
    assert res["accepted"], res["reason"]
    assert [d["stage"] for d in res["decisions"]] == ["validator", "net_alpha", "risk", "compliance", "integrity"]
    svc.advance({"bars": 3})
    ledger = svc.session.stack.ledger
    val = ledger.valuation(svc.session.marks, svc.session.fx)
    ov = svc.overview()["session"]
    pf = svc.portfolio()
    assert ov["equity"] == pytest.approx(val.by_currency[Currency.KRW]["equity"])
    assert ov["realized"] == pytest.approx(ledger.realized_pnl[Currency.KRW])
    assert ov["unrealized"] == pytest.approx(val.by_currency[Currency.KRW]["unrealized"])
    assert pf["by_currency"]["KRW"]["cash"] == pytest.approx(ledger.cash[Currency.KRW])
    pos = pf["positions"][0]
    assert pos["quantity"] == pytest.approx(ledger.get_position("KR", "SYNKR1").quantity)
    assert pos["mark"] == pytest.approx(svc.session.bar.close)
    orders = svc.orders()
    assert orders["orders"][0]["status"] == "FILLED"
    assert orders["fills"][0]["price"] == pytest.approx(svc.session.stack.broker.orders[orders["orders"][0]["order_id"]].avg_fill_price)
    assert any(a["stage"] == "fill_settled" for a in orders["audit"])


def test_client_cannot_set_price_or_approval(svc):
    for field, value in [("status", "APPROVED"), ("reference_price", 1), ("price", 1),
                         ("approved", True), ("symbol", "OTHER"), ("strategy_id", "x")]:
        with pytest.raises(ServiceError):
            _buy(svc, f"bad-{field}", **{field: value})
    assert svc.session.stack.broker.orders == {}


def test_duplicate_request_is_idempotent(svc):
    first = _buy(svc, "same-id")
    second = _buy(svc, "same-id")
    assert first["accepted"] and second["idempotent_replay"] is True
    assert len(svc.session.stack.broker.orders) == 1


def test_concurrent_requests_cannot_double_reserve(svc):
    close = svc.session.bar.close
    qty = int(10_000_000 * 0.6 / close)  # each order needs 60% of cash
    results = []

    def go(i):
        results.append(_buy(svc, f"c{i}", qty=qty))

    threads = [threading.Thread(target=go, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(r["accepted"] for r in results) == 1
    assert svc.session.stack.ledger.available_cash("KRW") >= 0


def test_zero_expected_return_is_blocked_by_net_alpha(svc):
    res = _buy(svc, "z", exp=0.0)
    assert not res["accepted"] and res["stage"] == "net_alpha"
    assert svc.orders()["rejections"][0]["stage"] == "net_alpha"


def test_limit_order_expires_and_releases_reservation(svc):
    close = svc.session.bar.close
    res = svc.submit_proposal({"side": "BUY", "quantity": 5, "order_type": "LIMIT",
                               "limit_price": round(close * 0.5, 2), "expected_gross_return": 0.05,
                               "client_request_id": "lim"})
    assert res["accepted"]
    svc.advance({"bars": 1})
    order = svc.orders()["orders"][0]
    assert order["status"] == "CANCELLED"
    assert svc.session.stack.ledger.reserved_cash[Currency.KRW] == pytest.approx(0.0)


def test_cancel_releases_reservation(svc):
    res = _buy(svc, "cx")
    svc.cancel_order(res["order"]["order_id"])
    assert svc.session.stack.ledger.reserved_cash[Currency.KRW] == pytest.approx(0.0)
    with pytest.raises(ConflictError):
        svc.cancel_order(res["order"]["order_id"])


# ------------------------------------------------------------- safety latches
def test_kill_switch_survives_reset_and_restart(svc):
    svc.engage_kill_switch({"reason": "test"})
    assert not _buy(svc, "k1")["accepted"]
    svc.start_session({"dataset_id": KR, "initial_cash": 10_000_000, "start_bar": 60})
    res = _buy(svc, "k2")
    assert not res["accepted"] and "kill_switch" in res["reason"]
    restarted = QATService()
    restarted.start_session({"dataset_id": KR, "initial_cash": 10_000_000, "start_bar": 60})
    assert "kill_switch" in _buy(restarted, "k3")["reason"]
    latch = SafetyLatches().active()[0]
    with pytest.raises(ServiceError):
        SafetyLatches().clear(latch["id"], approver="", note="")
    SafetyLatches().clear(latch["id"], approver="reviewer", note="offline review")
    fresh = QATService()
    fresh.start_session({"dataset_id": KR, "initial_cash": 10_000_000, "start_bar": 60})
    assert _buy(fresh, "k4")["accepted"]


def test_breach_latch_blocks_new_session(tmp_path):
    root = tmp_path / "gapdata"
    root.mkdir()
    rows = ["timestamp,open,high,low,close,volume"]
    t0 = datetime(2024, 1, 1, tzinfo=timezone.utc)
    prices = [100] * 70 + [200] * 10  # 2x gap at bar 70
    for i, p in enumerate(prices):
        rows.append(f"{(t0 + timedelta(days=i)).strftime('%Y-%m-%dT%H:%M:%S')},{p},{p},{p},{p},1000")
    (root / "gap.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
    (root / "gap.csv.meta.json").write_text(json.dumps({
        "market": "CRYPTO", "symbol": "GAP/KRW", "timeframe": "1d", "timezone": "UTC",
        "timestamp_label": "open", "source": "TEST", "synthetic": False}), encoding="utf-8")
    service = QATService()
    service._dataset_ids = lambda: ["gap"]
    from qat.data.loader import load_dataset
    ds = load_dataset(root / "gap.csv")
    service.dataset = lambda _id: ds
    # OD-02: manual/Paper trading needs an explicit allowlist entry for this ad-hoc symbol
    service.settings = {**service.settings, "paper_universe": ["GAP/KRW"]}
    service.start_session({"dataset_id": "gap", "initial_cash": 1_000_000, "start_bar": 69})
    assert _buy(service, "g1", qty=1000, exp=0.05)["accepted"]
    out = service.advance({"bars": 1})
    assert out["new_breaches"]
    assert any(l["kind"] == "RESERVATION_BREACH" for l in service.latches.active())
    with pytest.raises(ConflictError):
        service.start_session({"dataset_id": "gap", "initial_cash": 1_000_000, "start_bar": 1})


# ------------------------------------------------------------------ research
def test_research_via_service_saves_and_reopens():
    service = QATService()
    out = service.run_backtest({"dataset_id": KR, "strategy": "ma_trend",
                                "params": {"alpha_mode": "fixture", "fixture_expected_return": 0.01}})
    detail = service.run_detail(out["run_id"])
    assert detail["manifest"]["labels"]["synthetic_data"] is True
    assert detail["metrics"]["fills"] == len(detail["result"]["fills"])
    assert service.runs()[0]["run_id"] == out["run_id"]
    with pytest.raises(ServiceError):
        service.run_backtest({"dataset_id": "../config/settings.yaml", "strategy": "ma_trend"})
    with pytest.raises(ServiceError):
        service.run_backtest({"dataset_id": KR, "strategy": "ma_trend", "zero_cost_fixture": True})


# ---------------------------------------------------------------------- HTTP
@pytest.fixture
def http():
    server = make_server("127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    yield base, server
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


def test_http_static_and_security_headers(http):
    base, _ = http
    status, headers, body = _req(base + "/")
    assert status == 200 and b"QAT" in body
    assert "default-src 'self'" in headers["Content-Security-Policy"]
    assert _req(base + "/app.js")[0] == 200
    assert _req(base + "/../config/settings.yaml")[0] == 404
    assert _req(base + "/api/runs/..%2F..%2Fx")[0] in (400, 404)


def test_http_post_guards(http):
    base, _ = http
    assert _req(base + "/api/paper/session", "POST", {}, {"X-QAT-Client": ""})[0] == 403
    assert _req(base + "/api/paper/session", "POST", {}, {"Content-Type": "text/plain"})[0] == 415
    big = {"x": "a" * 70_000}
    assert _req(base + "/api/paper/session", "POST", big)[0] == 413
    assert _req(base + "/api/paper/kill-switch/release", "POST", {})[0] == 404
    assert _req(base + "/api/paper/kill-switch", "DELETE")[0] == 405


def test_http_end_to_end_matches_backend(http):
    base, server = http
    status, _, _ = _req(base + "/api/paper/session", "POST", {"dataset_id": KR, "start_bar": 60})
    assert status == 200
    status, _, body = _req(base + "/api/paper/proposals", "POST",
                           {"side": "BUY", "quantity": 10, "expected_gross_return": 0.05,
                            "client_request_id": "h1"})
    assert status == 200 and json.loads(body)["accepted"] is True
    bad = _req(base + "/api/paper/proposals", "POST",
               {"side": "BUY", "quantity": 10, "status": "APPROVED", "client_request_id": "h2"})
    assert bad[0] == 400
    assert _req(base + "/api/paper/advance", "POST", {"bars": 2})[0] == 200
    overview = json.loads(_req(base + "/api/overview")[2])["session"]
    ledger = server.qat_service.session.stack.ledger
    assert overview["realized"] == pytest.approx(ledger.realized_pnl[Currency.KRW])
    portfolio = json.loads(_req(base + "/api/portfolio")[2])
    assert portfolio["by_currency"]["KRW"]["cash"] == pytest.approx(ledger.cash[Currency.KRW])
    risk = json.loads(_req(base + "/api/risk")[2])
    # SPEC CHANGE (QAT v1 final completion): R-07/R-08/R-09, MI-05/MI-06 and Strategy Health are implemented. With no thresholds in
    # config/ops.yaml they are NOT_CONFIGURED (inert, never a PASS) and a session that has observed nothing is not HEALTHY.
    assert risk["live"] == "BLOCKED" and risk["strategy_health"] in ("UNKNOWN", "HEALTHY", "DEGRADED")
    assert all(r["implemented"] for r in risk["risk_rules"]) and all(r["implemented"] for r in risk["integrity_rules"])
    assert {r["id"] for r in risk["risk_rules"] if r["id"] in ("R-07", "R-08", "R-09") and not r["configured"]} == {"R-07", "R-08", "R-09"}
    assert {r["id"] for r in risk["integrity_rules"] if r["id"] in ("MI-05", "MI-06") and not r["configured"]} == {"MI-05", "MI-06"}
