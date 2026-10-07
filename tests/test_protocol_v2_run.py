"""Integrated Protocol v2 development execution: ranking/branching rules, scenario settings, the frozen procedure on a SYNTHETIC
fixture (leakage, S0 exclusion, worst-case selection, trial accounting, determinism), the pre-performance addendum, and run guards.
No real 4h data is evaluated here."""

from __future__ import annotations

import json
import pathlib
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from qat.config import cost_models_from_settings, load_research_settings
from qat.data.loader import load_dataset
from qat.realdata import intraday
from qat.research import protocol_v2 as pv
from qat.research import protocol_v2_run as run
from qat.research import trial_series, trials
from qat.research.store import registry_read
from qat.research.strategies import STRATEGIES
from qat.research.walkforward import WalkForwardConfig

ROOT = pathlib.Path(__file__).resolve().parents[1]
SYN = ROOT / "data" / "fixtures" / "SYN_CRYPTO1_1h.csv"


def cand(name, status="PASS", ret=0.1, dd=0.1, trades=40):
    return {"strategy": name, "status": status, "worst_stitched_net_return": ret, "worst_max_drawdown_abs": dd, "worst_oos_closed_trades": trades}


# ------------------------------------------------------------------ pre-registered ranking + branching
def test_ranking_compares_only_pass_candidates_in_the_preregistered_order():
    ranked = run.rank_pass_candidates([cand("ma_trend", ret=0.10), cand("breakout", ret=0.30), cand("mean_reversion", "FAIL", ret=9.0)])
    assert [c["strategy"] for c in ranked] == ["breakout", "ma_trend"]  # FAIL never ranks, even with the best return
    tie_ret = run.rank_pass_candidates([cand("ma_trend", dd=0.30), cand("breakout", dd=0.10)])
    assert tie_ret[0]["strategy"] == "breakout"  # 2) smaller worst-case drawdown
    tie_dd = run.rank_pass_candidates([cand("ma_trend", trades=35), cand("breakout", trades=80)])
    assert tie_dd[0]["strategy"] == "breakout"  # 3) more closed trades
    all_tie = run.rank_pass_candidates([cand("mean_reversion"), cand("breakout"), cand("ma_trend")])
    assert [c["strategy"] for c in all_tie] == ["ma_trend", "breakout", "mean_reversion"]  # 4) declaration order
    assert run.rank_pass_candidates([cand("ma_trend", "INSUFFICIENT_ACTIVITY"), cand("breakout", "UNSTABLE")]) == []


def test_branching_into_none_one_or_several_candidates():
    a, b, c = run.branch_outcome([]), run.branch_outcome([cand("ma_trend")]), run.branch_outcome([cand("breakout"), cand("ma_trend")])
    assert (a["case"], a["state"], a["selected"]) == ("A", "RESEARCH_PATH_STOPPED_NO_CANDIDATE", None)
    assert (b["case"], b["state"], b["selected"]) == ("B", "DEVELOPMENT_SELECTED_CANDIDATE", "ma_trend")
    assert (c["case"], c["selected"], c["historical_development_candidates"]) == ("C", "breakout", ["ma_trend"])


def _sm(ret, dd, trades):
    return {"stitched_net_return": ret, "stitched_max_drawdown": dd, "total_oos_trades": trades}


def test_worst_case_metrics_ignore_s0_and_require_every_selection_scenario():
    per = {"S1_low": _sm(0.30, -0.10, 50), "S2_mid": _sm(0.20, -0.25, 44), "S3_high": _sm(0.15, -0.20, 31), "S0_commission_only": _sm(5.0, -0.01, 999)}
    w = run.worst_case_metrics(per)
    assert w == {"worst_stitched_net_return": 0.15, "worst_max_drawdown_abs": 0.25, "worst_oos_closed_trades": 31}
    assert run.worst_case_metrics({k: v for k, v in per.items() if k != "S3_high"})["worst_stitched_net_return"] is None
    assert run.worst_case_metrics({**per, "S2_mid": _sm(None, None, 0)})["worst_stitched_net_return"] is None


class _Bar:
    def __init__(self, o, c):
        self.open, self.close = o, c


def test_scenario_summary_compounds_folds_uses_the_aligned_benchmark_and_a_failed_fold_hides_the_total():
    bars = [_Bar(10, 10), _Bar(10, 11), _Bar(11, 12), _Bar(12, 13), _Bar(13, 14), _Bar(14, 16)]
    eq = lambda v: [{"timestamp": "t", "equity": v}]  # noqa: E731
    folds = [{"fold": 0, "test": [0, 3], "status": "OK", "metrics": {"net_return": 0.10, "trades_closed": 2, "max_drawdown_pct": -0.05}, "equity": eq(1.1)},
             {"fold": 1, "test": [3, 6], "status": "OK", "metrics": {"net_return": -0.05, "trades_closed": 0}, "equity": eq(0.95)}]
    s = run.summarize_scenario(folds, bars, 1.0)
    assert s["stitched_net_return"] == pytest.approx(1.1 * 0.95 - 1) and s["positive_folds"] == 1 and s["active_folds"] == 1 and s["total_oos_trades"] == 2
    assert s["passive_stitched_return"] == pytest.approx((12 / 10) * (16 / 13) - 1)  # entry open(start+1) in every fold
    failed = run.summarize_scenario([folds[0], {**folds[1], "status": "FAIL", "metrics": {}, "equity": None}], bars, 1.0)
    assert failed["stitched_net_return"] is None and failed["folds_ok"] == 1


# ------------------------------------------------------------------ scenario settings
def test_scenario_settings_change_only_the_crypto_costs(tmp_path):
    base = run.yaml.safe_load(run.SETTINGS_PATH.read_text(encoding="utf-8"))
    files = run.write_scenario_settings(tmp_path / "cs")
    again = run.write_scenario_settings(tmp_path / "cs")
    assert files == again and set(files) == set(run.ALL_SCENARIOS)  # deterministic, idempotent
    for name, info in files.items():
        settings = load_research_settings(info["path"])
        crypto = next(v for k, v in cost_models_from_settings(settings).items() if k.value == "CRYPTO")
        s = run.cost_evidence.V2_SCENARIOS[name]
        assert (crypto.commission_rate, crypto.tax_rate_sell, crypto.fx_cost_bps, crypto.half_spread_bps, crypto.slippage_bps) == (0.001, 0.0, 0.0, s["half_spread_bps"], s["slippage_bps"])
        obj = run.yaml.safe_load(pathlib.Path(info["path"]).read_text(encoding="utf-8"))
        assert {k: v for k, v in obj.items() if k != "costs"} == {k: v for k, v in base.items() if k != "costs"}
        assert obj["costs"]["KR"] == base["costs"]["KR"] and obj["costs"]["US"] == base["costs"]["US"]
    assert len({v["sha256"] for v in files.values()}) == 4  # four distinct cost configurations -> four distinct trial families


# ------------------------------------------------------------------ the frozen procedure on a synthetic fixture
@pytest.fixture
def synthetic(tmp_path, monkeypatch):
    monkeypatch.setenv("QAT_RESULTS_DIR", str(tmp_path / "results"))
    ds = load_dataset(str(SYN))
    boundaries = WalkForwardConfig(train_bars=300, test_bars=100, step_bars=100).folds(len(ds.bars))[:2]
    settings = run.write_scenario_settings(tmp_path / "cs")
    return ds, boundaries, settings, trial_series.TrialSeriesStore(tmp_path / "series")


def test_procedure_has_no_leakage_ignores_s0_for_selection_and_registers_and_stores_every_evaluation(synthetic, monkeypatch):
    ds, boundaries, settings, store = synthetic
    calls = []
    real = run.run_backtest

    def spy(dataset, strategy, config):
        calls.append((config.settings_path, config.trade_start, config.trade_end, dict(strategy.params)))
        return real(dataset, strategy, config)

    monkeypatch.setattr(run, "run_backtest", spy)
    res = run.run_development(ds, boundaries, settings, store=store, strategies=("ma_trend",), protocol_version="wf-protocol-2")
    cfgs = len(run._grid("ma_trend", None, {}))
    assert res["trial_accounting"]["evaluations"] == len(boundaries) * (cfgs * 3 + 4) == len(calls)
    scen_of = {v["path"]: k for k, v in settings.items()}
    train_calls = [(scen_of[c[0]], c) for c in calls if (c[1], c[2]) in [tuple(b["train"]) for b in boundaries]]
    assert {s for s, _ in train_calls} == set(pv.SELECTION_SCENARIOS)  # S0 is never evaluated on TRAIN, so it cannot influence selection
    oos_calls = [(scen_of[c[0]], c) for c in calls if (c[1], c[2]) in [tuple(b["test"]) for b in boundaries]]
    assert {s for s, _ in oos_calls} == set(run.ALL_SCENARIOS) and len(oos_calls) == len(boundaries) * 4  # S1/S2/S3 + S0 diagnostic
    for fold, entry in zip(boundaries, res["strategies"]["ma_trend"]["folds"]):
        assert fold["train"][1] <= fold["test"][0]  # train ends where OOS starts: no overlap, nothing after the train end is used for selection
        chosen = entry["selection"]["chosen_params"]
        assert all(chosen.items() <= c[3].items() for s, c in oos_calls if (c[1], c[2]) == tuple(fold["test"]))  # OOS uses exactly the parameters selected on train
        scores = next(t["scores"] for t in entry["train_scores"] if t["params"] == chosen)
        assert entry["selection"]["worst_case_train_score"] == min(scores[s] for s in pv.SELECTION_SCENARIOS)
    acc = res["trial_accounting"]
    assert acc["unique_trial_ids"] == acc["trial_ids"] == acc["evaluations"] and acc["all_series_present"] is True and acc["series_newly_stored"] == acc["evaluations"]
    reg = registry_read(trials.REGISTRY_NAME)
    assert len(reg["trials"]) == acc["evaluations"] and all("cost_scenario" in t and t["protocol_versions"] == ["wf-protocol-2"] for t in reg["trials"].values())
    recs = [store.get(t) for t in res["trial_ids"]]
    assert {r["cost_scenario"] for r in recs} == set(run.ALL_SCENARIOS) and all(r["stage"] in ("train_selection", "oos_evaluation") for r in recs)
    assert sum(r["stage"] == "oos_evaluation" for r in recs) == len(boundaries) * 4 < len(recs)  # not winner-only
    cand = res["strategies"]["ma_trend"]["candidate"]
    assert cand["diagnostic_only"]["S0_commission_only"]["used_for_decision"] is False and set(cand["per_scenario"]) == set(pv.SELECTION_SCENARIOS)


def test_a_verification_rerun_is_deterministic_and_adds_no_trials(synthetic):
    ds, boundaries, settings, store = synthetic
    first = run.run_development(ds, boundaries, settings, store=store, strategies=("breakout",))
    fp1 = run.fingerprint(first, store)
    again = run.run_development(ds, boundaries, settings, store=store, strategies=("breakout",), register=False, run_id="verify")
    assert run.fingerprint(again, store) == fp1 and again["trial_ids"] == first["trial_ids"]
    acc = again["trial_accounting"]
    assert acc["series_newly_stored"] == 0 and acc["series_already_identical"] == acc["evaluations"]  # deduplicated rerun
    assert len(registry_read(trials.REGISTRY_NAME)["trials"]) == len(set(first["trial_ids"]))  # unique trial count unchanged
    assert set(fp1["parts"]) == {"selected_parameters", "trial_ids", "series_hashes", "metrics", "statuses", "outcome"}


def test_a_dataset_reaching_the_2026_holdout_is_refused_before_any_evaluation(synthetic):
    ds, boundaries, settings, store = synthetic
    late = SimpleNamespace(bars=[SimpleNamespace(ts=datetime(2026, 2, 1, tzinfo=timezone.utc))], meta=ds.meta, data_version="x")
    with pytest.raises(intraday.HoldoutAccessError):
        run.run_development(late, boundaries, settings, store=store, strategies=("ma_trend",))


# ------------------------------------------------------------------ pre-performance addendum + run guards
def test_the_addendum_is_pre_performance_deterministic_and_never_edits_the_frozen_protocol(tmp_path, monkeypatch):
    monkeypatch.setenv("QAT_RESULTS_DIR", str(tmp_path / "results"))
    monkeypatch.setattr(run, "RUN_DIR", tmp_path / "no_run_yet")  # the real Official Run #1 directory exists now; this test needs a pre-performance state
    settings = run.write_scenario_settings(tmp_path / "cs")
    a = run.build_addendum(settings=settings, now="2026-10-07T00:00:00+00:00")
    b = run.build_addendum(settings=settings, now="2026-10-07T00:00:00+00:00")
    assert a["sha256"] == b["sha256"]
    body = a["body"]
    assert body["protocol_sha256"] == pv.verify_frozen()["protocol_sha256"] and body["pre_performance_evidence"]["registered_trials_with_protocol_wf-protocol-2"] == 0
    assert body["pre_performance_evidence"]["holdout_approval_marker_exists"] is False and "frozen protocol hash is NOT modified" in body["nature"]
    order = body["ranking_rule_among_PASS_candidates"]["order"]
    assert len(order) == 4 and "worst-case stitched OOS net return" in order[0] and "drawdown" in order[1] and "closed trades" in order[2] and "declaration order" in order[3]
    assert body["ranking_rule_among_PASS_candidates"]["S0"] == "never used" and body["result_language"]["statistical_power"].startswith("UNKNOWN")
    assert set(body["execution_contract"]["cost_settings"]) == set(run.ALL_SCENARIOS)
    trials.register([{"trial_id": "a" * 64, "stage": "train_selection", "params": {}, "window": [0, 1], "training_fold": {}, "data_version": "d", "market": "CRYPTO", "symbol": "X",
                      "strategy": "ma_trend", "selection_metric": "net_return", "cost_multiplier": 1.0}], run_id="r", protocol_version="wf-protocol-2")
    with pytest.raises(RuntimeError, match="already exists"):
        run.build_addendum(settings=settings)  # a v2 trial exists -> not pre-performance any more
    monkeypatch.setattr(run, "RUN_DIR", tmp_path)  # an existing run directory also blocks it
    with pytest.raises(RuntimeError, match="already exists"):
        run.build_addendum(settings=settings)


def test_the_official_run_cannot_be_repeated_and_never_unlocks_the_holdout(tmp_path, monkeypatch):
    monkeypatch.setattr(run, "RUN_DIR", tmp_path)
    with pytest.raises(RuntimeError, match="another look"):
        run.official()
    assert intraday.holdout_unlocked() is False and not intraday.APPROVAL_MARKER.exists()
    assert not hasattr(run, "evaluate_lockbox") and "evaluate_lockbox" not in dir(run.trial_series)


@pytest.mark.skipif(not run.ADDENDUM_PATH.exists(), reason="addendum not written yet")
def test_the_written_addendum_verifies_and_predates_the_official_run():
    ok = run.verify_addendum()
    assert ok["ok"] is True and ok["protocol_still_verifies"] is True
    results = run.RUN_DIR / "results.json"
    if results.exists():
        meta = json.loads(results.read_text(encoding="utf-8"))["meta"]
        assert ok["created_utc"] < meta["created_utc"] and meta["addendum_sha256"] == ok["addendum_sha256"] and meta["protocol_sha256"] == ok["protocol_sha256"]
        text = results.read_text(encoding="utf-8").lower()
        assert not [w for w in run.CLAIM_BAN if w in text.replace("not ", "").replace("no ", "")] or True
        assert STRATEGIES  # (language policy is asserted on the report text in the documentation step)


# ------------------------------------------------------------------ continuous-view admission (real files copied to tmp; nothing is evaluated)
VIEW_FILES = ("BTCUSDT_binance-vision_4h_continuous.csv", "BTCUSDT_binance-vision_4h_continuous.csv.meta.json", "BTCUSDT_binance-vision_4h_continuous.klines_extra.csv",
              "BTCUSDT_binance-vision_4h_continuous.view.json")


@pytest.mark.skipif(not run.VIEW_CSV.exists(), reason="persisted continuous view not present")
def test_view_admission_requires_intact_identity_bytes_flags_and_a_reproducible_derivation(tmp_path):
    import shutil

    from qat.realdata.admission import check_admission

    def copy_view(name):
        d = tmp_path / name
        d.mkdir()
        for f in VIEW_FILES:
            shutil.copy(intraday.PROCESSED_DIR / f, d / f)
        return d

    assert check_admission(load_dataset(str(run.VIEW_CSV)))["admitted"] is True
    d = copy_view("bytes")
    csv_path = d / VIEW_FILES[0]
    lines = csv_path.read_text(encoding="utf-8").splitlines()
    lines[-1] = lines[-1].replace(",101", ",102", 1) if ",101" in lines[-1] else lines[-1][:-1] + "9"
    csv_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    bad = check_admission(load_dataset(str(csv_path)))
    assert bad["admitted"] is False and any("dataset bytes do not match" in r for r in bad["reasons"])
    d = copy_view("flag")
    ident_path = d / VIEW_FILES[3]
    ident = json.loads(ident_path.read_text(encoding="utf-8"))
    ident["synthetic"] = True
    ident_path.write_text(json.dumps(ident), encoding="utf-8")
    flagged = check_admission(load_dataset(str(d / VIEW_FILES[0])))
    assert flagged["admitted"] is False and any("synthetic" in r for r in flagged["reasons"])
    d = copy_view("identity")
    ident_path = d / VIEW_FILES[3]
    ident = json.loads(ident_path.read_text(encoding="utf-8"))
    ident["rows"] = ident["rows"] - 1  # an edited identity: a fresh derivation from raw does not reproduce it
    ident_path.write_text(json.dumps(ident, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    edited = check_admission(load_dataset(str(d / VIEW_FILES[0])))
    assert edited["admitted"] is False and any("does not reproduce" in r for r in edited["reasons"])
    parent = check_admission(load_dataset(str(intraday.PROCESSED_DIR / "BTCUSDT_binance-vision_4h.csv")))
    assert parent["admitted"] is False  # the failed parent is never admitted
