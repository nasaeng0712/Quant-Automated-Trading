"""Batch #3C - research-validity foundation: trial identity/registry, cost-evidence classification,
benchmark audit, Lockbox non-access. No strategy is evaluated on real data here (the live-path test
uses the synthetic fixture and a tmp results dir)."""

from __future__ import annotations

import copy
import json
import pathlib
from types import SimpleNamespace

import pytest

from qat.data.loader import load_dataset
from qat.research import cost_evidence as ce
from qat.research import protocol, trials, validity
from qat.research import walkforward as wf
from qat.research.backtest import BacktestConfig
from qat.research.store import registry_read
from qat.research.walkforward import WalkForwardConfig, run_walkforward

ROOT = pathlib.Path(__file__).resolve().parents[1]
FROZEN = ROOT / "artifacts" / "verification" / "walkforward_3b"
HAVE_3B = (FROZEN / "summary.json").exists() and all((ROOT / protocol.MARKETS[m]).exists() for m in protocol.MARKETS)

BASE = {"initial_cash": 1e7, "cost_multiplier": 1.0, "position_fraction": 0.95, "reservation_buffer_pct": 0.02, "seed": 0,
        "trade_start": 0, "trade_end": None, "settings_path": "x.yaml"}


def tid(**over):
    kw = dict(data_version="DV", strategy="ma_trend", strategy_version=1, params={"fast": 5, "slow": 30}, window=[0, 100],
              stage="train_selection", selection_metric="net_return", base_config=BASE, settings_sha="S")
    kw.update(over)
    return trials.trial_id(**kw)


@pytest.fixture
def results(tmp_path, monkeypatch):
    monkeypatch.setenv("QAT_RESULTS_DIR", str(tmp_path / "results"))
    return tmp_path / "results"


# ------------------------------------------------------------------ trial identity / counting
def test_trial_id_is_deterministic_and_sensitive_to_everything_that_changes_the_numbers():
    assert tid() == tid() and tid(params={"slow": 30, "fast": 5}) == tid()  # key order irrelevant
    assert tid(base_config={**BASE, "settings_path": "elsewhere.yaml", "trade_start": 9, "trade_end": 9}) == tid()  # window/path fields are not identity
    for change in ({"data_version": "DV2"}, {"strategy": "breakout"}, {"strategy_version": 2}, {"params": {"fast": 10, "slow": 30}},
                   {"window": [0, 101]}, {"stage": "oos_evaluation"}, {"selection_metric": "sharpe"}, {"settings_sha": "S2"},
                   {"base_config": {**BASE, "cost_multiplier": 2.0}}, {"base_config": {**BASE, "seed": 1}}):
        assert tid(**change) != tid(), change
    with pytest.raises(ValueError):
        tid(stage="nonsense")


def _fold_rows():
    return [{"fold": 0, "train": [0, 100], "test": [100, 120], "chosen_params": {"fast": 5},
             "calibration": [{"params": {"fast": 5}, "net_return": 0.1, "status": "OK"},
                             {"params": {"fast": 10}, "net_return": None, "status": "FAIL"}]},  # a failed evaluation is still a trial
            {"fold": 1, "train": [20, 120], "test": [120, 140], "chosen_params": {"fast": 10},
             "calibration": [{"params": {"fast": 5}, "net_return": 0.0, "status": "OK"}, {"params": {"fast": 10}, "net_return": 0.2, "status": "OK"}]}]


def _collect(rows=None, **over):
    kw = dict(data_version="DV", market="KR", symbol="X", strategy="ma_trend", strategy_version=1, base_config=BASE, settings_sha="S",
              fold_rows=rows or _fold_rows())
    kw.update(over)
    return trials.collect(**kw)


def test_collect_counts_every_evaluated_candidate_and_does_not_touch_the_fold_rows():
    rows = _fold_rows()
    before = copy.deepcopy(rows)
    got = _collect(rows)
    assert len(got) == 4 + 2 and sum(t["stage"] == "oos_evaluation" for t in got) == 2
    assert rows == before  # contamination detectors compare these rows across perturbed datasets
    assert {t["trial_id"] for t in got} == {t["trial_id"] for t in _collect()} and len({t["trial_id"] for t in got}) == 6
    oos = next(t for t in got if t["stage"] == "oos_evaluation")
    assert oos["training_fold"] == {"fold": 0, "train": [0, 100], "test": [100, 120]} and oos["selection_metric"] == "net_return"
    assert {"trial_id", "market", "strategy", "params", "data_version"} <= set(oos)


def test_registry_never_double_counts_a_rerun_and_records_protocol_and_runs(results):
    t = _collect()
    assert trials.register(t, run_id="run-1", protocol_version="p1") == {"added": 6, "already_registered": 0}
    assert trials.register(t, run_id="run-1", protocol_version="p1") == {"added": 0, "already_registered": 6}  # same run again: nothing changes
    assert trials.register(t, run_id="run-2", protocol_version="p2") == {"added": 0, "already_registered": 6}  # same computation, new run/protocol
    reg = registry_read(trials.REGISTRY_NAME)
    s = trials.summarize(reg)
    assert s["unique_trials"] == 6 and s["executions"] == 12 and s["reused_trials"] == 6
    rec = next(iter(reg["trials"].values()))
    assert rec["run_ids"] == ["run-1", "run-2"] and rec["protocol_versions"] == ["p1", "p2"]
    trials.register(_collect(base_config={**BASE, "cost_multiplier": 2.0}), run_id="run-3", protocol_version="p1")  # different cost config = new trials
    s = trials.summarize(registry_read(trials.REGISTRY_NAME))
    assert s["unique_trials"] == 12 and s["candidate_procedures"] == 1 and s["distinct_configurations"] == 4
    assert trials.summarize(registry_read(trials.REGISTRY_NAME), real_only=True)["unique_trials"] == 12
    trials.register(_collect(), run_id="syn", protocol_version=None, synthetic=True)  # synthetic runs never count as real trials
    assert trials.summarize(registry_read(trials.REGISTRY_NAME))["unique_trials"] == 12


def test_live_path_and_reconstruction_agree_and_a_repeat_adds_no_trials(results):
    ds = load_dataset(str(ROOT / "data" / "fixtures" / "SYN_KR1_1d.csv"))
    cfg = WalkForwardConfig(train_bars=250, test_bars=60, base=BacktestConfig(settings_path=str(ROOT / "config" / "settings.yaml")))
    first = run_walkforward(ds, "ma_trend", cfg, extra_manifest={"research_protocol": {"version": "unit-proto"}})
    ids = first["manifest"]["trial_context"]["trial_ids"]
    assert first["manifest"]["trial_context"]["trial_count"] == len(ids) and len(set(ids)) == len(ids) > 0
    assert all("trial_id" not in c for f in first["result"]["folds"] for c in f["calibration"])
    reg = registry_read(trials.REGISTRY_NAME)
    assert set(reg["trials"]) == set(ids) and all(r["protocol_versions"] == ["unit-proto"] for r in reg["trials"].values())
    assert trials.reconstruct()["trials_added"] == 0  # reconstruction from the saved run yields exactly the registered ids
    run_walkforward(ds, "ma_trend", cfg)  # identical computation again
    s = trials.summarize(registry_read(trials.REGISTRY_NAME), real_only=False)
    assert s["unique_trials"] == len(ids) and s["executions"] == 2 * len(ids)
    assert trials.reconstruct()["trials_added"] == 0


# ------------------------------------------------------------------ cost evidence
def test_every_settings_cost_parameter_is_classified_and_none_is_verified_without_a_primary_match():
    rep = ce.report()
    configured = ce.configured_costs()
    assert set(rep["markets"]) == set(configured)
    for market, fields in configured.items():
        for name in ce.COST_FIELDS:
            entry = rep["markets"][market][name]
            assert entry["status"] in ce.STATUSES and entry["configured"] == fields[name]
    assert rep["status_counts"]["VERIFIED"] == 0  # placeholder numbers are not verified by anything we hold
    assert rep["markets"]["CRYPTO"]["commission_rate"]["status"] == "PLACEHOLDER" and "LOWER" in rep["markets"]["CRYPTO"]["commission_rate"]["why"]
    assert rep["markets"]["KR"]["half_spread_bps"]["why"].endswith("unavailable") and rep["markets"]["CRYPTO"]["tax_rate_sell"]["status"] == "UNKNOWN"
    assert rep["markets"]["KR"]["commission_rate"]["dependency"].startswith("user decision")


def test_cost_evidence_validation_rejects_inconsistent_classifications():
    configured = ce.configured_costs()
    good = copy.deepcopy(ce.EVIDENCE)
    ce.validate(good, configured)
    bad = copy.deepcopy(good)
    bad["KR"]["commission_rate"]["status"] = "VERIFIED"  # no primary source, no match
    with pytest.raises(ce.CostEvidenceError, match="VERIFIED needs a primary source"):
        ce.validate(bad, configured)
    bad = copy.deepcopy(good)
    bad["US"]["commission_rate"]["matches_primary_source"] = True  # a PLACEHOLDER cannot claim a match
    with pytest.raises(ce.CostEvidenceError, match="cannot claim"):
        ce.validate(bad, configured)
    bad = copy.deepcopy(good)
    bad["US"]["slippage_bps"]["status"] = "GREAT"
    with pytest.raises(ce.CostEvidenceError, match="invalid status"):
        ce.validate(bad, configured)
    bad = copy.deepcopy(good)
    del bad["CRYPTO"]["fx_cost_bps"]
    with pytest.raises(ce.CostEvidenceError, match="not classified"):
        ce.validate(bad, configured)
    bad = copy.deepcopy(good)
    bad["CRYPTO"]["slippage_bps"]["status"] = "VERIFIED"
    bad["CRYPTO"]["slippage_bps"]["matches_primary_source"] = True  # primary source (the archive) exists but says nothing about slippage
    bad["CRYPTO"]["slippage_bps"]["sources"] = [{"kind": "secondary"}]
    with pytest.raises(ce.CostEvidenceError):
        ce.validate(bad, configured)


# ------------------------------------------------------------------ benchmark + frozen status + Lockbox non-access
def test_aligned_benchmark_enters_at_the_earliest_engine_feasible_open():
    class B:
        def __init__(self, o, c):
            self.open, self.close = o, c

    bars = [B(10, 10), B(10, 11), B(12, 12), B(12, 15)]
    assert protocol.passive_return(bars, [1, 4]) == pytest.approx(15 / 10 - 1)  # v1: open(test_start)
    assert validity.aligned_passive_return(bars, [1, 4]) == pytest.approx(15 / 12 - 1)  # open(test_start+1): the engine's first possible fill


def test_threshold_provenance_is_classified_as_an_internal_heuristic():
    for name, entry in validity.THRESHOLD_PROVENANCE.items():
        assert entry["classification"] == "internal pre-registered heuristic" and entry["literature_basis_in_repository"].startswith("none")
        assert entry["value"] == protocol.THRESHOLDS[name]
    assert validity.METHODS["white_reality_check"]["decision"].startswith("not applied")
    assert validity.METHODS["chosen"]["primary_text_verified_by_this_audit"] is False


@pytest.mark.skipif(not HAVE_3B, reason="Batch #3B evidence / persisted datasets not present")
class TestAgainstTheFrozenBatch3B:
    def test_frozen_protocol_and_results_are_untouched(self):
        status = validity.frozen_status()
        assert status["code_matches_frozen_protocol"] is True and status["thresholds"]["min_total_oos_trades"] == 30
        assert status["thresholds"]["min_active_fold_fraction"] == 0.5
        recorded = ROOT / "artifacts" / "verification" / "validity_3c" / "frozen_3b_status.json"
        if recorded.exists():  # the hashes recorded by Batch #3C still match the files
            old = json.loads(recorded.read_text(encoding="utf-8"))
            assert old["protocol_json_file_sha256"] == status["protocol_json_file_sha256"]
            assert old["summary_json_file_sha256"] == status["summary_json_file_sha256"]
        summary = json.loads((FROZEN / "summary.json").read_text(encoding="utf-8"))
        assert summary["lockbox"]["decision"] == "NOT_READY_FOR_LOCKBOX" and summary["lockbox"]["opened"] is False
        assert all(e["market_specific"]["decision"]["status"] == "INSUFFICIENT_ACTIVITY" for e in summary["results"].values())

    def test_benchmark_audit_reads_only_the_development_window_and_never_the_lockbox(self, monkeypatch, results):
        summary = json.loads((FROZEN / "summary.json").read_text(encoding="utf-8"))
        seen = []

        class Recording(list):
            def __getitem__(self, key):
                seen.append(key)
                return super().__getitem__(key)

        real_load = validity.load_dataset

        def spy(path):
            ds = real_load(path)
            return SimpleNamespace(bars=Recording(ds.bars), meta=ds.meta)

        monkeypatch.setattr(validity, "load_dataset", spy)
        monkeypatch.setattr(wf, "evaluate_lockbox", lambda *a, **k: (_ for _ in ()).throw(AssertionError("Lockbox opened")))
        audit = validity.benchmark_audit(summary)
        n = {"KR": 1473, "US": 2011, "CRYPTO": 2922}
        slices = [k for k in seen if isinstance(k, slice)]
        assert len(slices) == 3 and all(k.stop == n[m] - protocol.WINDOWS[m][2] for k, m in zip(slices, summary["markets"]))
        assert all(isinstance(k, slice) for k in seen)  # the audit never indexes the full-length bars directly
        assert set(audit["markets"]) == {"KR", "US", "CRYPTO"} and not (results / "lockbox_registry.json").exists()

    def test_status_decision_and_lockbox_report(self):
        summary = json.loads((FROZEN / "summary.json").read_text(encoding="utf-8"))
        rates = validity.observed_trade_rates(summary)
        decision = validity.protocol_v2_decision(rates)
        assert decision["decision"] in ("PROTOCOL_V2_RECOMMENDED", "RESEARCH_STOP_RECOMMENDED")
        assert any("USER DECISIONS" in x for x in decision["required_new_data_and_evidence"])
        assert "no strategy execution" in decision["not_allowed"] and set(decision["options"]) == {
            "A_longer_verified_history", "B_higher_frequency_bars", "C_keep_daily_and_stop"}
        lock = validity.lockbox_status()
        assert lock["opened"] is False and lock["evaluate_lockbox_imported_by_this_module"] is False
