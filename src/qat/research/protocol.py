"""Batch #3B - frozen real-data Walk-Forward research protocol.

The protocol is DATA, fixed in this module and written (with its SHA-256) to
``artifacts/verification/walkforward_3b/protocol.json`` BEFORE any result exists. It only
re-uses the existing engine: ``run_walkforward`` (rolling folds, grid selection on the train
window only, OOS evaluation only), the existing metrics, strategies, search spaces and the
placeholder cost model. Nothing here tunes anything or reads a Lockbox.

Lockbox: the final ``lockbox_bars`` of every dataset are excluded from all folds and from
every engine call (``trade_end <= development end``). ``evaluate_lockbox`` and
``results/lockbox_registry.json`` are never touched; this module only decides ELIGIBILITY.

Status vocabulary (market-specific Walk-Forward, per market x strategy):
  UNKNOWN               a fold failed / fewer than ``min_folds`` / dataset not admitted / metric missing
  INSUFFICIENT_ACTIVITY OOS closed trades < ``min_total_oos_trades`` or active folds below the fraction
  FAIL                  stitched OOS net return after costs <= 0
  UNSTABLE              profitable but positive folds not a strict majority, or the 2x cost stress is not
  PASS                  all of the above satisfied. PASS is NOT proof of profitability.
Lockbox eligibility additionally requires beating the passive (buy-and-hold, cost-free) return over
the same OOS windows. No multiple-testing correction exists: 9 candidates are tested (UNKNOWN effect).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import statistics
from dataclasses import asdict

from qat.config import cost_models_from_settings, load_research_settings
from qat.data.bars import DataRejected, resolve_timezone
from qat.data.loader import load_dataset
from qat.realdata.admission import check_admission, require_coverage
from qat.research.backtest import BacktestConfig
from qat.research.manifest import PROJECT_ROOT
from qat.research.metrics import MIN_TRADES_FOR_STATS
from qat.research.store import results_root
from qat.research.strategies import STRATEGIES
from qat.research.walkforward import WalkForwardConfig, run_walkforward

PROTOCOL_VERSION = "wf-protocol-1"
OUT_DIR = PROJECT_ROOT / "artifacts" / "verification" / "walkforward_3b"
SETTINGS_PATH = PROJECT_ROOT / "config" / "settings.yaml"
STRATEGY_NAMES = ("ma_trend", "breakout", "mean_reversion")
STRESS_MULTIPLIERS = (2.0, 3.0)
MARKETS = {
    "KR": "data/processed/real/005930_data.go.kr_1d.csv",
    "US": "data/processed/real/AAPL_yahoo-chart_1d.csv",
    "CRYPTO": "data/processed/real/BTCUSDT_binance-vision_1d.csv",
}
# (train, test, lockbox) bars = 2.0 / 0.5 / 1.0 years of daily bars (252 per year KR/US, 365 crypto)
WINDOWS = {"KR": (504, 126, 252), "US": (504, 126, 252), "CRYPTO": (730, 183, 365)}
THRESHOLDS = {
    "min_folds": 3,
    "min_total_oos_trades": MIN_TRADES_FOR_STATS,  # the existing sample-size warning threshold
    "min_active_fold_fraction": 0.5,  # active = at least one closed OOS trade
    "positive_fold_fraction_must_exceed": 0.5,
    "pass_requires_cost_stress_multiplier": 2.0,
    "lockbox_requires": "PASS and stitched OOS net return > passive buy-and-hold return over the same OOS windows",
}


# ------------------------------------------------------------------ frozen definition
def _sha(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")).hexdigest()


def protocol_definition() -> dict:
    settings = load_research_settings(str(SETTINGS_PATH))
    costs = {m.value: asdict(cm) for m, cm in cost_models_from_settings(settings).items()}
    base = asdict(BacktestConfig())
    base.pop("settings_path", None)
    body = {
        "version": PROTOCOL_VERSION,
        "purpose": "Walk-Forward development/OOS research on verified real data; Lockbox NOT opened",
        "markets": {m: {"dataset": p, "train_bars": WINDOWS[m][0], "test_bars": WINDOWS[m][1], "lockbox_bars": WINDOWS[m][2]}
                    for m, p in MARKETS.items()},
        "fold_design": {"window": "rolling, fixed-length train", "step": "= test_bars (non-overlapping OOS)",
                        "train_selection": "max train net_return over the existing grid; ties -> grid order (existing contract)",
                        "oos": "evaluation only; parameters of a fold are never changed after its OOS is seen",
                        "boundary": "train=[s,s+train) test=[s+train,s+train+test); history before a window is warm-up only",
                        "warmup": "strategies see bars before trade_start for indicators; signals start at trade_start",
                        "min_bars": "train+test+lockbox must fit, else ValueError (no fold is invented)",
                        "lockbox": "final lockbox_bars excluded from folds and engine calls; never evaluated in this batch"},
        "market_scopes": {"market_specific": "PRIMARY: each market's own verified coverage (start_bar=0)",
                          "common_period": "SECONDARY: intersection of verified coverages; reported separately, never mixed"},
        "search_space": {n: STRATEGIES[n].param_grid for n in STRATEGY_NAMES},
        "strategy_versions": {n: STRATEGIES[n].version for n in STRATEGY_NAMES},
        "base_config": base,
        "cost_model": {"settings_sha256": hashlib.sha256(SETTINGS_PATH.read_bytes()).hexdigest(),
                       "status": "PLACEHOLDER numbers (config/settings.yaml): not calibrated to any broker; NOT tuned here",
                       "per_market": costs},
        "cost_stress_multipliers": list(STRESS_MULTIPLIERS),
        "metrics": ["net_return", "max_drawdown_pct", "trades_closed", "win_rate", "avg_win", "avg_loss", "turnover",
                    "explicit_costs", "slippage_estimate", "exposure_time", "rejections", "per-fold performance"],
        "benchmark": "passive buy-and-hold: open(first OOS bar) -> close(last OOS bar), no costs (favours the benchmark)",
        "thresholds": THRESHOLDS,
        "statuses": ["PASS", "FAIL", "INSUFFICIENT_ACTIVITY", "UNSTABLE", "UNKNOWN"],
        "tie_handling": "grid order (first wins)",
        "determinism": "every primary run is repeated (save=False) and must reproduce fold params, metrics and equity exactly",
        "strategy_state": "strategy.reset() before every engine run (existing contract)",
        "data_identity": "each run manifest carries provider, symbol, data_version, raw/normalized hashes, verified coverage, research window",
        "kr_coverage": "KR requests outside 2020-01-02..2025-12-30 are rejected (DataRejected); no Yahoo/Naver fallback",
        "multiple_testing": "UNKNOWN: no correction; 3 strategies x 3 markets are tested once each",
    }
    return {**body, "protocol_sha256": _sha(body)}


# ------------------------------------------------------------------ pure helpers
def local_dates(dataset) -> list[str]:
    tz = resolve_timezone(dataset.meta.timezone)
    return [b.ts.astimezone(tz).date().isoformat() for b in dataset.bars]


def verified_coverage(dataset) -> tuple[str, str]:
    adm = check_admission(dataset)
    if not adm.get("admitted") or "covered_start" not in adm:
        raise DataRejected(f"dataset {dataset.meta.symbol} has no verified coverage: {adm.get('reasons')}")
    return adm["covered_start"], adm["covered_end"]


def common_period(coverages: dict[str, tuple[str, str]]) -> tuple[str, str] | None:
    start, end = max(c[0] for c in coverages.values()), min(c[1] for c in coverages.values())
    return (start, end) if start <= end else None


def first_index_on_or_after(dates: list[str], day: str) -> int:
    for i, d in enumerate(dates):
        if d >= day:
            return i
    raise ValueError(f"no bar on or after {day}")


def passive_return(bars, test: list[int]) -> float:
    return bars[test[1] - 1].close / bars[test[0]].open - 1


def _stitched_drawdown(folds: list[dict], initial: float) -> float | None:
    level, peak, worst, seen = 1.0, 1.0, 0.0, False
    for f in folds:
        eq = [p["equity"] for p in f.get("oos_equity", []) if p["equity"] is not None]
        for value in eq:
            seen = True
            cur = level * value / initial
            peak = max(peak, cur)
            worst = min(worst, cur / peak - 1)
        if eq:
            level *= eq[-1] / initial
    return worst if seen else None


def summarize(run: dict, dataset, dates: list[str], initial: float) -> dict:
    """Fold table + aggregates from a ``run_walkforward`` result (pure; no engine call)."""

    folds = run["result"]["folds"]
    rows, ok = [], [f for f in folds if f["status"] == "OK" and f["oos_metrics"]["net_return"] is not None]
    for f in folds:
        m = f.get("oos_metrics") or {}
        rows.append({
            "fold": f["fold"], "status": f["status"], "train": f["train"], "test": f["test"],
            "train_dates": [dates[f["train"][0]], dates[f["train"][1] - 1]], "test_dates": [dates[f["test"][0]], dates[f["test"][1] - 1]],
            "chosen_params": f.get("chosen_params"), "net_return": m.get("net_return"), "max_drawdown_pct": m.get("max_drawdown_pct"),
            "trades_closed": m.get("trades_closed"), "win_rate": m.get("win_rate"), "avg_win": m.get("avg_win"), "avg_loss": m.get("avg_loss"),
            "turnover": m.get("turnover"), "explicit_costs": m.get("explicit_costs"), "slippage_estimate": m.get("slippage_estimate"),
            "exposure_time": m.get("exposure_time"), "rejections": m.get("rejections"),
            "passive_return": passive_return(dataset.bars, f["test"]), "error": f.get("error")})
    returns = [r["net_return"] for r in rows if r["net_return"] is not None]
    trades = sum(r["trades_closed"] or 0 for r in rows)
    passive = 1.0
    for r in rows:
        passive *= 1 + r["passive_return"]
    wins = sum((r["win_rate"] or 0) * (r["trades_closed"] or 0) for r in rows)
    return {
        "folds": len(folds), "folds_ok": len(ok), "folds_failed": len(folds) - len(ok), "rows": rows,
        "stitched_net_return": run["metrics"]["oos_stitched_return"], "positive_folds": sum(1 for v in returns if v > 0),
        "active_folds": sum(1 for r in rows if (r["trades_closed"] or 0) >= 1), "total_oos_trades": trades,
        "win_rate_weighted": (wins / trades) if trades else None,
        "mean_exposure_time": statistics.fmean([r["exposure_time"] for r in rows if r["exposure_time"] is not None]) if rows else None,
        "total_turnover": sum(r["turnover"] or 0 for r in rows), "total_explicit_costs": sum(r["explicit_costs"] or 0 for r in rows),
        "total_slippage_estimate": sum(r["slippage_estimate"] or 0 for r in rows), "total_rejections": sum(r["rejections"] or 0 for r in rows),
        "fold_return_stdev": statistics.pstdev(returns) if len(returns) > 1 else None,
        "worst_fold_return": min(returns) if returns else None, "best_fold_return": max(returns) if returns else None,
        "stitched_max_drawdown": _stitched_drawdown(folds, initial), "passive_stitched_return": passive - 1,
    }


def classify(summary: dict, stress_returns: dict[float, float | None], *, admitted: bool = True) -> dict:
    """Status per the frozen thresholds (pure). ``stress_returns``: cost multiplier -> stitched OOS net return."""

    t = THRESHOLDS
    n, ok = summary["folds"], summary["folds_ok"]
    stitched = summary["stitched_net_return"]
    active_frac = summary["active_folds"] / n if n else 0.0
    pos_frac = summary["positive_folds"] / n if n else 0.0
    stress = stress_returns.get(t["pass_requires_cost_stress_multiplier"])
    criteria = {
        "integrity": admitted and ok == n and n >= t["min_folds"] and stitched is not None,
        "activity": summary["total_oos_trades"] >= t["min_total_oos_trades"] and active_frac >= t["min_active_fold_fraction"],
        "profitable_after_costs": stitched is not None and stitched > 0,
        "positive_fold_majority": pos_frac > t["positive_fold_fraction_must_exceed"],
        "survives_cost_stress": stress is not None and stress > 0,
        "beats_passive": stitched is not None and stitched > summary["passive_stitched_return"],
    }
    if not criteria["integrity"]:
        status = "UNKNOWN"
    elif not criteria["activity"]:
        status = "INSUFFICIENT_ACTIVITY"
    elif not criteria["profitable_after_costs"]:
        status = "FAIL"
    elif not (criteria["positive_fold_majority"] and criteria["survives_cost_stress"]):
        status = "UNSTABLE"
    else:
        status = "PASS"
    return {"status": status, "criteria": criteria, "active_fold_fraction": active_frac, "positive_fold_fraction": pos_frac,
            "lockbox_candidate": status == "PASS" and criteria["beats_passive"]}


def _comparable(result: dict) -> str:
    keep = [{k: f.get(k) for k in ("fold", "status", "train", "test", "chosen_params", "calibration", "oos_metrics", "oos_equity")}
            for f in result["folds"]]
    return _sha(keep)


# ------------------------------------------------------------------ runner
def _identity(dataset) -> dict:
    marker = dataset.meta.extra.get("real_data") or {}
    ident = json.loads((pathlib.Path(dataset.path).parent / marker["identity_file"]).read_text(encoding="utf-8"))
    return {"provider": ident["provider"], "symbol": ident["symbol"], "market": ident["market"], "data_version": ident["data_version"],
            "raw_set_sha256": ident["raw_set_sha256"], "normalized_sha256": ident["normalized_sha256"],
            "verified_coverage": [ident["date_range"]["first"], ident["date_range"]["last"]], "rows": ident["rows"],
            "validation_status": ident["validation_status"], "adjustment_assurance": ident["adjustment"].get("assurance"),
            "calendar_sha256": ident["calendar"]["sha256"]}


def _run(dataset, name, market, *, start_bar=0, multiplier=1.0, role, scope, proto, identity, window, save=True):
    train, test, lockbox = WINDOWS[market]
    base = BacktestConfig(settings_path=str(SETTINGS_PATH), cost_multiplier=multiplier)
    cfg = WalkForwardConfig(train_bars=train, test_bars=test, lockbox_bars=lockbox, start_bar=start_bar, base=base)
    extra = {"research_protocol": {"version": proto["version"], "sha256": proto["protocol_sha256"], "role": role, "market_scope": scope},
             "data_identity": identity, "research_window": window}
    return run_walkforward(dataset, name, cfg, save=save, extra_manifest=extra), base


def freeze_protocol(out_dir: pathlib.Path | None = None) -> dict:
    """Write protocol.json (idempotent). A different frozen protocol under the same version is refused."""

    out_dir = pathlib.Path(out_dir or OUT_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    proto = protocol_definition()
    frozen = out_dir / "protocol.json"
    if frozen.exists():
        if json.loads(frozen.read_text(encoding="utf-8"))["protocol_sha256"] != proto["protocol_sha256"]:
            raise RuntimeError("the frozen protocol differs from the code; a protocol change needs a new PROTOCOL_VERSION")
    else:
        frozen.write_text(json.dumps(proto, indent=1, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    return proto


def run_protocol(*, markets=None, out_dir: pathlib.Path | None = None, repo_root: pathlib.Path = PROJECT_ROOT) -> dict:
    out_dir = pathlib.Path(out_dir or OUT_DIR)
    proto = freeze_protocol(out_dir)
    if (out_dir / "summary.json").exists():
        raise RuntimeError("a result already exists for this protocol version: re-running would be another OOS look (refused)")
    lockbox_registry = results_root() / "lockbox_registry.json"
    if lockbox_registry.exists():
        raise RuntimeError("lockbox registry already exists: Lockbox was used before this batch started")

    chosen = list(markets or MARKETS)
    datasets, coverages, dates_by, ident_by = {}, {}, {}, {}
    for market in chosen:
        ds = load_dataset(str(repo_root / MARKETS[market]))
        coverages[market] = verified_coverage(ds)
        require_coverage(ds, *coverages[market])  # the whole verified interval: admitted; anything outside is DataRejected
        datasets[market], dates_by[market], ident_by[market] = ds, local_dates(ds), _identity(ds)
    common = common_period(coverages) if len(chosen) > 1 else None

    results = {}
    for market in chosen:
        ds, dates = datasets[market], dates_by[market]
        train, test, lockbox = WINDOWS[market]
        n = len(ds.bars)
        dev_end = n - lockbox
        window = {"market": market, "development_bars": [0, dev_end], "development_dates": [dates[0], dates[dev_end - 1]],
                  "lockbox_range_bars": [dev_end, n], "lockbox_dates_RESERVED_NOT_READ": [dates[dev_end], dates[n - 1]],
                  "verified_coverage": list(coverages[market])}
        common_start = first_index_on_or_after(dates, common[0]) if common else None
        for name in STRATEGY_NAMES:
            entry = {"market": market, "strategy": name, "window": window}
            primary, base = _run(ds, name, market, role="primary", scope="market_specific", proto=proto, identity=ident_by[market], window=window)
            entry["market_specific"] = {"run_id": primary["manifest"]["run_id"], **summarize(primary, ds, dates, base.initial_cash)}
            stress = {}
            for mult in STRESS_MULTIPLIERS:
                out, b = _run(ds, name, market, multiplier=mult, role=f"cost_stress_{mult:g}x", scope="market_specific", proto=proto,
                              identity=ident_by[market], window=window)
                stress[mult] = {"run_id": out["manifest"]["run_id"], **summarize(out, ds, dates, b.initial_cash)}
            entry["cost_stress"] = {f"{m:g}x": v for m, v in stress.items()}
            entry["market_specific"]["decision"] = classify(entry["market_specific"], {m: v["stitched_net_return"] for m, v in stress.items()})
            if common and common_start:
                cwin = {**window, "common_period": list(common), "start_bar": common_start}
                out, b = _run(ds, name, market, start_bar=common_start, role="primary", scope="common_period", proto=proto,
                              identity=ident_by[market], window=cwin)
                entry["common_period"] = {"run_id": out["manifest"]["run_id"], "period": list(common), **summarize(out, ds, dates, b.initial_cash)}
            elif common:
                entry["common_period"] = {"same_as_market_specific": True, "run_id": primary["manifest"]["run_id"], "period": list(common)}
            repeat, _ = _run(ds, name, market, role="determinism_repeat", scope="market_specific", proto=proto, identity=ident_by[market],
                             window=window, save=False)
            entry["determinism"] = {"identical": _comparable(repeat["result"]) == _comparable(primary["result"]),
                                    "fold_hash": _comparable(primary["result"])}
            results[f"{market}/{name}"] = entry

    eligible = [k for k, v in results.items() if v["market_specific"]["decision"]["lockbox_candidate"]]
    robustness = {name: [m for m in chosen if results[f"{m}/{name}"]["market_specific"]["decision"]["status"] == "PASS"] for name in STRATEGY_NAMES}
    summary = {
        "protocol_version": proto["version"], "protocol_sha256": proto["protocol_sha256"], "markets": chosen,
        "datasets": ident_by, "verified_coverage": {m: list(c) for m, c in coverages.items()},
        "common_period": list(common) if common else None, "results": results,
        "lockbox": {"opened": False, "registry_exists": lockbox_registry.exists(), "eligible_candidates": eligible,
                    "decision": "LOCKBOX_ELIGIBLE" if eligible else "NOT_READY_FOR_LOCKBOX"},
        "cross_market_robustness": {n: {"pass_markets": m, "pass_count": len(m)} for n, m in robustness.items()},
        "all_runs_deterministic": all(v["determinism"]["identical"] for v in results.values()),
        "candidates_tested": len(results),
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=1, ensure_ascii=False, sort_keys=True, default=str) + "\n", encoding="utf-8")
    return summary


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m qat.research.protocol")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("show")
    sub.add_parser("freeze")
    sub.add_parser("run")
    args = parser.parse_args(argv)
    if args.cmd == "freeze":
        print("frozen:", freeze_protocol()["protocol_sha256"])
        return 0
    if args.cmd == "show":
        print(json.dumps(protocol_definition(), indent=1, ensure_ascii=False))
        return 0
    summary = run_protocol()
    print(json.dumps({"lockbox": summary["lockbox"], "deterministic": summary["all_runs_deterministic"],
                      "status": {k: v["market_specific"]["decision"]["status"] for k, v in summary["results"].items()}}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
