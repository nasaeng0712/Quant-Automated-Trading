"""Result storage (Batch #2): every run - including failed folds and blocked or
losing experiments - is written to ``results/runs/<run_id>/`` and can be listed
and reopened. Nothing is deleted by this module.

Layout per run
  manifest.json   reproducibility manifest (see qat.research.manifest)
  metrics.json    headline metrics (or per-fold / per-multiplier tables)
  result.json     full result: equity, fills, orders, rejections, trades, ...
  equity.csv      equity curve for human inspection (single backtests)
  fills.csv       fills for human inspection (single backtests)
  audit.jsonl     pipeline audit records (single backtests)

The root is ``$QAT_RESULTS_DIR`` or ``<project>/results``.
"""

from __future__ import annotations

import csv
import json
import os
import pathlib
import re
from datetime import datetime, timezone

from qat.ops.atomic import atomic_write_text, read_json_strict
from qat.research.manifest import PROJECT_ROOT, code_identity, economic_fingerprint, new_run_id, sha256_of

_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,120}$")


def results_root() -> pathlib.Path:
    root = pathlib.Path(os.environ.get("QAT_RESULTS_DIR") or (PROJECT_ROOT / "results"))
    (root / "runs").mkdir(parents=True, exist_ok=True)
    return root


def _write_json(path: pathlib.Path, obj) -> None:
    # atomic (temp file + fsync + replace): a crash mid-write leaves the previous file, never a truncated one. Single-writer only.
    atomic_write_text(path, json.dumps(obj, indent=2, ensure_ascii=False, default=str) + "\n")


def _write_csv(path: pathlib.Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields = [k for k in rows[0].keys() if not isinstance(rows[0][k], (dict, list))]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def backtest_manifest(result: dict, *, run_id: str, kind: str, extra: dict | None = None) -> dict:
    cfg, ds = result["config"], result["dataset"]
    equity = result["equity"]
    return {
        "run_id": run_id,
        "kind": kind,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        **code_identity(),
        "data": {k: ds[k] for k in ("data_version", "sha256", "path", "validation_status", "rows",
                                    "first_ts", "last_ts")},
        "dataset_meta": ds["meta"],
        "market": ds["meta"]["market"],
        "symbol": ds["meta"]["symbol"],
        "timeframe": ds["meta"]["timeframe"],
        "currency": ds["currency"],
        "strategy": result["strategy"],
        "strategy_version": result["strategy"]["version"],
        "model_version": "n/a (rule-based benchmark)",
        "config": cfg,
        "config_version": result["settings_meta"]["sha256"][:12],
        "settings": result["settings_meta"],
        "cost_models": result["cost_models"],
        "cost_model_version": sha256_of(result["cost_models"])[:12],
        "risk_overrides": cfg.get("risk_overrides", {}),
        "initial_capital": cfg["initial_cash"],
        "period": {"start": equity[0]["timestamp"] if equity else None,
                   "end": equity[-1]["timestamp"] if equity else None,
                   "trade_start_bar": cfg["trade_start"], "trade_end_bar": cfg["trade_end"]},
        "seed": cfg["seed"],
        "labels": result["labels"],
        "universe": result["labels"].get("research_universe"),
        "warnings": result["warnings"],
        "strategy_health": "NOT_IMPLEMENTED",
        "economic_fingerprint": economic_fingerprint(result["metrics"], result["fills"], equity),
        **(extra or {}),
    }


def save_backtest(result, *, kind: str = "backtest", extra: dict | None = None) -> dict:
    data = result.to_dict() if hasattr(result, "to_dict") else dict(result)
    run_id = new_run_id(kind)
    run_dir = results_root() / "runs" / run_id
    run_dir.mkdir(parents=True)
    manifest = backtest_manifest(data, run_id=run_id, kind=kind, extra=extra)
    audit = data.pop("audit", [])
    _write_json(run_dir / "manifest.json", manifest)
    _write_json(run_dir / "metrics.json", data["metrics"])
    _write_json(run_dir / "result.json", data)
    _write_csv(run_dir / "equity.csv", data["equity"])
    _write_csv(run_dir / "fills.csv", data["fills"])
    with (run_dir / "audit.jsonl").open("w", encoding="utf-8") as handle:
        for rec in audit:
            handle.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    return manifest


def save_composite(kind: str, manifest_fields: dict, metrics: dict, result: dict) -> dict:
    run_id = new_run_id(kind)
    run_dir = results_root() / "runs" / run_id
    run_dir.mkdir(parents=True)
    manifest = {"run_id": run_id, "kind": kind, "created_utc": datetime.now(timezone.utc).isoformat(),
                **code_identity(), "strategy_health": "NOT_IMPLEMENTED", **manifest_fields}
    _write_json(run_dir / "manifest.json", manifest)
    _write_json(run_dir / "metrics.json", metrics)
    _write_json(run_dir / "result.json", result)
    return manifest


def _run_dir(run_id: str) -> pathlib.Path:
    if not _RUN_ID_RE.match(run_id or ""):
        raise KeyError(f"invalid run id {run_id!r}")
    path = results_root() / "runs" / run_id
    if not (path / "manifest.json").exists():
        raise KeyError(f"run not found: {run_id}")
    return path


def load_run(run_id: str, *, include_audit: bool = False) -> dict:
    path = _run_dir(run_id)
    out = {
        "manifest": json.loads((path / "manifest.json").read_text(encoding="utf-8")),
        "metrics": json.loads((path / "metrics.json").read_text(encoding="utf-8")),
        "result": json.loads((path / "result.json").read_text(encoding="utf-8")),
    }
    if include_audit and (path / "audit.jsonl").exists():
        out["audit"] = [json.loads(line) for line in (path / "audit.jsonl").read_text(encoding="utf-8").splitlines() if line]
    return out


def list_runs() -> list[dict]:
    rows = []
    for manifest_path in (results_root() / "runs").glob("*/manifest.json"):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            metrics = json.loads((manifest_path.parent / "metrics.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            rows.append({"run_id": manifest_path.parent.name, "kind": "UNREADABLE"})
            continue
        rows.append({
            "run_id": manifest["run_id"],
            "kind": manifest["kind"],
            "created_utc": manifest["created_utc"],
            "strategy": (manifest.get("strategy") or {}).get("name") or manifest.get("strategy_name"),
            "params": (manifest.get("strategy") or {}).get("params"),
            "symbol": manifest.get("symbol"),
            "market": manifest.get("market"),
            "timeframe": manifest.get("timeframe"),
            "synthetic": (manifest.get("labels") or {}).get("synthetic_data", manifest.get("synthetic_data")),
            "net_return": metrics.get("net_return") if isinstance(metrics, dict) else None,
            "headline": metrics.get("headline") if isinstance(metrics, dict) else None,
            "status": manifest.get("status", "COMPLETED"),
        })
    return sorted(rows, key=lambda r: r.get("created_utc") or "", reverse=True)


def _registry(name: str) -> pathlib.Path:
    return results_root() / name


def registry_read(name: str) -> dict:
    # a corrupt registry raises StateCorrupt (a clear error): it is never read as "empty", which would silently reset trial / OOS accounting
    return read_json_strict(_registry(name), default={})


def registry_write(name: str, data: dict) -> None:
    _write_json(_registry(name), data)
