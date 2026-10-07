"""Batch #2 - Backtest / metrics / walk-forward / manifest / storage."""

from __future__ import annotations

import pathlib
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from qat.data.bars import Bar, DatasetMeta
from qat.data.loader import Dataset, load_dataset
from qat.data.validation import ValidationReport
from qat.research.backtest import BacktestConfig, DataRejected, run_backtest
from qat.research.store import list_runs, load_run, save_backtest
from qat.research.strategies import ENTER_LONG, EXIT_LONG, History, Signal, Strategy, make_strategy
from qat.research.walkforward import (
    LockboxAlreadyUsed,
    WalkForwardConfig,
    evaluate_lockbox,
    run_cost_stress,
    run_walkforward,
)

ROOT = pathlib.Path(__file__).resolve().parents[1]
KR_FIXTURE = ROOT / "data" / "fixtures" / "SYN_KR1_1d.csv"

HAND_SETTINGS = """
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


@pytest.fixture(autouse=True)
def isolated_results(tmp_path, monkeypatch):
    monkeypatch.setenv("QAT_RESULTS_DIR", str(tmp_path / "results"))


@pytest.fixture
def hand_settings(tmp_path):
    path = tmp_path / "hand.yaml"
    path.write_text(HAND_SETTINGS, encoding="utf-8")
    return str(path)


def make_dataset(ohlc, *, start="2024-01-01", market="KR", symbol="TEST", synthetic=False):
    t0 = datetime.fromisoformat(start).replace(tzinfo=timezone.utc)
    bars = [Bar(t0 + timedelta(days=i), o, h, lo, c, 1000) for i, (o, h, lo, c) in enumerate(ohlc)]
    meta = DatasetMeta(market, symbol, "1d", "UTC", source="SYNTHETIC" if synthetic else "TEST",
                       synthetic=synthetic)
    report = ValidationReport("PASS", len(bars), len(bars), bars[0].ts.isoformat(), bars[-1].ts.isoformat())
    return Dataset(meta=meta, bars=bars, path="<memory>", sha256="0" * 64,
                   data_version=f"TEST-{len(bars)}-{sum(b.close for b in bars):.4f}", validation=report)


class Scripted(Strategy):
    """Emits fixed actions at fixed bar indices (test double, fixture alpha)."""

    name = "scripted"
    default_params = {"script": {}}

    @property
    def warmup(self) -> int:
        return 0

    def _events_at(self, i):
        return False, False

    def decide(self, h: History, holding: bool):
        t = len(h) - 1
        self.max_seen = max(getattr(self, "max_seen", 0), len(h))
        action = self.params["script"].get(t)
        if action == ENTER_LONG and not holding:
            return Signal(ENTER_LONG, "scripted:entry", 0.05, "FIXTURE")
        if action == EXIT_LONG and holding:
            return Signal(EXIT_LONG, "scripted:exit", 0.05, "FIXTURE")
        return None


HAND_BARS = [(100, 101, 99, 100), (100, 102, 99, 101), (102, 104, 101, 103),
             (104, 106, 103, 105), (106, 108, 105, 107)]


# ------------------------------------------------------------- hand example
def test_hand_computed_round_trip_matches_ledger_and_metrics(hand_settings):
    ds = make_dataset(HAND_BARS)
    res = run_backtest(ds, Scripted(script={1: ENTER_LONG, 3: EXIT_LONG}),
                       BacktestConfig(initial_cash=1_000_000, settings_path=hand_settings,
                                      position_fraction=0.5, reservation_buffer_pct=0.02))
    buy, sell = res.fills
    # sizing: floor(0.5 * 1e6 / (101 * 1.02 * 1.001 * 1.001)) = 4843
    assert buy["quantity"] == 4843 and sell["quantity"] == 4843
    assert (buy["bar"], buy["signal_bar"], sell["bar"], sell["signal_bar"]) == (2, 1, 4, 3)
    assert buy["price"] == pytest.approx(102.102)          # open(2) * (1 + 10bp)
    assert sell["price"] == pytest.approx(105.894)         # open(4) * (1 - 10bp)
    assert buy["commission"] == pytest.approx(494.479986)
    assert sell["commission"] == pytest.approx(512.844642)
    assert sell["tax"] == pytest.approx(1025.689284)
    m = res.metrics
    assert m["net_pnl"] == pytest.approx(16331.642088)
    assert m["gross_pnl"] == pytest.approx(18364.656)       # sell gross - buy gross
    assert m["explicit_costs"] == pytest.approx(2033.013912)
    assert m["slippage_estimate"] == pytest.approx(1007.344)  # recorded, not re-deducted
    assert m["ending_equity"] == pytest.approx(1016331.642088)
    assert res.end_state["realized_pnl"] == pytest.approx(16331.642088)
    assert res.end_state["reserved_cash"] == pytest.approx(0.0)
    assert m["trades_closed"] == 1 and m["win_rate"] == 1.0
    assert m["profit_factor"] is None and any("profit factor undefined" in n for n in m["notes"])
    assert res.trades[0]["net_pnl"] == pytest.approx(16331.642088)
    # equity at close(2) marks 4843 @ 103 on top of remaining cash
    eq2 = next(p for p in res.equity if p["bar"] == 2)
    assert eq2["equity"] == pytest.approx(505025.534014 + 4843 * 103)


def test_signal_and_fill_times_are_separated(hand_settings):
    ds = make_dataset(HAND_BARS)
    res = run_backtest(ds, Scripted(script={1: ENTER_LONG, 3: EXIT_LONG}),
                       BacktestConfig(initial_cash=1_000_000, settings_path=hand_settings))
    for fill in res.fills:
        assert fill["bar"] == fill["signal_bar"] + 1
        assert fill["reference_price"] == ds.bars[fill["bar"]].open
        assert fill["timestamp"] == ds.bars[fill["bar"]].ts.isoformat()
    for order in res.orders:
        assert order["reference_price"] == ds.bars[order["signal_bar"]].close
        assert order["timestamp"] == (ds.bars[order["signal_bar"]].ts + timedelta(days=1)).isoformat()


def test_last_bar_emits_no_signal(hand_settings):
    ds = make_dataset(HAND_BARS)
    res = run_backtest(ds, Scripted(script={4: ENTER_LONG}),
                       BacktestConfig(initial_cash=1_000_000, settings_path=hand_settings))
    assert res.orders == [] and res.end_state["pending_orders_cancelled"] == []


# ------------------------------------------------------------ look-ahead
def test_history_view_is_causal():
    h = History(list(range(10)), 4)
    assert len(h) == 4 and h[-1] == 3
    with pytest.raises(IndexError):
        h[4]


@pytest.mark.parametrize("name", ["ma_trend", "breakout", "mean_reversion"])
def test_future_bars_do_not_change_past_decisions(name):
    ds = load_dataset(KR_FIXTURE)
    k = 400
    perturbed_bars = ds.bars[:k + 1] + [
        Bar(b.ts, b.open * 1.7, b.high * 1.9, b.low * 1.5, b.close * 1.8, b.volume) for b in ds.bars[k + 1:]
    ]
    ds2 = replace(ds, bars=perturbed_bars, data_version="perturbed")
    cfg = BacktestConfig()
    a = run_backtest(ds, make_strategy(name), cfg)
    b = run_backtest(ds2, make_strategy(name), cfg)

    def upto(rows, field):
        return [r for r in rows if r[field] <= k]

    assert upto(a.orders, "signal_bar") == upto(b.orders, "signal_bar")
    assert upto(a.rejections, "bar") == upto(b.rejections, "bar")
    assert upto(a.fills, "bar") == upto(b.fills, "bar")
    assert upto(a.equity, "bar") == upto(b.equity, "bar")


def test_strategy_sees_exactly_bars_up_to_current_close(hand_settings):
    ds = make_dataset(HAND_BARS * 3)

    class Spy(Scripted):
        def decide(self, h, holding):
            self.calls = getattr(self, "calls", []) + [(len(h), h.last.ts)]
            return None

    spy = Spy(script={})
    run_backtest(ds, spy, BacktestConfig(initial_cash=1_000_000, settings_path=hand_settings, trade_start=2))
    n = len(ds.bars)
    assert [c[0] for c in spy.calls] == list(range(3, n))  # t = 2..n-2 -> len t+1
    assert all(ts == ds.bars[length - 1].ts for length, ts in spy.calls)


def test_engine_never_receives_bars_beyond_window(hand_settings):
    ds = make_dataset(HAND_BARS * 4)
    strat = Scripted(script={})
    run_backtest(ds, strat, BacktestConfig(initial_cash=1_000_000, settings_path=hand_settings,
                                           trade_start=5, trade_end=12))
    assert strat.max_seen <= 12


# ------------------------------------------------------------ walk-forward
def test_walkforward_folds_boundaries_and_lockbox():
    cfg = WalkForwardConfig(train_bars=100, test_bars=50, step_bars=50, lockbox_bars=60)
    folds = cfg.folds(400)
    usable = 400 - 60
    for f in folds:
        assert f["train"][1] == f["test"][0]
        assert f["test"][1] <= usable
    tests = [tuple(f["test"]) for f in folds]
    assert all(a[1] <= b[0] for a, b in zip(tests, tests[1:]))
    with pytest.raises(ValueError):
        WalkForwardConfig(train_bars=300, test_bars=200).folds(400)


def test_walkforward_oos_windows_and_reuse_flag():
    ds = load_dataset(KR_FIXTURE)
    cfg = WalkForwardConfig(train_bars=250, test_bars=100, lockbox_bars=100,
                            grid={"fast": [5, 10], "slow": [30]})
    out = run_walkforward(ds, "ma_trend", cfg, {"alpha_mode": "fixture", "fixture_expected_return": 0.01})
    folds = out["result"]["folds"]
    assert folds and all(f["status"] == "OK" for f in folds)
    for f in folds:
        lo, hi = f["test"]
        oos_ts = [p["timestamp"] for p in f["oos_equity"]]
        assert oos_ts[0] == (ds.bars[lo].ts + timedelta(days=1)).isoformat()
        assert oos_ts[-1] == (ds.bars[hi - 1].ts + timedelta(days=1)).isoformat()
        assert all(len(c["params"]) for c in f["calibration"])
    assert out["metrics"]["independent_oos"] is True
    again = run_walkforward(ds, "ma_trend", cfg, {"alpha_mode": "fixture", "fixture_expected_return": 0.01})
    assert again["metrics"]["independent_oos"] is False
    assert again["metrics"]["prior_oos_evaluations"] == 1
    # lockbox: first use independent, second refused, acknowledged reuse flagged
    run_id = out["manifest"]["run_id"]
    first = evaluate_lockbox(ds, run_id)
    assert first["independent"] is True
    with pytest.raises(LockboxAlreadyUsed):
        evaluate_lockbox(ds, run_id)
    reused = evaluate_lockbox(ds, run_id, acknowledge_reuse=True)
    assert reused["independent"] is False


def test_calibration_is_not_contaminated_by_test_window():
    ds = load_dataset(KR_FIXTURE)
    cfg = WalkForwardConfig(train_bars=250, test_bars=100, grid={"fast": [5, 10, 20], "slow": [30, 50]})
    base = run_walkforward(ds, "ma_trend", cfg, save=False)
    first_test = base["result"]["folds"][0]["test"]
    bars = list(ds.bars)
    for i in range(first_test[0], len(bars)):
        b = bars[i]
        bars[i] = Bar(b.ts, b.open * 0.5, b.high * 0.5, b.low * 0.5, b.close * 0.5, b.volume)
    ds2 = replace(ds, bars=bars, data_version="contam-test")
    other = run_walkforward(ds2, "ma_trend", cfg, save=False)
    f0a, f0b = base["result"]["folds"][0], other["result"]["folds"][0]
    assert f0a["calibration"] == f0b["calibration"]
    assert f0a["chosen_params"] == f0b["chosen_params"]


def test_failed_fold_is_preserved(monkeypatch):
    import qat.research.walkforward as wf

    ds = load_dataset(KR_FIXTURE)
    calls = {"n": 0}
    real = wf.run_backtest

    def flaky(dataset, strategy, config):
        calls["n"] += 1
        if config.trade_start == 350:  # OOS window of fold 1
            raise RuntimeError("injected failure")
        return real(dataset, strategy, config)

    monkeypatch.setattr(wf, "run_backtest", flaky)
    out = run_walkforward(ds, "ma_trend", WalkForwardConfig(train_bars=250, test_bars=100,
                                                            grid={"fast": [10], "slow": [30]}))
    statuses = [f["status"] for f in out["result"]["folds"]]
    assert "FAIL" in statuses and "OK" in statuses
    assert out["metrics"]["folds_failed"] == statuses.count("FAIL")
    reloaded = load_run(out["manifest"]["run_id"])
    assert any("injected failure" in f.get("error", "") for f in reloaded["result"]["folds"])


# ------------------------------------------------------ determinism / manifest
def test_determinism_and_manifest_roundtrip():
    ds = load_dataset(KR_FIXTURE)
    strat = {"alpha_mode": "fixture", "fixture_expected_return": 0.01}
    a = run_backtest(ds, make_strategy("breakout", **strat), BacktestConfig())
    b = run_backtest(ds, make_strategy("breakout", **strat), BacktestConfig())
    assert a.fills == b.fills and a.orders == b.orders and a.equity == b.equity
    assert a.audit == b.audit  # ids + simulated timestamps are deterministic
    ma, mb = save_backtest(a), save_backtest(b)
    assert ma["run_id"] != mb["run_id"]
    assert ma["economic_fingerprint"] == mb["economic_fingerprint"]
    for key in ("code_commit", "git_dirty", "source_tree_sha256", "data", "strategy", "config",
                "cost_models", "cost_model_version", "settings", "initial_capital", "market",
                "symbol", "timeframe", "period", "seed", "labels"):
        assert key in ma, key
    assert ma["data"]["sha256"] == ds.sha256
    assert ma["settings"]["placeholder"] is True
    assert ma["strategy_health"] == "NOT_IMPLEMENTED"
    if not (ROOT / ".git").exists():
        assert str(ma["code_commit"]).startswith("unavailable")
        assert ma["git_dirty"] == "unavailable"
    loaded = load_run(ma["run_id"], include_audit=True)
    assert loaded["result"]["fills"] == a.fills
    assert loaded["metrics"]["net_pnl"] == pytest.approx(a.metrics["net_pnl"])
    assert loaded["audit"]
    assert {r["run_id"] for r in list_runs()} >= {ma["run_id"], mb["run_id"]}
    with pytest.raises(KeyError):
        load_run("../etc")


# --------------------------------------------------------------- edge cases
def test_zero_trades_metrics_are_undefined_not_zero(hand_settings):
    ds = make_dataset(HAND_BARS)
    res = run_backtest(ds, Scripted(script={}), BacktestConfig(initial_cash=1_000_000,
                                                                settings_path=hand_settings))
    m = res.metrics
    assert m["trades_closed"] == 0 and m["fills"] == 0
    assert m["win_rate"] is None and m["expectancy"] is None and m["profit_factor"] is None
    assert m["net_pnl"] == 0.0 and m["sample_warning"] is True
    assert any("no closed trades" in n for n in m["notes"])


def test_failed_dataset_is_blocked():
    ds = make_dataset(HAND_BARS)
    bad = replace(ds, validation=replace(ds.validation, status="FAIL"))
    with pytest.raises(DataRejected):
        run_backtest(bad, Scripted(script={}), BacktestConfig())


def test_research_requires_explicit_settings(tmp_path):
    ds = make_dataset(HAND_BARS)
    with pytest.raises(FileNotFoundError):
        run_backtest(ds, Scripted(script={}), BacktestConfig(settings_path=str(tmp_path / "none.yaml")))


def test_kill_switch_blocks_every_backtest_order(hand_settings):
    ds = make_dataset(HAND_BARS)
    res = run_backtest(ds, Scripted(script={1: ENTER_LONG}),
                       BacktestConfig(initial_cash=1_000_000, settings_path=hand_settings,
                                      risk_overrides={"kill_switch": True}))
    assert res.fills == [] and res.orders == []
    assert res.rejections and all(r["stage"] == "risk" and "kill_switch" in r["reason"] for r in res.rejections)


def test_backtest_orders_go_through_gateway_and_settlement(hand_settings):
    ds = make_dataset(HAND_BARS)
    res = run_backtest(ds, Scripted(script={1: ENTER_LONG, 3: EXIT_LONG}),
                       BacktestConfig(initial_cash=1_000_000, settings_path=hand_settings))
    stages = [r["stage"] for r in res.audit]
    for s in ("proposal_received", "validator_decision", "net_alpha_decision", "risk_decision",
              "compliance_decision", "integrity_decision", "order_reserved", "order_submitted"):
        assert stages.count(s) == len(res.orders), s
    assert stages.count("fill_settled") == len(res.fills)


def test_next_bar_gap_records_breach_and_halts_trading(hand_settings):
    gap = [(100, 101, 99, 100), (100, 102, 99, 101), (130, 131, 129, 130),
           (130, 131, 129, 130), (131, 132, 130, 131), (131, 132, 130, 131)]
    ds = make_dataset(gap)
    res = run_backtest(ds, Scripted(script={1: ENTER_LONG, 2: EXIT_LONG, 3: EXIT_LONG}),
                       BacktestConfig(initial_cash=1_000_000, settings_path=hand_settings,
                                      position_fraction=0.5, reservation_buffer_pct=0.02))
    assert len(res.breaches) == 1 and res.breaches[0]["overrun"] > 0
    assert res.end_state["trading_halted_by_breach"] is True
    assert res.fills[0]["price"] == pytest.approx(130 * 1.001)  # real fill kept, not hidden
    assert all("reservation_breach" in r["reason"] for r in res.rejections)


def test_cost_stress_increases_costs_and_lowers_return():
    ds = load_dataset(KR_FIXTURE)
    out = run_cost_stress(ds, "ma_trend", {"alpha_mode": "fixture", "fixture_expected_return": 0.01},
                          BacktestConfig(), (1.0, 2.0, 3.0))
    rows = out["metrics"]["rows"]
    assert [r["cost_multiplier"] for r in rows] == [1.0, 2.0, 3.0]
    assert rows[0]["explicit_costs"] < rows[1]["explicit_costs"] < rows[2]["explicit_costs"]
    assert rows[0]["net_return"] > rows[1]["net_return"] > rows[2]["net_return"]
    assert load_run(out["manifest"]["run_id"])["metrics"]["rows"] == rows


def test_synthetic_and_fixture_labels_propagate():
    ds = load_dataset(KR_FIXTURE)
    res = run_backtest(ds, make_strategy("ma_trend", alpha_mode="fixture", fixture_expected_return=0.01),
                       BacktestConfig())
    assert res.labels["synthetic_data"] is True and res.labels["alpha_mode"] == "fixture"
    assert any("SYNTHETIC" in w for w in res.warnings)
    assert any("FIXTURE" in w for w in res.warnings)
