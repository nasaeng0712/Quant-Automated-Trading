"""Batch #3E - Protocol v2 pre-registration and freeze (BTCUSDT Binance Spot 4h).

Nothing here runs a strategy or reads a price-derived performance number. The module contains

* the frozen CONSTANTS and the pure rule functions of the selection procedure (worst-case cost selection,
  per-scenario status, candidate status, fold plan), so the specification can be tested on synthetic inputs;
* the freeze: canonical JSON components + a protocol hash + a freeze marker under ``artifacts/verification/protocol_v2/``.

Selection procedure (one deterministic algorithm, evaluated once on the 2026 temporal holdout in a separate Batch):
  dataset (continuous view) -> rolling folds (train 4380 / OOS 1095 / step 1095 bars) -> every parameter configuration of the
  existing grid in grid order -> evaluate each on the TRAIN window under S1, S2, S3 -> rank by the WORST train score across
  S1/S2/S3 (ties: earlier in grid order) -> selected parameters -> evaluate them on the OOS window under S1, S2, S3 (S0 recorded
  as diagnostic only) -> per-scenario activity/stability status -> candidate status = the least favourable status across
  S1/S2/S3 -> candidate decision (holdout eligibility needs PASS in the worst case AND beating the aligned passive benchmark in
  every scenario). S0 never influences selection, status or eligibility.

The freeze marker is only a TECHNICAL permission. Reading 2026 values additionally needs a holdout-evaluation approval marker
that only a separate, user-approved one-shot Batch may create (see ``qat.realdata.intraday.holdout_unlocked``).
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import pathlib

from qat.realdata import intraday
from qat.research import cost_evidence
from qat.research.manifest import PROJECT_ROOT
from qat.research.strategies import STRATEGIES
from qat.research.walkforward import WalkForwardConfig

PROTOCOL_VERSION = "wf-protocol-2"
CANDIDATE_ID = "wf-protocol-2-candidate-1"
OUT_DIR = PROJECT_ROOT / "artifacts" / "verification" / "protocol_v2"
DEVELOPMENT_END = "2025-12-31"
HOLDOUT_START, HOLDOUT_END = "2026-01-01", "2026-12-31"
TRAIN_BARS, TEST_BARS, STEP_BARS = 4380, 1095, 1095
BARS_PER_YEAR = 6 * 365
MIN_FOLDS = 3
STRATEGY_NAMES = ("ma_trend", "breakout", "mean_reversion")
SELECTION_SCENARIOS = ("S1_low", "S2_mid", "S3_high")
DIAGNOSTIC_SCENARIOS = ("S0_commission_only",)
STATUS_ORDER = ("UNKNOWN", "INSUFFICIENT_ACTIVITY", "FAIL", "UNSTABLE", "PASS")  # least -> most favourable
THRESHOLDS = {
    "min_folds": MIN_FOLDS,
    "min_total_oos_closed_trades": 30,
    "min_active_fold_fraction": 0.5,
    "positive_fold_fraction_must_exceed": 0.5,
    "classification": "INTERNAL_PRE_REGISTERED_HEURISTIC",
    "note": "no literature basis is claimed; unchanged from Batch #3B and not lowered after seeing its results",
}
COMPONENTS = ("dataset_scope.json", "cost_contract.json", "holdout_boundary.json", "design_registry.json")


# ------------------------------------------------------------------ pure rules
def fold_plan(n_bars: int) -> dict:
    """Fold counts from the bar count alone (no price is read)."""

    try:
        folds = WalkForwardConfig(train_bars=TRAIN_BARS, test_bars=TEST_BARS, step_bars=STEP_BARS, lockbox_bars=0).folds(n_bars)
    except ValueError:
        folds = []
    oos_bars = sum(f["test"][1] - f["test"][0] for f in folds)
    return {"bars": n_bars, "folds": len(folds), "boundaries": folds, "oos_bars": oos_bars, "oos_years": round(oos_bars / BARS_PER_YEAR, 3),
            "oos_windows_non_overlapping": all(b["test"][0] >= a["test"][1] for a, b in zip(folds, folds[1:])), "viable": len(folds) >= MIN_FOLDS}


def select_parameters(train_scores: list[dict]) -> dict:
    """``train_scores``: configurations IN GRID ORDER, each ``{"params": {...}, "scores": {scenario: float | None}}``.
    Rank = the WORST train score across S1/S2/S3 (S0 is ignored); a configuration with a missing score in any of them is not rankable;
    ties keep the earlier configuration in grid order."""

    best, ranking = None, []
    for index, entry in enumerate(train_scores):
        scores = [entry["scores"].get(s) for s in SELECTION_SCENARIOS]
        worst = None if any(v is None for v in scores) else min(scores)
        ranking.append({"index": index, "params": entry["params"], "worst_case_train_score": worst})
        if worst is not None and (best is None or worst > best["worst_case_train_score"]):
            best = ranking[-1]
    if best is None:
        raise ValueError("no configuration is rankable under S1/S2/S3")
    return {"chosen_index": best["index"], "chosen_params": best["params"], "worst_case_train_score": best["worst_case_train_score"], "ranking": ranking}


def classify_scenario(summary: dict, *, admitted: bool = True, min_folds: int | None = None) -> dict:
    """Status of one cost scenario (frozen thresholds). ``summary``: folds, folds_ok, stitched_net_return, positive_folds, active_folds,
    total_oos_trades, passive_stitched_return (aligned). ``min_folds`` overrides the development minimum ONLY for a single-block holdout evaluation
    (declared in the holdout addendum); the development default is the frozen ``THRESHOLDS['min_folds']``."""

    t = THRESHOLDS
    n, ok, stitched = summary["folds"], summary["folds_ok"], summary["stitched_net_return"]
    criteria = {
        "integrity": admitted and ok == n and n >= (t["min_folds"] if min_folds is None else min_folds) and stitched is not None,
        "activity": summary["total_oos_trades"] >= t["min_total_oos_closed_trades"] and (summary["active_folds"] / n if n else 0.0) >= t["min_active_fold_fraction"],
        "profitable_after_costs": stitched is not None and stitched > 0,
        "positive_fold_majority": (summary["positive_folds"] / n if n else 0.0) > t["positive_fold_fraction_must_exceed"],
        "beats_aligned_passive": stitched is not None and stitched > summary["passive_stitched_return"],
    }
    if not criteria["integrity"]:
        status = "UNKNOWN"
    elif not criteria["activity"]:
        status = "INSUFFICIENT_ACTIVITY"
    elif not criteria["profitable_after_costs"]:
        status = "FAIL"
    elif not criteria["positive_fold_majority"]:
        status = "UNSTABLE"
    else:
        status = "PASS"
    return {"status": status, "criteria": criteria}


def classify_candidate(per_scenario: dict, *, min_folds: int | None = None) -> dict:
    """Candidate status = the least favourable per-scenario status across S1/S2/S3. S0 may be present but is diagnostic only."""

    results = {}
    for name in SELECTION_SCENARIOS:
        results[name] = classify_scenario(per_scenario[name], min_folds=min_folds) if name in per_scenario else {"status": "UNKNOWN", "criteria": {}}
    worst = min((r["status"] for r in results.values()), key=STATUS_ORDER.index)
    beats_all = all(r["criteria"].get("beats_aligned_passive") for r in results.values())
    return {"status": worst, "per_scenario": {k: v["status"] for k, v in results.items()}, "criteria": {k: v["criteria"] for k, v in results.items()},
            "holdout_eligible": worst == "PASS" and beats_all,
            "diagnostic_only": {s: ({"status_if_it_counted": classify_scenario(per_scenario[s])["status"], "used_for_decision": False} if s in per_scenario else None)
                                for s in DIAGNOSTIC_SCENARIOS}}


# ------------------------------------------------------------------ canonical hashing
def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def sha(obj) -> str:
    return hashlib.sha256(canonical(obj).encode("utf-8")).hexdigest()


# ------------------------------------------------------------------ components
def _timestamps(view_csv: str) -> list[str]:
    return [line.split(",", 1)[0] for line in view_csv.splitlines()[1:] if line]


def build_components(view: dict, view_csv: str, holdout_meta: list[dict], design_timestamp: str) -> dict:
    stamps = _timestamps(view_csv)
    plan = fold_plan(len(stamps))
    folds = [{"fold": f["fold"], "train_bars": f["train"], "test_bars": f["test"], "train_start": stamps[f["train"][0]], "train_end": stamps[f["train"][1] - 1],
              "oos_start": stamps[f["test"][0]], "oos_end": stamps[f["test"][1] - 1]} for f in plan["boundaries"]]
    scope = {
        "dataset": {"venue": "Binance Spot", "symbol": "BTCUSDT", "interval": "4h", "development_end": DEVELOPMENT_END},
        "parent": {**view["parent"], "meaning": "the full 2018-2025 historical artifact keeps its strict status; it is NOT converted to PASS"},
        "research_view": {"status": view["view_status"], "view_data_version": view["view_data_version"], "continuous_start": view["continuous_start"],
                          "continuous_end": view["continuous_end"], "rows": view["rows"], "view_normalized_sha256": view["view_normalized_sha256"],
                          "view_extra_sha256": view["view_extra_sha256"], "validation": view["validation"], "rule_id": view["rule_id"],
                          "derivation_rule": view["derivation"]["rule"], "excluded_irregular_intervals": view["excluded_irregular_intervals"],
                          "rows_modified": view["rows_modified"], "rows_synthesized": view["rows_synthesized"], "interpolation_or_fill": "NONE"},
        "window_design": {"train_bars": TRAIN_BARS, "oos_bars": TEST_BARS, "step_bars": STEP_BARS, "type": "rolling, fixed-length train, non-overlapping OOS", "lockbox_bars_inside_view": 0,
                          "calendar_equivalent": "2.0y train / 0.5y OOS (6 bars per day)"},
        "fold_plan": {"folds": plan["folds"], "oos_bars": plan["oos_bars"], "oos_years": plan["oos_years"], "min_folds_required": MIN_FOLDS, "viable": plan["viable"],
                      "oos_windows_non_overlapping": plan["oos_windows_non_overlapping"], "boundaries": folds},
        "power_plumbing": {"computed_from": "bar counts and timestamps only; no price, return, trade count or strategy output",
                           "bars": plan["bars"], "maximum_oos_exposure_windows": plan["folds"], "maximum_oos_years": plan["oos_years"],
                           "trade_count_scaling_assumption_6x": "UNKNOWN - the Batch #3D scaling assumption is NOT used as a basis for this protocol"},
        "excluded_from_development": [f"before {view['continuous_start']} (irregular region + the regular bars preceding it, excluded by rule)", f"{HOLDOUT_START} onward (temporal holdout)"],
    }
    contract = cost_evidence.v2_report()
    cost = {
        "venue": cost_evidence.V2_VENUE, "fee_tier": cost_evidence.V2_TIER,
        "current_verified_fee": {"maker": 0.001, "taker": 0.001, "status": "VERIFIED_CURRENT", "retrieved": cost_evidence.RETRIEVED, "scope": "current only"},
        "historical_fee_2018_2025": {"status": "HISTORICAL_UNKNOWN", "research_use": "ASSUMED_FOR_RESEARCH",
                                     "statement": "the current 0.100% is applied to historical bars as a labelled research assumption; it is NOT declared the historical fee"},
        "bid_ask_spread_historical": "HISTORICAL_UNKNOWN", "slippage_market_impact_historical": "HISTORICAL_UNKNOWN",
        "commission_execution_assumption": "taker 0.100% on every fill: the engine executes marketable orders at the next bar open and models a single commission rate (no maker/taker distinction); "
                                           "no maker fill is assumed",
        "scenarios": {k: {"half_spread_bps": v["half_spread_bps"], "slippage_bps": v["slippage_bps"], "label": "SCENARIO ASSUMPTION", "status": "SCENARIO"}
                      for k, v in contract["scenarios"].items()},
        "scenario_values_origin": "round numbers fixed before any v2 result (Batch #3D); not derived from data or performance; never described as actual spread/slippage",
        "engine_mapping": "settings costs.CRYPTO: commission_rate=0.001, tax_rate_sell=0, fx_cost_bps=0, half_spread_bps/slippage_bps per scenario",
        "s0_restriction": "S0 (0/0) is for plumbing and lower-cost diagnostics ONLY: never used for parameter selection, candidate status or holdout eligibility",
        "selection_scenarios": list(SELECTION_SCENARIOS), "robust_selection_rule": "rank each train configuration by the WORST train score across S1/S2/S3; ties -> grid order",
        "candidate_rule": "per-scenario status on OOS for S1/S2/S3; candidate status = least favourable; S0 recorded as diagnostic only",
        "purpose": "prevent cherry-picking among unknown execution costs; it does not claim the true cost is known",
        "result_label": "scenario results - NOT actual-cost performance"}
    holdout = {
        "development_end": DEVELOPMENT_END, "temporal_holdout": {"start": HOLDOUT_START, "end": HOLDOUT_END, "status": "UNTOUCHED, NOT YET COMPLETE",
                                                              "complete_year_available": False},
        "metadata_known_at_freeze": {"archive_files": holdout_meta, "months_listed": len(holdout_meta), "note": "file names, sizes and modification dates only"},
        "values_accessed": "NO", "forbidden_before_evaluation": ["OHLC", "volume", "returns", "indicators", "strategy execution", "performance", "descriptive statistics derived from values"],
        "allowed": ["filename", "month availability", "archive size", "checksum filename existence", "coverage metadata"],
        "guard": "HoldoutAccessError unless the freeze marker AND a holdout-evaluation approval marker exist",
        "freeze_marker_meaning": "technical permission only; it is not approval to evaluate",
        "evaluation": "one-shot, in a separate user-approved Batch after the full 2026 year exists; the whole selection procedure is evaluated once",
        "existing_daily_lockbox": "historical artifact, untouched, NOT used as the v2 holdout (its 2025 year is part of v2 development evidence)"}
    registry = {
        "protocol_candidate_id": CANDIDATE_ID, "design_timestamp_utc": design_timestamp,
        "dataset_coverage_known_at_design": {"parent_4h": [intraday.DEV_FIRST.isoformat(), DEVELOPMENT_END], "continuous_view": [view["continuous_start"], view["continuous_end"]],
                                             "holdout_2026": "file metadata only"},
        "performance_data_accessed": {"btcusdt_4h_strategy_results": "NO", "btcusdt_2026_values": "NO", "btcusdt_4h_prices_read_for": "integrity validation and timestamp-only view derivation",
                                      "daily_batch_3B_results": "YES (observed development evidence)"},
        "gap_handling_decision": {"classification": "POST-DATA-INTEGRITY OBSERVATION / PRE-PERFORMANCE",
                                  "meaning": "decided after the 4h gaps were observed, before any strategy result existed",
                                  "policy": "gaps are NOT permitted and the strict parent FAIL is kept; the irregular region is EXCLUDED by a deterministic continuous-subperiod rule "
                                            "(timestamps and integrity only)",
                                  "not_done": ["relaxing 'missing bar = FAIL'", "interpolation", "forward fill", "merging", "synthetic bars", "choosing continuous_start by returns, trade counts or performance"]},
        "decisions": [
            {"id": "P1", "decision": "development = maximal trailing continuous segment of the 4h parent, end 2025-12-31", "performance_accessed": "NO"},
            {"id": "P2", "decision": "temporal holdout 2026-01-01..2026-12-31, evaluated once after the year completes", "performance_accessed": "NO"},
            {"id": "P3", "decision": "windows 4380/1095/1095 bars (2y/0.5y calendar equivalent), rolling", "performance_accessed": "NO"},
            {"id": "P4", "decision": "existing strategies and grids unchanged; grids are BAR counts (NEW INTRADAY HORIZON)", "performance_accessed": "NO"},
            {"id": "P5", "decision": "robust cost selection: worst case across S1/S2/S3; S0 diagnostic only", "performance_accessed": "NO"},
            {"id": "P6", "decision": "30-trade and 50%-active-fold thresholds unchanged (INTERNAL_PRE_REGISTERED_HEURISTIC)", "performance_accessed": "NO (not lowered after Batch #3B)"},
            {"id": "P7", "decision": "aligned benchmark (entry open(start+1)); per-trial series for every evaluated configuration", "performance_accessed": "NO"}],
        "trial_registry": "extends results/trial_registry.json; identical re-runs do not add trials"}
    return {"dataset_scope.json": scope, "cost_contract.json": cost, "holdout_boundary.json": holdout, "design_registry.json": registry}


def parameter_semantics() -> dict:
    return {"decision": "Option A: the existing grids are used UNCHANGED as bar counts; on 4h bars they denote a NEW INTRADAY HORIZON (e.g. slow=50 is 50 four-hour bars = 200h)",
            "reason": "no strategy parameter carries a calendar meaning in the repository (all are bar counts); rescaling to daily-calendar horizons would add a new search space",
            "rescaling_to_daily_calendar_horizons": "NOT used (Option B was not tried)",
            "grids": {n: STRATEGIES[n].param_grid for n in STRATEGY_NAMES}, "strategy_versions": {n: STRATEGIES[n].version for n in STRATEGY_NAMES},
            "new_strategies": "forbidden", "grid_expansion_after_results": "forbidden"}


def build_body(components: dict) -> dict:
    return {
        "version": PROTOCOL_VERSION, "candidate_id": CANDIDATE_ID, "scope": "BTCUSDT / Binance Spot / 4h only",
        "components": {name: sha(components[name]) for name in COMPONENTS},
        "parameter_semantics": parameter_semantics(),
        "selection_procedure": ["dataset: continuous research view (ADMITTED_CONTINUOUS_SUBPERIOD), development end 2025-12-31",
                                f"folds: rolling train={TRAIN_BARS} OOS={TEST_BARS} step={STEP_BARS} bars, no OOS overlap",
                                "parameter configurations: every combination of the existing grid, in grid order",
                                "train evaluation: each configuration under S1, S2, S3 (S0 diagnostic only)",
                                "ranking: worst train score across S1/S2/S3; ties -> earlier grid order",
                                "selected parameters per fold (never changed after OOS is seen)",
                                "OOS evaluation: selected parameters under S1, S2, S3 (S0 diagnostic only)",
                                "per-scenario status: UNKNOWN > INSUFFICIENT_ACTIVITY > FAIL > UNSTABLE > PASS (frozen thresholds)",
                                "candidate status: least favourable across S1/S2/S3; holdout eligibility needs PASS and beating the aligned passive benchmark in every scenario"],
        "thresholds": THRESHOLDS, "status_order_least_to_most_favourable": list(STATUS_ORDER),
        "scenarios": {"selection": list(SELECTION_SCENARIOS), "diagnostic_only": list(DIAGNOSTIC_SCENARIOS)},
        "benchmark": "aligned passive: entry open(start+1), exit close of the last window bar, cost-free, fully invested (qat.research.trial_series)",
        "trial_series": {"required_for": "every evaluated configuration (all train candidates in every scenario, failed ones, every OOS evaluation); winner-only storage forbidden",
                         "fields": ["trial_id", "strategy", "parameters", "cost_scenario", "fold", "stage", "timestamps", "returns", "benchmark_returns", "dataset identity"],
                         "identical_rerun": "does not increase the statistical trial count (same trial_id)"},
        "multiple_testing": {"white_reality_check": "CANDIDATE_METHOD - APPLICABILITY UNVERIFIED", "deflated_sharpe_ratio": "not selected", "probability_of_backtest_overfitting": "not selected",
                             "role_of_holdout": "the untouched 2026 temporal holdout is the independent evaluation of the whole selection procedure"},
        "holdout": {"development_end": DEVELOPMENT_END, "start": HOLDOUT_START, "end": HOLDOUT_END, "values_read_in_freeze": "NO"},
        "forbidden_in_this_protocol_batch": ["strategy profitability runs", "parameter ranking results", "OOS returns", "Sharpe", "candidate status", "Walk-Forward on real 4h data",
                                             "reading 2026 values"],
        "change_policy": "any change to this specification is a NEW protocol version; silent modification is detected by the hash"}


# ------------------------------------------------------------------ freeze / verify
def _write(path: pathlib.Path, obj) -> None:
    path.write_text(json.dumps(obj, indent=1, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")


def freeze(view: dict, view_csv: str, holdout_meta: list[dict], *, out_dir: pathlib.Path | None = None, now: str | None = None) -> dict:
    out_dir = pathlib.Path(out_dir or OUT_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    if (out_dir / "protocol_v2.json").exists():
        raise RuntimeError("Protocol v2 is already frozen; a change requires a new protocol version")
    stamp = now or dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
    components = build_components(view, view_csv, holdout_meta, stamp)
    body = build_body(components)
    digest = sha(body)
    for name, obj in components.items():
        _write(out_dir / name, obj)
    _write(out_dir / "protocol_v2.json", {**body, "protocol_sha256": digest})
    (out_dir / "protocol_v2.sha256").write_text(f"{digest}  protocol_v2.json (sha256 of the canonical body without protocol_sha256)\n", encoding="utf-8")
    marker = {"protocol_version": PROTOCOL_VERSION, "protocol_sha256": digest, "frozen_utc": stamp, "permission": "TECHNICAL ONLY",
              "holdout_evaluation_approved": False, "note": "the freeze marker is not approval to read or evaluate 2026 data"}
    _write(out_dir / "freeze.json", marker)
    return {"protocol_sha256": digest, "frozen_utc": stamp}


def verify_frozen(out_dir: pathlib.Path | None = None) -> dict:
    """Detects any modification after the freeze: component files, protocol body, hash file, marker and the code constants."""

    out_dir = pathlib.Path(out_dir or OUT_DIR)
    frozen = json.loads((out_dir / "protocol_v2.json").read_text(encoding="utf-8"))
    recorded = frozen.pop("protocol_sha256")
    components = {name: json.loads((out_dir / name).read_text(encoding="utf-8")) for name in COMPONENTS}
    code_body = build_body(components)  # embeds the CURRENT code constants + the stored component hashes
    marker = json.loads((out_dir / "freeze.json").read_text(encoding="utf-8"))
    hash_file = (out_dir / "protocol_v2.sha256").read_text(encoding="utf-8").split()[0]
    checks = {"body_hash_matches_recorded": sha(frozen) == recorded, "component_files_match": frozen["components"] == {n: sha(components[n]) for n in COMPONENTS},
              "hash_file_matches": hash_file == recorded, "marker_matches": marker["protocol_sha256"] == recorded and marker["protocol_version"] == PROTOCOL_VERSION,
              "code_matches_frozen_spec": sha(code_body) == recorded}
    return {"ok": all(checks.values()), "protocol_sha256": recorded, "checks": checks}


def freeze_from_repo(out_dir: pathlib.Path | None = None) -> dict:
    """Re-derives the parent and the view from the preserved raw archives, requires them to equal the persisted view identity,
    records the 2026 archive FILE METADATA (names, sizes, dates only) and freezes."""

    built = intraday.build()
    view = intraday.derive_view(built)
    if intraday.identity_bytes(view["identity"]) != (intraday.PROCESSED_DIR / f"{intraday.VIEW_NAME}.view.json").read_bytes():
        raise RuntimeError("the persisted continuous view does not match a fresh derivation")
    if view["identity"]["view_status"] != intraday.VIEW_STATUS_ADMITTED:
        raise RuntimeError("the continuous view is not admitted")
    entries = [e for e in intraday.archive_metadata()["entries"] if "-2026-" in e["key"]]
    meta = [{"name": e["key"].split("/")[-1], "size_bytes": e["size_bytes"], "last_modified": e["last_modified"][:10]} for e in entries]
    return freeze(view["identity"], view["csv"], meta, out_dir=out_dir)


def decision_report(out_dir: pathlib.Path | None = None) -> dict:
    """Batch #3E final decision from verifiable facts (timestamps, hashes, guards). Reads no price-derived performance value."""

    from qat.research import intraday_evidence, trial_series
    from qat.research.store import results_root

    out_dir = pathlib.Path(out_dir or OUT_DIR)
    verify = verify_frozen(out_dir)
    ident = json.loads((intraday.PROCESSED_DIR / f"{intraday.VIEW_NAME}.view.json").read_text(encoding="utf-8"))
    built = intraday.build()
    fresh = intraday.derive_view(built)
    deterministic = intraday.identity_bytes(fresh["identity"]) == intraday.identity_bytes(ident) and intraday.identity_bytes(intraday.derive_view(built)["identity"]) == intraday.identity_bytes(ident)
    plan = fold_plan(ident["rows"])
    cost_ok = True
    try:
        cost_evidence.validate_v2()
    except cost_evidence.CostEvidenceError:
        cost_ok = False
    class _B:  # toy bars: the aligned benchmark must enter at open(start+1)
        def __init__(self, o, c):
            self.open, self.close = o, c
    toy = [_B(10, 10), _B(10, 11), _B(12, 12), _B(12, 15)]
    aligned = abs(trial_series.aligned_benchmark_return(toy, [1, 4]) - (15 / 12 - 1)) < 1e-12
    series = intraday_evidence.trial_series_demo()
    late = [p.name for p in intraday.RAW_4H_ROOT.rglob("*") if p.is_file() and "-2026-" in p.name]
    gates = {
        "valid_continuous_development_subperiod": {"met": ident["view_status"] == intraday.VIEW_STATUS_ADMITTED and ident["rows"] > 0 and ident["validation"]["status"] == "PASS",
                                                    "start": ident["continuous_start"], "end": ident["continuous_end"], "rows": ident["rows"]},
        "parent_unchanged_and_still_failed": {"met": built["identity"]["validation_status"] == "FAIL" and ident["parent"]["admission_status"] == intraday.PARENT_STATUS_FAIL,
                                              "parent_data_version": built["identity"]["data_version"], "rows_modified": ident["rows_modified"], "rows_synthesized": ident["rows_synthesized"]},
        "sufficient_folds": {"met": plan["viable"], "folds": plan["folds"], "min_required": MIN_FOLDS, "oos_years": plan["oos_years"]},
        "admission_deterministic": {"met": deterministic},
        "cost_contract_frozen": {"met": cost_ok and verify["checks"]["component_files_match"]},
        "benchmark_aligned": {"met": aligned},
        "trial_series_ready": {"met": series["all_trials_have_series"] and series["winner_only_store_rejected"], "demonstrated_on": series["dataset"]},
        "holdout_guard_active": {"met": (not intraday.holdout_unlocked()) and not intraday.APPROVAL_MARKER.exists() and not late, "freeze_marker_exists": intraday.FREEZE_MARKER.exists(),
                                 "approval_marker_exists": intraday.APPROVAL_MARKER.exists(), "raw_files_with_2026_in_name": late, "values_accessed": "NO"},
        "protocol_hash_frozen": {"met": verify["ok"], "protocol_sha256": verify["protocol_sha256"], "checks": verify["checks"]},
        "lockbox_untouched": {"met": not (results_root() / "lockbox_registry.json").exists()},
    }
    ready = all(g["met"] for g in gates.values())
    not_viable = not plan["viable"]
    decision = "PROTOCOL_V2_FROZEN" if ready else ("PROTOCOL_V2_NOT_VIABLE" if not_viable else "RESEARCH_STOP_RECOMMENDED")
    return {"decision": decision, "gates": gates, "performance_run": "NONE - no strategy was executed on real 4h data", "holdout_values_accessed": "NO",
            "unused_tail_note": "the rolling folds leave the final bars of the view after the last OOS window unused (fewer than one full OOS window); the windows were not adjusted"}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m qat.research.protocol_v2")
    parser.add_argument("cmd", choices=["verify", "freeze", "decide"])
    args = parser.parse_args(argv)
    if args.cmd == "decide":
        report = decision_report()
        _write(OUT_DIR / "freeze_verification.json", report)
        print(json.dumps({"decision": report["decision"], "gates": {k: v["met"] for k, v in report["gates"].items()}}, indent=1))
    elif args.cmd == "freeze":
        print(json.dumps(freeze_from_repo(), indent=1))
    elif args.cmd == "verify":
        print(json.dumps(verify_frozen(), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
