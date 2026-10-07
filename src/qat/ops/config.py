"""config/ops.yaml loading and validation (fail-closed). The production entry point refuses to trade on an invalid configuration;
test scaffolding that wants unrestricted behaviour must say so explicitly through the gate constructors, never through this file."""

from __future__ import annotations

import math
import pathlib

import yaml

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[3]
DEFAULT_PATH = PROJECT_ROOT / "config" / "ops.yaml"
SNAPSHOT_POLICIES = ("paper_replay", "broker_connected")


class OpsConfigError(ValueError):
    pass


def _num(value, name: str, *, minimum: float | None = None, positive: bool = False, allow_none: bool = False):
    if value is None:
        if allow_none:
            return None
        raise OpsConfigError(f"{name} is required")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise OpsConfigError(f"{name} must be a finite number")
    if positive and value <= 0:
        raise OpsConfigError(f"{name} must be > 0")
    if minimum is not None and value < minimum:
        raise OpsConfigError(f"{name} must be >= {minimum}")
    return float(value)


def load_ops_config(path=None) -> dict:
    target = pathlib.Path(path) if path is not None else DEFAULT_PATH
    if not target.is_file():
        raise OpsConfigError(f"ops config not found: {target.name}")
    try:
        raw = yaml.safe_load(target.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise OpsConfigError(f"ops config is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise OpsConfigError("ops config must be a mapping")
    return validate_ops_config(raw)


def validate_ops_config(raw: dict) -> dict:
    cfg = {"version": raw.get("version"), "mode": str(raw.get("mode", "")).lower()}
    if cfg["version"] != 1:
        raise OpsConfigError("ops config version must be 1")
    if cfg["mode"] not in ("paper", "live"):
        raise OpsConfigError("mode must be paper (or live, which the startup self-check refuses)")
    snap = raw.get("snapshot") or {}
    policy = snap.get("policy")
    if policy not in SNAPSHOT_POLICIES:
        raise OpsConfigError(f"snapshot.policy must be one of {SNAPSHOT_POLICIES}")
    max_age = _num(snap.get("max_age_seconds"), "snapshot.max_age_seconds", positive=True, allow_none=policy == "paper_replay")
    if policy == "broker_connected" and max_age is None:
        raise OpsConfigError("snapshot.max_age_seconds is required for broker_connected (no silent freshness bypass)")
    cfg["snapshot"] = {"policy": policy, "max_age_seconds": max_age,
                       "future_tolerance_seconds": _num(snap.get("future_tolerance_seconds", 5), "snapshot.future_tolerance_seconds", minimum=0.0)}
    rules = raw.get("market_rules") or {}
    cfg["market_rules"] = {
        "min_bar_volume": _num(rules.get("min_bar_volume"), "market_rules.min_bar_volume", minimum=0.0, allow_none=True),
        "max_spread_bps": _num(rules.get("max_spread_bps"), "market_rules.max_spread_bps", positive=True, allow_none=True),
        "volatility_shock_range_pct": _num(rules.get("volatility_shock_range_pct"), "market_rules.volatility_shock_range_pct", positive=True, allow_none=True),
        "max_cancels_per_window": _num(rules.get("max_cancels_per_window"), "market_rules.max_cancels_per_window", minimum=0.0, allow_none=True),
        "cancel_window_seconds": _num(rules.get("cancel_window_seconds", 300), "market_rules.cancel_window_seconds", positive=True),
        "max_participation_rate": _num(rules.get("max_participation_rate"), "market_rules.max_participation_rate", positive=True, allow_none=True),
    }
    if cfg["market_rules"]["max_participation_rate"] is not None and cfg["market_rules"]["max_participation_rate"] > 1.0:
        raise OpsConfigError("market_rules.max_participation_rate must be <= 1.0")
    health = raw.get("strategy_health") or {}
    cfg["strategy_health"] = {
        "max_consecutive_errors": int(_num(health.get("max_consecutive_errors", 3), "strategy_health.max_consecutive_errors", positive=True)),
        "max_data_age_seconds": _num(health.get("max_data_age_seconds", 172800), "strategy_health.max_data_age_seconds", positive=True),
        "max_heartbeat_age_seconds": _num(health.get("max_heartbeat_age_seconds", 172800), "strategy_health.max_heartbeat_age_seconds", positive=True),
        "rejection_window": int(_num(health.get("rejection_window", 20), "strategy_health.rejection_window", positive=True)),
        "rejection_concentration": _num(health.get("rejection_concentration", 0.8), "strategy_health.rejection_concentration", positive=True),
        "min_rejections_for_concentration": int(_num(health.get("min_rejections_for_concentration", 10), "strategy_health.min_rejections_for_concentration", positive=True)),
    }
    if cfg["strategy_health"]["rejection_concentration"] > 1.0:
        raise OpsConfigError("strategy_health.rejection_concentration must be <= 1.0")
    grad = raw.get("paper_graduation") or {}
    cfg["paper_graduation"] = {
        "min_completed_sessions": int(_num(grad.get("min_completed_sessions", 5), "paper_graduation.min_completed_sessions", positive=True)),
        "min_applied_fills": int(_num(grad.get("min_applied_fills", 20), "paper_graduation.min_applied_fills", positive=True)),
        "require_recovery_drill": bool(grad.get("require_recovery_drill", True)),
    }
    audit = raw.get("audit") or {}
    cfg["audit"] = {"max_page_size": int(_num(audit.get("max_page_size", 500), "audit.max_page_size", positive=True))}
    return cfg


def rule_states(cfg: dict) -> dict:
    """Configured vs NOT_CONFIGURED for every operations rule (shown by the UI; an unset threshold is never a PASS)."""

    r = cfg["market_rules"]
    return {"R-07": r["min_bar_volume"] is not None, "R-08": r["max_spread_bps"] is not None, "R-09": r["volatility_shock_range_pct"] is not None,
            "MI-05": r["max_cancels_per_window"] is not None, "MI-06": r["max_participation_rate"] is not None}
