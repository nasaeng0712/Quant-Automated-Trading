"""Batch #3E - Protocol v2 pre-registration: continuous-view derivation + admission semantics, worst-case cost selection,
candidate rules, fold plan, freeze/hash integrity, holdout guard. Synthetic archives only; the real-data checks read
timestamps/metadata and never evaluate a strategy."""

from __future__ import annotations

import copy
import hashlib
import io
import json
import pathlib
import zipfile
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from qat.data.loader import load_dataset
from qat.realdata import intraday as it
from qat.research import protocol_v2 as pv
from qat.research import trial_series as ts
from qat.research import trials
from qat.research.strategies import STRATEGIES

ROOT = pathlib.Path(__file__).resolve().parents[1]
H4 = it.INTERVAL_MS
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
REAL_VIEW = it.PROCESSED_DIR / f"{it.VIEW_NAME}.view.json"
FROZEN = pv.OUT_DIR / "protocol_v2.json"


def ms(dt):
    return int((dt - EPOCH).total_seconds() * 1000)


def line(open_ms, *, close_delta=H4 - 1, unit="us"):  # 2025 archives use microseconds
    k = 1000 if unit == "us" else 1
    return ",".join(str(x) for x in (open_ms * k, 100.0, 105.0, 95.0, 101.0, 10.0, (open_ms + close_delta) * k, 1010.0, 50, 5.0, 505.0, 0))


def make_zip(name, lines):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(name.replace(".zip", ".csv"), "\n".join(lines) + "\n")
    return buf.getvalue()


def month_lines(year, month, *, skip=(), short=None):
    t = datetime(year, month, 1, tzinfo=timezone.utc)
    end = datetime(year + (month == 12), month % 12 + 1, 1, tzinfo=timezone.utc)
    out = []
    while t < end:
        if ms(t) not in skip:
            out.append(line(ms(t), close_delta=(3 * 3600 * 1000 - 1) if short and ms(t) == short else H4 - 1))
        t += timedelta(hours=4)
    return out


def parent_build(tmp_path, monkeypatch, *, irregular=True):
    """Two synthetic months: November with a short bar (Nov 10 04:00) and a missing bar (Nov 20 08:00), December regular."""

    nov = datetime(2025, 11, 1, tzinfo=timezone.utc)
    skip = {ms(nov) + (19 * 6 + 2) * H4} if irregular else set()
    short = ms(nov) + (9 * 6 + 1) * H4 if irregular else None
    files = {}
    for (y, m, lines) in ((2025, 11, month_lines(2025, 11, skip=skip, short=short)), (2025, 12, month_lines(2025, 12))):
        name = f"BTCUSDT-4h-{y:04d}-{m:02d}.zip"
        content = make_zip(name, lines)
        files[f"{it.BASE}/BTCUSDT/4h/{name}"] = content
        files[f"{it.BASE}/BTCUSDT/4h/{name}.CHECKSUM"] = f"{hashlib.sha256(content).hexdigest()}  {name}\n".encode()
    monkeypatch.setattr(it.http, "get", lambda url, **k: SimpleNamespace(status=200 if url in files else 404, body=files.get(url, b""), error=None))
    root = tmp_path / "raw"
    for y, m in ((2025, 11), (2025, 12)):
        it.fetch_month(y, m, root=root)
    return it.build(root)


# ------------------------------------------------------------------ continuous segment + admission semantics
def _rows(spacings, durations=None):
    out, t = [], 0
    for i, gap in enumerate(spacings):
        t += gap
        d = (durations or {}).get(i, H4 - 1)
        out.append({"open_ms": t, "close_ms": t + d})
    return out


def test_trailing_segment_is_deterministic_and_stops_at_the_first_discontinuity_going_backward():
    regular = _rows([H4] * 30)
    assert pv.intraday.trailing_continuous_segment(regular) == {"start_index": 0, "end_index": 29}
    gap = _rows([H4] * 10 + [2 * H4] + [H4] * 19)          # a missing bar before index 10
    assert it.trailing_continuous_segment(gap)["start_index"] == 10
    short = _rows([H4] * 30, durations={12: 3 * 3600 * 1000})  # bar 12 is shortened: it is excluded, the next regular bar starts the segment
    assert it.trailing_continuous_segment(short)["start_index"] == 13
    dup = _rows([H4] * 10 + [0] + [H4] * 19)
    assert it.trailing_continuous_segment(dup)["start_index"] == 10
    overlap = _rows([H4] * 10 + [H4 // 2] + [H4] * 19)
    assert it.trailing_continuous_segment(overlap)["start_index"] == 10
    assert it.trailing_continuous_segment(short) == it.trailing_continuous_segment(copy.deepcopy(short))
    with pytest.raises(it.IntradayError):
        it.trailing_continuous_segment(_rows([H4] * 5, durations={4: 1000}))  # irregular last bar: no segment


def test_the_derived_view_excludes_irregular_bars_while_the_parent_stays_failed_and_unmodified(tmp_path, monkeypatch):
    built = parent_build(tmp_path, monkeypatch)
    assert built["report"]["status"] == "FAIL" and "I11" in built["report"]["failed_checks"] and "I09" in built["report"]["failed_checks"]
    view = it.derive_view(built)
    ident = view["identity"]
    assert ident["parent"]["admission_status"] == it.PARENT_STATUS_FAIL == "VALIDATION_FAIL_INCOMPLETE" and ident["view_status"] == it.VIEW_STATUS_ADMITTED
    assert ident["continuous_start"] == "2025-11-20T12:00:00" and ident["continuous_end"] == "2025-12-31T20:00:00"  # first regular bar after the LAST discontinuity
    assert ident["rows"] == ident["expected_rows_in_range"] and ident["validation"]["status"] == "PASS" and ident["validation"]["failed_checks"] == []
    assert ident["rows_modified"] == 0 and ident["rows_synthesized"] == 0 and ident["view_is_byte_identical_slice_of_parent"] is True
    ex = ident["excluded_irregular_intervals"]
    assert ex["all_irregular_intervals_precede_continuous_start"] is True and [e["first_missing"] for e in ex["missing_episodes"]][-1] == "2025-11-20T08:00"  # (the synthetic parent also lacks everything since 2018-01-01)
    assert [b["open"] for b in ex["irregular_or_shortened_bars"]] == ["2025-11-10T04:00"]
    parent_lines = built["norm"]["csv_bytes"].decode().splitlines()[1:]
    view_lines = view["csv"].splitlines()[1:]
    assert view_lines == parent_lines[len(parent_lines) - len(view_lines):]  # verbatim trailing slice: nothing modified, nothing filled
    rows = [r for r in built["norm"]["rows"] if r["open_dt"] >= datetime(2025, 11, 20, 12, tzinfo=timezone.utc)]
    assert all(b["open_ms"] - a["open_ms"] == H4 for a, b in zip(rows, rows[1:])) and all(it.is_regular_bar(r) for r in rows)  # no gap, no short bar
    again = it.derive_view(it.build(tmp_path / "raw"))
    assert it.identity_bytes(again["identity"]) == it.identity_bytes(ident)  # deterministic
    paths = it.persist_view(view, tmp_path / "proc")
    ds = load_dataset(str(paths["csv"]))
    assert ds.meta.timeframe == "4h" and len(ds.bars) == ident["rows"] and ds.bars[0].ts == datetime(2025, 11, 20, 12, tzinfo=timezone.utc)
    assert built["identity"]["validation_status"] == "FAIL"  # the parent identity itself was not touched by the view derivation


def test_a_clean_parent_gives_the_full_period_view_and_admission_never_upgrades_a_failed_parent():
    assert it.admission_status("PARENT", "FAIL") == "VALIDATION_FAIL_INCOMPLETE" and it.admission_status("PARENT", "WARN") == "VALIDATION_FAIL_INCOMPLETE"
    assert it.admission_status("PARENT", "PASS") == "ADMITTED_FULL_PERIOD"
    assert it.admission_status("CONTINUOUS_VIEW", "PASS", derived_by_rule=True) == "ADMITTED_CONTINUOUS_SUBPERIOD"
    assert it.admission_status("CONTINUOUS_VIEW", "PASS", derived_by_rule=False) == "NOT_ADMITTED"   # a hand-picked subperiod is not admitted
    assert it.admission_status("CONTINUOUS_VIEW", "FAIL", derived_by_rule=True) == "NOT_ADMITTED"
    with pytest.raises(ValueError):
        it.admission_status("OTHER", "PASS")


# ------------------------------------------------------------------ boundaries + guard
def test_development_end_holdout_year_and_guard_are_pinned():
    assert pv.DEVELOPMENT_END == "2025-12-31" and (pv.HOLDOUT_START, pv.HOLDOUT_END) == ("2026-01-01", "2026-12-31")
    assert it.DEV_LAST.isoformat() == pv.DEVELOPMENT_END and it.HOLDOUT_START.isoformat() == pv.HOLDOUT_START
    assert it.holdout_unlocked() is False  # in the real repository: no approval marker, so even after the freeze no 2026 value can be read
    with pytest.raises(it.HoldoutAccessError):
        it.assert_not_holdout(2026, 6)


# ------------------------------------------------------------------ cost selection + candidate rules
def _cfg(i, s0, s1, s2, s3):
    return {"params": {"i": i}, "scores": {"S0_commission_only": s0, "S1_low": s1, "S2_mid": s2, "S3_high": s3}}


def test_parameter_selection_ranks_by_the_worst_case_across_s1_s2_s3_and_ignores_s0():
    configs = [_cfg(0, 0.50, 0.30, 0.10, -0.05),   # best-case/S0 champion, poor worst case (-0.05)
               _cfg(1, 0.10, 0.08, 0.07, 0.06),    # robust: worst case 0.06
               _cfg(2, 0.20, 0.09, 0.06, 0.05)]    # worst case 0.05
    out = pv.select_parameters(configs)
    assert out["chosen_index"] == 1 and out["worst_case_train_score"] == pytest.approx(0.06)
    best_case = max(range(3), key=lambda i: max(configs[i]["scores"][s] for s in pv.SELECTION_SCENARIOS))
    assert best_case == 0 and out["chosen_index"] != best_case  # the discriminating case: best-case selection would pick another configuration
    assert pv.select_parameters([_cfg(0, 9.0, 0.01, 0.01, 0.01), _cfg(1, -9.0, 0.02, 0.02, 0.02)])["chosen_index"] == 1  # S0 is irrelevant
    tie = pv.select_parameters([_cfg(0, 0, 0.05, 0.05, 0.05), _cfg(1, 0, 0.05, 0.05, 0.05)])
    assert tie["chosen_index"] == 0  # ties keep grid order
    unranked = pv.select_parameters([_cfg(0, 0.9, None, 0.9, 0.9), _cfg(1, 0.0, 0.01, 0.01, 0.01)])
    assert unranked["chosen_index"] == 1 and unranked["ranking"][0]["worst_case_train_score"] is None
    with pytest.raises(ValueError):
        pv.select_parameters([_cfg(0, 0.1, None, 0.1, 0.1)])


def _summary(**over):
    base = {"folds": 4, "folds_ok": 4, "stitched_net_return": 0.20, "positive_folds": 3, "active_folds": 4, "total_oos_trades": 40, "passive_stitched_return": 0.10}
    base.update(over)
    return base


def test_candidate_status_is_the_least_favourable_scenario_and_s0_never_counts():
    good = {s: _summary() for s in pv.SELECTION_SCENARIOS}
    res = pv.classify_candidate(good)
    assert res["status"] == "PASS" and res["holdout_eligible"] is True
    with_s0 = {**good, "S0_commission_only": _summary(stitched_net_return=-0.5, total_oos_trades=0)}
    r0 = pv.classify_candidate(with_s0)
    assert r0["status"] == "PASS" and r0["diagnostic_only"]["S0_commission_only"]["used_for_decision"] is False  # a bad S0 changes nothing
    only_s0_good = {"S1_low": _summary(stitched_net_return=-0.01), "S2_mid": _summary(), "S3_high": _summary(), "S0_commission_only": _summary(stitched_net_return=0.9)}
    assert pv.classify_candidate(only_s0_good)["status"] == "FAIL" and pv.classify_candidate(only_s0_good)["holdout_eligible"] is False  # S0 cannot rescue it
    worst = {"S1_low": _summary(), "S2_mid": _summary(positive_folds=1), "S3_high": _summary(total_oos_trades=5)}
    assert pv.classify_candidate(worst)["status"] == "INSUFFICIENT_ACTIVITY" and pv.classify_candidate(worst)["per_scenario"]["S2_mid"] == "UNSTABLE"
    assert pv.classify_candidate({"S1_low": _summary(), "S2_mid": _summary()})["status"] == "UNKNOWN"  # a missing scenario is unknown, not favourable
    not_beating = {s: _summary(passive_stitched_return=0.5) for s in pv.SELECTION_SCENARIOS}
    assert pv.classify_candidate(not_beating)["status"] == "PASS" and pv.classify_candidate(not_beating)["holdout_eligible"] is False
    one_fold_failed = {s: _summary(folds_ok=3) for s in pv.SELECTION_SCENARIOS}
    assert pv.classify_candidate(one_fold_failed)["status"] == "UNKNOWN"
    assert pv.STATUS_ORDER == ("UNKNOWN", "INSUFFICIENT_ACTIVITY", "FAIL", "UNSTABLE", "PASS")


def test_thresholds_are_unchanged_internal_heuristics():
    t = pv.THRESHOLDS
    assert (t["min_total_oos_closed_trades"], t["min_active_fold_fraction"], t["positive_fold_fraction_must_exceed"], t["min_folds"]) == (30, 0.5, 0.5, 3)
    assert t["classification"] == "INTERNAL_PRE_REGISTERED_HEURISTIC" and (pv.TRAIN_BARS, pv.TEST_BARS, pv.STEP_BARS) == (4380, 1095, 1095)


def test_fold_plan_counts_bars_only_and_a_too_short_view_is_not_viable():
    plan = pv.fold_plan(9328)
    assert plan["folds"] == 4 and plan["oos_bars"] == 4380 and plan["oos_years"] == pytest.approx(2.0) and plan["viable"] and plan["oos_windows_non_overlapping"]
    first = plan["boundaries"][0]
    assert first["train"] == [0, 4380] and first["test"] == [4380, 5475] and plan["boundaries"][-1]["test"][1] <= 9328
    assert pv.fold_plan(4380 + 1095 + 1095)["viable"] is False and pv.fold_plan(100)["folds"] == 0  # 2 folds < min_folds(3); too short -> no fold


# ------------------------------------------------------------------ freeze + hash integrity
def _frozen_in(tmp_path, monkeypatch):
    built = parent_build(tmp_path, monkeypatch)
    view = it.derive_view(built)
    meta = [{"name": "BTCUSDT-4h-2026-01.zip", "size_bytes": 11897, "last_modified": "2026-02-02"}]
    out = tmp_path / "pv2"
    res = pv.freeze(view["identity"], view["csv"], meta, out_dir=out, now="2026-10-07T00:00:00+00:00")
    return out, res, view


def test_freeze_writes_canonical_components_a_hash_and_a_technical_only_marker(tmp_path, monkeypatch):
    out, res, view = _frozen_in(tmp_path, monkeypatch)
    names = {p.name for p in out.iterdir()}
    assert {"protocol_v2.json", "protocol_v2.sha256", "dataset_scope.json", "cost_contract.json", "holdout_boundary.json", "design_registry.json", "freeze.json"} <= names
    ok = pv.verify_frozen(out)
    assert ok["ok"] is True and ok["protocol_sha256"] == res["protocol_sha256"] == (out / "protocol_v2.sha256").read_text().split()[0]
    marker = json.loads((out / "freeze.json").read_text())
    assert marker["permission"] == "TECHNICAL ONLY" and marker["holdout_evaluation_approved"] is False
    assert pv.canonical({"b": 1, "a": 2}) == pv.canonical({"a": 2, "b": 1}) and pv.sha({"a": 1}) == pv.sha({"a": 1})
    with pytest.raises(RuntimeError, match="already frozen"):
        pv.freeze(view["identity"], view["csv"], [], out_dir=out)


def test_every_post_freeze_modification_is_detected(tmp_path, monkeypatch):
    out, _, _ = _frozen_in(tmp_path, monkeypatch)
    for name, key in (("dataset_scope.json", "dataset"), ("cost_contract.json", "venue"), ("holdout_boundary.json", "development_end"), ("design_registry.json", "protocol_candidate_id")):
        path = out / name
        original = path.read_text(encoding="utf-8")
        obj = json.loads(original)
        obj[key] = "tampered"
        path.write_text(json.dumps(obj, sort_keys=True), encoding="utf-8")
        result = pv.verify_frozen(out)
        assert result["ok"] is False and result["checks"]["component_files_match"] is False, name
        path.write_text(original, encoding="utf-8")
    assert pv.verify_frozen(out)["ok"] is True
    body = json.loads((out / "protocol_v2.json").read_text())
    body["thresholds"]["min_total_oos_closed_trades"] = 10  # lowering the threshold in the frozen file
    (out / "protocol_v2.json").write_text(json.dumps(body), encoding="utf-8")
    assert pv.verify_frozen(out)["checks"]["body_hash_matches_recorded"] is False
    monkeypatch.setitem(pv.THRESHOLDS, "min_total_oos_closed_trades", 10)  # ... or in the code constants
    fresh, _, _ = _frozen_in(tmp_path / "again", monkeypatch)
    assert pv.verify_frozen(fresh)["ok"] is True  # frozen WITH the lowered constant: internally consistent
    monkeypatch.setitem(pv.THRESHOLDS, "min_total_oos_closed_trades", 30)
    assert pv.verify_frozen(fresh)["checks"]["code_matches_frozen_spec"] is False  # code now differs from what was frozen


def test_frozen_contracts_state_costs_semantics_and_holdout_without_values(tmp_path, monkeypatch):
    out, _, view = _frozen_in(tmp_path, monkeypatch)
    cost = json.loads((out / "cost_contract.json").read_text())
    assert cost["current_verified_fee"]["status"] == "VERIFIED_CURRENT" and cost["historical_fee_2018_2025"]["status"] == "HISTORICAL_UNKNOWN"
    assert cost["historical_fee_2018_2025"]["research_use"] == "ASSUMED_FOR_RESEARCH" and cost["bid_ask_spread_historical"] == "HISTORICAL_UNKNOWN"
    assert "taker" in cost["commission_execution_assumption"] and "no maker fill" in cost["commission_execution_assumption"]
    assert {k: (v["half_spread_bps"], v["slippage_bps"]) for k, v in cost["scenarios"].items()} == {
        "S0_commission_only": (0.0, 0.0), "S1_low": (1.0, 1.0), "S2_mid": (2.5, 2.5), "S3_high": (5.0, 5.0)}
    assert all(v["label"] == "SCENARIO ASSUMPTION" and v["status"] == "SCENARIO" for v in cost["scenarios"].values())
    assert "S0_commission_only" not in cost["selection_scenarios"] and "ONLY" in cost["s0_restriction"]
    hold = json.loads((out / "holdout_boundary.json").read_text())
    assert hold["temporal_holdout"]["start"] == "2026-01-01" and hold["temporal_holdout"]["end"] == "2026-12-31" and hold["values_accessed"] == "NO"
    assert "technical permission only" in hold["freeze_marker_meaning"] and "NOT used as the v2 holdout" in hold["existing_daily_lockbox"]
    scope = json.loads((out / "dataset_scope.json").read_text())
    assert scope["parent"]["admission_status"] == "VALIDATION_FAIL_INCOMPLETE" and scope["research_view"]["status"] == "ADMITTED_CONTINUOUS_SUBPERIOD"
    assert scope["research_view"]["interpolation_or_fill"] == "NONE" and scope["research_view"]["rows_synthesized"] == 0
    assert scope["power_plumbing"]["trade_count_scaling_assumption_6x"].startswith("UNKNOWN")
    reg = json.loads((out / "design_registry.json").read_text())
    assert reg["gap_handling_decision"]["classification"] == "POST-DATA-INTEGRITY OBSERVATION / PRE-PERFORMANCE"
    assert "EXCLUDED by a deterministic continuous-subperiod rule" in reg["gap_handling_decision"]["policy"]
    assert reg["performance_data_accessed"]["btcusdt_2026_values"] == "NO" and reg["performance_data_accessed"]["btcusdt_4h_strategy_results"] == "NO"
    banned = {"net_return", "stitched_net_return", "sharpe", "max_drawdown_pct", "trades_closed", "returns", "equity", "oos_metrics"}

    def keys(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                yield k
                yield from keys(v)
        elif isinstance(obj, list):
            for v in obj:
                yield from keys(v)

    found = {k for n in pv.COMPONENTS + ("protocol_v2.json",) for k in keys(json.loads((out / n).read_text()))}
    assert not (found & banned)  # no performance field anywhere in the freeze


def test_parameter_semantics_keep_the_existing_grids_as_bar_counts():
    sem = pv.parameter_semantics()
    assert sem["decision"].startswith("Option A") and "NEW INTRADAY HORIZON" in sem["decision"]
    assert sem["grids"] == {n: STRATEGIES[n].param_grid for n in pv.STRATEGY_NAMES} and sem["grids"]["ma_trend"] == {"fast": [5, 10, 20], "slow": [30, 50]}
    assert "Option B was not tried" in sem["rescaling_to_daily_calendar_horizons"]


def test_trial_series_records_carry_the_cost_scenario_and_stay_complete(tmp_path):
    ds = load_dataset(str(ROOT / "data" / "fixtures" / "SYN_KR1_1d.csv"))
    equity = [{"timestamp": "t0", "equity": 1.0}, {"timestamp": "t1", "equity": 1.01}]
    sink = {0: [{"params": {"fast": 5}, "window": [0, 2], "equity": equity}]}
    tid = trials.trial_id(data_version=ds.data_version, strategy="ma_trend", strategy_version=STRATEGIES["ma_trend"].version, params={"fast": 5}, window=[0, 2],
                          stage="train_selection", selection_metric=trials.SELECTION_METRIC, base_config={}, settings_sha=None)
    store = ts.TrialSeriesStore(tmp_path / "s")
    ts.store_run(store, dataset=ds, strategy_name="ma_trend", base_config={}, settings_sha=None, fold_sinks=sink, initial_cash=1.0, protocol_version="wf-protocol-2",
                 trial_rows=[{"trial_id": tid}], cost_scenario="S2_mid")
    rec = store.get(tid)
    assert rec["cost_scenario"] == "S2_mid" and rec["protocol_version"] == "wf-protocol-2" and rec["benchmark_returns"][0] == 0.0
    with pytest.raises(ts.TrialSeriesIncomplete):
        store.verify_complete([tid, "f" * 64])


def test_lockbox_and_holdout_values_are_never_touched_by_the_protocol_module():
    assert not hasattr(pv, "evaluate_lockbox") and "evaluate_lockbox" not in dir(pv.intraday)
    assert not (ROOT / "results" / "lockbox_registry.json").exists()
    assert not [p for p in it.RAW_4H_ROOT.rglob("*") if "-2026-" in p.name] if it.RAW_4H_ROOT.exists() else True


# ------------------------------------------------------------------ real repository (timestamps / metadata only)
@pytest.mark.skipif(not REAL_VIEW.exists(), reason="persisted continuous view not present")
class TestRealView:
    def test_view_is_the_deterministic_timestamp_only_derivation_of_the_failed_parent(self):
        ident = json.loads(REAL_VIEW.read_text(encoding="utf-8"))
        assert ident["parent"]["strict_validation_status"] == "FAIL" and ident["parent"]["admission_status"] == "VALIDATION_FAIL_INCOMPLETE"
        assert ident["view_status"] == "ADMITTED_CONTINUOUS_SUBPERIOD" and ident["validation"]["status"] == "PASS" and ident["derivation"]["performance_data_used"] is False
        assert ident["continuous_end"] == "2025-12-31T20:00:00" and ident["rows"] == ident["expected_rows_in_range"] and ident["rows_modified"] == 0
        assert ident["excluded_irregular_intervals"]["all_irregular_intervals_precede_continuous_start"] is True
        built = it.build()
        assert built["identity"]["validation_status"] == "FAIL" and built["identity"]["normalized_sha256"] == ident["parent"]["normalized_sha256"]  # parent unchanged
        assert it.identity_bytes(it.derive_view(built)["identity"]) == REAL_VIEW.read_bytes()
        assert pv.fold_plan(ident["rows"])["viable"] is True

    def test_frozen_protocol_verifies_and_the_view_matches_the_frozen_scope(self):
        if not FROZEN.exists():
            pytest.skip("Protocol v2 not frozen yet")
        assert pv.verify_frozen()["ok"] is True
        scope = json.loads((pv.OUT_DIR / "dataset_scope.json").read_text(encoding="utf-8"))
        ident = json.loads(REAL_VIEW.read_text(encoding="utf-8"))
        assert scope["research_view"]["view_normalized_sha256"] == ident["view_normalized_sha256"] and scope["research_view"]["continuous_start"] == ident["continuous_start"]
        assert scope["fold_plan"]["folds"] == pv.fold_plan(ident["rows"])["folds"] >= pv.MIN_FOLDS
        assert (pv.OUT_DIR / "freeze.json").exists() and not it.APPROVAL_MARKER.exists() and it.holdout_unlocked() is False
