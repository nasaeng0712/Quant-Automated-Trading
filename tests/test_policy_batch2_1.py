"""Batch #2.1 Policy Closure - OD-01 / OD-02 / OD-03 (Control Tower decisions).

OD-01  exposure-reducing orders are exempt from the Net Alpha threshold ONLY.
OD-02  no allowlist => no silent allow; research = validated dataset symbol, scoped to the run.
OD-03  reservation buffer 2% is a provisional policy value (unchanged).
Also pins the CURRENT behaviour of the risk-reducing-exit hierarchy (OPEN POLICY):
these characterization tests document what the code does today; they are not a
decision that it is right.
"""

from __future__ import annotations

import pathlib
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from conftest import make_proposal as mk

from qat.app import build_paper_stack
from qat.compliance.gate import ComplianceGate
from qat.cost.engine import CostModel
from qat.core.models import Currency, Market, Position, Side, TradeProposal
from qat.data.bars import Bar, DatasetMeta
from qat.data.loader import Dataset, load_dataset
from qat.data.validation import ValidationReport
from qat.research.backtest import BacktestConfig, run_backtest
from qat.research.store import load_run
from qat.research.strategies import ENTER_LONG, EXIT_LONG, make_strategy
from qat.research.walkforward import WalkForwardConfig, run_walkforward
from qat.ui.service import QATService, ServiceError

ROOT = pathlib.Path(__file__).resolve().parents[1]
KR = "data/fixtures/SYN_KR1_1d.csv"
BASE_YAML = (ROOT / "config" / "settings.yaml").read_text(encoding="utf-8")
COSTS = {Market.KR: CostModel(commission_rate=0.001, tax_rate_sell=0.002, half_spread_bps=5, slippage_bps=5)}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("QAT_RESULTS_DIR", str(tmp_path / "results"))
    monkeypatch.setenv("QAT_STATE_DIR", str(tmp_path / "state"))


def long_stack(qty=100, **kwargs):
    """Stack holding ``qty`` KR shares, built through the real pipeline."""
    stack = build_paper_stack(starting_cash=10_000_000, cost_models=COSTS, commission_rate=0.001,
                              tax_rate_sell=0.002, **kwargs)
    buy = stack.submit_trade_proposal(mk(Market.KR, "005930", Side.BUY, qty, expected_gross_return=0.05,
                                         strategy_id="entry"), 1000)
    assert buy.accepted, buy.reason
    stack.settle(stack.broker.simulate_fill(buy.order_id, 1000))
    assert stack.ledger.get_position(Market.KR, "005930").quantity == qty
    return stack


def sell(stack, qty, exp=0.0, **kw):
    return stack.submit_trade_proposal(
        mk(Market.KR, "005930", Side.SELL, qty, expected_gross_return=exp, strategy_id="exit", **kw), 1000)


def net_alpha_reasons(stack):
    return [r.payload["reasons"] for r in stack.audit.records if r.stage == "net_alpha_decision"][-1]


# ================================================================== OD-01
@pytest.mark.parametrize("qty", [50, 100])
def test_od01_reducing_sell_is_exempt_from_net_alpha(qty):
    stack = long_stack()
    res = sell(stack, qty)  # expected return 0 would normally fail the cost threshold
    assert res.accepted, res.reason
    assert "net_alpha_exempt:exposure_reducing" in net_alpha_reasons(stack)
    fill = stack.broker.simulate_fill(res.order_id, 1000)
    stack.settle(fill)
    # the exemption waives the threshold only - execution costs are still charged once
    assert fill.commission == pytest.approx(fill.gross * 0.001) and fill.tax == pytest.approx(fill.gross * 0.002)
    assert stack.ledger.get_position(Market.KR, "005930").quantity == pytest.approx(100 - qty)


def test_od01_same_sell_without_a_position_is_not_exempt():
    stack = build_paper_stack(starting_cash=10_000_000, cost_models=COSTS)
    res = sell(stack, 10)
    assert not res.accepted and res.stage == "net_alpha"
    assert "net_alpha_exempt:exposure_reducing" not in net_alpha_reasons(stack)


def test_od01_buy_on_a_long_is_not_exempt():
    stack = long_stack()
    res = stack.submit_trade_proposal(mk(Market.KR, "005930", Side.BUY, 10, expected_gross_return=0.0,
                                         strategy_id="add"), 1000)
    assert not res.accepted and res.stage == "net_alpha"


def test_od01_oversized_sell_cannot_flip_the_position():
    stack = long_stack()
    weak = sell(stack, 101)  # exp 0: exemption must not apply to the 101 > 100 order
    assert not weak.accepted and weak.stage == "net_alpha"
    strong = sell(stack, 101, exp=0.05)  # clears Net Alpha on its own merits -> Risk must still stop it
    assert not strong.accepted and strong.stage == "risk" and "insufficient_position" in strong.reason
    assert stack.ledger.get_position(Market.KR, "005930").quantity == 100
    assert all(p.quantity >= 0 for p in stack.ledger.positions.values())  # never short
    assert len(stack.broker.orders) == 1  # only the original entry


def test_od01_reserved_quantity_is_not_exempt_twice():
    stack = long_stack()
    first = sell(stack, 100)
    assert first.accepted  # pending: all 100 shares reserved
    second = stack.submit_trade_proposal(mk(Market.KR, "005930", Side.SELL, 1, expected_gross_return=0.0,
                                            strategy_id="exit2"), 1000)
    assert not second.accepted  # available quantity is 0 -> no exemption, and Risk would stop it too
    assert "net_alpha_exempt:exposure_reducing" not in net_alpha_reasons(stack)


@pytest.mark.parametrize("stage,arm", [
    ("risk", lambda s: s.risk.trip_kill_switch(True)),
    ("compliance", lambda s: s.compliance.halted_symbols.add("005930")),
])
def test_od01_reducing_sell_still_passes_every_other_gate(stage, arm):
    stack = long_stack()
    arm(stack)
    res = sell(stack, 50)
    assert not res.accepted and res.stage == stage
    assert len(stack.broker.orders) == 1
    assert stack.ledger.reserved_cash[Currency.KRW] == 0
    assert stack.ledger.get_position(Market.KR, "005930").reserved_quantity == 0


def test_od01_integrity_and_order_size_limits_still_apply_to_reducing_sell():
    stack = long_stack()
    opener = stack.submit_trade_proposal(mk(Market.KR, "005930", Side.BUY, 1, expected_gross_return=0.05,
                                            strategy_id="other"), 1000)
    assert opener.accepted  # an open BUY now exists -> a SELL is an opposing order
    res = sell(stack, 50)
    assert not res.accepted and res.stage == "integrity" and "opposing_order" in res.reason
    capped = long_stack()
    capped.risk.max_order_notional = 10_000
    res = sell(capped, 50)  # 50 x 1000 = 50,000 > 10,000
    assert not res.accepted and res.stage == "risk" and "max_order_notional" in res.reason


def test_od01_client_cannot_claim_exit_status():
    with pytest.raises(TypeError):
        TradeProposal(market=Market.KR, symbol="005930", side=Side.SELL, quantity=1, exit=True)  # type: ignore[call-arg]
    svc = QATService()
    svc.start_session({"dataset_id": KR, "start_bar": 60})
    for field in ("exit", "reduce_only", "reducing", "net_alpha_exempt", "is_exit"):
        with pytest.raises(ServiceError):
            svc.submit_proposal({"side": "SELL", "quantity": 1, "expected_gross_return": 0.0,
                                 "client_request_id": f"f-{field}", field: True})
    # a reason code that merely says "exit" grants nothing: no position -> Net Alpha applies
    res = svc.submit_proposal({"side": "SELL", "quantity": 1, "expected_gross_return": 0.0,
                               "reason_code": "exit", "client_request_id": "fx"})
    assert not res["accepted"] and res["stage"] == "net_alpha"
    assert svc.session.stack.broker.orders == {}


def test_od01_exempt_decision_is_audited():
    stack = long_stack()
    assert sell(stack, 10).accepted
    reasons = net_alpha_reasons(stack)
    assert reasons[0] == "net_alpha_exempt:exposure_reducing" and any(r.startswith("expected_net_alpha=") for r in reasons)


def _exits_ok(res):
    return res.metrics["exit_rejections"] == 0 and all(r["action"] == ENTER_LONG for r in res.rejections)


def test_od01_backtest_exits_are_no_longer_blocked_by_net_alpha():
    """Pre-policy reproduction: SYN_KR1 ma_trend had 189 exit rejections (position stuck at 144),
    SYN_CRYPTO1 mean_reversion 619. Now exits clear Net Alpha; entries are still judged by it."""
    kr = run_backtest(load_dataset(ROOT / KR), make_strategy("ma_trend"), BacktestConfig())
    assert _exits_ok(kr) and kr.metrics["trades_closed"] == 8 and kr.end_state["open_position_qty"] == 0
    assert kr.metrics["rejections_by_stage"].get("net_alpha", 0) > 0  # entries still gated
    crypto = run_backtest(load_dataset(ROOT / "data/fixtures/SYN_CRYPTO1_1h.csv"),
                          make_strategy("mean_reversion"), BacktestConfig(initial_cash=100_000_000))
    assert _exits_ok(crypto) and crypto.metrics["trades_closed"] >= 1 and crypto.end_state["open_position_qty"] == 0
    # exits executed at real cost: sell-side tax and commission were charged
    assert any(f["side"] == "SELL" and f["tax"] > 0 and f["commission"] > 0 for f in kr.fills)


def test_od01_exempt_exit_still_blocked_by_other_gates_in_backtest(tmp_path):
    path = tmp_path / "ks.yaml"
    path.write_text(BASE_YAML, encoding="utf-8")
    res = run_backtest(load_dataset(ROOT / KR), make_strategy("ma_trend", alpha_mode="fixture",
                                                              fixture_expected_return=0.01),
                       BacktestConfig(settings_path=str(path), risk_overrides={"kill_switch": True}))
    assert res.fills == [] and all(r["stage"] == "risk" and "kill_switch" in r["reason"] for r in res.rejections)


# ================================================================== OD-02
@pytest.mark.fail_closed_universe
def test_od02_no_allowlist_is_never_a_silent_allow():
    assert ComplianceGate.DEFAULT_UNRESTRICTED_UNIVERSE is False
    decision = ComplianceGate().evaluate(mk(Market.KR, "005930", Side.BUY, 1))
    assert decision.status.value == "UNKNOWN" and "tradable_universe_not_configured" in decision.reasons
    stack = build_paper_stack(starting_cash=1_000_000)
    res = stack.submit_trade_proposal(mk(Market.KR, "005930", Side.BUY, 1), 100)
    assert not res.accepted and res.stage == "compliance" and "tradable_universe_not_configured" in res.reason
    live = build_paper_stack(starting_cash=1_000_000, mode="LIVE")
    res = live.submit_trade_proposal(mk(Market.KR, "005930", Side.BUY, 1), 100)
    assert not res.accepted  # LIVE is blocked regardless; UNKNOWN is coerced to BLOCK by the gate
    coerced = ComplianceGate(mode="LIVE").evaluate(mk(Market.KR, "005930", Side.BUY, 1))
    assert coerced.status.value == "BLOCK"


@pytest.mark.fail_closed_universe
def test_od02_allowlist_membership_controls_the_outcome():
    stack = build_paper_stack(starting_cash=1_000_000, compliance_kwargs={"tradable_symbols": {"005930"}})
    ok = stack.submit_trade_proposal(mk(Market.KR, "005930", Side.BUY, 1), 100)
    assert ok.accepted
    assert "universe:allowlist" in [r.payload["reasons"] for r in stack.audit.records
                                    if r.stage == "compliance_decision"][-1]
    other = stack.submit_trade_proposal(mk(Market.KR, "000660", Side.BUY, 1), 100)
    # not a PASS and never executed: UNKNOWN in PAPER (existing T-018b contract), BLOCK in LIVE
    assert not other.accepted and other.stage == "compliance" and "symbol_not_verified" in other.reason
    assert len(stack.broker.orders) == 1


def test_od02_explicit_unrestricted_opt_in_is_traceable():
    stack = build_paper_stack(starting_cash=1_000_000, compliance_kwargs={"unrestricted_universe": True})
    res = stack.submit_trade_proposal(mk(Market.KR, "005930", Side.BUY, 1), 100)
    assert res.accepted
    assert "universe:unrestricted_explicit" in [r.payload["reasons"] for r in stack.audit.records
                                                if r.stage == "compliance_decision"][-1]


def settings_without_universe(tmp_path, universe=None):
    head = BASE_YAML[: BASE_YAML.index("# ---------------------------------------------------------------------------\n# OD-02")]
    text = head if universe is None else head + "paper_universe:\n" + "".join(f"  - {s}\n" for s in universe)
    path = tmp_path / "u.yaml"
    path.write_text(text, encoding="utf-8")
    return str(path)


def _ui_buy(svc, rid):
    return svc.submit_proposal({"side": "BUY", "quantity": 1, "expected_gross_return": 0.05, "client_request_id": rid})


@pytest.mark.fail_closed_universe
def test_od02_ui_manual_proposals_follow_the_explicit_allowlist(tmp_path):
    none = QATService(settings_without_universe(tmp_path))
    none.start_session({"dataset_id": KR, "start_bar": 60})
    res = _ui_buy(none, "a")
    assert not res["accepted"] and res["stage"] == "compliance" and "tradable_universe_not_configured" in res["reason"]
    assert none.risk()["compliance"]["universe_configured"] is False
    assert none.session.stack.broker.orders == {}
    included = QATService(settings_without_universe(tmp_path, ["SYNKR1"]))
    included.start_session({"dataset_id": KR, "start_bar": 60})
    assert _ui_buy(included, "b")["accepted"]
    excluded = QATService(settings_without_universe(tmp_path, ["SYNUS1"]))
    excluded.start_session({"dataset_id": KR, "start_bar": 60})
    res = _ui_buy(excluded, "c")
    assert not res["accepted"] and "symbol_not_verified" in res["reason"]
    assert excluded.session.stack.broker.orders == {}


def leak_dataset():
    ds = load_dataset(ROOT / KR)
    meta = replace(ds.meta, symbol="LEAK1", synthetic=True, source="SYNTHETIC")
    return replace(ds, meta=meta, data_version="KR-LEAK1-1d-test")


@pytest.mark.fail_closed_universe
def test_od02_research_uses_dataset_symbol_and_records_it_in_the_manifest():
    ds = leak_dataset()  # LEAK1 is in NO allowlist anywhere
    res = run_backtest(ds, make_strategy("ma_trend", alpha_mode="fixture", fixture_expected_return=0.01),
                       BacktestConfig())
    assert res.metrics["fills"] > 0 and not any(r["stage"] == "compliance" for r in res.rejections)
    compliance = [a["reasons"] for a in res.audit if a["stage"] == "compliance_decision"]
    assert compliance and all(f"universe:research_run:{ds.data_version}" in r for r in compliance)
    universe = res.labels["research_universe"]
    assert universe["scope"] == "research_run" and universe["symbols"] == ["LEAK1"]
    assert universe["manual_trading_universe_effect"] == "none"
    wf = run_walkforward(ds, "ma_trend", WalkForwardConfig(train_bars=250, test_bars=100,
                                                           grid={"fast": [10], "slow": [30]}),
                         {"alpha_mode": "fixture", "fixture_expected_return": 0.01})
    assert wf["manifest"]["universe"]["symbols"] == ["LEAK1"]


@pytest.mark.fail_closed_universe
def test_od02_research_symbol_does_not_leak_into_manual_paper_universe(tmp_path):
    ds = leak_dataset()
    svc = QATService(settings_without_universe(tmp_path, ["SYNKR1"]))
    svc._dataset_ids = lambda: ["leak"]
    svc.dataset = lambda _id: ds
    out = svc.run_backtest({"dataset_id": "leak", "strategy": "ma_trend",
                            "params": {"alpha_mode": "fixture", "fixture_expected_return": 0.01}})
    detail = svc.run_detail(out["run_id"])
    assert detail["manifest"]["universe"]["symbols"] == ["LEAK1"]  # research accepted it ...
    assert load_run(out["run_id"])["manifest"]["universe"]["manual_trading_universe_effect"] == "none"
    svc.start_session({"dataset_id": "leak", "start_bar": 60})
    res = _ui_buy(svc, "leak-1")  # ... manual/Paper trading of the same symbol is still refused
    assert not res["accepted"] and "symbol_not_verified" in res["reason"]
    assert svc.session.stack.compliance.tradable_symbols == {"SYNKR1"}
    assert svc.settings["paper_universe"] == ["SYNKR1"]  # research never edits the allowlist
    assert svc.session.stack.broker.orders == {}


def test_od02_invalid_paper_universe_setting_is_rejected(tmp_path):
    from qat.config import SettingsError, load_research_settings

    path = tmp_path / "bad.yaml"
    path.write_text(BASE_YAML.replace("  - SYNKR1\n", "  - 5\n"), encoding="utf-8")
    with pytest.raises(SettingsError):
        load_research_settings(path)


# ================================================================== OD-03
def test_od03_reservation_buffer_stays_at_two_percent_everywhere():
    assert BacktestConfig().reservation_buffer_pct == 0.02
    svc = QATService()
    svc.start_session({"dataset_id": KR, "start_bar": 60})
    assert svc.session.stack.settlement.execution_buffer_pct == 0.02
    _, _, _, base = svc._research_inputs({"dataset_id": KR, "strategy": "ma_trend"})
    assert base.reservation_buffer_pct == 0.02
    assert 'id="rf-buf" inputmode="decimal" value="0.02"' in (ROOT / "src/qat/ui/static/app.js").read_text(encoding="utf-8")


HAND_YAML = """
project: {name: QAT, mode: paper}
markets: {KR: true, US: false, CRYPTO: false}
execution: {live_enabled: false}
base_currency: KRW
fx: {rates: []}
costs:
  KR: {commission_rate: 0.001, tax_rate_sell: 0.002, half_spread_bps: 0, slippage_bps: 10, fx_cost_bps: 0}
risk: {max_order_notional: null, max_symbol_exposure: null, max_market_exposure: null,
       max_total_exposure: null, daily_loss_limit: null, max_drawdown: null}
net_alpha: {min_net_alpha_bps: 0}
"""


def gap_dataset(gap):
    t0 = datetime(2024, 1, 1, tzinfo=timezone.utc)
    closes = [100, 100, 100 * (1 + gap), 100 * (1 + gap), 100 * (1 + gap)]
    opens = [100, 100, 100 * (1 + gap), 100 * (1 + gap), 100 * (1 + gap)]
    bars = [Bar(t0 + timedelta(days=i), o, max(o, c), min(o, c), c, 1000) for i, (o, c) in enumerate(zip(opens, closes))]
    meta = DatasetMeta("KR", "TEST", "1d", "UTC", source="TEST")
    report = ValidationReport("PASS", len(bars), len(bars), bars[0].ts.isoformat(), bars[-1].ts.isoformat())
    return Dataset(meta=meta, bars=bars, path="<mem>", sha256="0" * 64, data_version=f"GAP-{gap}", validation=report)


@pytest.mark.parametrize("gap,breach", [(0.015, False), (0.019, False), (0.025, True), (0.05, True)])
def test_od03_two_percent_buffer_breach_boundary(tmp_path, gap, breach):
    path = tmp_path / "hand.yaml"
    path.write_text(HAND_YAML, encoding="utf-8")
    from test_research import Scripted  # the causal test-double strategy

    res = run_backtest(gap_dataset(gap), Scripted(script={1: ENTER_LONG, 2: EXIT_LONG}),
                       BacktestConfig(initial_cash=1_000_000, settings_path=str(path)))
    assert bool(res.breaches) is breach  # gap > ~2% outruns the provisional buffer -> breach recorded
    assert res.end_state["trading_halted_by_breach"] is breach
    if breach:
        assert res.fills and res.fills[0]["price"] > 100  # the real fill is kept, never hidden


def test_od03_documents_the_value_as_provisional():
    text = (ROOT / "docs" / "QAT_통합_설계_운영.md").read_text(encoding="utf-8")
    assert "2% is a provisional policy value, not empirically validated" in text
    assert "provisional policy value" in (ROOT / "README.md").read_text(encoding="utf-8")


# ================================================================== Risk-reducing exit hierarchy
# Batch #2.1 pinned the then-CURRENT behaviour as "OPEN POLICY" (no design existed). Batch #2.2
# closed it by Control Tower decision, so four of these characterization tests were UPDATED
# in place to the decided behaviour (daily loss, drawdown, reservation overrun, absolute
# exposure). Kill Switch, unknown drawdown baseline and order-size limit are unchanged.
# The full matrix lives in tests/test_exit_policy_batch2_2.py.
def held(**risk_kwargs):
    stack = build_paper_stack(starting_cash=1_000_000, cost_models=COSTS, risk_kwargs=risk_kwargs)
    stack.ledger.positions[("KR", "005930")] = Position(quantity=100, avg_cost=1000.0)
    return stack


def test_open_policy_kill_switch_blocks_a_reducing_sell():
    stack = held(kill_switch=True)
    res = sell(stack, 50)
    assert not res.accepted and res.stage == "risk" and "kill_switch" in res.reason


def test_open_policy_daily_loss_no_longer_blocks_a_reducing_sell():  # updated by #2.2
    stack = held(daily_loss_limit=1000)
    stack.risk.start_new_day()
    stack.ledger.realized_pnl[Currency.KRW] = -5000.0
    res = sell(stack, 50)
    assert res.accepted, res.reason


def test_open_policy_drawdown_no_longer_blocks_a_reducing_sell():  # updated by #2.2
    stack = held(max_drawdown=0.10)
    stack.risk.observe_equity(1_200_000)  # current equity 1,000,000 + 100 x 1000 = 1,100,000
    stack.ledger.cash[Currency.KRW] = 800_000.0
    res = sell(stack, 50)
    assert res.accepted, res.reason


def test_open_policy_unknown_drawdown_baseline_blocks_a_reducing_sell():
    stack = held(max_drawdown=0.10)
    res = sell(stack, 50)
    assert not res.accepted and res.stage == "risk" and "drawdown_baseline_unknown" in res.reason


def test_open_policy_plain_reservation_overrun_no_longer_traps_a_reducing_sell():  # updated by #2.2
    stack = held()
    stack.ledger.record_reservation_breach(Currency.KRW, 1.0, order_id="x")
    res = sell(stack, 50)
    assert res.accepted, res.reason  # consistent ledger, no reconciliation mismatch


def test_open_policy_exposure_cap_no_longer_blocks_reducing_sells_order_cap_still_applies():  # updated by #2.2
    over = held(max_base_exposure=50_000)  # existing exposure 100,000 alone exceeds the cap
    res = sell(over, 10)
    assert res.accepted, res.reason
    ratio = held(max_symbol_exposure=0.0001, max_market_exposure=0.0001, max_total_exposure=0.0001)
    assert sell(ratio, 10).accepted  # ratio-of-equity caps are evaluated for BUY only
    capped = held(max_order_notional=1_000)
    res = sell(capped, 10)
    assert not res.accepted and "max_order_notional" in res.reason


def test_open_policy_documented_in_design_doc():  # now CLOSED by #2.2
    text = (ROOT / "docs" / "QAT_통합_설계_운영.md").read_text(encoding="utf-8")
    assert "CLOSED — Risk-reducing exit hierarchy" in text
    assert "OPEN POLICY — Risk-reducing exit hierarchy" not in text


# ================================================================== extra audit regressions found during Batch #2.1
def test_net_alpha_gate_without_a_ledger_never_exempts():
    """No authoritative position source => nothing can be shown to reduce exposure."""
    from qat.cost.engine import CostEngine, NetAlphaGate

    gate = NetAlphaGate(CostEngine(COSTS))
    decision = gate.evaluate(mk(Market.KR, "005930", Side.SELL, 10, expected_gross_return=0.0), 1000)
    assert decision.status.value == "BLOCK" and not gate.reduces_exposure(mk(Market.KR, "005930", Side.SELL, 1))


def test_rejected_post_requests_always_get_their_status_not_a_connection_reset():
    """Every early POST rejection (403 host/header, 415, 413, 400) must reach the client as a
    status code. The reset used to appear in ~1 of 120 requests (Windows, unread body)."""
    import json as _json
    import threading
    import urllib.error
    import urllib.request

    from qat.ui.server import make_server

    server = make_server("127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    ok = {"Content-Type": "application/json", "X-QAT-Client": "ui"}
    cases = [
        (403, {**ok, "X-QAT-Client": ""}, b"{}"),
        (403, {**ok, "Host": "evil.example"}, b"x" * 5_000),
        (415, {**ok, "Content-Type": "text/plain"}, b"x" * 2_000),
        (413, ok, _json.dumps({"x": "a" * 70_000}).encode()),
        (400, ok, b"{not json" * 50),
    ]
    try:
        seen = {}
        for _ in range(60):
            for want, headers, body in cases:
                req = urllib.request.Request(base + "/api/paper/session", data=body, method="POST", headers=headers)
                try:
                    urllib.request.urlopen(req, timeout=10)
                    got = 200
                except urllib.error.HTTPError as exc:
                    got = exc.code
                seen.setdefault(want, set()).add(got)
        assert seen == {403: {403}, 415: {415}, 413: {413}, 400: {400}}
    finally:
        server.shutdown()
        server.server_close()
