"""Integrated Protocol v2 development execution (BTCUSDT Binance Spot 4h, wf-protocol-2).

* ``build_addendum`` - the immutable PRE-PERFORMANCE ``candidate_selection_addendum_v1`` (ranking rule among PASS candidates,
  metric definitions, execution contract, branching). It is a separate file: the frozen Protocol v2 hash is not touched.
* ``run_development`` - the frozen selection procedure on the continuous research view: every configuration of the existing grid is
  evaluated on the TRAIN window under S1/S2/S3, ranked by the WORST train score, then the selected parameters are evaluated on the OOS
  window under S1/S2/S3 (S0 recorded as diagnostic only). EVERY evaluation is a registered trial with a stored per-bar series.
* ``official`` - Official Development Run #1 + verification rerun + branching (no PASS / one PASS / several PASS).

Nothing here reads 2026 values: the view ends 2025-12-31 and ``run_development`` refuses datasets that reach the holdout.
"""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import json
import pathlib
import statistics
from dataclasses import asdict

import yaml

from qat.data.loader import load_dataset
from qat.realdata import intraday
from qat.realdata.admission import check_admission
from qat.research import cost_evidence, protocol_v2 as pv, trial_series, trials
from qat.research.backtest import BacktestConfig, run_backtest
from qat.research.protocol import SETTINGS_PATH, _stitched_drawdown
from qat.research.store import registry_read, results_root
from qat.research.strategies import STRATEGIES, make_strategy
from qat.research.walkforward import _grid

RUN_ID = "protocol-v2-dev-run-1"
ADDENDUM_ID = "candidate_selection_addendum_v1"
RUN_DIR = pv.OUT_DIR / "development_run_1"
ADDENDUM_PATH = pv.OUT_DIR / f"{ADDENDUM_ID}.json"
ADDENDUM_HASH_PATH = pv.OUT_DIR / f"{ADDENDUM_ID}.sha256"
SETTINGS_DIR = pv.OUT_DIR / "cost_settings"
VIEW_CSV = intraday.PROCESSED_DIR / f"{intraday.VIEW_NAME}.csv"
ALL_SCENARIOS = pv.SELECTION_SCENARIOS + pv.DIAGNOSTIC_SCENARIOS
CLAIM_BAN = ["proven alpha", "robustly profitable", "production-ready", "statistically proven"]


# ------------------------------------------------------------------ cost scenarios as engine settings
def scenario_settings_obj(scenario: str, base_path: pathlib.Path = SETTINGS_PATH) -> dict:
    """config/settings.yaml with ONLY costs.CRYPTO replaced by the frozen v2 contract: taker commission 0.001, no tax, no FX cost, scenario spread/slippage."""

    s = cost_evidence.V2_SCENARIOS[scenario]
    obj = yaml.safe_load(base_path.read_text(encoding="utf-8"))
    obj["costs"]["CRYPTO"] = {"commission_rate": cost_evidence.V2_CONTRACT["commission_taker_current"]["value"], "tax_rate_sell": 0.0,
                              "half_spread_bps": float(s["half_spread_bps"]), "slippage_bps": float(s["slippage_bps"]), "fx_cost_bps": 0.0}
    return obj


def write_scenario_settings(directory: pathlib.Path = SETTINGS_DIR, base_path: pathlib.Path = SETTINGS_PATH) -> dict:
    directory.mkdir(parents=True, exist_ok=True)
    out = {}
    for name in ALL_SCENARIOS:
        path = directory / f"{name}.yaml"
        text = ("# Protocol v2 cost scenario (SCENARIO ASSUMPTION; commission = current Binance taker 0.100% taken as ASSUMED_FOR_RESEARCH). Derived from config/settings.yaml.\n"
                + yaml.safe_dump(scenario_settings_obj(name, base_path), sort_keys=True, default_flow_style=False))
        if path.exists() and path.read_text(encoding="utf-8") != text:
            raise RuntimeError(f"{path.name} already exists with different content")
        path.write_text(text, encoding="utf-8")
        out[name] = {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    return out


# ------------------------------------------------------------------ pure rules: summaries, ranking, branching
def _sha(obj) -> str:
    return pv.sha(obj)


def summarize_scenario(folds: list[dict], bars, initial_cash: float) -> dict:
    """``folds``: [{fold, test, status, metrics, equity}] for ONE scenario. Aligned passive benchmark; stitched = compounding of fold returns."""

    ok = [f for f in folds if f["status"] == "OK" and f["metrics"]["net_return"] is not None]
    rows = []
    for f in folds:
        m = f.get("metrics") or {}
        rows.append({"fold": f["fold"], "status": f["status"], "test": f["test"], "net_return": m.get("net_return"), "max_drawdown_pct": m.get("max_drawdown_pct"),
                     "trades_closed": m.get("trades_closed"), "win_rate": m.get("win_rate"), "avg_win": m.get("avg_win"), "avg_loss": m.get("avg_loss"),
                     "turnover": m.get("turnover"), "commission": m.get("commission"), "explicit_costs": m.get("explicit_costs"), "slippage_estimate": m.get("slippage_estimate"),
                     "exposure_time": m.get("exposure_time"), "rejections": m.get("rejections"),
                     "benchmark_aligned_return": trial_series.aligned_benchmark_return(bars, f["test"]), "error": f.get("error")})
    returns = [r["net_return"] for r in rows if r["net_return"] is not None]
    stitched = None
    if returns and len(ok) == len(folds):
        stitched = 1.0
        for r in returns:
            stitched *= 1 + r
        stitched -= 1
    passive = 1.0
    for r in rows:
        passive *= 1 + r["benchmark_aligned_return"]
    trades = sum(r["trades_closed"] or 0 for r in rows)
    exposures = [r["exposure_time"] for r in rows if r["exposure_time"] is not None]
    return {"folds": len(folds), "folds_ok": len(ok), "stitched_net_return": stitched, "positive_folds": sum(1 for v in returns if v > 0),
            "active_folds": sum(1 for r in rows if (r["trades_closed"] or 0) >= 1), "total_oos_trades": trades, "passive_stitched_return": passive - 1,
            "stitched_max_drawdown": _stitched_drawdown([{"oos_equity": f["equity"] or []} for f in folds], initial_cash),
            "total_commission": sum(r["commission"] or 0 for r in rows), "total_explicit_costs": sum(r["explicit_costs"] or 0 for r in rows),
            "total_slippage_estimate": sum(r["slippage_estimate"] or 0 for r in rows), "total_turnover": sum(r["turnover"] or 0 for r in rows),
            "mean_exposure_time": statistics.fmean(exposures) if exposures else None,
            "total_rejections": sum(r["rejections"] or 0 for r in rows), "worst_fold_return": min(returns) if returns else None, "best_fold_return": max(returns) if returns else None,
            "fold_rows": rows}


def worst_case_metrics(per_scenario: dict) -> dict:
    """Worst case across S1/S2/S3 (S0 never enters)."""

    sel = [per_scenario[s] for s in pv.SELECTION_SCENARIOS if s in per_scenario]
    if len(sel) != len(pv.SELECTION_SCENARIOS) or any(x["stitched_net_return"] is None for x in sel):
        return {"worst_stitched_net_return": None, "worst_max_drawdown_abs": None, "worst_oos_closed_trades": None}
    dd = [abs(x["stitched_max_drawdown"]) if x["stitched_max_drawdown"] is not None else 0.0 for x in sel]
    return {"worst_stitched_net_return": min(x["stitched_net_return"] for x in sel), "worst_max_drawdown_abs": max(dd),
            "worst_oos_closed_trades": min(x["total_oos_trades"] for x in sel)}


def rank_pass_candidates(candidates: list[dict], declaration_order=pv.STRATEGY_NAMES) -> list[dict]:
    """Pre-registered deterministic ranking among PASS candidates ONLY (addendum v1):
    1) higher worst-case stitched OOS net return; 2) smaller worst-case max drawdown (absolute); 3) more OOS closed trades (worst case);
    4) existing strategy declaration order. ``candidates``: {strategy, status, worst_stitched_net_return, worst_max_drawdown_abs, worst_oos_closed_trades}."""

    passing = [c for c in candidates if c["status"] == "PASS"]
    return sorted(passing, key=lambda c: (-c["worst_stitched_net_return"], c["worst_max_drawdown_abs"], -c["worst_oos_closed_trades"], declaration_order.index(c["strategy"])))


def branch_outcome(ranked: list[dict]) -> dict:
    if not ranked:
        return {"case": "A", "state": "RESEARCH_PATH_STOPPED_NO_CANDIDATE", "selected": None, "historical_development_candidates": []}
    selected = ranked[0]["strategy"]
    return {"case": "B" if len(ranked) == 1 else "C", "state": "DEVELOPMENT_SELECTED_CANDIDATE", "selected": selected,
            "historical_development_candidates": [c["strategy"] for c in ranked[1:]]}


# ------------------------------------------------------------------ the frozen procedure
def _base_config(settings_path: str, window=None) -> BacktestConfig:
    cfg = BacktestConfig(settings_path=settings_path)
    return cfg if window is None else BacktestConfig(settings_path=settings_path, trade_start=window[0], trade_end=window[1])


def run_development(dataset, boundaries: list[dict], settings: dict, *, store: trial_series.TrialSeriesStore, protocol_version: str = pv.PROTOCOL_VERSION,
                    strategies=pv.STRATEGY_NAMES, register: bool = True, run_id: str = RUN_ID, min_folds: int | None = None) -> dict:
    """``boundaries``: fold plan boundaries ({fold, train:[a,b), test:[b,c)}); ``settings``: {scenario: {path, sha256}}."""

    if dataset.bars[-1].ts.date() >= intraday.HOLDOUT_START and not intraday.holdout_unlocked():
        raise intraday.HoldoutAccessError("the dataset reaches the 2026 holdout")
    out, all_trials, evaluations = {}, [], 0
    for name in strategies:
        configs = _grid(name, None, {})  # existing grid, grid order
        fold_out, per_scen_folds = [], {s: [] for s in ALL_SCENARIOS}
        for fold in boundaries:
            train, test = fold["train"], fold["test"]
            entry = {"fold": fold["fold"], "train": train, "test": test, "status": "OK"}
            train_scores, train_ids = [], {}
            for params in configs:
                scores = {}
                for scen in pv.SELECTION_SCENARIOS:
                    cfg = _base_config(settings[scen]["path"], train)
                    tid = trials.trial_id(data_version=dataset.data_version, strategy=name, strategy_version=STRATEGIES[name].version, params=params, window=train,
                                          stage="train_selection", selection_metric=trials.SELECTION_METRIC, base_config=asdict(cfg), settings_sha=settings[scen]["sha256"])
                    try:
                        res = run_backtest(dataset, make_strategy(name, **params), cfg)
                        scores[scen], equity = res.metrics["net_return"], res.equity
                    except Exception:  # noqa: BLE001 - a failed evaluation is still a registered trial
                        scores[scen], equity = None, None
                    evaluations += 1
                    train_ids[(tuple(sorted(params.items())), scen)] = tid
                    all_trials.append({"trial_id": tid, "stage": "train_selection", "params": dict(params), "window": list(train), "strategy": name, "cost_scenario": scen,
                                       "training_fold": {"fold": fold["fold"], "train": list(train), "test": list(test)}, "_equity": equity})
                train_scores.append({"params": params, "scores": scores})
            entry["train_scores"] = train_scores
            try:
                selection = pv.select_parameters(train_scores)
            except ValueError as exc:
                entry.update(status="FAIL", error=str(exc))
                for scen in ALL_SCENARIOS:
                    per_scen_folds[scen].append({"fold": fold["fold"], "test": test, "status": "FAIL", "metrics": {}, "equity": None, "error": str(exc)})
                fold_out.append(entry)
                continue
            chosen = selection["chosen_params"]
            entry["selection"] = {k: selection[k] for k in ("chosen_index", "chosen_params", "worst_case_train_score")}
            entry["oos"] = {}
            for scen in ALL_SCENARIOS:
                cfg = _base_config(settings[scen]["path"], test)
                tid = trials.trial_id(data_version=dataset.data_version, strategy=name, strategy_version=STRATEGIES[name].version, params=chosen, window=test,
                                      stage="oos_evaluation", selection_metric=trials.SELECTION_METRIC, base_config=asdict(cfg), settings_sha=settings[scen]["sha256"])
                try:
                    res = run_backtest(dataset, make_strategy(name, **chosen), cfg)
                    metrics, equity, err = res.metrics, res.equity, None
                except Exception as exc:  # noqa: BLE001
                    metrics, equity, err = {}, None, repr(exc)
                evaluations += 1
                all_trials.append({"trial_id": tid, "stage": "oos_evaluation", "params": dict(chosen), "window": list(test), "strategy": name, "cost_scenario": scen,
                                   "training_fold": {"fold": fold["fold"], "train": list(train), "test": list(test)}, "_equity": equity})
                entry["oos"][scen] = {"trial_id": tid, "net_return": metrics.get("net_return"), "trades_closed": metrics.get("trades_closed"), "error": err}
                per_scen_folds[scen].append({"fold": fold["fold"], "test": test, "status": "OK" if err is None else "FAIL", "metrics": metrics, "equity": equity, "error": err})
            fold_out.append(entry)
        initial = BacktestConfig().initial_cash
        summaries = {s: summarize_scenario(per_scen_folds[s], dataset.bars, initial) for s in ALL_SCENARIOS}
        candidate = pv.classify_candidate({s: summaries[s] for s in ALL_SCENARIOS}, min_folds=min_folds)
        out[name] = {"folds": fold_out, "scenario_summaries": summaries, "candidate": candidate, "worst_case": worst_case_metrics(summaries)}
    # every evaluated configuration gets a stored series and a registry entry (winner-only storage is impossible: all evaluations are in all_trials)
    ids, dedup, newly = [], 0, 0
    for t in all_trials:
        eq = t.pop("_equity")
        record = trial_series.build_record(dataset=dataset, strategy_name=t["strategy"], trial_id=t["trial_id"], stage=t["stage"], fold=t["training_fold"]["fold"], params=t["params"],
                                           window=t["window"], equity=eq, initial_cash=BacktestConfig().initial_cash, protocol_version=protocol_version, cost_scenario=t["cost_scenario"])
        status = store.put(record)
        newly += status == "stored"
        dedup += status == "unchanged"
        ids.append(t["trial_id"])
    store.verify_complete(ids)
    if register:
        trials.register([{**t, "data_version": dataset.data_version, "market": dataset.meta.market, "symbol": dataset.meta.symbol, "selection_metric": trials.SELECTION_METRIC,
                          "cost_multiplier": 1.0} for t in all_trials], run_id=run_id, protocol_version=protocol_version, synthetic=bool(dataset.meta.synthetic))
    candidates = [{"strategy": n, "status": out[n]["candidate"]["status"], **out[n]["worst_case"]} for n in strategies]
    ranked = [c for c in rank_pass_candidates(candidates)]
    return {"strategies": out, "ranking_among_pass": ranked, "outcome": branch_outcome(ranked),
            "trial_accounting": {"evaluations": evaluations, "trial_ids": len(ids), "unique_trial_ids": len(set(ids)), "series_newly_stored": newly, "series_already_identical": dedup,
                                 "all_series_present": not store.missing(ids)},
            "trial_ids": ids}


def fingerprint(result: dict, store: trial_series.TrialSeriesStore) -> dict:
    """Everything a verification rerun must reproduce exactly."""

    sel = {n: [(f["fold"], f.get("selection", {}).get("chosen_params")) for f in r["folds"]] for n, r in result["strategies"].items()}
    metrics = {n: dict(r["scenario_summaries"]) for n, r in result["strategies"].items()}
    status = {n: r["candidate"] for n, r in result["strategies"].items()}
    series = [hashlib.sha256(store.path(t).read_bytes()).hexdigest() for t in result["trial_ids"]]
    parts = {"selected_parameters": sel, "trial_ids": result["trial_ids"], "series_hashes": series, "metrics": metrics, "statuses": status, "outcome": result["outcome"]}
    return {"parts": {k: _sha(v) for k, v in parts.items()}, "all": _sha(parts)}


# ------------------------------------------------------------------ addendum (pre-performance, immutable)
def build_addendum(*, settings: dict | None = None, now: str | None = None) -> dict:
    verify = pv.verify_frozen()
    if not verify["ok"]:
        raise RuntimeError("frozen Protocol v2 does not verify; no addendum is written")
    reg = registry_read(trials.REGISTRY_NAME)
    v2_trials = sum(1 for r in reg.get("trials", {}).values() if pv.PROTOCOL_VERSION in r.get("protocol_versions", []))
    series_dir = results_root() / "trial_series"
    evidence = {"development_run_dir_exists": RUN_DIR.exists(), "registered_trials_with_protocol_wf-protocol-2": v2_trials,
                "holdout_approval_marker_exists": intraday.APPROVAL_MARKER.exists(), "holdout_unlocked": intraday.holdout_unlocked(),
                "trial_series_files_for_wf-protocol-2": sum(1 for p in series_dir.glob("*.json.gz") if json.loads(gzip.decompress(p.read_bytes())).get("protocol_version") == pv.PROTOCOL_VERSION) if series_dir.exists() else 0,
                "frozen_protocol_verified": verify}
    if evidence["development_run_dir_exists"] or v2_trials or evidence["trial_series_files_for_wf-protocol-2"]:
        raise RuntimeError("a Protocol v2 performance run already exists: the addendum would not be pre-performance")
    settings = settings or write_scenario_settings()
    base = asdict(BacktestConfig())
    for k in ("settings_path", "trade_start", "trade_end"):
        base.pop(k, None)
    body = {
        "id": ADDENDUM_ID, "protocol_version": pv.PROTOCOL_VERSION, "protocol_sha256": verify["protocol_sha256"],
        "created_utc": now or dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
        "nature": "PRE-PERFORMANCE immutable addendum to the frozen Protocol v2; the frozen protocol hash is NOT modified; written before Official Development Run #1",
        "pre_performance_evidence": evidence,
        "ranking_rule_among_PASS_candidates": {
            "scope": "only candidates whose least-favourable S1/S2/S3 status is PASS are compared",
            "order": ["1) higher worst-case stitched OOS net return (minimum over S1/S2/S3)", "2) smaller worst-case max drawdown, absolute value (maximum over S1/S2/S3)",
                      "3) more worst-case OOS closed trades (minimum over S1/S2/S3)", "4) existing strategy declaration order: ma_trend, breakout, mean_reversion"],
            "S0": "never used", "new_metrics_after_results": "forbidden"},
        "metric_definitions": {"stitched_net_return": "product over folds of (1 + fold OOS net return) - 1, each fold starting from the same initial capital",
                               "max_drawdown": "maximum peak-to-trough decline of the stitched OOS equity chain (folds chained by their return ratios)",
                               "oos_closed_trades": "sum of closed round-trip trades over all OOS folds", "active_fold": "OOS fold with at least one closed trade",
                               "train_score": "train-window net return of one configuration under one scenario (existing selection metric)",
                               "benchmark": "aligned passive buy-and-hold, entry open(start+1), cost-free, fully invested, compounded per fold"},
        "execution_contract": {"dataset": "continuous research view (ADMITTED_CONTINUOUS_SUBPERIOD)", "folds": "frozen fold plan (train 4380 / OOS 1095 / step 1095)",
                               "strategies": list(pv.STRATEGY_NAMES), "grids": {n: STRATEGIES[n].param_grid for n in pv.STRATEGY_NAMES},
                               "engine_config": base, "cost_settings": {k: {"sha256": v["sha256"]} for k, v in settings.items()},
                               "cost_settings_rule": "config/settings.yaml with only costs.CRYPTO replaced (taker 0.001, tax 0, fx 0, scenario spread/slippage); every other setting identical",
                               "evaluation_matrix": "train: every grid configuration x S1,S2,S3; OOS: selected parameters x S1,S2,S3 + S0 (diagnostic only)",
                               "failure_handling": "a failed evaluation is a registered trial with status FAILED; a configuration with a missing score in S1/S2/S3 is not rankable; "
                                                   "a fold without any rankable configuration fails and the candidate becomes UNKNOWN",
                               "trial_storage": "every evaluation is registered and its series stored; run completeness is verified",
                               "official_run": "exactly once; results are written immutably; re-running is refused"},
        "determinism": "a verification rerun must reproduce selected parameters, trial ids, series hashes, metrics, statuses and the selected candidate; it is deduplicated, not a new trial",
        "branching": {"A": "no PASS -> RESEARCH_PATH_STOPPED_NO_CANDIDATE; 2026 not opened; no threshold/grid/strategy change",
                      "B": "one PASS -> DEVELOPMENT_SELECTED_CANDIDATE frozen; one-shot holdout evaluator built and tested on synthetic data only",
                      "C": "several PASS -> the ranking rule above selects one; the rest are historical development candidates"},
        "holdout": "2026 values are never read in this work; no approval marker is created; the daily Lockbox is untouched",
        "result_language": {"use": "development evidence", "forbidden_phrases": CLAIM_BAN, "statistical_power": "UNKNOWN (4 folds, about 2 OOS years)"},
        "multiple_testing": "White RC / DSR / PBO are not applied after seeing results",
    }
    digest = _sha(body)
    return {"body": body, "sha256": digest}


def write_addendum(**kwargs) -> dict:
    if ADDENDUM_PATH.exists():
        raise RuntimeError("the addendum already exists and is immutable")
    built = build_addendum(**kwargs)
    with ADDENDUM_PATH.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps({**built["body"], "addendum_sha256": built["sha256"]}, indent=1, ensure_ascii=False, sort_keys=True) + "\n")
    with ADDENDUM_HASH_PATH.open("x", encoding="utf-8") as handle:
        handle.write(f"{built['sha256']}  {ADDENDUM_ID}.json (sha256 of the canonical body without addendum_sha256)\n")
    return {"addendum_sha256": built["sha256"]}


def verify_addendum() -> dict:
    data = json.loads(ADDENDUM_PATH.read_text(encoding="utf-8"))
    recorded = data.pop("addendum_sha256")
    frozen = pv.verify_frozen()
    return {"ok": _sha(data) == recorded == ADDENDUM_HASH_PATH.read_text(encoding="utf-8").split()[0] and data["protocol_sha256"] == frozen["protocol_sha256"] and frozen["ok"],
            "addendum_sha256": recorded, "created_utc": data["created_utc"], "protocol_sha256": data["protocol_sha256"], "protocol_still_verifies": frozen["ok"]}


# ------------------------------------------------------------------ official run
def _write_immutable(path: pathlib.Path, obj) -> None:
    with path.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(obj, indent=1, ensure_ascii=False, sort_keys=True, default=str) + "\n")


def official() -> dict:
    if RUN_DIR.exists():
        raise RuntimeError("Official Development Run #1 already exists; a re-run would be another look")
    frozen, add = pv.verify_frozen(), verify_addendum()
    if not (frozen["ok"] and add["ok"]):
        raise RuntimeError(f"freeze/addendum verification failed: {frozen['checks']} {add}")
    dataset = load_dataset(str(VIEW_CSV))
    adm = check_admission(dataset)
    scope = json.loads((pv.OUT_DIR / "dataset_scope.json").read_text(encoding="utf-8"))
    ident = json.loads((intraday.PROCESSED_DIR / f"{intraday.VIEW_NAME}.view.json").read_text(encoding="utf-8"))
    if not adm["admitted"] or ident["view_normalized_sha256"] != scope["research_view"]["view_normalized_sha256"] or ident["view_data_version"] != scope["research_view"]["view_data_version"]:
        raise RuntimeError(f"the research view does not match the frozen scope or is not admitted: {adm['reasons']}")
    plan = pv.fold_plan(len(dataset.bars))
    if [f["train"] + f["test"] for f in plan["boundaries"]] != [f["train_bars"] + f["test_bars"] for f in scope["fold_plan"]["boundaries"]]:
        raise RuntimeError("fold boundaries differ from the frozen scope")
    settings = {k: v for k, v in json.loads(ADDENDUM_PATH.read_text(encoding="utf-8"))["execution_contract"]["cost_settings"].items()}
    on_disk = write_scenario_settings()
    if {k: v["sha256"] for k, v in on_disk.items()} != {k: v["sha256"] for k, v in settings.items()}:
        raise RuntimeError("scenario settings differ from the addendum")
    store = trial_series.TrialSeriesStore()
    result = run_development(dataset, plan["boundaries"], on_disk, store=store, register=True)
    fp1 = fingerprint(result, store)
    verify_result = run_development(dataset, plan["boundaries"], on_disk, store=store, register=False, run_id=RUN_ID + "-verification")
    fp2 = fingerprint(verify_result, store)
    reg = registry_read(trials.REGISTRY_NAME)
    v2 = [r for r in reg["trials"].values() if pv.PROTOCOL_VERSION in r["protocol_versions"]]
    accounting = {**result["trial_accounting"], "registry_unique_v2_trials": len(v2), "registry_v2_executions": sum(len(r["run_ids"]) for r in v2),
                  "verification_rerun_evaluations": verify_result["trial_accounting"]["evaluations"], "verification_rerun_series_identical": verify_result["trial_accounting"]["series_already_identical"],
                  "verification_rerun_newly_stored": verify_result["trial_accounting"]["series_newly_stored"], "verification_rerun_counts_as_new_trials": False}
    deterministic = fp1 == fp2
    RUN_DIR.mkdir(parents=True)
    meta = {"run_id": RUN_ID, "label": "Protocol v2 Official Development Run #1", "protocol_sha256": frozen["protocol_sha256"], "addendum_sha256": add["addendum_sha256"],
            "view_data_version": ident["view_data_version"], "dataset_data_version": dataset.data_version, "view_normalized_sha256": ident["view_normalized_sha256"],
            "parent_data_version": ident["parent"]["data_version"], "created_utc": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
            "evidence_language": "development evidence", "statistical_power": "UNKNOWN", "holdout_values_accessed": "NO", "S0": "diagnostic only"}
    _write_immutable(RUN_DIR / "results.json", {"meta": meta, "result": {k: v for k, v in result.items() if k != "trial_ids"}, "trial_accounting": accounting})
    _write_immutable(RUN_DIR / "verification_rerun.json", {"deterministic": deterministic, "fingerprint_first": fp1, "fingerprint_rerun": fp2, "rerun_accounting": verify_result["trial_accounting"]})
    _write_immutable(RUN_DIR / "trial_ids.json", {"trial_ids": result["trial_ids"], "sha256": _sha(result["trial_ids"])})
    return {"meta": meta, "outcome": result["outcome"], "deterministic": deterministic, "accounting": accounting,
            "statuses": {n: r["candidate"]["status"] for n, r in result["strategies"].items()}}


# ------------------------------------------------------------------ development-selected candidate (immutable identity)
CANDIDATE_PATH = pv.OUT_DIR / "development_selected_candidate.json"
CANDIDATE_HASH_PATH = pv.OUT_DIR / "development_selected_candidate.sha256"


def _file_sha(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def candidate_identity(run_dir: pathlib.Path = RUN_DIR, store: trial_series.TrialSeriesStore | None = None) -> dict:
    store = store or trial_series.TrialSeriesStore()
    results = json.loads((run_dir / "results.json").read_text(encoding="utf-8"))
    result, meta = results["result"], results["meta"]
    outcome = result["outcome"]
    if outcome["state"] != "DEVELOPMENT_SELECTED_CANDIDATE":
        raise RuntimeError(f"no development-selected candidate (state {outcome['state']})")
    name = outcome["selected"]
    strat = result["strategies"][name]
    ids = json.loads((run_dir / "trial_ids.json").read_text(encoding="utf-8"))["trial_ids"]
    series = [_file_sha(store.path(t)) for t in ids]
    sums = strat["scenario_summaries"]
    return {
        "id": "protocol-v2-development-selected-candidate-1", "state": "DEVELOPMENT_SELECTED_CANDIDATE", "strategy": name,
        "procedure": f"the frozen Protocol v2 selection procedure for {name}: existing grid in bar counts (NEW INTRADAY HORIZON), train ranking by the worst train score across S1/S2/S3, "
                     "OOS under S1/S2/S3, status = least favourable scenario",
        "protocol_sha256": meta["protocol_sha256"], "addendum_sha256": meta["addendum_sha256"], "view_data_version": meta["view_data_version"],
        "view_normalized_sha256": meta["view_normalized_sha256"], "parent_data_version": meta["parent_data_version"],
        "official_run": {"run_id": meta["run_id"], "results_json_sha256": _file_sha(run_dir / "results.json"),
                         "verification_rerun_json_sha256": _file_sha(run_dir / "verification_rerun.json"), "trial_ids_sha256": _sha(ids), "trial_count": len(ids),
                         "series_hashes_sha256": _sha(series), "series_files": len(series)},
        "development_metrics_hashes": {s: _sha(v) for s, v in sums.items()},
        "selected_parameters_by_fold": [{"fold": f["fold"], "params": f["selection"]["chosen_params"]} for f in strat["folds"]],
        "development_status": {"candidate": strat["candidate"]["status"], "per_scenario": strat["candidate"]["per_scenario"], "worst_case": strat["worst_case"]},
        "holdout_eligible_by_frozen_rule": strat["candidate"]["holdout_eligible"],
        "development_vs_aligned_passive": {s: {"stitched_net_return": v["stitched_net_return"], "aligned_passive_stitched_return": v["passive_stitched_return"],
                                                "mean_exposure_time": v["mean_exposure_time"]} for s, v in sums.items() if s in pv.SELECTION_SCENARIOS},
        "ranking_among_pass": result["ranking_among_pass"], "historical_development_candidates": outcome["historical_development_candidates"],
        "other_strategies": {n: r["candidate"]["status"] for n, r in result["strategies"].items() if n != name},
        "statistical_power": "UNKNOWN", "evidence_language": "development evidence",
        "holdout": {"status": "NOT_EVALUATED", "values_accessed": "NO", "approval_marker_exists": intraday.APPROVAL_MARKER.exists(),
                    "needs": "full 2026 verified coverage + holdout evaluation addendum + user approval marker"}}


def freeze_candidate() -> dict:
    if CANDIDATE_PATH.exists():
        raise RuntimeError("the development-selected candidate is already frozen")
    body = candidate_identity()
    digest = _sha(body)
    with CANDIDATE_PATH.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps({**body, "candidate_sha256": digest}, indent=1, ensure_ascii=False, sort_keys=True, default=str) + "\n")
    with CANDIDATE_HASH_PATH.open("x", encoding="utf-8") as handle:
        handle.write(f"{digest}  development_selected_candidate.json (sha256 of the canonical body without candidate_sha256)\n")
    return {"candidate_sha256": digest, "strategy": body["strategy"]}


def verify_candidate(path: pathlib.Path = CANDIDATE_PATH, run_dir: pathlib.Path = RUN_DIR, store: trial_series.TrialSeriesStore | None = None) -> dict:
    """Candidate file hash + every upstream artifact it points at (results, rerun, trial ids, series, protocol, addendum)."""

    data = json.loads(path.read_text(encoding="utf-8"))
    recorded = data.pop("candidate_sha256")
    fresh = candidate_identity(run_dir, store)
    frozen, add = pv.verify_frozen(), verify_addendum()
    checks = {"candidate_hash": _sha(data) == recorded, "matches_recomputed_identity": _sha({k: v for k, v in fresh.items() if k != "holdout"}) == _sha({k: v for k, v in data.items() if k != "holdout"}),
              "protocol_verifies": frozen["ok"] and data["protocol_sha256"] == frozen["protocol_sha256"], "addendum_verifies": add["ok"] and data["addendum_sha256"] == add["addendum_sha256"]}
    return {"ok": all(checks.values()), "candidate_sha256": recorded, "checks": checks}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m qat.research.protocol_v2_run")
    parser.add_argument("cmd", choices=["addendum", "verify-addendum", "official", "freeze-candidate", "verify-candidate"])
    args = parser.parse_args(argv)
    if args.cmd == "freeze-candidate":
        print(json.dumps(freeze_candidate(), indent=1))
    elif args.cmd == "verify-candidate":
        print(json.dumps(verify_candidate(), indent=1))
    elif args.cmd == "addendum":
        print(json.dumps(write_addendum(), indent=1))
    elif args.cmd == "verify-addendum":
        print(json.dumps(verify_addendum(), indent=1))
    else:
        print(json.dumps(official(), indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
