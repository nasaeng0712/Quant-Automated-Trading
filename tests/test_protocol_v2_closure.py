"""Protocol v2 is CLOSED / NOT_HOLDOUT_ELIGIBLE: frozen eligibility cannot be bypassed by user approval, a closed protocol cannot unlock 2026 values,
and the historical development evidence is preserved. Nothing here reads 2026 values or the daily Lockbox."""

from __future__ import annotations

import json
import pathlib
import shutil
from types import SimpleNamespace

import pytest

from qat.realdata import intraday as it
from qat.research import holdout_eval as ho
from qat.research import protocol_v2 as pv
from qat.research import protocol_v2_closure as cl
from qat.research import protocol_v2_run as run

ROOT = pathlib.Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(not cl.CLOSURE_PATH.exists(), reason="Protocol v2 closure not recorded yet")


def _marker(path, **fields):
    path.write_text(json.dumps(fields), encoding="utf-8")
    return path


def _full_approval(tmp_path, candidate, **over):
    add = ho.verify_addendum()
    fields = {"approved_by": "user", "approved_utc": "2027-01-02T00:00:00+00:00", "candidate_sha256": candidate["candidate_sha256"], "protocol_sha256": candidate["protocol_sha256"],
              "development_addendum_sha256": candidate["addendum_sha256"], "holdout_addendum_sha256": add["holdout_addendum_sha256"], "full_2026_year_confirmed": True,
              "acknowledged_not_holdout_eligible": True}  # even the old acknowledgement field cannot help any more
    fields.update(over)
    return _marker(tmp_path / "approval.json", **fields)


def test_the_closure_records_the_distinct_states_and_preserves_the_historical_evidence():
    data = json.loads(cl.CLOSURE_PATH.read_text(encoding="utf-8"))
    d = data["distinctions"]
    assert d == {"development_candidate": "breakout", "development_status": "PASS", "benchmark_eligibility": "FAIL", "temporal_holdout_eligibility": False,
                 "statistical_power": "UNKNOWN", "protocol_v2_final_state": "CLOSED / NOT_HOLDOUT_ELIGIBLE"}
    assert data["final_label"] == "DEVELOPMENT_SELECTED_CANDIDATE — NOT_HOLDOUT_ELIGIBLE" == cl.FINAL_LABEL
    detail = data["benchmark_eligibility_detail"]
    assert detail["status"] == "FAIL" and all(v["exceeds_aligned_passive"] is False for v in detail["per_scenario"].values())  # computed from the frozen figures, not asserted
    assert data["holdout_approval_path"]["deactivated_for_protocol_v2"] is True and data["holdout_approval_path"]["user_approval_cannot_bypass_frozen_eligibility"] is True
    assert {"performance figures", "thresholds (30 trades / 50% active folds / positive-fold majority)", "parameter grids"} <= set(data["unchanged"])
    ok = cl.verify_closure()
    assert ok["ok"] is True and ok["changed_files"] == [] and all(ok["checks"].values())
    cand = json.loads(run.CANDIDATE_PATH.read_text(encoding="utf-8"))
    assert cand["holdout_eligible_by_frozen_rule"] is False and data["candidate_sha256"] == cand["candidate_sha256"] and data["protocol_sha256"] == pv.verify_frozen()["protocol_sha256"]


def test_tampering_with_preserved_development_evidence_is_detected(tmp_path):
    for rel in cl.PRESERVED:
        dest = tmp_path / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(pv.OUT_DIR / rel, dest)
    shutil.copy(cl.CLOSURE_PATH, tmp_path / "closure.json")
    shutil.copy(cl.CLOSURE_HASH_PATH, tmp_path / "closure.sha256")
    assert cl.verify_closure(tmp_path / "closure.json", tmp_path / "closure.sha256", tmp_path)["ok"] is True
    results = tmp_path / "development_run_1" / "results.json"
    results.write_text(results.read_text(encoding="utf-8").replace("breakout", "breakoutX", 1), encoding="utf-8")
    bad = cl.verify_closure(tmp_path / "closure.json", tmp_path / "closure.sha256", tmp_path)
    assert bad["ok"] is False and bad["changed_files"] == ["development_run_1/results.json"]


def test_user_approval_cannot_bypass_frozen_eligibility(tmp_path):
    """A fully valid-looking approval for the real Protocol v2 candidate (all hashes, full year, freeze marker present) is still refused, before any value is read."""

    cand = json.loads(run.CANDIDATE_PATH.read_text(encoding="utf-8"))

    def provider():
        raise AssertionError("data was read for a candidate that is not holdout-eligible")

    for closure_path in (None, tmp_path / "no_closure.json"):  # with the real closure AND with the closure removed: the eligibility rule alone refuses
        with pytest.raises(ho.ProtocolClosed, match="CLOSED|NOT_HOLDOUT_ELIGIBLE"):
            ho.evaluate_holdout(provider=provider, available_months=ho.REQUIRED_MONTHS, approval_path=_full_approval(tmp_path, cand), out_dir=tmp_path / "ho", closure_path=closure_path)
    assert not (tmp_path / "ho" / "attempt.json").exists()
    problems = ho.check_approval(_full_approval(tmp_path, cand), cand, ho.verify_addendum()["holdout_addendum_sha256"])
    assert problems and "NOT_HOLDOUT_ELIGIBLE" in problems[0] and "cannot bypass" in problems[0]
    eligible = {**cand, "holdout_eligible_by_frozen_rule": True}
    assert ho.check_approval(_full_approval(tmp_path, cand), eligible, ho.verify_addendum()["holdout_addendum_sha256"]) == []  # infrastructure still works for an ELIGIBLE (future) candidate


def test_the_closed_protocols_candidate_cannot_run_even_if_its_eligibility_flag_is_forged(tmp_path):
    cand = json.loads(run.CANDIDATE_PATH.read_text(encoding="utf-8"))
    forged = tmp_path / "forged.json"
    forged.write_text(json.dumps({**cand, "holdout_eligible_by_frozen_rule": True}), encoding="utf-8")

    def provider():
        raise AssertionError("data was read for a closed protocol")

    with pytest.raises(ho.ProtocolClosed, match="CLOSED"):
        ho.evaluate_holdout(provider=provider, available_months=ho.REQUIRED_MONTHS, approval_path=_full_approval(tmp_path, cand), out_dir=tmp_path / "ho2", candidate_path=forged)
    assert not (tmp_path / "ho2").exists()


def test_forged_freeze_and_approval_markers_cannot_unlock_2026_while_the_protocol_is_closed(tmp_path, monkeypatch):
    for name in ("FREEZE_MARKER", "APPROVAL_MARKER"):
        monkeypatch.setattr(it, name, _marker(tmp_path / f"{name}.json"))
    assert it.CLOSURE_MARKER.exists() and it.holdout_unlocked() is False
    with pytest.raises(it.HoldoutAccessError):
        it.assert_not_holdout(2026, 3)
    monkeypatch.setattr(it.http, "get", lambda *a, **k: (_ for _ in ()).throw(AssertionError("network used for a 2026 month")))
    with pytest.raises(it.HoldoutAccessError):
        it.fetch_month(2026, 1, root=tmp_path / "raw")
    monkeypatch.setattr(it, "CLOSURE_MARKER", tmp_path / "no_closure.json")
    assert it.holdout_unlocked() is True  # the closure marker is what locks it: a FUTURE protocol (own markers/closure state) can reuse the infrastructure


def test_no_code_path_creates_the_approval_marker_and_the_real_holdout_is_untouched():
    writers = []
    for path in (ROOT / "src").rglob("*.py"):
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "APPROVAL_MARKER" in line and any(k in line for k in ("write_text", "write_bytes", ".open(", "touch(", "mkdir")):
                writers.append(f"{path.name}:{n}")
    assert writers == []
    assert not it.APPROVAL_MARKER.exists() and not ho.HOLDOUT_DIR.exists() and not ho.RAW_HOLDOUT_ROOT.exists()
    assert not [p for p in it.RAW_4H_ROOT.rglob("*") if "-2026-" in p.name] and not (ROOT / "results" / "lockbox_registry.json").exists()
    assert "evaluate_lockbox" not in dir(cl) and "evaluate_lockbox" not in dir(ho)
    assert SimpleNamespace  # (imported for symmetry with the other protocol tests)


def test_benchmark_eligibility_needs_every_scenario_to_beat_the_aligned_passive_return():
    def cand(a, b, c):
        mk = lambda v: {"stitched_net_return": v, "aligned_passive_stitched_return": 1.0}  # noqa: E731
        return {"development_vs_aligned_passive": {"S1_low": mk(a), "S2_mid": mk(b), "S3_high": mk(c)}}

    assert cl.benchmark_eligibility(cand(2.0, 2.0, 2.0))["status"] == "PASS"
    assert cl.benchmark_eligibility(cand(2.0, 2.0, 0.5))["status"] == "FAIL"  # one scenario below passive is enough to fail
    assert cl.benchmark_eligibility(cand(0.9, 0.9, 0.9))["status"] == "FAIL"


def test_closure_verification_reports_an_unlocked_holdout(tmp_path, monkeypatch):
    for name in ("FREEZE_MARKER", "APPROVAL_MARKER"):
        monkeypatch.setattr(it, name, _marker(tmp_path / f"{name}.json"))
    monkeypatch.setattr(it, "CLOSURE_MARKER", tmp_path / "gone.json")  # the lock is gone: verification must say so (closure FILE is read from its own path)
    result = cl.verify_closure()
    assert result["ok"] is False and result["checks"]["holdout_locked"] is False and result["checks"]["no_approval_marker"] is False
