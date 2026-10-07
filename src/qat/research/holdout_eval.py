"""One-shot Protocol v2 2026 temporal-holdout evaluator (Batch: integrated v2 development execution).

THIS MODULE HAS NEVER BEEN RUN ON 2026 DATA. It exists, tested on synthetic fixtures only, so that a later, user-approved Batch can evaluate the
development-selected candidate exactly once. Nothing here creates the approval marker.

Evaluation design (fixed in ``holdout_evaluation_addendum_v1`` before any 2026 value is read):
  final selection window = the last 4,380 bars of the continuous research view (ending 2025-12-31T20:00);
  every configuration of the existing grid x S1/S2/S3 on that window -> rank by the WORST train score -> parameters P*;
  P* is applied, without retraining, to the COMPLETE 2026 year (one OOS block of 2,190 bars; earlier bars are warm-up history only)
  under S1/S2/S3 (S0 diagnostic); status = the frozen thresholds via ``classify_candidate`` with ``min_folds=1`` (a single block).

Guards: full 2026 coverage (12 monthly archives, every expected 4h bar present), explicit user approval marker bound to the candidate/protocol/
addenda hashes, candidate + protocol + addendum verification, exactly-once semantics (an attempt file is created before any value is read and is never
removed: no retry after a result, no retry after a failure), immutable result storage, no partial-year evaluation.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import pathlib
import tempfile

from qat.data.loader import load_dataset
from qat.realdata import intraday
from qat.realdata.provenance import verify_artifact
from qat.research import protocol_v2 as pv
from qat.research import protocol_v2_run as run
from qat.research import trial_series

HOLDOUT_DIR = pv.OUT_DIR / "holdout_evaluation"
ADDENDUM_PATH = pv.OUT_DIR / "holdout_evaluation_addendum_v1.json"
ADDENDUM_HASH_PATH = pv.OUT_DIR / "holdout_evaluation_addendum_v1.sha256"
RAW_HOLDOUT_ROOT = intraday.RAW_ROOT / "_intraday_4h_holdout"
REQUIRED_MONTHS = tuple(f"2026-{m:02d}" for m in range(1, 13))
EXPECTED = {"first_ts": "2026-01-01T00:00:00", "last_ts": "2026-12-31T20:00:00", "bars": 2190}
PROTOCOL_VERSION = "wf-protocol-2-holdout"
RUN_ID = "protocol-v2-holdout-evaluation"


class HoldoutNotReady(RuntimeError):
    """Preconditions are not met; no value was read and the one-shot attempt is NOT consumed."""


class ProtocolClosed(HoldoutNotReady):
    """The candidate's protocol is CLOSED (not holdout-eligible by its frozen rule): it can never be evaluated on the holdout."""


class HoldoutAlreadyEvaluated(RuntimeError):
    """An attempt already exists (completed or failed): the holdout is consumed; there is no retry."""


def _sha(obj) -> str:
    return pv.sha(obj)


# ------------------------------------------------------------------ pre-evaluation addendum (immutable)
def addendum_body(candidate: dict, *, now: str | None = None) -> dict:
    worst = candidate["development_status"]["worst_case"]
    oos_years = 2.0
    return {
        "id": "holdout_evaluation_addendum_v1", "created_utc": now or dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
        "nature": "PRE-EVALUATION immutable addendum: written before any 2026 value is read; the frozen Protocol v2 hash is not modified",
        "candidate_sha256": candidate["candidate_sha256"], "protocol_sha256": candidate["protocol_sha256"], "development_addendum_sha256": candidate["addendum_sha256"],
        "strategy": candidate["strategy"],
        "evaluation_design": {
            "unit": "the whole frozen selection procedure for the selected strategy, evaluated exactly once",
            "final_selection_window": f"the last {pv.TRAIN_BARS} bars of the continuous research view (ending 2025-12-31T20:00)",
            "selection": "every grid configuration x S1/S2/S3 on that window; rank by the WORST train score across S1/S2/S3; ties -> grid order",
            "evaluation_block": "the COMPLETE year 2026-01-01T00:00 .. 2026-12-31T20:00 as ONE out-of-sample block; parameters are not changed inside 2026; earlier bars are warm-up history only",
            "scenarios": {"decision": list(pv.SELECTION_SCENARIOS), "diagnostic_only": list(pv.DIAGNOSTIC_SCENARIOS)},
            "status_rule": "frozen thresholds via classify_candidate(min_folds=1): the single 2026 block is one fold; closed trades >= 30, active fold, profitable after costs, "
                           "positive-fold majority (1/1); candidate status = least favourable of S1/S2/S3; 'holdout_eligible_like' additionally needs beating the aligned passive benchmark in every scenario",
            "min_folds_override": "1, only because a single holdout block cannot contain 3 folds; no other threshold is changed",
            "benchmark": "aligned passive (entry open(start+1)), cost-free, fully invested",
            "language": "holdout evidence (one-shot); no proof of alpha, robustness or profitability is claimed; statistical power UNKNOWN"},
        "known_risk_disclosed_before_evaluation": {
            "development_oos_closed_trades_worst_case": worst["worst_oos_closed_trades"], "development_oos_years": oos_years,
            "implied_closed_trades_per_year": worst["worst_oos_closed_trades"] / oos_years,
            "note": "a one-year block at the development trade rate gives fewer than the frozen 30 closed trades, so INSUFFICIENT_ACTIVITY is a likely outcome of the frozen heuristic (counts only; UNKNOWN)"},
        "data_requirements": {"months": list(REQUIRED_MONTHS), "expected_bars": EXPECTED["bars"], "first_bar": EXPECTED["first_ts"], "last_bar": EXPECTED["last_ts"],
                              "checks": "official checksums, immutable raw (separate holdout raw root), strict 4h validation (continuity, interval, units, symbol, source, synthetic), contiguity with the view end",
                              "partial_year": "forbidden: any missing monthly archive or missing/short bar prevents (or, after values were read, invalidates) the evaluation",
                              "data_not_verified": "if the 2026 data fails verification AFTER values were read, the single result is HOLDOUT_DATA_NOT_VERIFIED and the attempt stays consumed"},
        "approval": {"marker": "artifacts/verification/protocol_v2/holdout_evaluation_approval.json",
                     "required_fields": ["approved_by == user", "approved_utc", "candidate_sha256", "protocol_sha256", "development_addendum_sha256", "holdout_addendum_sha256",
                                         "full_2026_year_confirmed == true", "acknowledged_not_holdout_eligible (required true when the candidate is not holdout-eligible by the frozen rule)"],
                     "created_by": "only a separate, user-approved one-shot Batch"},
        "exactly_once": {"attempt_file": "created exclusively before any value is read, never removed", "after_result": "no retry", "after_failure": "no retry (failure.json is stored)",
                         "result": "immutable (exclusive create) with its hash"},
        "forbidden": ["partial-year evaluation", "re-evaluation after a result", "changing parameters after seeing 2026", "using the existing daily Lockbox"]}


def write_addendum() -> dict:
    if ADDENDUM_PATH.exists():
        raise RuntimeError("the holdout evaluation addendum already exists and is immutable")
    if run.verify_candidate()["ok"] is not True:
        raise RuntimeError("the development-selected candidate does not verify")
    candidate = json.loads(run.CANDIDATE_PATH.read_text(encoding="utf-8"))
    if intraday.APPROVAL_MARKER.exists() or HOLDOUT_DIR.exists():
        raise RuntimeError("a holdout approval or evaluation already exists: the addendum would not be pre-evaluation")
    body = addendum_body(candidate)
    digest = _sha(body)
    with ADDENDUM_PATH.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps({**body, "holdout_addendum_sha256": digest}, indent=1, ensure_ascii=False, sort_keys=True) + "\n")
    with ADDENDUM_HASH_PATH.open("x", encoding="utf-8") as handle:
        handle.write(f"{digest}  holdout_evaluation_addendum_v1.json (sha256 of the canonical body without holdout_addendum_sha256)\n")
    return {"holdout_addendum_sha256": digest}


def verify_addendum(path: pathlib.Path = ADDENDUM_PATH, hash_path: pathlib.Path = ADDENDUM_HASH_PATH) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    recorded = data.pop("holdout_addendum_sha256")
    return {"ok": _sha(data) == recorded == hash_path.read_text(encoding="utf-8").split()[0], "holdout_addendum_sha256": recorded, "candidate_sha256": data["candidate_sha256"]}


# ------------------------------------------------------------------ preconditions (no value is read; the attempt is not consumed)
def check_approval(marker_path: pathlib.Path, candidate: dict, holdout_addendum_sha: str) -> list[str]:
    if not pathlib.Path(marker_path).exists():
        return ["the user approval marker does not exist"]
    try:
        marker = json.loads(pathlib.Path(marker_path).read_text(encoding="utf-8"))
    except ValueError:
        return ["the approval marker is not valid JSON"]
    if candidate["holdout_eligible_by_frozen_rule"] is not True:
        return ["the candidate is NOT_HOLDOUT_ELIGIBLE by the frozen rule: user approval cannot bypass frozen eligibility"]
    problems = []
    want = {"approved_by": "user", "candidate_sha256": candidate["candidate_sha256"], "protocol_sha256": candidate["protocol_sha256"],
            "development_addendum_sha256": candidate["addendum_sha256"], "holdout_addendum_sha256": holdout_addendum_sha, "full_2026_year_confirmed": True}
    problems += [f"approval field {k} does not match" for k, v in want.items() if marker.get(k) != v]
    if not marker.get("approved_utc"):
        problems.append("approval has no approved_utc")
    return problems


def preconditions(*, available_months, approval_path, candidate_path, addendum_path, addendum_hash_path, out_dir, candidate_store=None, closure_path=None) -> dict:
    out_dir = pathlib.Path(out_dir)
    candidate_header = json.loads(pathlib.Path(candidate_path).read_text(encoding="utf-8"))
    closure = pathlib.Path(closure_path if closure_path is not None else intraday.CLOSURE_MARKER)
    if closure.exists() and json.loads(closure.read_text(encoding="utf-8")).get("protocol_sha256") == candidate_header.get("protocol_sha256"):
        raise ProtocolClosed("this candidate's protocol is CLOSED / NOT_HOLDOUT_ELIGIBLE: it cannot be evaluated on the holdout")
    if candidate_header.get("holdout_eligible_by_frozen_rule") is not True:
        raise ProtocolClosed("the candidate is NOT_HOLDOUT_ELIGIBLE by the frozen rule: user approval cannot bypass frozen eligibility")
    if (out_dir / "attempt.json").exists() or (out_dir / "result.json").exists() or (out_dir / "failure.json").exists():
        raise HoldoutAlreadyEvaluated("the holdout evaluation was already attempted: it is one-shot, there is no retry")
    problems = []
    missing = [m for m in REQUIRED_MONTHS if m not in set(available_months)]
    if missing:
        problems.append(f"full-year coverage is required: missing {len(missing)} monthly archives ({missing[0]} ...); partial-year evaluation is forbidden")
    if not intraday.FREEZE_MARKER.exists():
        problems.append("the Protocol v2 freeze marker does not exist")
    cand_check = run.verify_candidate(candidate_path, store=candidate_store)
    if not cand_check["ok"]:
        problems.append(f"candidate identity does not verify: {cand_check['checks']}")
    add_check = verify_addendum(addendum_path, addendum_hash_path)
    if not add_check["ok"]:
        problems.append("the holdout evaluation addendum does not verify")
    candidate = json.loads(pathlib.Path(candidate_path).read_text(encoding="utf-8"))
    if add_check["candidate_sha256"] != candidate["candidate_sha256"]:
        problems.append("the holdout addendum belongs to a different candidate")
    problems += check_approval(approval_path, candidate, add_check["holdout_addendum_sha256"])
    if problems:
        raise HoldoutNotReady("; ".join(problems))
    return {"candidate": candidate, "holdout_addendum_sha256": add_check["holdout_addendum_sha256"]}


# ------------------------------------------------------------------ the evaluation
def _create(path: pathlib.Path, obj) -> None:
    with path.open("x", encoding="utf-8") as handle:  # exclusive create: never overwrites
        handle.write(json.dumps(obj, indent=1, ensure_ascii=False, sort_keys=True, default=str) + "\n")


def evaluate_holdout(*, provider, available_months, approval_path=None, out_dir=None, candidate_path=None, addendum_path=None, addendum_hash_path=None,
                     final_train_bars: int = pv.TRAIN_BARS, expected: dict | None = None, store: trial_series.TrialSeriesStore | None = None,
                     settings: dict | None = None, candidate_store: trial_series.TrialSeriesStore | None = None, closure_path=None) -> dict:
    """``provider()`` -> {"dataset", "n_dev", "n_total", "validation": {"status", "failed_checks"}, "first_ts", "last_ts"} (the combined dev view + 2026 series)."""

    out_dir = pathlib.Path(out_dir or HOLDOUT_DIR)
    expected = expected or EXPECTED
    pre = preconditions(available_months=available_months, approval_path=approval_path or intraday.APPROVAL_MARKER, candidate_path=candidate_path or run.CANDIDATE_PATH,
                        addendum_path=addendum_path or ADDENDUM_PATH, addendum_hash_path=addendum_hash_path or ADDENDUM_HASH_PATH, out_dir=out_dir,
                        candidate_store=candidate_store, closure_path=closure_path)
    candidate = pre["candidate"]
    out_dir.mkdir(parents=True, exist_ok=True)
    meta = {"candidate_sha256": candidate["candidate_sha256"], "protocol_sha256": candidate["protocol_sha256"], "holdout_addendum_sha256": pre["holdout_addendum_sha256"],
            "strategy": candidate["strategy"]}
    _create(out_dir / "attempt.json", {"status": "STARTED", "started_utc": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(), **meta})  # consumed from here on
    try:
        data = provider()
        problems = []
        if data["validation"]["status"] != "PASS":
            problems.append(f"2026 data validation {data['validation']['status']}: {data['validation'].get('failed_checks')}")
        if data["n_total"] - data["n_dev"] != expected["bars"] or data["first_ts"] != expected["first_ts"] or data["last_ts"] != expected["last_ts"]:
            problems.append("the 2026 series is not the complete expected year")
        if problems:
            result = {"meta": meta, "status": "HOLDOUT_DATA_NOT_VERIFIED", "problems": problems, "performance": "NOT COMPUTED", "values_read": "YES (verification only)"}
        else:
            ds, n_dev, n_total = data["dataset"], data["n_dev"], data["n_total"]
            boundaries = [{"fold": 0, "train": [n_dev - final_train_bars, n_dev], "test": [n_dev, n_total]}]
            settings = settings or run.write_scenario_settings()
            res = run.run_development(ds, boundaries, settings, store=store or trial_series.TrialSeriesStore(), strategies=(candidate["strategy"],), protocol_version=PROTOCOL_VERSION,
                                      run_id=RUN_ID, min_folds=1)
            strat = res["strategies"][candidate["strategy"]]
            result = {"meta": meta, "status": "EVALUATED", "evidence_language": "holdout evidence (one-shot)", "statistical_power": "UNKNOWN",
                      "selection": strat["folds"][0].get("selection"), "train_window": boundaries[0]["train"], "evaluation_block": boundaries[0]["test"],
                      "scenario_summaries": {s: {k: v for k, v in sm.items() if k != "fold_rows"} for s, sm in strat["scenario_summaries"].items()},
                      "candidate": strat["candidate"], "holdout_eligible_like": strat["candidate"]["holdout_eligible"], "trial_accounting": res["trial_accounting"]}
        _create(out_dir / "result.json", result)
        (out_dir / "result.sha256").write_text(hashlib.sha256((out_dir / "result.json").read_bytes()).hexdigest() + "  result.json\n", encoding="utf-8")
        return result
    except Exception as exc:  # noqa: BLE001 - the attempt is consumed; the failure is stored, never retried
        if not (out_dir / "result.json").exists():
            _create(out_dir / "failure.json", {"meta": meta, "error": f"{type(exc).__name__}: {exc}", "note": "the one-shot attempt is consumed; no retry"})
        raise


# ------------------------------------------------------------------ the real data provider (used only by the future approved evaluation)
def real_provider(*, view_csv: pathlib.Path = run.VIEW_CSV, raw_root: pathlib.Path = RAW_HOLDOUT_ROOT, work_dir: pathlib.Path | None = None):
    def provide() -> dict:
        for m in range(1, 13):
            intraday.fetch_month(2026, m, root=raw_root)  # raises HoldoutAccessError unless the freeze AND approval markers exist
        rows, raw_meta = [], []
        for path in sorted((p for p in intraday.raw_artifacts(raw_root)), key=lambda p: p.name):
            art = verify_artifact(path)
            raw_meta.append({"name": path.name, "sha256": art.sidecar["sha256"], "size_bytes": art.sidecar["size_bytes"], "sidecar": art.sidecar})
            rows.extend(intraday.parse_klines(path.read_bytes(), source_file=path.name))
        tz = dt.timezone.utc
        report = intraday.validate(rows, raw_meta, first_expected=dt.datetime(2026, 1, 1, tzinfo=tz), last_expected=dt.datetime(2026, 12, 31, 20, tzinfo=tz), holdout_phase=True)
        canon, _ = intraday.canonical_lines(rows)
        view_text = view_csv.read_text(encoding="utf-8")
        n_dev = len(view_text.splitlines()) - 1
        view_last = dt.datetime.strptime(view_text.splitlines()[-1].split(",", 1)[0], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=tz)
        if rows and rows[0]["open_dt"] - view_last != dt.timedelta(hours=4):
            report = {**report, "status": "FAIL", "failed_checks": [*report["failed_checks"], "contiguity_with_view"]}
        work = pathlib.Path(work_dir or tempfile.mkdtemp(prefix="qat_holdout_"))
        work.mkdir(parents=True, exist_ok=True)
        combined = work / "combined_4h.csv"
        combined.write_text(view_text + "\n".join(canon[1:]) + "\n", encoding="utf-8")
        meta = {"market": "CRYPTO", "symbol": "BTC/USDT", "timeframe": "4h", "timezone": "UTC", "timestamp_label": "open", "source": "protocol-v2-holdout-composite", "synthetic": False,
                "description": "continuous research view + verified 2026 holdout bars (one-shot evaluation only)"}
        (work / "combined_4h.csv.meta.json").write_text(json.dumps(meta), encoding="utf-8")
        ds = load_dataset(str(combined))
        return {"dataset": ds, "n_dev": n_dev, "n_total": len(ds.bars), "validation": report,
                "first_ts": rows[0]["open_dt"].strftime("%Y-%m-%dT%H:%M:%S") if rows else None, "last_ts": rows[-1]["open_dt"].strftime("%Y-%m-%dT%H:%M:%S") if rows else None}
    return provide


def status() -> dict:
    """Readiness from metadata only (file names): no value is read."""

    entries = [e for e in intraday.archive_metadata()["entries"] if "-2026-" in e["key"]]
    months = sorted(e["key"].rsplit("-4h-", 1)[1][:7] for e in entries)
    missing = [m for m in REQUIRED_MONTHS if m not in months]
    closed = intraday.CLOSURE_MARKER.exists()
    return {"months_available": months, "months_missing": missing, "full_year_available": not missing, "approval_marker_exists": intraday.APPROVAL_MARKER.exists(),
            "attempt_exists": (HOLDOUT_DIR / "attempt.json").exists(), "protocol_v2_closed": closed,
            "ready": False if (closed or missing or not intraday.APPROVAL_MARKER.exists()) else None, "values_read": "NO"}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m qat.research.holdout_eval")
    parser.add_argument("cmd", choices=["addendum", "verify-addendum", "status", "evaluate"])
    args = parser.parse_args(argv)
    if args.cmd == "addendum":
        print(json.dumps(write_addendum(), indent=1))
    elif args.cmd == "verify-addendum":
        print(json.dumps(verify_addendum(), indent=1))
    elif args.cmd == "status":
        print(json.dumps(status(), indent=1))
    else:
        months = status()["months_available"]
        print(json.dumps(evaluate_holdout(provider=real_provider(), available_months=months), indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
