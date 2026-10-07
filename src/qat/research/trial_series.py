"""Batch #3D - per-trial return-series storage (foundation for White RC / PBO / DSR style methods) + aligned benchmark v2.

Every configuration evaluated in a Walk-Forward run - every candidate on every train window, FAILED candidates
included, and every OOS evaluation - gets one record: trial id, stage, fold, parameters, window, bar timestamps, the
per-bar strategy return series, the per-bar aligned passive-benchmark series, and the dataset identity. Storing only
the selected winner is forbidden: ``verify_complete`` raises unless every trial of the run has a record.

Storage is deterministic: canonical JSON, gzip with mtime 0, one file per trial id under ``results/trial_series``.
Re-storing the identical record is a no-op; a different record for an existing id is a determinism violation.

Benchmark v2 (corrects the Batch #3C finding): the engine can fill the earliest order at open(start+1) (signal at the
close of the first window bar, fill at the next open), so the passive benchmark is exposed from open(start+1), not
open(start). Its per-bar series is 0.0 for the first bar, close[start+1]/open[start+1]-1 for the second, then bar-to-bar
close returns. It is cost-free and fully invested; those asymmetries are stated, not hidden.
"""

from __future__ import annotations

import gzip
import json
import pathlib

from qat.research import trials
from qat.research.store import results_root

SERIES_SCHEMA = 1


class TrialSeriesIncomplete(RuntimeError):
    pass


class TrialSeriesConflict(RuntimeError):
    pass


def aligned_benchmark_returns(bars, window) -> list[float]:
    a, b = int(window[0]), int(window[1])
    if b - a < 2:
        raise ValueError("benchmark window needs at least two bars")
    out = [0.0, bars[a + 1].close / bars[a + 1].open - 1]
    out += [bars[t].close / bars[t - 1].close - 1 for t in range(a + 2, b)]
    return out


def aligned_benchmark_return(bars, window) -> float:
    """Earliest engine-feasible entry open(start+1) -> close of the last window bar."""

    return bars[window[1] - 1].close / bars[window[0] + 1].open - 1


def returns_from_equity(equity: list[dict], initial: float) -> list[float | None]:
    out, prev = [], initial
    for point in equity:
        value = point["equity"]
        out.append(None if (value is None or prev in (None, 0)) else value / prev - 1)
        prev = value
    return out


def _canonical(record: dict) -> bytes:
    raw = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode("utf-8")
    return gzip.compress(raw, compresslevel=9, mtime=0)


class TrialSeriesStore:
    def __init__(self, root: pathlib.Path | None = None):
        self.root = pathlib.Path(root) if root else results_root() / "trial_series"

    def path(self, trial_id: str) -> pathlib.Path:
        return self.root / f"{trial_id}.json.gz"

    def put(self, record: dict) -> str:
        data = _canonical(record)
        path = self.path(record["trial_id"])
        if path.exists():
            if path.read_bytes() != data:
                raise TrialSeriesConflict(f"trial {record['trial_id'][:12]} already stored with a different series (non-deterministic evaluation)")
            return "unchanged"
        self.root.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return "stored"

    def get(self, trial_id: str) -> dict:
        return json.loads(gzip.decompress(self.path(trial_id).read_bytes()))

    def missing(self, trial_ids) -> list[str]:
        return [t for t in trial_ids if not self.path(t).exists()]

    def verify_complete(self, trial_ids) -> None:
        gone = self.missing(trial_ids)
        if gone:
            raise TrialSeriesIncomplete(f"{len(gone)} of {len(list(trial_ids))} trials have no stored series (winner-only storage is not allowed)")


def build_record(*, dataset, strategy_name: str, trial_id: str, stage: str, fold, params: dict, window, equity: list | None, initial_cash: float,
                 protocol_version: str | None, cost_scenario: str | None = None) -> dict:
    """One per-trial record (also used by the Protocol v2 runner). ``equity=None`` marks a FAILED evaluation (no strategy series)."""

    failed = equity is None
    eq = equity or []
    record = {"schema": SERIES_SCHEMA, "trial_id": trial_id, "stage": stage, "fold": fold, "params": params, "window": list(window),
              "status": "FAILED" if failed else "OK", "timestamps": [p["timestamp"] for p in eq],
              "returns": None if failed else returns_from_equity(eq, initial_cash),
              "benchmark_returns": aligned_benchmark_returns(dataset.bars, window), "benchmark": "aligned passive (entry open(start+1)), cost-free, fully invested",
              "dataset": {"data_version": dataset.data_version, "market": dataset.meta.market, "symbol": dataset.meta.symbol, "timeframe": dataset.meta.timeframe},
              "strategy": strategy_name, "protocol_version": protocol_version or "none", "cost_scenario": cost_scenario, "initial_equity": initial_cash}
    if not failed and len(record["returns"]) != len(record["benchmark_returns"]):
        raise TrialSeriesConflict(f"strategy ({len(record['returns'])}) and benchmark ({len(record['benchmark_returns'])}) series differ in length")
    return record


def store_run(store: TrialSeriesStore, *, dataset, strategy_name: str, base_config: dict, settings_sha: str | None, fold_sinks: dict,
              initial_cash: float, protocol_version: str | None, trial_rows: list[dict], cost_scenario: str | None = None) -> dict:
    """Store a record for EVERY evaluated candidate (``fold_sinks``: fold -> list of evaluation dicts), then require completeness."""

    from qat.research.strategies import STRATEGIES

    version = STRATEGIES[strategy_name].version
    stored = 0
    for fold, sink in fold_sinks.items():
        for item in sink:
            stage = item.get("stage", "train_selection")
            tid = trials.trial_id(data_version=dataset.data_version, strategy=strategy_name, strategy_version=version, params=item["params"],
                                  window=item["window"], stage=stage, selection_metric=trials.SELECTION_METRIC, base_config=base_config,
                                  settings_sha=settings_sha)
            record = build_record(dataset=dataset, strategy_name=strategy_name, trial_id=tid, stage=stage, fold=fold, params=item["params"], window=item["window"],
                                  equity=item["equity"], initial_cash=initial_cash, protocol_version=protocol_version, cost_scenario=cost_scenario)
            stored += store.put(record) == "stored"
    store.verify_complete([t["trial_id"] for t in trial_rows])
    return {"newly_stored": stored, "trials": len(trial_rows)}
