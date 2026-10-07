"""Batch #3C - trial identity, trial registry and counting.

A *trial* is one deterministic evaluation of one configuration on one window:

  stage ``train_selection``  one candidate parameter set backtested on a fold's TRAIN window
  stage ``oos_evaluation``   the parameter set chosen for that fold backtested on its OOS window

``trial_id`` is a SHA-256 of everything that determines the numbers - dataset identity
(``data_version``), strategy + version, parameters, window, stage, selection metric, the full
base configuration (costs multiplier, capital, seed, ...) and the settings file content hash.
Re-running the identical computation yields the identical id and is therefore NOT a new
statistical trial; it only adds a ``run_id`` to the existing record. The protocol version is
a recorded attribute, not part of the identity (a different protocol that re-evaluates the same
configuration on the same window and data is the same statistical trial).

Counting semantics (see ``summarize``):
  unique_trials            distinct trial ids over all stages
  train_selection_trials   distinct train-window evaluations (the selection pool)
  oos_evaluations          distinct OOS evaluations
  distinct_configurations  distinct (dataset, strategy, parameters, cost config) ignoring window
  candidate_procedures     distinct (dataset, strategy) at cost multiplier 1.0 (what is compared)
  executions               sum of recorded runs over all trials (>= unique when re-run / re-used)
Cost-stress runs are different configurations (their cost config differs) but are re-evaluations
of the same hypothesis, so they are reported separately and not counted as new candidates.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib

from qat.research.store import registry_read, registry_write, results_root
from qat.research.strategies import STRATEGIES

TRIAL_SCHEMA = 1
SELECTION_METRIC = "net_return"  # train-window net return (the existing selection contract)
REGISTRY_NAME = "trial_registry.json"
STAGES = ("train_selection", "oos_evaluation")
_WINDOW_FIELDS = ("trade_start", "trade_end", "settings_path")


def _sha(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")).hexdigest()


def settings_sha256(path) -> str | None:
    """Content hash of the settings file the run uses (``None`` path -> the default file)."""

    from qat.config import load_research_settings

    try:
        return load_research_settings(path)["_meta"]["sha256"]
    except Exception:  # noqa: BLE001 - an unreadable settings file cannot have produced a run; id then carries None
        return None


def trial_id(*, data_version: str, strategy: str, strategy_version, params: dict, window, stage: str,
             selection_metric: str, base_config: dict, settings_sha: str | None) -> str:
    if stage not in STAGES:
        raise ValueError(f"unknown trial stage {stage!r}")
    base = {k: v for k, v in base_config.items() if k not in _WINDOW_FIELDS}
    return _sha({"schema": TRIAL_SCHEMA, "data_version": data_version, "strategy": strategy, "strategy_version": strategy_version,
                 "params": dict(params), "window": [int(window[0]), int(window[1])], "stage": stage,
                 "selection_metric": selection_metric, "base": base, "settings_sha256": settings_sha})


def collect(*, data_version: str, market: str, symbol: str, strategy: str, strategy_version, base_config: dict,
            settings_sha: str | None, fold_rows: list[dict]) -> list[dict]:
    """Trial records for every evaluated candidate of a Walk-Forward run (FAILED candidates included:
    a failed evaluation was still a trial). Fold rows are NOT modified: the contamination detectors
    compare calibration outputs across perturbed datasets and must keep seeing the original rows."""

    out = []
    common = {"data_version": data_version, "market": market, "symbol": symbol, "strategy": strategy,
              "selection_metric": SELECTION_METRIC, "cost_multiplier": base_config.get("cost_multiplier", 1.0)}

    def make(params, window, stage, row):
        tid = trial_id(data_version=data_version, strategy=strategy, strategy_version=strategy_version, params=params, window=window,
                       stage=stage, selection_metric=SELECTION_METRIC, base_config=base_config, settings_sha=settings_sha)
        out.append({"trial_id": tid, "stage": stage, "params": dict(params), "window": [int(window[0]), int(window[1])],
                    "training_fold": {"fold": row["fold"], "train": list(row["train"]), "test": list(row["test"])}, **common})
        return tid

    for row in fold_rows:
        for cand in row.get("calibration") or []:
            make(cand["params"], row["train"], "train_selection", row)
        if row.get("chosen_params") is not None:
            make(row["chosen_params"], row["test"], "oos_evaluation", row)
    return out


def register(trials: list[dict], *, run_id: str, protocol_version: str | None, synthetic: bool = False) -> dict:
    """Merge into the persistent registry. An existing id only gains the run id / protocol version."""

    reg = registry_read(REGISTRY_NAME) or {"schema": TRIAL_SCHEMA, "trials": {}}
    added = seen = 0
    for t in trials:
        rec = reg["trials"].get(t["trial_id"])
        if rec is None:
            reg["trials"][t["trial_id"]] = {**t, "synthetic_data": synthetic, "protocol_versions": [protocol_version or "none"], "run_ids": [run_id]}
            added += 1
        else:
            seen += 1
            if run_id not in rec["run_ids"]:
                rec["run_ids"].append(run_id)
            if (protocol_version or "none") not in rec["protocol_versions"]:
                rec["protocol_versions"].append(protocol_version or "none")
    registry_write(REGISTRY_NAME, reg)
    return {"added": added, "already_registered": seen}


def summarize(registry: dict, *, real_only: bool = True) -> dict:
    recs = [r for r in registry.get("trials", {}).values() if not (real_only and r.get("synthetic_data"))]
    by = lambda key: {k: sum(1 for r in recs if r[key] == k) for k in sorted({r[key] for r in recs})}  # noqa: E731
    configs = {(r["data_version"], r["strategy"], json.dumps(r["params"], sort_keys=True), r["cost_multiplier"]) for r in recs}
    cands = {(r["data_version"], r["strategy"]) for r in recs if r["cost_multiplier"] == 1.0}
    return {
        "scope": "real data only" if real_only else "all data",
        "unique_trials": len(recs),
        "train_selection_trials": sum(1 for r in recs if r["stage"] == "train_selection"),
        "oos_evaluations": sum(1 for r in recs if r["stage"] == "oos_evaluation"),
        "distinct_configurations": len(configs),
        "candidate_procedures": len(cands),
        "executions": sum(len(r["run_ids"]) for r in recs),
        "by_market": by("market"), "by_strategy": by("strategy"),
        "by_cost_multiplier": {str(k): v for k, v in by("cost_multiplier").items()},
        "reused_trials": sum(1 for r in recs if len(r["run_ids"]) > 1),
    }


def registry_fingerprint(registry: dict) -> str:
    return _sha(sorted(registry.get("trials", {})))


def reconstruct(root: pathlib.Path | None = None) -> dict:
    """Rebuild trial records from saved Walk-Forward runs (manifest + result). Idempotent: ids already
    in the registry only gain run ids. Uses the same ``collect`` as the live path."""

    runs = pathlib.Path(root or results_root()) / "runs"
    n_runs = added = seen = 0
    for run_dir in sorted(runs.glob("walkforward-*")):
        manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
        result = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
        name = manifest["strategy_name"]
        base = manifest["base_config"]
        trials = collect(data_version=manifest["data"]["data_version"], market=manifest["market"], symbol=manifest["symbol"], strategy=name,
                         strategy_version=STRATEGIES[name].version, base_config=base, settings_sha=settings_sha256(base.get("settings_path")),
                         fold_rows=result["folds"])
        stats = register(trials, run_id=manifest["run_id"], protocol_version=(manifest.get("research_protocol") or {}).get("version"),
                         synthetic=bool(manifest.get("synthetic_data")))
        n_runs += 1
        added += stats["added"]
        seen += stats["already_registered"]
    return {"runs_scanned": n_runs, "trials_added": added, "already_registered": seen}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m qat.research.trials")
    parser.add_argument("cmd", choices=["reconstruct", "summary"])
    args = parser.parse_args(argv)
    if args.cmd == "reconstruct":
        print(json.dumps(reconstruct(), indent=1))
    print(json.dumps(summarize(registry_read(REGISTRY_NAME)), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
