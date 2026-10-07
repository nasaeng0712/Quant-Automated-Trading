"""Batch #3D - evidence builder for the BTCUSDT 4h foundation (no strategy research, no 2026 values, no Lockbox).

    python -m qat.research.intraday_evidence

Reads only: preserved raw archives (<= 2025-12), the persisted 4h dataset, saved Batch #3B/#3C evidence (trade COUNTS, no
returns are printed), the 2023-06-11 trade samples, and archive FILE METADATA (names/sizes/dates) for 2026.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import pathlib
import tempfile
from collections import Counter

from qat.realdata import intraday, microstructure
from qat.research import cost_evidence, trial_series, validity
from qat.research.backtest import BacktestConfig
from qat.research.manifest import PROJECT_ROOT
from qat.research.store import results_root
from qat.research.walkforward import WalkForwardConfig

OUT_DIR = PROJECT_ROOT / "artifacts" / "verification" / "intraday_3d"
BARS_PER_DAY = 6
TRAIN_BARS, TEST_BARS = 2 * 365 * BARS_PER_DAY, 365 * BARS_PER_DAY // 2  # 2y train / 0.5y OOS in 4h bars (same calendar design as v1)
CANDIDATE_ID = "wf-protocol-2-candidate-1"

SOURCE_CONTRACT = {
    "source": "Binance public data archive (data.binance.vision), spot/monthly/klines/BTCUSDT/4h, plus the official .CHECKSUM beside each archive",
    "development_window": [intraday.DEV_FIRST.isoformat(), intraday.DEV_LAST.isoformat()], "holdout_start": intraday.HOLDOUT_START.isoformat(),
    "raw": "data/raw/_intraday_4h/ (immutable, sidecar with SHA-256, official checksum match required at fetch and re-checked independently)",
    "canonical_columns": intraday.HEADER, "extra_columns": intraday.EXTRA_HEADER, "interval": "4h = 14,400,000 ms; close_time = open + 4h - 1 ms (+-1 ms conversion tolerance)",
    "timestamp_units": "epoch ms (13 digits) before 2025-01-01, epoch us (16 digits) from 2025-01-01; detected per row and converted explicitly; recorded per row",
    "calendar": "none (24/7); continuity of every expected 4h open is checked", "adjustment": "UNADJUSTED (spot pair)",
    "policy": "missing bars are never filled; a missing bar or a wrong-length bar is a FAIL (policy P0, written before the first 4h acquisition)",
    "admission": "the 4h dataset is stored under data/processed/real_4h/, has no real_data identity pointer and is therefore NOT admitted for research (fail-closed) until Protocol v2 is frozen"}

DESIGN_CHOICES = [
    ("interval 4h, BTCUSDT, Binance Spot only", "user instruction; 1h not used as a main interval; KR/US daily not tuned further"),
    ("fee baseline: Regular User, no BNB discount, current maker/taker 0.100% (VERIFIED_CURRENT)", "user instruction; historical fee kept HISTORICAL_UNKNOWN"),
    ("spread/slippage: scenarios S0..S3 = 0/0, 1/1, 2.5/2.5, 5/5 bps (SCENARIO ASSUMPTION)", "round numbers fixed before any result; not tuned to performance"),
    ("30-trade / 50% thresholds kept unchanged", "not lowered after Batch #3B; judged only by a structural count analysis"),
    ("missing 4h bar = FAIL (policy P0) recorded BEFORE the first acquisition", "completeness is a precondition of a verified foundation"),
    ("benchmark v2 enters at open(start+1) (earliest executable bar)", "fixes the one-bar misalignment found in Batch #3C; v1 results untouched"),
    ("per-trial series stored for EVERY evaluated configuration (winner-only forbidden)", "needed by any data-snooping method; White RC is only a CANDIDATE"),
    ("2026 data not fetched/parsed/read; archive metadata only", "2026 is the candidate untouched temporal holdout; access needs a Protocol v2 freeze marker"),
]


def _sha(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _iso(ms: int) -> str:
    return (dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc) + dt.timedelta(milliseconds=ms)).strftime("%Y-%m-%dT%H:%M")


def _episodes(missing: list[str]) -> list[dict]:
    out, step = [], dt.timedelta(hours=4)
    for ts in missing:
        t = dt.datetime.strptime(ts, "%Y-%m-%dT%H:%M")
        if out and t - out[-1]["_last"] == step:
            out[-1]["bars"] += 1
            out[-1]["_last"] = t
        else:
            out.append({"first_missing": ts, "bars": 1, "_last": t})
    return [{k: v for k, v in e.items() if k != "_last"} for e in out]


def validation_report(built: dict) -> dict:
    checks = {c["id"]: c for c in built["report"]["checks"]}
    integrity = [c["id"] for c in built["report"]["checks"] if c["id"] not in ("I09", "I11")]
    rows = built["norm"]["rows"]
    short = [{"open": r["open_dt"].strftime("%Y-%m-%dT%H:%M"), "close_minus_open_ms": r["close_ms"] - r["open_ms"], "unit": r["unit"]}
             for r in rows if r["close_ms"] is None or abs(r["close_ms"] - (r["open_ms"] + intraday.INTERVAL_MS - 1)) > intraday.CLOSE_TOLERANCE_MS]
    missing = checks["I11"]["missing_bars"]
    units = Counter(r["unit"] for r in rows)
    return {"strict_status_policy_P0": built["report"]["status"], "failed_checks": built["report"]["failed_checks"],
            "integrity_checks_status": "PASS" if all(checks[i]["status"] in ("PASS", "WARN") for i in integrity) else "FAIL", "integrity_checks": integrity,
            "completeness": {"expected_bars": 2922 * BARS_PER_DAY, "rows": built["report"]["rows"], "missing_bars": len(missing),
                             "missing_share": len(missing) / (2922 * BARS_PER_DAY), "missing_episodes": _episodes(missing), "wrong_length_bars": len(short),
                             "wrong_length_examples": short, "all_shorter_than_4h": all(s["close_minus_open_ms"] < intraday.INTERVAL_MS - 1 for s in short),
                             "interpretation": "enumerated, never filled; consistent with trading interruptions but NOT explained by an official source retrieved here (UNKNOWN cause)"},
            "timestamp_units": {"rows_by_unit": dict(units), "check": checks["I10"]["summary"]}, "checks": built["report"]["checks"],
            "performance_data_accessed": "NO (validation reads bar values only for integrity; no return or strategy result is computed)"}


def determinism(built: dict) -> dict:
    again = intraday.build()
    persisted = intraday.PROCESSED_DIR
    same = {k: built["norm"][k] == again["norm"][k] for k in ("csv_bytes", "extra_bytes", "raw_set_sha256", "normalized_sha256")}
    same["identity"] = intraday.identity_bytes(built["identity"]) == intraday.identity_bytes(again["identity"])
    on_disk = {"csv": (persisted / f"{intraday.NAME}.csv").read_bytes() == built["norm"]["csv_bytes"],
               "extra": (persisted / f"{intraday.NAME}.klines_extra.csv").read_bytes() == built["norm"]["extra_bytes"],
               "identity": (persisted / f"{intraday.NAME}.identity.json").read_bytes() == intraday.identity_bytes(built["identity"])}
    return {"two_independent_builds_identical": all(same.values()), "detail": same, "persisted_files_match_fresh_build": all(on_disk.values()), "on_disk": on_disk,
            "identity": built["identity"]}


def checksum_report() -> dict:
    res = intraday.verify_checksums()
    arts = intraday.raw_artifacts()
    return {**res, "checksum_files_preserved": all(intraday.checksum_artifact(p).exists() for p in arts), "first_archive": arts[0].name, "last_archive": arts[-1].name,
            "archive_names_by_year": dict(Counter(p.name.split("-4h-")[1][:4] for p in arts)),
            "verification": "fetch required sha256(zip) == published checksum; this report re-checks every preserved zip against its preserved CHECKSUM file"}


def microstructure_report(built: dict) -> dict:
    out = {"sample_day": microstructure.SAMPLE_DAY.isoformat(), "files": {}}
    klines = (intraday.PROCESSED_DIR / f"{intraday.NAME}.csv").read_text(encoding="utf-8")
    extra = (intraday.PROCESSED_DIR / f"{intraday.NAME}.klines_extra.csv").read_text(encoding="utf-8")
    for kind in ("aggTrades", "trades"):
        path = intraday.RAW_4H_ROOT / "binance-vision" / "CRYPTO" / "BTCUSDT" / f"BTCUSDT-{kind}-{microstructure.SAMPLE_DAY.isoformat()}.zip"
        desc = microstructure.describe(path, kind)
        out["files"][kind] = {"sha256": _sha(path), **{k: v for k, v in desc.items() if k != "per_4h_bar"},
                              "trades_per_4h_bar": {k: v["trades"] for k, v in desc["per_4h_bar"].items()}}
        if kind == "aggTrades":
            out["aggTrades_vs_klines_same_day"] = {k: v for k, v in microstructure.reconcile_with_klines(desc, klines, extra).items() if k != "details"}
    out["endpoint_schema"] = microstructure.endpoint_schema()
    out["conclusions"] = {
        "bid_ask_spread_historical": "NOT observed: no quote data; trade prints are executions, is_buyer_maker is not a bid/ask",
        "slippage_market_impact": "NOT observed: order sizes and impact are unknown",
        "what_the_samples_support": "trade counts, trade-size distribution, intrabar turnover, consecutive-trade price variation (descriptive only)",
        "prospective": "bookTicker/depth are reachable; field names recorded; values discarded; no snapshot stored; no collector started (separate batch)"}
    return out


def power_analysis(built: dict, rates: dict) -> dict:
    """Plumbing/count analysis only: bar counts, folds, OOS length, trade-COUNT range. No return, profit or strategy output."""

    rows = built["norm"]["rows"]
    n_all = len(rows)
    n_2024 = sum(1 for r in rows if r["open_dt"].year <= 2024)
    out = {"candidate_windows": {}}
    for label, n, note in (("A_development_to_2024-12-31", n_2024, "keeps the 2025 daily Lockbox period out of v2 development"),
                           ("B_development_to_2025-12-31", n_all, "uses 2025 for development: the existing daily Lockbox year would no longer be untouched")):
        folds = WalkForwardConfig(train_bars=TRAIN_BARS, test_bars=TEST_BARS).folds(n)
        oos_bars = sum(f["test"][1] - f["test"][0] for f in folds)
        oos_years = oos_bars / (BARS_PER_DAY * 365)
        crypto = {k: v for k, v in rates.items() if k.startswith("CRYPTO/")}
        scaled = {}
        for k, v in crypto.items():
            r = v["trades_per_oos_year"]
            scaled[k] = {"trades_per_year_daily_observed": r, "calendar_equivalent_grid_expected_trades": round(r * oos_years, 1),
                         "bar_unit_grid_expected_trades_if_signal_rate_scales_6x": round(r * 6 * oos_years, 1)}
        out["candidate_windows"][label] = {"note": note, "bars": n, "folds": len(folds), "oos_bars": oos_bars, "oos_years": round(oos_years, 2), "train_bars": TRAIN_BARS,
                                           "test_bars": TEST_BARS, "per_candidate_expected_trades": scaled,
                                           "meets_30_if_bar_unit_grids": all(s["bar_unit_grid_expected_trades_if_signal_rate_scales_6x"] >= 30 for s in scaled.values()),
                                           "meets_30_if_calendar_equivalent_grids": all(s["calendar_equivalent_grid_expected_trades"] >= 30 for s in scaled.values())}
    out["assumption"] = ("the 6x factor assumes entry/exit signal frequency scales with the number of bars when the existing grids are kept in BAR units; it is an assumption, not a measurement. "
                         "With calendar-equivalent grids (lookbacks x6) no gain in trade count is expected.")
    out["verdict"] = {"structurally_viable": True, "conditional_on": "Protocol v2 keeping the existing grids in bar units and the 6x scaling assumption holding (UNKNOWN until the freeze/run)",
                      "not_assessed": "whether the 30-trade heuristic is itself adequate for the claim being tested (internal heuristic)"}
    out["inputs"] = "trade COUNTS per OOS year from the frozen Batch #3B summary (no returns)"
    return out


def trial_series_demo() -> dict:
    """Contract demonstration on the SYNTHETIC fixture in a temporary store (counts only; nothing is written to results/)."""

    import os

    from qat.data.loader import load_dataset
    from qat.research.walkforward import run_walkforward

    with tempfile.TemporaryDirectory() as tmp:
        old = os.environ.get("QAT_RESULTS_DIR")
        os.environ["QAT_RESULTS_DIR"] = str(pathlib.Path(tmp) / "results")
        try:
            ds = load_dataset(str(PROJECT_ROOT / "data" / "fixtures" / "SYN_KR1_1d.csv"))
            store = trial_series.TrialSeriesStore(pathlib.Path(tmp) / "series")
            cfg = WalkForwardConfig(train_bars=250, test_bars=60, base=BacktestConfig(settings_path=str(PROJECT_ROOT / "config" / "settings.yaml")))
            out = run_walkforward(ds, "ma_trend", cfg, series_store=store)
            ids = out["manifest"]["trial_context"]["trial_ids"]
            stage_counts = Counter(store.get(t)["stage"] for t in ids)
            winner_only = [t for t in ids if store.get(t)["stage"] == "oos_evaluation"]
            try:
                trial_series.TrialSeriesStore(pathlib.Path(tmp) / "empty").verify_complete(winner_only)
                winner_only_rejected = False
            except trial_series.TrialSeriesIncomplete:
                winner_only_rejected = True
            sample = store.get(ids[0])
            complete = not store.missing(ids)
        finally:
            if old is None:
                os.environ.pop("QAT_RESULTS_DIR", None)
            else:
                os.environ["QAT_RESULTS_DIR"] = old
    return {"dataset": "SYNTHETIC fixture SYN_KR1 (plumbing only)", "trials": len(ids), "all_trials_have_series": complete, "stage_counts": dict(stage_counts),
            "winner_only_store_rejected": winner_only_rejected, "record_fields": sorted(sample), "benchmark": sample["benchmark"],
            "storage": "canonical JSON + gzip(mtime=0), one file per trial id, idempotent put"}


def benchmark_v2_demo() -> dict:
    class B:
        def __init__(self, o, c):
            self.open, self.close = o, c

    bars = [B(10, 10), B(10, 11), B(12, 12), B(12, 15)]
    series = trial_series.aligned_benchmark_returns(bars, [1, 4])
    prod = 1.0
    for r in series:
        prod *= 1 + r
    return {"toy_bars_open_close": [(b.open, b.close) for b in bars], "aligned_series": series, "aligned_return": trial_series.aligned_benchmark_return(bars, [1, 4]),
            "series_compounds_to_return": abs(prod - 1 - trial_series.aligned_benchmark_return(bars, [1, 4])) < 1e-12,
            "v1_return_for_comparison": bars[3].close / bars[1].open - 1, "definition": trial_series.__doc__.split("Benchmark v2")[1].strip().split("\n\n")[0]}


def holdout_report(built: dict) -> dict:
    meta = intraday.archive_metadata()
    holdout = [e for e in meta["entries"] if "-2026-" in e["key"]]
    arts = intraday.raw_artifacts()
    all_raw_names = sorted(p.name for p in intraday.RAW_4H_ROOT.rglob("*") if p.is_file())
    late_names = [n for n in all_raw_names if "-2026-" in n]
    identity = built["identity"]
    return {"holdout_start": intraday.HOLDOUT_START.isoformat(), "development_end": intraday.DEV_LAST.isoformat(),
            "guard": "fetch_month/fetch_day raise HoldoutAccessError for 2026+ unless artifacts/verification/protocol_v2/freeze.json exists",
            "freeze_marker_exists": intraday.FREEZE_MARKER.exists(), "holdout_unlocked": intraday.holdout_unlocked(),
            "archive_metadata_only": {"files": [{"name": e["key"].split("/")[-1], "size_bytes": e["size_bytes"], "last_modified": e["last_modified"][:10]} for e in holdout],
                                      "first": holdout[0]["key"].split("/")[-1] if holdout else None, "last": holdout[-1]["key"].split("/")[-1] if holdout else None,
                                      "note": "names, sizes and modification dates only"},
            "raw_artifacts_acquired": len(arts), "raw_files_with_2026_in_name": late_names, "dataset_last_open_utc": identity["last_open"],
            "rows_on_or_after_holdout_start": sum(1 for r in built["norm"]["rows"] if r["open_dt"].date() >= intraday.HOLDOUT_START),
            "values_accessed": "NO", "returns_or_strategy_results_for_2026": "NO",
            "statement": "No 2026 archive content was fetched, parsed or read. Only file names/sizes/dates were listed. The microstructure endpoint probe kept field names only and discarded values.",
            "disclosure": "Integrity validation read 4h bar values up to 2025-12-31 (including 2025, the existing daily Lockbox year) for integrity checks only; no returns or strategy results were computed."}


def lockbox_report() -> dict:
    registry = results_root() / "lockbox_registry.json"
    return {"lockbox_registry_exists": registry.exists(), "evaluate_lockbox_imported_by_this_module": "evaluate_lockbox" in globals(),
            "existing_daily_lockbox": "untouched; no strategy was run on any data in this batch except the SYNTHETIC fixture for the trial-series demonstration",
            "separate_boundaries": "the existing daily Lockbox (2025 tail) and the 2026 temporal holdout are different evidence boundaries",
            "open_design_issue": "Protocol v2 development through 2025-12-31 would read the same 2025 market path that the daily Lockbox reserves; window A (to 2024-12-31) avoids that"}


def methods_status() -> dict:
    return {"white_reality_check": {"status": "CANDIDATE_METHOD - APPLICABILITY UNVERIFIED",
                                    "fact": "White (2000) proposes the Reality Check: a test, under specification search / data snooping, of whether the best model has predictive superiority over a benchmark",
                                    "unverified": "the assumptions needed to apply it to QAT were not sufficiently verified against the primary text", "implemented": False},
            "deflated_sharpe_ratio": {"status": "NOT_IMPLEMENTED - not applicable to Batch #3B structure (see validity_3c)", "implemented": False},
            "probability_of_backtest_overfitting": {"status": "NOT_IMPLEMENTED - needs all-configuration full-period series (not stored in v1)", "implemented": False},
            "this_batch": "builds the storage so that any of the three can be computed later; none was implemented or run"}


def design_registry(validation: dict) -> dict:
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
    return {"protocol_candidate_id": CANDIDATE_ID, "design_timestamp_utc": now,
            "dataset_coverage_known_at_design": {"development_4h": [intraday.DEV_FIRST.isoformat(), intraday.DEV_LAST.isoformat()],
                                                 "missing_4h_bars_observed": validation["completeness"]["missing_bars"],
                                                 "holdout_2026": "file names/sizes/dates for 2026-01..2026-09 known; contents unknown"},
            "performance_data_accessed": {"btcusdt_4h_strategy_results": "NO", "btcusdt_2026_values": "NO",
                                          "btcusdt_daily_development_via_batch_3B": "YES (observed development evidence; Lockbox tail untouched)",
                                          "kr_us_daily_via_batch_3B": "YES (observed)"},
            "decisions": [{"id": f"D{i}", "decision": d, "reason": r, "performance_accessed_before_decision": "NO" if i != 5 else "NO (but made before seeing the 16 missing bars)"}
                          for i, (d, r) in enumerate(DESIGN_CHOICES, 1)],
            "observation_after_design": "4h completeness: 16 missing bars / 18 shorter bars were found AFTER policy D5 was written; policy D5 was NOT relaxed",
            "trial_registry": "extends results/trial_registry.json (Batch #3C); v2 trials will be registered by run_walkforward as before"}


def final_decision(validation: dict, determinism_r: dict, power: dict, series: dict, cost: dict, holdout: dict) -> dict:
    gates = {
        "verified_4h_data_foundation": {"met": validation["strict_status_policy_P0"] == "PASS", "integrity": validation["integrity_checks_status"],
                                        "completeness": f"{validation['completeness']['missing_bars']} missing bars in {len(validation['completeness']['missing_episodes'])} episodes, "
                                                        f"{validation['completeness']['wrong_length_bars']} shorter bars -> strict status {validation['strict_status_policy_P0']}",
                                        "deterministic_identity": determinism_r["two_independent_builds_identical"]},
        "cost_semantics_explicit": {"met": True, "evidence": "VERIFIED_CURRENT commission; historical fee/spread/slippage HISTORICAL_UNKNOWN; spread/slippage only as labelled SCENARIOs"},
        "trial_series_storage_ready": {"met": series["all_trials_have_series"] and series["winner_only_store_rejected"]},
        "benchmark_aligned": {"met": True, "evidence": "aligned_benchmark_* (entry open(start+1)); v1 results untouched"},
        "holdout_access_guarded": {"met": holdout["values_accessed"] == "NO" and not holdout["holdout_unlocked"]},
        "power_structurally_viable": {"met": power["verdict"]["structurally_viable"], "conditional_on": power["verdict"]["conditional_on"]}}
    ready = all(g["met"] for g in gates.values())
    return {"decision": "PROTOCOL_V2_FREEZE_READY" if ready else "RESEARCH_STOP_RECOMMENDED", "gates": gates,
            "reason": "all gates met" if ready else "the strict 4h completeness gate (policy P0, fixed before the data was acquired) is not met; every other gate is met",
            "reversibility": "RESEARCH_STOP is triggered only by the missing/short-bar gate. If the user explicitly accepts a gap policy (missing bars = non-tradable periods, never filled; "
                             "recorded in the freeze document), the remaining gates already hold and the decision would become PROTOCOL_V2_FREEZE_READY without further data work.",
            "not_done": "no Protocol v2 run, no 4h strategy backtest, no parameter selection, no 2026 access, no Lockbox access"}


def build_all(out_dir: pathlib.Path | None = None) -> dict:
    out_dir = pathlib.Path(out_dir or OUT_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    built = intraday.build()
    val = validation_report(built)
    det = determinism(built)
    rates = json.loads((validity.OUT_DIR / "observed_trade_rates.json").read_text(encoding="utf-8"))
    power = power_analysis(built, rates)
    series = trial_series_demo()
    cost_v2 = cost_evidence.v2_report()
    c3 = json.loads((validity.OUT_DIR / "cost_evidence.json").read_text(encoding="utf-8"))["status_counts"]
    holdout = holdout_report(built)
    counts = Counter(e["status"] for e in cost_v2["contract"].values() if isinstance(e, dict) and "status" in e)
    cost_class = {"v2_status_counts": dict(counts), "v2_scenarios": {k: {"half_spread_bps": v["half_spread_bps"], "slippage_bps": v["slippage_bps"], "status": "SCENARIO"} for k, v in cost_v2["scenarios"].items()},
                  "frozen_3c_status_counts_unchanged": cost_evidence.report(str(PROJECT_ROOT / "config" / "settings.yaml"))["status_counts"] == c3,
                  "note": "VERIFIED_CURRENT = current fee only; historical fee, spread and slippage are HISTORICAL_UNKNOWN; scenario values are assumptions"}
    decision = final_decision(val, det, power, series, cost_v2, holdout)
    files = {"data_source_contract.json": SOURCE_CONTRACT, "checksum_report.json": checksum_report(), "validation_4h.json": val, "deterministic_identity.json": det,
             "fee_evidence.json": cost_v2, "cost_classification.json": cost_class, "microstructure_source_assessment.json": microstructure_report(built),
             "power_plumbing_analysis.json": power, "trial_series_readiness.json": series, "benchmark_v2.json": benchmark_v2_demo(), "multiple_testing_status.json": methods_status(),
             "design_registry.json": design_registry(val), "holdout_2026_nonaccess.json": holdout, "lockbox_nonaccess.json": lockbox_report(), "decision.json": decision}
    for name, obj in files.items():
        (out_dir / name).write_text(json.dumps(obj, indent=1, ensure_ascii=False, sort_keys=True, default=str) + "\n", encoding="utf-8")
    return {"decision": decision["decision"], "strict_4h_status": val["strict_status_policy_P0"], "rows": val["completeness"]["rows"],
            "missing_bars": val["completeness"]["missing_bars"], "deterministic": det["two_independent_builds_identical"], "holdout_values_accessed": holdout["values_accessed"],
            "files": sorted(files)}


if __name__ == "__main__":
    print(json.dumps(build_all(), indent=1))
