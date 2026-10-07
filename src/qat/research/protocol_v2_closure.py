"""Protocol v2 closure record: CLOSED / NOT_HOLDOUT_ELIGIBLE.

The development-selected candidate (breakout) passed the frozen development status rules but did not satisfy the frozen benchmark criterion
(PASS AND stitched OOS net return above the aligned passive benchmark in every cost scenario), so by the frozen rule it is not eligible for the
2026 temporal holdout. This record closes the Protocol v2 research path:

* the holdout approval path is deactivated for Protocol v2: ``intraday.holdout_unlocked()`` is False whenever the closure marker exists, and the
  one-shot evaluator refuses any candidate that is not holdout-eligible or whose protocol is closed - user approval cannot bypass frozen eligibility;
* the Official Development Run #1, its trial series and the candidate artifact are preserved as historical development evidence (hashes recorded);
* no figure, threshold, grid or benchmark definition is changed.

The one-shot evaluator code stays as infrastructure a FUTURE protocol may reuse (with its own candidate, freeze and closure state).
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import pathlib

from qat.realdata import intraday
from qat.research import protocol_v2 as pv
from qat.research import protocol_v2_run as run

CLOSURE_PATH = intraday.CLOSURE_MARKER
CLOSURE_HASH_PATH = pv.OUT_DIR / "protocol_v2_closure.sha256"
FINAL_LABEL = "DEVELOPMENT_SELECTED_CANDIDATE — NOT_HOLDOUT_ELIGIBLE"
PRESERVED = ("development_run_1/results.json", "development_run_1/verification_rerun.json", "development_run_1/trial_ids.json", "development_selected_candidate.json",
             "candidate_selection_addendum_v1.json", "holdout_evaluation_addendum_v1.json", "protocol_v2.json", "dataset_scope.json", "cost_contract.json",
             "holdout_boundary.json", "design_registry.json")


def _file_sha(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def benchmark_eligibility(candidate: dict) -> dict:
    """Frozen rule: stitched OOS net return must exceed the aligned passive return in EVERY S1/S2/S3 scenario."""

    per = {s: {"stitched_net_return": v["stitched_net_return"], "aligned_passive_stitched_return": v["aligned_passive_stitched_return"],
               "exceeds_aligned_passive": v["stitched_net_return"] > v["aligned_passive_stitched_return"]}
           for s, v in candidate["development_vs_aligned_passive"].items()}
    return {"status": "PASS" if all(v["exceeds_aligned_passive"] for v in per.values()) else "FAIL", "per_scenario": per}


def build_closure(*, now: str | None = None, out_dir: pathlib.Path = pv.OUT_DIR) -> dict:
    cand_check = run.verify_candidate(out_dir / "development_selected_candidate.json")
    if not cand_check["ok"]:
        raise RuntimeError("the development-selected candidate does not verify")
    candidate = json.loads((out_dir / "development_selected_candidate.json").read_text(encoding="utf-8"))
    bench = benchmark_eligibility(candidate)
    eligible = candidate["development_status"]["candidate"] == "PASS" and bench["status"] == "PASS"
    if eligible != candidate["holdout_eligible_by_frozen_rule"]:
        raise RuntimeError("the recorded eligibility disagrees with the frozen rule")
    if eligible:
        raise RuntimeError("the candidate is holdout-eligible: Protocol v2 is not closed by this record")
    body = {
        "id": "protocol_v2_closure", "closed_utc": now or dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
        "protocol_version": pv.PROTOCOL_VERSION, "protocol_sha256": candidate["protocol_sha256"], "candidate_sha256": candidate["candidate_sha256"],
        "final_state": "CLOSED / NOT_HOLDOUT_ELIGIBLE", "final_label": FINAL_LABEL,
        "distinctions": {"development_candidate": candidate["strategy"], "development_status": candidate["development_status"]["candidate"],
                         "benchmark_eligibility": bench["status"], "temporal_holdout_eligibility": False, "statistical_power": "UNKNOWN",
                         "protocol_v2_final_state": "CLOSED / NOT_HOLDOUT_ELIGIBLE"},
        "benchmark_eligibility_detail": bench,
        "reason": "the frozen eligibility rule needs PASS and a stitched OOS net return above the aligned passive benchmark in every scenario; the benchmark criterion is FAIL",
        "holdout_approval_path": {"deactivated_for_protocol_v2": True,
                                  "mechanisms": ["intraday.holdout_unlocked() is False while the closure marker exists (forged freeze/approval markers cannot unlock 2026 values)",
                                                 "the evaluator raises ProtocolClosed for this protocol's candidates and for any candidate that is not holdout-eligible",
                                                 "the former 'acknowledged_not_holdout_eligible' approval field no longer exists as a bypass"],
                                  "user_approval_cannot_bypass_frozen_eligibility": True,
                                  "superseded_text": "holdout_evaluation_addendum_v1 (immutable, preserved) described an acknowledgement path for ineligible candidates; this closure supersedes it"},
        "preserved_historical_development_evidence": {rel: _file_sha(out_dir / rel) for rel in PRESERVED},
        "preserved_series": {"trial_ids_sha256": candidate["official_run"]["trial_ids_sha256"], "series_hashes_sha256": candidate["official_run"]["series_hashes_sha256"],
                             "series_files": candidate["official_run"]["series_files"]},
        "unchanged": ["performance figures", "thresholds (30 trades / 50% active folds / positive-fold majority)", "parameter grids", "benchmark definition (aligned passive, entry open(start+1))",
                      "cost scenarios", "fold design"],
        "future_use": "the one-shot evaluator is infrastructure a future protocol may reuse with its own candidate, freeze and eligibility; it cannot run on this candidate",
        "holdout_values_accessed": "NO", "existing_daily_lockbox": "untouched", "language": "development evidence only; no claim of alpha, robustness or profitability"}
    return {"body": body, "sha256": pv.sha(body)}


def write_closure() -> dict:
    if CLOSURE_PATH.exists():
        raise RuntimeError("the closure record already exists and is immutable")
    built = build_closure()
    with CLOSURE_PATH.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps({**built["body"], "closure_sha256": built["sha256"]}, indent=1, ensure_ascii=False, sort_keys=True) + "\n")
    with CLOSURE_HASH_PATH.open("x", encoding="utf-8") as handle:
        handle.write(f"{built['sha256']}  protocol_v2_closure.json (sha256 of the canonical body without closure_sha256)\n")
    return {"closure_sha256": built["sha256"], "final_label": FINAL_LABEL}


def verify_closure(path: pathlib.Path = CLOSURE_PATH, hash_path: pathlib.Path = CLOSURE_HASH_PATH, out_dir: pathlib.Path = pv.OUT_DIR) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    recorded = data.pop("closure_sha256")
    preserved = {rel: _file_sha(out_dir / rel) == h for rel, h in data["preserved_historical_development_evidence"].items()}
    checks = {"closure_hash": pv.sha(data) == recorded == hash_path.read_text(encoding="utf-8").split()[0], "historical_evidence_unchanged": all(preserved.values()),
              "holdout_locked": intraday.holdout_unlocked() is False, "no_approval_marker": not intraday.APPROVAL_MARKER.exists()}
    return {"ok": all(checks.values()), "closure_sha256": recorded, "checks": checks, "changed_files": [r for r, ok in preserved.items() if not ok]}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m qat.research.protocol_v2_closure")
    parser.add_argument("cmd", choices=["close", "verify"])
    args = parser.parse_args(argv)
    print(json.dumps(write_closure() if args.cmd == "close" else verify_closure(), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
