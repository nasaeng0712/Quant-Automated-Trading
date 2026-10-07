"""Batch #3D - BTCUSDT 4h foundation: parsing/validation/identity/holdout guard (synthetic archives built in tmp),
cost v2 contract, per-trial series, aligned benchmark, Lockbox non-access. Real-data checks skip when the persisted
4h dataset is absent. No strategy is evaluated on BTC data here."""

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
from qat.realdata import microstructure
from qat.realdata.provenance import ProvenanceError, verify_artifact
from qat.realdata.sources import ParseError
from qat.research import cost_evidence as ce
from qat.research import intraday_evidence as ev
from qat.research import trial_series as ts
from qat.research.backtest import BacktestConfig
from qat.research.walkforward import WalkForwardConfig, run_walkforward

ROOT = pathlib.Path(__file__).resolve().parents[1]
H4 = it.INTERVAL_MS
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
REAL_4H = it.PROCESSED_DIR / f"{it.NAME}.csv"


def ms(dt):
    return int((dt - EPOCH).total_seconds() * 1000)


def kline_line(open_ms, *, unit="ms", o=100.0, h=105.0, lo=95.0, c=101.0, v=10.0, close_delta=H4 - 1, trades=50):
    scale = 1000 if unit == "us" else 1
    return ",".join(str(x) for x in (open_ms * scale, o, h, lo, c, v, (open_ms + close_delta) * scale, v * c, trades, v / 2, v * c / 2, 0))


def make_zip(name, lines):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(name.replace(".zip", ".csv"), "\n".join(lines) + "\n")
    return buf.getvalue()


def month_lines(year, month, *, unit="ms", skip=(), **over):
    start = datetime(year, month, 1, tzinfo=timezone.utc)
    end = datetime(year + (month == 12), month % 12 + 1, 1, tzinfo=timezone.utc)
    out, t = [], start
    while t < end:
        if ms(t) not in skip:
            out.append(kline_line(ms(t), unit=unit, **over))
        t += timedelta(hours=4)
    return out


def sidecar(name, content, **over):
    base = {"symbol": "BTCUSDT", "provider": "binance-vision", "synthetic": False, "official_checksum": hashlib.sha256(content).hexdigest(),
            "official_checksum_match": True, "interval": "4h"}
    base.update(over)
    return {"name": name, "sha256": hashlib.sha256(content).hexdigest(), "size_bytes": len(content), "sidecar": base}


def validated(lines, name="BTCUSDT-4h-2024-12.zip", first=None, last=None, **side):
    content = make_zip(name, lines)
    rows = it.parse_klines(content, source_file=name)
    return it.validate(rows, [sidecar(name, content, **side)], first_expected=first, last_expected=last), rows


def status(report, cid):
    return next(c for c in report["checks"] if c["id"] == cid)["status"]


DEC = datetime(2024, 12, 1, tzinfo=timezone.utc)
DEC_LAST = datetime(2024, 12, 31, 20, tzinfo=timezone.utc)


# ------------------------------------------------------------------ timestamp semantics
def test_ms_and_us_timestamps_convert_to_the_same_utc_open_time_and_a_bad_unit_is_rejected():
    t = ms(datetime(2025, 1, 1, 4, tzinfo=timezone.utc))
    a = it.parse_klines(make_zip("BTCUSDT-4h-2024-12.zip", [kline_line(t, unit="ms")]), source_file="x")[0]
    b = it.parse_klines(make_zip("BTCUSDT-4h-2025-01.zip", [kline_line(t, unit="us")]), source_file="y")[0]
    assert (a["unit"], b["unit"]) == ("ms", "us") and a["open_dt"] == b["open_dt"] == datetime(2025, 1, 1, 4, tzinfo=timezone.utc)
    assert a["close_ms"] == b["close_ms"] == t + H4 - 1
    with pytest.raises(ParseError, match="digits"):
        it.parse_klines(make_zip("BTCUSDT-4h-2025-01.zip", ["12345678901234,1,2,3,4,5,6,7,8,9,10,0"]), source_file="z")  # 14 digits
    with pytest.raises(ParseError):
        it.parse_klines(make_zip("BTCUSDT-4h-2025-01.zip", ["abc,1,2,3,4,5,6,7,8,9,10,0"]), source_file="z")


def test_unit_contradicting_the_expected_one_is_a_warning_not_a_silent_conversion():
    report, rows = validated(month_lines(2024, 12, unit="us"), first=DEC, last=DEC_LAST)  # us in 2024: unexpected
    assert status(report, "I10") == "WARN" and report["status"] == "WARN" and {r["unit"] for r in rows} == {"us"}
    assert status(report, "I11") == "PASS"  # still correctly converted, so continuity holds


# ------------------------------------------------------------------ validation (never repairs)
def test_a_clean_month_passes_every_check():
    report, rows = validated(month_lines(2024, 12), first=DEC, last=DEC_LAST)
    assert report["status"] == "PASS" and report["rows"] == len(rows) == 31 * 6 and not report["failed_checks"]


@pytest.mark.parametrize("case,cid", [("missing", "I11"), ("duplicate", "I05"), ("unordered", "I06"), ("overlap", "I07"), ("misaligned", "I08"), ("interval", "I09"),
                                      ("ohlc", "I03"), ("nan", "I01"), ("negative_volume", "I04"), ("zero_price", "I02")])
def test_each_defect_is_flagged_and_nothing_is_filled(case, cid):
    lines = month_lines(2024, 12)
    first_ms = ms(DEC)
    if case == "missing":
        lines = month_lines(2024, 12, skip={first_ms + 5 * H4})
    elif case == "duplicate":
        lines = lines[:5] + [lines[4]] + lines[5:]
    elif case == "unordered":
        lines[3], lines[4] = lines[4], lines[3]
    elif case == "overlap":
        lines[5] = kline_line(first_ms + 5 * H4 - H4 // 2)
    elif case == "misaligned":
        lines[5] = kline_line(first_ms + 5 * H4 + 60_000)
    elif case == "interval":
        lines[5] = kline_line(first_ms + 5 * H4, close_delta=2 * H4)
    elif case == "ohlc":
        lines[5] = kline_line(first_ms + 5 * H4, h=90.0)
    elif case == "nan":
        lines[5] = kline_line(first_ms + 5 * H4).replace("101.0", "nan", 1)
    elif case == "negative_volume":
        lines[5] = kline_line(first_ms + 5 * H4, v=-1.0)
    elif case == "zero_price":
        lines[5] = kline_line(first_ms + 5 * H4, o=0.0, lo=0.0)
    report, rows = validated(lines, first=DEC, last=DEC_LAST)
    assert status(report, cid) == "FAIL" and report["status"] == "FAIL"
    assert len(rows) == len(lines)  # no row added, removed or repaired
    if case == "missing":
        assert next(c for c in report["checks"] if c["id"] == "I11")["missing_bars"] == ["2024-12-01T20:00"]


def test_symbol_source_synthetic_checksum_and_holdout_contradictions_fail():
    lines = month_lines(2024, 12)
    assert status(validated(lines, name="ETHUSDT-4h-2024-12.zip", first=DEC, last=DEC_LAST)[0], "I13") == "FAIL"
    assert status(validated(lines, first=DEC, last=DEC_LAST, provider="someone-else")[0], "I14") == "FAIL"
    assert status(validated(lines, first=DEC, last=DEC_LAST, synthetic=True)[0], "I15") == "FAIL"
    assert status(validated(lines, first=DEC, last=DEC_LAST, official_checksum="0" * 64)[0], "I16") == "FAIL"
    late = [kline_line(ms(datetime(2026, 1, 1, tzinfo=timezone.utc)))]
    assert status(validated(late, name="BTCUSDT-4h-2026-01.zip")[0], "I17") == "FAIL"


# ------------------------------------------------------------------ raw preservation, checksum, identity
def _install(tmp_path, monkeypatch, months, *, tamper_checksum=False):
    """Fake the archive over http and fetch through the real code path into a tmp raw root."""

    files = {}
    for (y, m, unit) in months:
        name = f"BTCUSDT-4h-{y:04d}-{m:02d}.zip"
        content = make_zip(name, month_lines(y, m, unit=unit))
        digest = hashlib.sha256(content).hexdigest()
        files[f"{it.BASE}/BTCUSDT/4h/{name}"] = content
        files[f"{it.BASE}/BTCUSDT/4h/{name}.CHECKSUM"] = f"{('0' * 64) if tamper_checksum else digest}  {name}\n".encode()
    monkeypatch.setattr(it.http, "get", lambda url, **k: SimpleNamespace(status=200 if url in files else 404, body=files.get(url, b""), error=None))
    root = tmp_path / "raw"
    for y, m, _ in months:
        it.fetch_month(y, m, root=root)
    return root


def test_fetch_requires_the_official_checksum_and_preserves_raw_immutably(tmp_path, monkeypatch):
    with pytest.raises(it.IntradayError, match="CHECKSUM"):
        _install(tmp_path / "bad", monkeypatch, [(2024, 12, "ms")], tamper_checksum=True)
    root = _install(tmp_path, monkeypatch, [(2024, 12, "ms"), (2025, 1, "us")])
    arts = it.raw_artifacts(root)
    assert [a.name for a in arts] == ["BTCUSDT-4h-2024-12.zip", "BTCUSDT-4h-2025-01.zip"]
    assert it.verify_checksums(root) == {"archives": 2, "mismatches": []}
    zip_path = arts[0]
    original = zip_path.read_bytes()
    zip_path.write_bytes(original + b"x")  # raw tamper: sidecar SHA-256 no longer matches
    with pytest.raises(ProvenanceError):
        verify_artifact(zip_path)
    zip_path.write_bytes(original)
    chk = it.checksum_artifact(zip_path)
    good = chk.read_bytes()
    chk.write_bytes(b"0" * 64 + b"  " + zip_path.name.encode() + b"\n")  # preserved CHECKSUM file altered
    assert it.verify_checksums(root)["mismatches"] == [zip_path.name]
    chk.write_bytes(good)
    assert it.verify_checksums(root)["mismatches"] == []


def test_identity_is_deterministic_and_the_canonical_csv_loads_as_a_4h_engine_dataset(tmp_path, monkeypatch):
    root = _install(tmp_path, monkeypatch, [(2024, 12, "ms"), (2025, 1, "us")])
    a, b = it.build(root), it.build(root)
    assert a["norm"]["csv_bytes"] == b["norm"]["csv_bytes"] and it.identity_bytes(a["identity"]) == it.identity_bytes(b["identity"])
    ident = a["identity"]
    assert ident["data_version"].startswith("CRYPTO-BTCUSDT-4h-binance-vision-") and ident["performance_data_accessed"] is False
    assert ident["interval"] == "4h" and ident["calendar"].startswith("24x7") and ident["rows"] == 62 * 6
    paths = it.persist(a, tmp_path / "proc")
    ds = load_dataset(str(paths["csv"]))
    assert ds.meta.timeframe == "4h" and ds.meta.timezone == "UTC" and len(ds.bars) == 62 * 6
    assert ds.bars[1].ts - ds.bars[0].ts == timedelta(hours=4)
    extra = paths["extra"].read_text(encoding="utf-8").splitlines()
    assert extra[0] == it.EXTRA_HEADER and extra[1].split(",")[4] == "ms" and extra[-1].split(",")[4] == "us"  # unit preserved per row
    assert "real_data" not in json.loads((tmp_path / "proc" / f"{it.NAME}.csv.meta.json").read_text(encoding="utf-8"))["extra"]  # not admitted for research


# ------------------------------------------------------------------ 2026 holdout guard
def test_holdout_is_guarded_until_a_freeze_marker_exists_and_no_network_call_is_made(tmp_path, monkeypatch):
    it.assert_not_holdout(2025, 12)  # development: fine
    for ym in ((2026, 1), (2026, 9), (2027, 1)):
        with pytest.raises(it.HoldoutAccessError):
            it.assert_not_holdout(*ym)
    monkeypatch.setattr(it.http, "get", lambda *a, **k: (_ for _ in ()).throw(AssertionError("network used for a holdout month")))
    with pytest.raises(it.HoldoutAccessError):
        it.fetch_month(2026, 1, root=tmp_path)
    with pytest.raises(it.HoldoutAccessError):
        microstructure.fetch_day("aggTrades", datetime(2026, 2, 1).date(), root=tmp_path)
    marker = tmp_path / "freeze.json"
    marker.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(it, "CLOSURE_MARKER", tmp_path / "no_closure.json")  # isolate the two-marker rule (the real Protocol v2 closure would lock it anyway)
    monkeypatch.setattr(it, "FREEZE_MARKER", marker)
    with pytest.raises(it.HoldoutAccessError):  # the freeze marker alone is only a technical permission
        it.assert_not_holdout(2026, 1)
    approval = tmp_path / "approval.json"
    approval.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(it, "APPROVAL_MARKER", approval)
    it.assert_not_holdout(2026, 1)  # only with the freeze AND a separate evaluation approval


@pytest.mark.skipif(not REAL_4H.exists(), reason="persisted 4h dataset not present")
def test_real_4h_artifacts_contain_no_2026_value_and_the_identity_is_reproducible():
    arts = it.raw_artifacts()
    assert len(arts) == 96 and max(a.name for a in arts) == "BTCUSDT-4h-2025-12.zip"
    assert not [p for p in it.RAW_4H_ROOT.rglob("*") if "-2026-" in p.name]
    assert it.verify_checksums()["mismatches"] == []
    ident = json.loads((it.PROCESSED_DIR / f"{it.NAME}.identity.json").read_text(encoding="utf-8"))
    assert ident["rows"] == 17516 and ident["last_open"].startswith("2025-12-31T20:00") and ident["performance_data_accessed"] is False
    assert it.identity_bytes(it.build()["identity"]) == (it.PROCESSED_DIR / f"{it.NAME}.identity.json").read_bytes()
    report = it.build()["report"]
    assert report["status"] == "FAIL" and report["failed_checks"] == ["I09", "I11"]  # the recorded, enumerated anomalies are not hidden


# ------------------------------------------------------------------ cost v2 contract
def test_current_fee_is_verified_current_and_historical_values_are_never_verified():
    ce.validate_v2()
    c = ce.V2_CONTRACT
    assert c["commission_taker_current"]["status"] == "VERIFIED_CURRENT" and c["commission_taker_current"]["value"] == 0.001
    assert any(s["kind"] == "primary" for s in c["commission_taker_current"]["sources"])
    for key in ("commission_historical_2018_2025", "bid_ask_spread_historical", "slippage_market_impact_historical"):
        assert c[key]["status"] == "HISTORICAL_UNKNOWN" and c[key]["value"] is None
    assert c["commission_historical_2018_2025"]["research_assumption"].startswith("ASSUMED_FOR_RESEARCH")
    bad = copy.deepcopy(c)
    bad["commission_historical_2018_2025"]["status"] = "VERIFIED_CURRENT"
    with pytest.raises(ce.CostEvidenceError, match="needs scope 'current'|historical"):
        ce.validate_v2(bad)
    bad = copy.deepcopy(c)
    bad["commission_historical_2018_2025"]["value"] = 0.001
    with pytest.raises(ce.CostEvidenceError, match="must not carry a value"):
        ce.validate_v2(bad)
    bad = copy.deepcopy(c)
    bad["commission_taker_current"]["sources"] = [{"kind": "secondary"}]
    with pytest.raises(ce.CostEvidenceError, match="primary source"):
        ce.validate_v2(bad)
    bad = copy.deepcopy(c)
    bad["bid_ask_spread_historical"]["scope"] = "historical"
    bad["bid_ask_spread_historical"]["status"] = "PLACEHOLDER"
    with pytest.raises(ce.CostEvidenceError, match="historical value cannot be"):
        ce.validate_v2(bad)


def test_scenarios_are_labelled_assumptions_and_keep_commission_separate():
    models = ce.scenario_cost_models()
    assert set(models) == {"S0_commission_only", "S1_low", "S2_mid", "S3_high"}
    for m in models.values():
        assert m["commission_rate"] == 0.001 and m["status"]["commission_rate"] == "VERIFIED_CURRENT"
        assert m["status"]["half_spread_bps"] == "SCENARIO" and m["status"]["slippage_bps"] == "SCENARIO"
        assert "SCENARIO ASSUMPTION" in m["label"] and "NOT actual-cost" in m["result_label"]
    block = ce.v2_report()["manifest_block"]
    assert block["current_verified_fee"]["status"] == "VERIFIED_CURRENT" and block["historical_fee_assumption"]["status"] == "HISTORICAL_UNKNOWN"
    ce.validate(ce.EVIDENCE, ce.configured_costs())  # the frozen Batch #3C classification is untouched and still valid


# ------------------------------------------------------------------ per-trial series + aligned benchmark
class _Bar:
    def __init__(self, o, c):
        self.open, self.close = o, c


def test_aligned_benchmark_is_exposed_from_the_earliest_executable_bar():
    bars = [_Bar(10, 10), _Bar(10, 11), _Bar(12, 12), _Bar(12, 15)]
    series = ts.aligned_benchmark_returns(bars, [1, 4])
    assert series[0] == 0.0 and series[1] == pytest.approx(12 / 12 - 1) and len(series) == 3
    total = 1.0
    for r in series:
        total *= 1 + r
    assert total - 1 == pytest.approx(ts.aligned_benchmark_return(bars, [1, 4])) == pytest.approx(15 / 12 - 1)  # entry open(start+1), not open(start)
    assert ts.aligned_benchmark_return(bars, [1, 4]) != pytest.approx(bars[3].close / bars[1].open - 1)  # differs from the v1 definition
    with pytest.raises(ValueError):
        ts.aligned_benchmark_returns(bars, [1, 2])


def test_series_store_is_deterministic_idempotent_and_conflicts_are_loud(tmp_path):
    store = ts.TrialSeriesStore(tmp_path / "s")
    rec = {"trial_id": "a" * 64, "returns": [0.1, None], "stage": "train_selection"}
    assert store.put(rec) == "stored" and store.put(copy.deepcopy(rec)) == "unchanged" and store.get("a" * 64) == rec
    with pytest.raises(ts.TrialSeriesConflict):
        store.put({**rec, "returns": [0.2, None]})
    assert store.path("a" * 64).read_bytes() == ts._canonical(rec)
    assert ts.returns_from_equity([{"equity": 110.0}, {"equity": None}, {"equity": 121.0}], 100.0) == [pytest.approx(0.1), None, None]


def test_every_trial_has_a_series_and_winner_only_storage_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("QAT_RESULTS_DIR", str(tmp_path / "results"))
    ds = load_dataset(str(ROOT / "data" / "fixtures" / "SYN_KR1_1d.csv"))
    cfg = WalkForwardConfig(train_bars=250, test_bars=60, base=BacktestConfig(settings_path=str(ROOT / "config" / "settings.yaml")))
    store = ts.TrialSeriesStore(tmp_path / "series")
    out = run_walkforward(ds, "ma_trend", cfg, series_store=store)
    ids = out["manifest"]["trial_context"]["trial_ids"]
    assert ids and not store.missing(ids)
    recs = [store.get(t) for t in ids]
    assert {r["stage"] for r in recs} == {"train_selection", "oos_evaluation"} and sum(r["stage"] == "oos_evaluation" for r in recs) < len(recs)
    assert all(len(r["returns"]) == len(r["benchmark_returns"]) == len(r["timestamps"]) == r["window"][1] - r["window"][0] for r in recs)
    assert all({"trial_id", "stage", "fold", "params", "window", "dataset", "returns", "benchmark_returns"} <= set(r) for r in recs)
    assert recs[0]["dataset"]["data_version"] == ds.data_version
    again = run_walkforward(ds, "ma_trend", cfg, series_store=store, save=False)  # identical re-run: same ids, nothing conflicts
    assert again["manifest"]["trial_context"]["trial_ids"] == ids
    # winner-only storage: only the OOS evaluations are provided for a run that evaluated many candidates
    winners = {f: [i for i in sink if i.get("stage") == "oos_evaluation"] for f, sink in {0: [{"params": {"fast": 5}, "window": [0, 2], "equity": [{"timestamp": "t0", "equity": 1.0}, {"timestamp": "t1", "equity": 1.0}], "stage": "oos_evaluation"}]}.items()}
    trial_rows = [{"trial_id": "x" * 64}, {"trial_id": "y" * 64}]
    with pytest.raises(ts.TrialSeriesIncomplete):
        ts.store_run(ts.TrialSeriesStore(tmp_path / "w"), dataset=ds, strategy_name="ma_trend", base_config={}, settings_sha=None, fold_sinks=winners,
                     initial_cash=1.0, protocol_version=None, trial_rows=trial_rows)
    plain = run_walkforward(ds, "ma_trend", cfg, save=False)  # without a store nothing is written and the result keeps its shape
    assert "trial_ids" in plain["manifest"]["trial_context"]


# ------------------------------------------------------------------ plumbing analysis + Lockbox non-access
def test_power_analysis_is_counts_only_and_uses_the_calendar_design():
    start = datetime(2018, 1, 1, tzinfo=timezone.utc)
    rows = [{"open_dt": start + timedelta(hours=4 * i)} for i in range(2922 * 6)]
    rates = {"CRYPTO/ma_trend": {"trades_per_oos_year": 4.88}, "CRYPTO/mean_reversion": {"trades_per_oos_year": 1.55}, "KR/ma_trend": {"trades_per_oos_year": 2.8}}
    out = ev.power_analysis({"norm": {"rows": rows}}, rates)
    a = out["candidate_windows"]["A_development_to_2024-12-31"]
    assert a["bars"] == 2557 * 6 and a["folds"] >= 8 and set(a["per_candidate_expected_trades"]) == {"CRYPTO/ma_trend", "CRYPTO/mean_reversion"}
    assert a["meets_30_if_calendar_equivalent_grids"] is False and out["verdict"]["structurally_viable"] is True
    dumped = json.dumps(out)
    assert "stitched" not in dumped and "net_return" not in dumped and "drawdown" not in dumped  # counts only, no performance fields
    assert ev.TRAIN_BARS == 4380 and ev.TEST_BARS == 1095


def test_decision_requires_every_gate_and_the_lockbox_is_never_touched(monkeypatch, tmp_path):
    ok_val = {"strict_status_policy_P0": "PASS", "integrity_checks_status": "PASS", "completeness": {"missing_bars": 0, "missing_episodes": [], "wrong_length_bars": 0}}
    det = {"two_independent_builds_identical": True}
    power = {"verdict": {"structurally_viable": True, "conditional_on": "x"}}
    series = {"all_trials_have_series": True, "winner_only_store_rejected": True}
    hold = {"values_accessed": "NO", "holdout_unlocked": False}
    assert ev.final_decision(ok_val, det, power, series, {}, hold)["decision"] == "PROTOCOL_V2_FREEZE_READY"
    bad_val = copy.deepcopy(ok_val)
    bad_val["strict_status_policy_P0"] = "FAIL"
    bad_val["completeness"] = {"missing_bars": 16, "missing_episodes": [1], "wrong_length_bars": 18}
    assert ev.final_decision(bad_val, det, power, series, {}, hold)["decision"] == "RESEARCH_STOP_RECOMMENDED"
    assert ev.final_decision(ok_val, det, power, {**series, "winner_only_store_rejected": False}, {}, hold)["decision"] == "RESEARCH_STOP_RECOMMENDED"
    assert ev.final_decision(ok_val, det, power, series, {}, {**hold, "values_accessed": "YES"})["decision"] == "RESEARCH_STOP_RECOMMENDED"
    monkeypatch.setenv("QAT_RESULTS_DIR", str(tmp_path / "r"))
    lock = ev.lockbox_report()
    assert lock["lockbox_registry_exists"] is False and lock["evaluate_lockbox_imported_by_this_module"] is False
    assert "evaluate_lockbox" not in dir(it) and "evaluate_lockbox" not in dir(microstructure)
