"""Walk-forward, lockbox and cost-stress runners (Batch #2).

Walk-forward: for each fold, candidate parameters from a small fixed grid are
backtested on the TRAIN window only; the best train ``net_return`` (ties -> grid
order) is then run once on the following OOS TEST window; the window rolls by
``step_bars``. Each backtest receives only bars up to the end of its own window,
so no later bar exists inside the engine. Bars before a window are usable as
indicator history (they are in the past); trades are restricted to the window.
Each fold starts from fresh capital; there is no carried portfolio state.

OOS independence: every walk-forward on the same (data_version, strategy) is
counted in ``results/oos_registry.json``; if earlier evaluations exist the run is
labelled ``independent_oos: false`` - re-looking at OOS while tuning makes it
in-sample. The final ``lockbox_bars`` are excluded from all folds and can be
evaluated once via :func:`evaluate_lockbox`; a second evaluation is refused
unless explicitly acknowledged, and is then labelled non-independent.
"""

from __future__ import annotations

import itertools
import statistics
import traceback
from dataclasses import asdict, dataclass, field, replace

from qat.data.bars import DataRejected
from qat.data.loader import Dataset
from qat.realdata.admission import require_admitted
from qat.research.backtest import BacktestConfig, run_backtest
from qat.research import trials
from qat.research.manifest import research_universe, sha256_of
from qat.research.store import (
    load_run,
    registry_read,
    registry_write,
    save_backtest,
    save_composite,
)
from qat.research.strategies import STRATEGIES, make_strategy

MAX_GRID = 12  # no large parameter sweeps in Batch #2


class LockboxAlreadyUsed(RuntimeError):
    pass


@dataclass
class WalkForwardConfig:
    train_bars: int = 250
    test_bars: int = 60
    step_bars: int | None = None
    lockbox_bars: int = 0
    start_bar: int = 0  # first bar of the development window (default: the dataset start); never inside the lockbox
    grid: dict | None = None
    base: BacktestConfig = field(default_factory=BacktestConfig)

    def folds(self, n_bars: int) -> list[dict]:
        step = self.step_bars or self.test_bars
        if min(self.train_bars, self.test_bars, step) <= 0 or self.lockbox_bars < 0 or self.start_bar < 0:
            raise ValueError("train/test/step must be > 0 and lockbox/start_bar >= 0")
        usable = n_bars - self.lockbox_bars
        out, start = [], self.start_bar
        while start + self.train_bars + self.test_bars <= usable:
            train_end = start + self.train_bars
            out.append({"fold": len(out), "train": [start, train_end],
                        "test": [train_end, train_end + self.test_bars]})
            start += step
        if not out:
            raise ValueError("not enough bars for a single train+test fold")
        return out


def _grid(strategy_name: str, grid: dict | None, fixed: dict) -> list[dict]:
    grid = grid if grid is not None else STRATEGIES[strategy_name].param_grid
    keys = sorted(grid)
    combos = [dict(zip(keys, values)) for values in itertools.product(*(grid[k] for k in keys))] or [{}]
    if len(combos) > MAX_GRID:
        raise ValueError(f"grid has {len(combos)} combinations > {MAX_GRID} (large sweeps are out of scope)")
    return [{**fixed, **c} for c in combos]


def _calibrate(dataset: Dataset, name: str, candidates: list[dict], base: BacktestConfig,
               train: list[int], sink: list | None = None) -> tuple[dict, list[dict]]:
    rows = []
    for params in candidates:
        cfg = replace(base, trade_start=train[0], trade_end=train[1])
        try:
            res = run_backtest(dataset, make_strategy(name, **params), cfg)
            rows.append({"params": params, "net_return": res.metrics["net_return"],
                         "trades_closed": res.metrics["trades_closed"], "status": "OK"})
            if sink is not None:  # per-trial series (Batch #3D): every candidate, not only the winner
                sink.append({"params": params, "window": list(train), "equity": res.equity})
        except Exception as exc:  # noqa: BLE001 - keep failed candidates visible
            rows.append({"params": params, "net_return": None, "status": "FAIL", "error": repr(exc)})
            if sink is not None:
                sink.append({"params": params, "window": list(train), "equity": None})
    scored = [r for r in rows if r["net_return"] is not None]
    if not scored:
        raise RuntimeError("no calibratable candidate on the train window")
    best = max(scored, key=lambda r: r["net_return"])  # max keeps first on ties
    return best["params"], rows


def run_walkforward(dataset: Dataset, strategy_name: str, config: WalkForwardConfig,
                    fixed_params: dict | None = None, *, save: bool = True, extra_manifest: dict | None = None,
                    series_store=None) -> dict:
    if not dataset.usable:
        raise DataRejected(f"dataset {dataset.data_version} failed validation: research BLOCKED")
    require_admitted(dataset)
    fixed_params = dict(fixed_params or {})
    folds = config.folds(len(dataset.bars))
    candidates = _grid(strategy_name, config.grid, fixed_params)
    registry = registry_read("oos_registry.json")
    reg_key = f"{dataset.data_version}|{strategy_name}"
    prior = registry.get(reg_key, [])

    fold_rows = []
    fold_sinks: dict = {}
    for fold in folds:
        row = {**fold, "status": "OK"}
        sink = fold_sinks.setdefault(fold["fold"], []) if series_store is not None else None
        try:
            chosen, calib = _calibrate(dataset, strategy_name, candidates, config.base, fold["train"], **({"sink": sink} if sink is not None else {}))
            row["calibration"] = calib
            row["chosen_params"] = chosen
            cfg = replace(config.base, trade_start=fold["test"][0], trade_end=fold["test"][1])
            oos = run_backtest(dataset, make_strategy(strategy_name, **chosen), cfg)
            if sink is not None:
                sink.append({"params": chosen, "window": list(fold["test"]), "equity": oos.equity, "stage": "oos_evaluation"})
            m = oos.metrics
            row["oos_metrics"] = m
            row["oos_period"] = [oos.equity[0]["timestamp"], oos.equity[-1]["timestamp"]] if oos.equity else None
            row["oos_equity"] = [{"timestamp": p["timestamp"], "equity": p["equity"]} for p in oos.equity]
            row["oos_warnings"] = oos.warnings
        except Exception as exc:  # noqa: BLE001 - failed folds are preserved, not hidden
            row["status"] = "FAIL"
            row["error"] = repr(exc)
            row["traceback"] = traceback.format_exc(limit=3)
        fold_rows.append(row)

    settings_sha = trials.settings_sha256(config.base.settings_path)
    trial_rows = trials.collect(  # Batch #3C: every evaluated candidate is a trial with a deterministic id
        data_version=dataset.data_version, market=dataset.meta.market, symbol=dataset.meta.symbol, strategy=strategy_name,
        strategy_version=STRATEGIES[strategy_name].version, base_config=asdict(config.base), settings_sha=settings_sha,
        fold_rows=fold_rows)
    if series_store is not None:
        from qat.research import trial_series

        trial_series.store_run(series_store, dataset=dataset, strategy_name=strategy_name, base_config=asdict(config.base), settings_sha=settings_sha,
                               fold_sinks=fold_sinks, initial_cash=config.base.initial_cash, trial_rows=trial_rows,
                               protocol_version=((extra_manifest or {}).get("research_protocol") or {}).get("version"))
    ok = [r for r in fold_rows if r["status"] == "OK" and r["oos_metrics"]["net_return"] is not None]
    returns = [r["oos_metrics"]["net_return"] for r in ok]
    stitched = 1.0
    for value in returns:
        stitched *= 1 + value
    n = len(dataset.bars)
    lockbox = [n - config.lockbox_bars, n] if config.lockbox_bars else None
    independent = not prior
    metrics = {
        "headline": "walk-forward OOS",
        "folds": len(fold_rows),
        "folds_failed": sum(1 for r in fold_rows if r["status"] == "FAIL"),
        "oos_fold_returns": returns,
        "oos_mean_return": statistics.fmean(returns) if returns else None,
        "oos_median_return": statistics.median(returns) if returns else None,
        "oos_positive_folds": sum(1 for v in returns if v > 0),
        "oos_stitched_return": stitched - 1 if returns else None,
        "oos_trades_closed": sum(r["oos_metrics"]["trades_closed"] for r in ok),
        "net_return": stitched - 1 if returns else None,
        "independent_oos": independent,
        "prior_oos_evaluations": len(prior),
        "notes": ([] if independent else
                  [f"{len(prior)} earlier walk-forward run(s) on this data+strategy: OOS is NOT independent"])
                 + (["SYNTHETIC data: says nothing about real Net Alpha"] if dataset.meta.synthetic else []),
    }
    manifest_fields = {
        "strategy_name": strategy_name,
        "strategy": {"name": strategy_name, "version": STRATEGIES[strategy_name].version,
                     "params": fixed_params},
        "grid": config.grid if config.grid is not None else STRATEGIES[strategy_name].param_grid,
        "data": dataset.summary(),
        "market": dataset.meta.market, "symbol": dataset.meta.symbol,
        "timeframe": dataset.meta.timeframe, "currency": dataset.meta.currency,
        "synthetic_data": dataset.meta.synthetic,
        "labels": {"synthetic_data": dataset.meta.synthetic, "data_source": dataset.meta.source},
        "universe": research_universe(dataset),
        "walkforward": {"train_bars": config.train_bars, "test_bars": config.test_bars,
                        "step_bars": config.step_bars or config.test_bars,
                        "start_bar": config.start_bar, "window": "rolling (fixed-length train)",
                        "lockbox_bars": config.lockbox_bars, "lockbox_range": lockbox,
                        "lockbox_status": "RESERVED_UNUSED" if lockbox else "NONE",
                        "selection": "max train net_return, ties -> grid order"},
        "fold_periods": [{"fold": r["fold"], "train": r["train"], "test": r["test"]} for r in fold_rows],
        "base_config": asdict(config.base),
        "initial_capital": config.base.initial_cash,
        "seed": config.base.seed,
        "independent_oos": independent,
        "prior_oos_evaluations": [p["run_id"] for p in prior],
        "trial_context": {"schema": trials.TRIAL_SCHEMA, "selection_metric": trials.SELECTION_METRIC,
                          "trial_count": len(trial_rows), "settings_sha256": settings_sha,
                          "trial_ids": [x["trial_id"] for x in trial_rows]},
        **(extra_manifest or {}),
    }
    result = {"folds": fold_rows, "metrics": metrics}
    if not save:
        return {"manifest": manifest_fields, "metrics": metrics, "result": result}
    manifest = save_composite("walkforward", manifest_fields, metrics, result)
    registry.setdefault(reg_key, []).append({"run_id": manifest["run_id"], "created_utc": manifest["created_utc"]})
    registry_write("oos_registry.json", registry)
    trials.register(trial_rows, run_id=manifest["run_id"], synthetic=bool(dataset.meta.synthetic),
                    protocol_version=((extra_manifest or {}).get("research_protocol") or {}).get("version"))
    return {"manifest": manifest, "metrics": metrics, "result": result}


def _overlapping_lockbox_uses(registry: dict, data_version: str, lo: int, hi: int) -> list[dict]:
    """Earlier lockbox evaluations on the same data whose bar range overlaps
    [lo, hi) - regardless of strategy or of the exact range chosen. (Audit fix D8:
    the registry used to be keyed by the exact range, so reserving a slightly
    different lockbox re-opened bars that had already been looked at.)"""

    uses: list[dict] = []
    for key, entries in registry.items():
        parts = key.split("|")
        if len(parts) != 3 or parts[0] != data_version:
            continue
        try:
            other_lo, other_hi = (int(x) for x in parts[2].split("-"))
        except ValueError:
            continue
        if lo < other_hi and other_lo < hi:
            uses.extend(entries)
    return uses


def evaluate_lockbox(dataset: Dataset, walkforward_run_id: str, *, acknowledge_reuse: bool = False) -> dict:
    parent = load_run(walkforward_run_id)["manifest"]
    wf = parent["walkforward"]
    if not wf.get("lockbox_range"):
        raise ValueError("this walk-forward run reserved no lockbox")
    if parent["data"]["data_version"] != dataset.data_version:
        raise ValueError("dataset differs from the walk-forward run's data_version")
    lo, hi = wf["lockbox_range"]
    name = parent["strategy_name"]
    key = f"{dataset.data_version}|{name}|{lo}-{hi}"
    registry = registry_read("lockbox_registry.json")
    previous = _overlapping_lockbox_uses(registry, dataset.data_version, lo, hi)
    if previous and not acknowledge_reuse:
        raise LockboxAlreadyUsed(
            f"lockbox {lo}-{hi} overlaps bars already evaluated by {previous[0]['run_id']}")
    base = BacktestConfig(**parent["base_config"])
    candidates = _grid(name, parent["grid"], parent["strategy"]["params"])
    chosen, calib = _calibrate(dataset, name, candidates, base, [max(0, lo - wf["train_bars"]), lo])
    res = run_backtest(dataset, make_strategy(name, **chosen), replace(base, trade_start=lo, trade_end=hi))
    independent = not previous
    res.warnings.append("LOCKBOX evaluation" + ("" if independent else " (REUSED - not independent)"))
    manifest = save_backtest(res, kind="lockbox", extra={
        "parent_walkforward": walkforward_run_id, "lockbox_range": [lo, hi],
        "lockbox_independent": independent, "lockbox_previous_uses": [p["run_id"] for p in previous],
        "calibration": calib, "chosen_params": chosen,
    })
    registry.setdefault(key, []).append({"run_id": manifest["run_id"], "created_utc": manifest["created_utc"]})
    registry_write("lockbox_registry.json", registry)
    return {"manifest": manifest, "metrics": res.metrics, "independent": independent}


def run_cost_stress(dataset: Dataset, strategy_name: str, params: dict, base: BacktestConfig,
                    multipliers=(1.0, 2.0, 3.0), *, save: bool = True) -> dict:
    require_admitted(dataset)
    rows = []
    for mult in multipliers:
        res = run_backtest(dataset, make_strategy(strategy_name, **params), replace(base, cost_multiplier=mult))
        m = res.metrics
        rows.append({"cost_multiplier": mult, **{k: m[k] for k in (
            "net_pnl", "net_return", "gross_pnl", "explicit_costs", "slippage_estimate", "fills",
            "trades_closed", "win_rate", "profit_factor", "max_drawdown_pct", "rejections")},
            "rejections_by_stage": m["rejections_by_stage"], "notes": m["notes"]})
    metrics = {"headline": "cost stress", "rows": rows,
               "net_return": rows[0]["net_return"] if rows else None,
               "note": "multiplier scales commission, sell tax, half-spread, slippage and fx bps "
                       "in both the Net Alpha estimate and the paper fills"}
    manifest_fields = {
        "strategy_name": strategy_name,
        "strategy": {"name": strategy_name, "version": STRATEGIES[strategy_name].version, "params": params},
        "data": dataset.summary(), "market": dataset.meta.market, "symbol": dataset.meta.symbol,
        "timeframe": dataset.meta.timeframe, "currency": dataset.meta.currency,
        "synthetic_data": dataset.meta.synthetic,
        "labels": {"synthetic_data": dataset.meta.synthetic, "data_source": dataset.meta.source},
        "universe": research_universe(dataset),
        "base_config": asdict(base), "multipliers": list(multipliers),
        "initial_capital": base.initial_cash, "seed": base.seed,
        "stress_fingerprint": sha256_of(rows),
    }
    if not save:
        return {"manifest": manifest_fields, "metrics": metrics}
    manifest = save_composite("stress", manifest_fields, metrics, {"rows": rows})
    return {"manifest": manifest, "metrics": metrics}
