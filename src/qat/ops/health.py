"""Operational health summary: one status per component, critical first.

Statuses: PASS / DEGRADED / BLOCKED / UNKNOWN / NOT_CONFIGURED. The summary is a pure function of the evidence dictionary the server collects from
the real objects (nothing client-supplied). ``NOT_CONFIGURED`` and ``UNKNOWN`` are never promoted to PASS. Components marked ``informational``
(Research, Paper graduation, Live) are shown but do not decide the overall status: Live is BLOCKED by design, and that is the expected state.
"""

from __future__ import annotations

PASS, DEGRADED, BLOCKED, UNKNOWN, NOT_CONFIGURED = "PASS", "DEGRADED", "BLOCKED", "UNKNOWN", "NOT_CONFIGURED"
_ORDER = {BLOCKED: 0, DEGRADED: 1, UNKNOWN: 2, NOT_CONFIGURED: 3, PASS: 4}


def _c(cid: str, name: str, status: str, reasons, *, informational: bool = False) -> dict:
    return {"id": cid, "name": name, "status": status, "reasons": [r for r in reasons if r], "informational": informational}


def build_health(ev: dict) -> dict:
    comps: list[dict] = []
    session = ev.get("session")  # None -> no paper session

    # Data
    if session is None:
        comps.append(_c("data", "Data", UNKNOWN, ["no paper session: no dataset replay in progress"]))
    else:
        data = session["data"]
        reasons = []
        status = PASS
        if data["dataset_validation"] != "PASS":
            status = BLOCKED
            reasons.append(f"dataset validation {data['dataset_validation']}")
        if data["missing_marks"] or data["missing_fx"]:
            status = DEGRADED if status == PASS else status
            reasons.append("missing marks / FX rates")
        if data.get("synthetic"):
            reasons.append("synthetic dataset (not market data)")
        reasons.append("historical replay: not live market data")
        comps.append(_c("data", "Data", status, reasons))

    # Ledger
    if session is None:
        comps.append(_c("ledger", "Ledger", UNKNOWN, ["no paper session"]))
    else:
        problems = session["ledger"]["integrity_problems"]
        breaches = session["ledger"]["reservation_breaches"]
        comps.append(_c("ledger", "Ledger", BLOCKED if problems else (DEGRADED if breaches else PASS),
                        [f"accounting integrity: {', '.join(problems[:3])}" if problems else "", f"{breaches} reservation breach(es)" if breaches else ""]))

    # Reconciliation (paper replay has no broker account: UNKNOWN, never PASS)
    recon = ev.get("reconciliation") or {"status": UNKNOWN, "reason": "no reconciliation recorded"}
    rs = {"PASS": PASS, "BLOCK": BLOCKED}.get(recon.get("status"), UNKNOWN)
    comps.append(_c("reconciliation", "Reconciliation", rs, [recon.get("reason", ""), *recon.get("reasons", [])]))

    # Risk
    risk = ev.get("risk", {})
    if risk.get("kill_switch") or risk.get("latches"):
        comps.append(_c("risk", "Risk", BLOCKED, ["Kill Switch engaged (top-level hard stop)" if risk.get("kill_switch") else "", f"{len(risk.get('latches', []))} safety latch(es) active"]))
    else:
        unconfigured = risk.get("unconfigured_rules", [])
        comps.append(_c("risk", "Risk", DEGRADED if unconfigured else PASS, [f"rules NOT_CONFIGURED: {', '.join(unconfigured)}" if unconfigured else ""]))

    # Compliance
    comp = ev.get("compliance", {})
    comps.append(_c("compliance", "Compliance", PASS if comp.get("universe_configured") else DEGRADED,
                    ["" if comp.get("universe_configured") else "paper universe not configured: every manual proposal is UNKNOWN (fail-closed)", "software PASS is not legal compliance"]))

    # Integrity (market integrity engine + accounting)
    mi = ev.get("integrity", {})
    comps.append(_c("integrity", "Integrity", DEGRADED if mi.get("unconfigured_rules") else PASS,
                    [f"rules NOT_CONFIGURED: {', '.join(mi['unconfigured_rules'])}" if mi.get("unconfigured_rules") else ""]))

    # Strategy health
    sh = ev.get("strategy_health")
    shs = {"HEALTHY": PASS, "DEGRADED": DEGRADED, "UNHEALTHY": BLOCKED, "UNKNOWN": UNKNOWN}.get((sh or {}).get("status"), UNKNOWN)
    comps.append(_c("strategy_health", "Strategy Health", shs, (list(sh.get("reasons", [])) if sh else ["not observed"]) + ["operational health only; not performance"]))

    # Audit
    audit = ev.get("audit", {})
    if audit.get("ok") and audit.get("healthy", True):
        comps.append(_c("audit", "Audit", PASS, [f"{audit.get('records', 0)} record(s); hash chain verified"]))
    else:
        comps.append(_c("audit", "Audit", BLOCKED, ["durable audit log failed verification or is in FAULT: " + "; ".join(audit.get("errors", [])[:2] or [str(audit.get("fault") or "fault")])]))

    # Broker
    broker = ev.get("broker", {})
    if not broker.get("configured"):
        comps.append(_c("broker", "Broker", NOT_CONFIGURED, ["no real broker adapter; Paper broker only (contract + fake adapter exist for tests)"]))
    else:
        comps.append(_c("broker", "Broker", BLOCKED if broker.get("unknown_orders") or not broker.get("connected", True) else PASS,
                        [f"unknown orders: {len(broker.get('unknown_orders', []))}" if broker.get("unknown_orders") else ""]))

    # Startup + Recovery
    startup = ev.get("startup", {})
    comps.append(_c("startup", "Startup", {"PASS": PASS, "WARN": DEGRADED, "BLOCK": BLOCKED}.get(startup.get("status"), UNKNOWN),
                    [f"{c['id']}: {c['detail']}" for c in startup.get("checks", []) if c["status"] != "PASS"]))
    rec = ev.get("recovery", {})
    rstatus = rec.get("status", "NORMAL")
    restart = (rec.get("restart") or {}).get("state")
    if rstatus in ("RECOVERY_REQUIRED",) or restart == "MANUAL_INTERVENTION_REQUIRED":
        comps.append(_c("recovery", "Recovery", BLOCKED, [f"recovery state {rstatus}", f"restart assessment {restart}" if restart else ""]))
    elif rstatus == "FLATTEN_SUBMITTED":
        comps.append(_c("recovery", "Recovery", DEGRADED, ["emergency flatten submitted; waiting for fills"]))
    else:
        comps.append(_c("recovery", "Recovery", PASS, [f"recovery state {rstatus}", f"restart assessment {restart}" if restart else ""]))

    # Informational
    comps.append(_c("research", "Research", UNKNOWN, [ev.get("research_label") or "research status unavailable", "alpha NOT PROVEN; 2026 holdout untouched"], informational=True))
    paper = ev.get("paper_graduation", {})
    ps = {"GRADUATED": PASS, "NOT_READY": DEGRADED, "BLOCKED": BLOCKED, "UNKNOWN": UNKNOWN}.get(paper.get("status"), UNKNOWN)
    comps.append(_c("paper", "Paper", ps, [f"graduation {paper.get('status', 'UNKNOWN')}"], informational=True))
    live = ev.get("live", {})
    comps.append(_c("live", "Live", BLOCKED, [f"Live is {live.get('status', 'BLOCKED')} by design: " + "; ".join(live.get("blockers", [])[:2])], informational=True))

    deciding = [c for c in comps if not c["informational"]]
    worst = min((c["status"] for c in deciding), key=lambda s: _ORDER[s])
    # NOT_CONFIGURED and UNKNOWN never produce an overall PASS
    overall = worst if worst != NOT_CONFIGURED else DEGRADED
    ordered = sorted(comps, key=lambda c: (c["informational"], _ORDER[c["status"]], c["id"]))  # operational components first, most severe first
    return {"overall": overall, "components": ordered, "note": "Live BLOCKED is the expected state; overall reflects operational components only"}
