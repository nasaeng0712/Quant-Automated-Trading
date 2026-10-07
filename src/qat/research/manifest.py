"""Reproducibility manifest helpers (Batch #2).

Identifier policy
  * ``run_id`` identifies one execution (UTC time + random suffix) and differs
    between otherwise identical runs by design.
  * Inside a run, proposal/order/fill ids are deterministic counters
    (P000001, O000001, F000001, BF000001) and every timestamp comes from the
    simulation clock, so identical inputs give identical trade logs.
  * ``economic_fingerprint`` hashes metrics + fills + equity curve; equal
    fingerprints mean the economic result was reproduced.
  * Code identity: ``git rev-parse`` when the project is a git work tree,
    otherwise ``unavailable`` (never invented) plus a SHA-256 over
    ``src/qat/**/*.py`` as the alternative source identifier.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import platform
import secrets
import subprocess
from datetime import datetime, timezone

import qat

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[3]
SRC_ROOT = pathlib.Path(__file__).resolve().parents[1]


def research_universe(dataset) -> dict:
    """OD-02: the universe of ONE research run is the declared market/symbol of its
    validated input dataset. It is never merged into the manual / Paper allowlist."""

    return {
        "scope": "research_run",
        "market": dataset.meta.market,
        "symbols": [dataset.meta.symbol],
        "source": "validated dataset declared symbol",
        "data_version": dataset.data_version,
        "dataset_validation": dataset.validation.status,
        "manual_trading_universe_effect": "none",
    }


def canonical_json(obj) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


def sha256_of(obj) -> str:
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


def new_run_id(kind: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{kind}-{stamp}-{secrets.token_hex(3)}"


def source_tree_sha256() -> str:
    digest = hashlib.sha256()
    for path in sorted(SRC_ROOT.rglob("*.py")):
        digest.update(path.relative_to(SRC_ROOT).as_posix().encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def code_identity() -> dict:
    info = {"qat_version": qat.__version__, "source_tree_sha256": source_tree_sha256(),
            "python": platform.python_version()}
    try:
        inside = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], cwd=PROJECT_ROOT,
                                capture_output=True, text=True, timeout=10)
        if inside.returncode != 0 or inside.stdout.strip() != "true":
            raise RuntimeError("not a git work tree")
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=PROJECT_ROOT,
                                capture_output=True, text=True, timeout=10)
        dirty = subprocess.run(["git", "status", "--porcelain"], cwd=PROJECT_ROOT,
                               capture_output=True, text=True, timeout=10)
        info["code_commit"] = commit.stdout.strip() if commit.returncode == 0 else "unavailable (no commit)"
        info["git_dirty"] = bool(dirty.stdout.strip()) if dirty.returncode == 0 else "unavailable"
    except Exception as exc:  # noqa: BLE001 - git missing or not a repository
        info["code_commit"] = f"unavailable ({exc.__class__.__name__}: not a git repository or git missing)"
        info["git_dirty"] = "unavailable"
    return info


def economic_fingerprint(metrics: dict, fills: list[dict], equity: list[dict]) -> str:
    def rounded(value):
        if isinstance(value, float):
            return round(value, 8)
        if isinstance(value, dict):
            return {k: rounded(v) for k, v in value.items()}
        if isinstance(value, list):
            return [rounded(v) for v in value]
        return value

    return sha256_of(rounded({"metrics": metrics, "fills": fills, "equity": equity}))
