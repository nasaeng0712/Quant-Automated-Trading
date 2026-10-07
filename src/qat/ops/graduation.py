"""Paper graduation evaluator and Live readiness evaluator.

* Paper graduation = OPERATIONAL readiness of the Paper system. It is NOT alpha, NOT profitability, NOT a statistical claim.
  Result: GRADUATED / NOT_READY / BLOCKED / UNKNOWN.
    BLOCKED    a hard operational blocker exists (startup BLOCK, audit/chain failure, active latch, accounting integrity problem)
    NOT_READY  a measurable criterion is not met (too little Paper operation, no validated strategy, no recovery drill ...)
    UNKNOWN    a criterion cannot be measured with the available evidence (placeholder costs, no broker account)
    GRADUATED  every criterion measured and met (precedence: BLOCKED > NOT_READY > UNKNOWN > GRADUATED)
  The criteria values are PROVISIONAL engineering numbers (``config/ops.yaml``); the owner has not defined graduation criteria.
  Protocol v2 is CLOSED / NOT_HOLDOUT_ELIGIBLE, so the strategy-validation gate honestly FAILS: no strategy has an eligible validated result.

* Live readiness: Live is BLOCKED in this build. ``evaluate_live_readiness`` PROBES the real refusal points (the router raises for LIVE and the
  Live broker stub refuses construction); no configuration value, UI action or evidence dictionary can turn the result into anything else.
"""

from __future__ import annotations

GRADUATED, NOT_READY, BLOCKED, UNKNOWN = "GRADUATED", "NOT_READY", "BLOCKED", "UNKNOWN"
OK, FAIL = "PASS", "FAIL"


def _gate(gid: str, name: str, status: str, detail: str, *, hard: bool = False) -> dict:
    return {"id": gid, "name": name, "status": status, "detail": detail, "hard": hard}


def evaluate_paper_graduation(*, criteria: dict, audit_summary: dict | None, audit_ok: bool, startup_status: str, latches_active: list,
                              integrity_problems: list, strategy_health: str | None, research_label: str | None,
                              costs_calibrated: bool = False, broker_reconciliation_evidence: bool = False) -> dict:
    gates: list[dict] = []

    hard = []
    if startup_status == "BLOCK":
        hard.append("startup_self_check_BLOCK")
    if not audit_ok:
        hard.append("durable_audit_not_verified")
    if latches_active:
        hard.append("safety_latch_active")
    if integrity_problems:
        hard.append("accounting_integrity_problem")
    gates.append(_gate("G1", "operational_integrity", FAIL if hard else OK, ", ".join(hard) if hard else "startup / audit chain / latches / accounting clean", hard=True))

    sessions = (audit_summary or {}).get("sessions", {})
    done = [uid for uid, s in sessions.items() if s.get("applied_fills", 0) > 0]
    need = int(criteria["min_completed_sessions"])
    gates.append(_gate("G2", "paper_operation_sessions", OK if len(done) >= need else FAIL, f"{len(done)} session(s) with applied fills (provisional minimum {need})"))
    fills = sum(s.get("applied_fills", 0) for s in sessions.values())
    need_f = int(criteria["min_applied_fills"])
    gates.append(_gate("G3", "paper_fill_volume", OK if fills >= need_f else FAIL, f"{fills} applied fill(s) (provisional minimum {need_f})"))

    if criteria.get("require_recovery_drill", True):
        drill = (audit_summary or {}).get("event_types", {}).get("recovery_flatten_completed", 0) > 0
        gates.append(_gate("G4", "recovery_drill_evidenced", OK if drill else FAIL, "emergency-flatten drill found in the durable audit" if drill else "no completed emergency-flatten drill in the durable audit"))

    if research_label is None:
        gates.append(_gate("G5", "validated_strategy", "UNKNOWN", "research evidence status unavailable"))
    elif "NOT_HOLDOUT_ELIGIBLE" in research_label or "CLOSED" in research_label:
        gates.append(_gate("G5", "validated_strategy", FAIL, f"no strategy has a validated, eligible result: {research_label}"))
    else:
        gates.append(_gate("G5", "validated_strategy", OK, research_label))

    gates.append(_gate("G6", "cost_calibration", OK if costs_calibrated else "UNKNOWN", "costs calibrated against real fills" if costs_calibrated else "every cost / tax / FX number is a PLACEHOLDER (no real fills to calibrate against)"))
    gates.append(_gate("G7", "broker_reconciliation_evidence", OK if broker_reconciliation_evidence else "UNKNOWN",
                       "reconciliation against a real broker account evidenced" if broker_reconciliation_evidence else "no broker account: reconciliation against a real account was never exercised"))
    if strategy_health in (None, "UNKNOWN"):
        gates.append(_gate("G8", "strategy_health", "UNKNOWN", "strategy health not yet observed"))
    else:
        gates.append(_gate("G8", "strategy_health", OK if strategy_health == "HEALTHY" else FAIL, f"strategy health {strategy_health}"))

    if any(g["hard"] and g["status"] == FAIL for g in gates):
        status = BLOCKED
    elif any(g["status"] == FAIL for g in gates):
        status = NOT_READY
    elif any(g["status"] == "UNKNOWN" for g in gates):
        status = UNKNOWN
    else:
        status = GRADUATED
    return {"status": status, "gates": gates,
            "scope": "operational readiness of the Paper system only: graduation is not alpha, not profitability, not Live readiness",
            "criteria_note": "criteria values are provisional engineering numbers (owner-defined criteria: UNKNOWN)", "criteria": dict(criteria)}


def _probe_live_refusals() -> list[str]:
    """Reasons Live cannot run, discovered by actually exercising the refusal points."""

    from qat.execution.router import ExecutionRouter, LiveExecutionDisabled
    from qat.live.broker_stub import LiveBrokerStub, LiveTradingDisabled

    reasons: list[str] = []
    try:
        LiveBrokerStub()
        reasons.append("live_broker_stub_constructible")  # would mean a live broker exists: still reported, never PASS
    except LiveTradingDisabled:
        reasons.append("live_broker_not_implemented")
    try:
        ExecutionRouter(mode="LIVE").route(object(), object())
        reasons.append("router_live_route_succeeded")
    except LiveExecutionDisabled:
        reasons.append("router_refuses_live_routing")
    except Exception:  # noqa: BLE001
        reasons.append("router_refuses_live_routing")
    return reasons


def evaluate_live_readiness(*, paper_graduation_status: str, broker_configured: bool = False, costs_calibrated: bool = False,
                            risk_limits_configured: bool = False, profitability: str = "UNKNOWN", requested_by_ui: bool = False) -> dict:
    """ALWAYS BLOCKED in this build. The structural blockers are probed, not asserted; the remaining blockers are informational."""

    probes = _probe_live_refusals()
    structural = [r for r in probes if r in ("live_broker_not_implemented", "router_refuses_live_routing")]
    blockers = ["live_execution_not_implemented (ExecutionRouter LIVE raises; LiveBrokerStub refuses every call)"]
    if not broker_configured:
        blockers.append("no real broker adapter / credentials configured")
    if paper_graduation_status != GRADUATED:
        blockers.append(f"paper graduation is {paper_graduation_status}")
    if not costs_calibrated:
        blockers.append("costs are placeholders (not calibrated)")
    if not risk_limits_configured:
        blockers.append("risk limits are placeholder values")
    if profitability != "PROVEN":
        blockers.append(f"profitability is {profitability} (alpha not proven)")
    if requested_by_ui:
        blockers.append("a UI request cannot enable Live (there is no such control)")
    # Live is never enabled in this build, whatever the probes or the other inputs say.
    return {"status": "BLOCKED", "blockers": blockers, "probes": probes, "structural_refusals_confirmed": len(structural) == 2,
            "unblock_path": "none in this build: Live needs a separate, explicitly approved design and implementation"}
