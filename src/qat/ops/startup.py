"""Startup self-check. Run before the server accepts any trading action; a fatal finding makes the whole startup BLOCK (fail-closed).

Each check reports PASS / WARN / BLOCK with a reason. BLOCK means "do not trade"; WARN means "visible, but not trading-critical".
The check never reads 2026 holdout values or the Lockbox: it only looks at marker files and hash-verified historical records.
"""

from __future__ import annotations

import os
import pathlib
import tempfile

from qat.ops.atomic import StateCorrupt, read_json_strict
from qat.ops.config import OpsConfigError, load_ops_config

PASS, WARN, BLOCK = "PASS", "WARN", "BLOCK"


def _check(cid: str, status: str, detail: str, **extra) -> dict:
    return {"id": cid, "status": status, "detail": detail, **extra}


def _writable(directory: pathlib.Path) -> tuple[bool, str]:
    try:
        directory.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".selfcheck.", dir=str(directory))
        os.close(fd)
        os.unlink(tmp)
        return True, "writable"
    except OSError as exc:
        return False, f"{type(exc).__name__}: {exc}"


def run_startup_check(*, state_dir, ops_config_path=None, settings=None, settings_error=None, latch_status=None, audit_store=None,
                      broker_adapter_configured: bool = False, results_dir=None, research_check=None) -> dict:
    """``latch_status``: callable -> {"ok": bool, "error": str|None, "active": [...]}. ``research_check``: optional callable -> dict with ``ok``/
    ``detail`` for the hash-verified Protocol v2 closure record (injected so tests need no real artifacts)."""

    state_dir = pathlib.Path(state_dir)
    checks: list[dict] = []
    cfg = None

    # 1. configuration
    try:
        cfg = load_ops_config(ops_config_path)
        checks.append(_check("ops_config", PASS, "config/ops.yaml is valid"))
    except OpsConfigError as exc:
        checks.append(_check("ops_config", BLOCK, f"invalid ops config: {exc}"))
    if settings is None:
        checks.append(_check("settings", BLOCK, f"settings unavailable: {settings_error or 'not loaded'}"))
    else:
        checks.append(_check("settings", PASS, "settings.yaml loaded"))

    # 2. unsupported Live configuration
    live_reasons = []
    if cfg is not None and cfg["mode"] != "paper":
        live_reasons.append(f"ops.mode={cfg['mode']}")
    mode = str(((settings or {}).get("project") or {}).get("mode", "paper")).lower()
    if mode == "live":
        live_reasons.append("settings.project.mode=live")
    if live_reasons:
        checks.append(_check("live_config", BLOCK, "Live is unsupported and BLOCKED: " + ", ".join(live_reasons)))
    elif ((settings or {}).get("execution") or {}).get("live_enabled"):
        checks.append(_check("live_config", WARN, "execution.live_enabled=true is ignored (Live is not implemented)"))
    else:
        checks.append(_check("live_config", PASS, "no Live configuration"))

    # 3. directories writable
    ok, detail = _writable(state_dir)
    checks.append(_check("state_dir_writable", PASS if ok else BLOCK, detail))
    if results_dir is not None:
        ok, detail = _writable(pathlib.Path(results_dir))
        checks.append(_check("results_dir_writable", PASS if ok else BLOCK, detail))

    # 4. latch store (a corrupt latch file is never a silent reset)
    if latch_status is not None:
        ls = latch_status()
        if not ls.get("ok", False):
            checks.append(_check("latches", BLOCK, f"safety latch state unreadable/corrupt: {ls.get('error')}"))
        else:
            active = ls.get("active", [])
            checks.append(_check("latches", WARN if active else PASS, f"{len(active)} active latch(es): kill switch will be engaged" if active else "no active latch"))

    # 5. persisted recovery / safety state readable
    for name in ("recovery_state.json", "safety_state.json"):
        try:
            read_json_strict(state_dir / name, default=None)
            checks.append(_check(f"state:{name}", PASS, "readable or absent"))
        except StateCorrupt as exc:
            checks.append(_check(f"state:{name}", BLOCK, f"corrupt persisted state: {exc}"))

    # 6. audit chain
    if audit_store is None:
        checks.append(_check("audit_chain", BLOCK, "no durable audit store configured"))
    else:
        report = audit_store.verify()
        checks.append(_check("audit_chain", PASS if report["ok"] else BLOCK,
                             f"{report['records']} record(s) verified" if report["ok"] else "audit log integrity failure: " + "; ".join(report["errors"][:3]),
                             records=report["records"]))

    # 7. broker reconciliation requirements
    if cfg is not None:
        if cfg["snapshot"]["policy"] == "broker_connected":
            if not broker_adapter_configured:
                checks.append(_check("broker_requirements", BLOCK, "snapshot.policy=broker_connected but no broker adapter is configured"))
            else:
                checks.append(_check("broker_requirements", PASS, f"broker_connected with max snapshot age {cfg['snapshot']['max_age_seconds']}s"))
        else:
            checks.append(_check("broker_requirements", PASS, "paper_replay: no broker account; reconciliation is reported UNKNOWN, never PASS"))

    # 8. research / dataset identity (hash-verified historical records; 2026 values are never read)
    if research_check is not None:
        try:
            res = research_check()
            checks.append(_check("research_identity", PASS if res.get("ok") else WARN, res.get("detail", "")))
        except Exception as exc:  # noqa: BLE001
            checks.append(_check("research_identity", WARN, f"research evidence could not be verified: {type(exc).__name__}"))

    blocked = [c["id"] for c in checks if c["status"] == BLOCK]
    warned = [c["id"] for c in checks if c["status"] == WARN]
    return {"status": BLOCK if blocked else (WARN if warned else PASS), "blocked": blocked, "warnings": warned, "checks": checks}


def research_identity_check() -> dict:
    """Default ``research_check``: Protocol v2 closure + immutable evidence hashes, 2026 holdout still locked, Lockbox registry absent."""

    from qat.realdata import intraday
    from qat.research import protocol_v2_closure as closure
    from qat.research.manifest import PROJECT_ROOT

    result = closure.verify_closure()
    lockbox = (PROJECT_ROOT / "results" / "lockbox_registry.json").exists()
    ok = bool(result["ok"]) and not lockbox and not intraday.APPROVAL_MARKER.exists()
    return {"ok": ok, "detail": f"protocol_v2 closure verified={result['ok']}; holdout approval marker absent={not intraday.APPROVAL_MARKER.exists()}; lockbox registry absent={not lockbox}"}
