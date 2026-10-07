"""One-shot holdout evaluator INFRASTRUCTURE (reusable by a future protocol) - SYNTHETIC fixtures only. The real 2026 data is never read: no approval
marker exists, the evaluator has never been run on real data, and every test here uses tmp dirs, a synthetic dataset or fake archives.
Protocol v2 itself is CLOSED / NOT_HOLDOUT_ELIGIBLE (see test_protocol_v2_closure.py): the success-path tests below use an ELIGIBLE candidate copy,
a stubbed candidate verification and no closure marker, i.e. they simulate a future protocol, never the Protocol v2 candidate."""

from __future__ import annotations

import hashlib
import io
import json
import pathlib
import zipfile
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from qat.data.loader import load_dataset
from qat.realdata import intraday as it
from qat.research import holdout_eval as ho
from qat.research import protocol_v2_run as run
from qat.research import trial_series

ROOT = pathlib.Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(not (run.CANDIDATE_PATH.exists() and ho.ADDENDUM_PATH.exists()), reason="candidate / holdout addendum not frozen yet")
SYN = ROOT / "data" / "fixtures" / "SYN_CRYPTO1_1h.csv"
REAL_VERIFY = run.verify_candidate
REAL_SERIES = trial_series.TrialSeriesStore(ROOT / "results" / "trial_series")  # the candidate's real development series (read-only verification)
H4 = it.INTERVAL_MS
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _approval(tmp_path, **over):
    cand = json.loads(run.CANDIDATE_PATH.read_text(encoding="utf-8"))
    add = ho.verify_addendum()
    marker = {"approved_by": "user", "approved_utc": "2027-01-02T00:00:00+00:00", "candidate_sha256": cand["candidate_sha256"], "protocol_sha256": cand["protocol_sha256"],
              "development_addendum_sha256": cand["addendum_sha256"], "holdout_addendum_sha256": add["holdout_addendum_sha256"], "full_2026_year_confirmed": True}
    marker.update(over)
    path = tmp_path / "approval.json"
    path.write_text(json.dumps(marker), encoding="utf-8")
    return path


def _provider(*, status="PASS", n_dev=1000, n_total=1500):
    ds = load_dataset(str(SYN))
    fmt = "%Y-%m-%dT%H:%M:%S"
    data = {"dataset": ds, "n_dev": n_dev, "n_total": n_total, "validation": {"status": status, "failed_checks": [] if status == "PASS" else ["I11"]},
            "first_ts": ds.bars[n_dev].ts.strftime(fmt), "last_ts": ds.bars[n_total - 1].ts.strftime(fmt)}
    return (lambda: data), {"bars": 500, "first_ts": data["first_ts"], "last_ts": data["last_ts"]}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("QAT_RESULTS_DIR", str(tmp_path / "results"))
    cand = json.loads(run.CANDIDATE_PATH.read_text(encoding="utf-8"))
    eligible = tmp_path / "eligible_candidate.json"  # a FUTURE-protocol style eligible candidate (the real Protocol v2 candidate is not eligible)
    eligible.write_text(json.dumps({**cand, "holdout_eligible_by_frozen_rule": True}), encoding="utf-8")
    monkeypatch.setattr(ho.run, "verify_candidate", lambda *a, **k: {"ok": True, "candidate_sha256": cand["candidate_sha256"], "checks": {}})
    monkeypatch.setattr(it, "CLOSURE_MARKER", tmp_path / "no_closure.json")
    return {"out": tmp_path / "ho", "store": trial_series.TrialSeriesStore(tmp_path / "series"), "settings": run.write_scenario_settings(tmp_path / "cs"), "tmp": tmp_path,
            "candidate": eligible, "closure": tmp_path / "no_closure.json"}


def go(env, provider_expected, months=ho.REQUIRED_MONTHS, approval=None, **kw):
    provider, expected = provider_expected
    kw.setdefault("candidate_path", env["candidate"])
    kw.setdefault("closure_path", env["closure"])
    return ho.evaluate_holdout(provider=provider, available_months=months, approval_path=approval or _approval(env["tmp"]), out_dir=env["out"], final_train_bars=300,
                               expected=expected, store=env["store"], settings=env["settings"], candidate_store=REAL_SERIES, **kw)


# ------------------------------------------------------------------ preconditions: nothing is read, the attempt is not consumed
def test_a_partial_year_is_refused_without_reading_anything_or_consuming_the_attempt(env):
    def boom():
        raise AssertionError("data was read for a partial year")

    for months in (ho.REQUIRED_MONTHS[:9], ho.REQUIRED_MONTHS[:11], ()):
        with pytest.raises(ho.HoldoutNotReady, match="full-year coverage"):
            ho.evaluate_holdout(provider=boom, available_months=months, approval_path=_approval(env["tmp"]), out_dir=env["out"], store=env["store"], settings=env["settings"],
                                 candidate_store=REAL_SERIES, candidate_path=env["candidate"], closure_path=env["closure"])
    assert not (env["out"] / "attempt.json").exists()


def test_the_approval_marker_must_exist_and_match_the_candidate_protocol_and_addenda(env):
    provider = _provider()
    with pytest.raises(ho.HoldoutNotReady, match="approval marker does not exist"):
        go(env, provider, approval=env["tmp"] / "none.json")
    for field in ("candidate_sha256", "protocol_sha256", "development_addendum_sha256", "holdout_addendum_sha256"):
        with pytest.raises(ho.HoldoutNotReady, match=f"approval field {field}"):
            go(env, provider, approval=_approval(env["tmp"], **{field: "0" * 64}))
    for over, text in (({"approved_by": "someone"}, "approved_by"), ({"full_2026_year_confirmed": False}, "full_2026_year_confirmed"), ({"approved_utc": ""}, "approved_utc")):
        with pytest.raises(ho.HoldoutNotReady, match=text):
            go(env, provider, approval=_approval(env["tmp"], **over))
    assert not (env["out"] / "attempt.json").exists()


def test_a_tampered_candidate_or_addendum_is_rejected(env, monkeypatch):
    monkeypatch.setattr(ho.run, "verify_candidate", REAL_VERIFY)  # real identity verification: the eligible COPY differs from the frozen candidate
    with pytest.raises(ho.HoldoutNotReady, match="candidate identity"):
        go(env, _provider())
    monkeypatch.setattr(ho.run, "verify_candidate", lambda *a, **k: {"ok": True, "checks": {}})
    add = json.loads(ho.ADDENDUM_PATH.read_text(encoding="utf-8"))
    bad_add = env["tmp"] / "add.json"
    bad_add.write_text(json.dumps({**add, "strategy": "ma_trend"}), encoding="utf-8")
    with pytest.raises(ho.HoldoutNotReady, match="addendum"):
        go(env, _provider(), addendum_path=bad_add)
    assert not (env["out"] / "attempt.json").exists()


def test_the_freeze_marker_is_required_too(env, monkeypatch):
    monkeypatch.setattr(it, "FREEZE_MARKER", env["tmp"] / "no_freeze.json")
    with pytest.raises(ho.HoldoutNotReady, match="freeze marker"):
        go(env, _provider())


# ------------------------------------------------------------------ the evaluation itself (synthetic)
def test_one_shot_evaluation_selects_on_the_final_window_only_and_applies_parameters_unchanged(env, monkeypatch):
    calls = []
    real = run.run_backtest

    def spy(dataset, strategy, config):
        calls.append((config.settings_path, config.trade_start, config.trade_end, dict(strategy.params)))
        return real(dataset, strategy, config)

    monkeypatch.setattr(run, "run_backtest", spy)
    result = go(env, _provider())
    assert result["status"] == "EVALUATED" and result["train_window"] == [700, 1000] and result["evaluation_block"] == [1000, 1500]
    scen = {v["path"]: k for k, v in env["settings"].items()}
    train = [(scen[c[0]], c) for c in calls if (c[1], c[2]) == (700, 1000)]
    test = [(scen[c[0]], c) for c in calls if (c[1], c[2]) == (1000, 1500)]
    assert {s for s, _ in train} == set(ho.pv.SELECTION_SCENARIOS) and {s for s, _ in test} == set(ho.run.ALL_SCENARIOS) and len(calls) == len(train) + len(test)
    assert all(c[2] <= 1000 for _, c in train)  # the selection never sees an evaluation bar
    chosen = result["selection"]["chosen_params"]
    assert all(chosen.items() <= c[3].items() for _, c in test)  # selected once, applied unchanged to the whole block
    assert result["statistical_power"] == "UNKNOWN" and result["evidence_language"].startswith("holdout evidence") and set(result["candidate"]["per_scenario"]) == set(ho.pv.SELECTION_SCENARIOS)
    assert result["candidate"]["status"] != "UNKNOWN"  # min_folds=1: a single block can be classified
    assert (env["out"] / "attempt.json").exists() and (env["out"] / "result.sha256").read_text().split()[0] == hashlib.sha256((env["out"] / "result.json").read_bytes()).hexdigest()
    acc = result["trial_accounting"]
    assert acc["all_series_present"] is True and acc["evaluations"] == len(train) + len(test)


def test_the_evaluation_is_exactly_once_and_results_are_immutable(env):
    go(env, _provider())
    with pytest.raises(ho.HoldoutAlreadyEvaluated, match="no retry"):
        go(env, _provider())
    with pytest.raises(FileExistsError):
        ho._create(env["out"] / "result.json", {"overwrite": True})
    assert json.loads((env["out"] / "result.json").read_text())["status"] == "EVALUATED"


def test_a_failure_after_data_access_consumes_the_attempt_and_is_never_retried(env):
    def broken():
        raise RuntimeError("provider crashed after reading")

    with pytest.raises(RuntimeError, match="crashed"):
        go(env, (broken, {"bars": 500, "first_ts": "x", "last_ts": "y"}))
    assert (env["out"] / "attempt.json").exists() and (env["out"] / "failure.json").exists() and not (env["out"] / "result.json").exists()
    with pytest.raises(ho.HoldoutAlreadyEvaluated):
        go(env, _provider())  # no retry after a failure either


def test_unverified_or_incomplete_2026_data_yields_no_performance(env, tmp_path):
    result = go(env, _provider(status="FAIL"))
    assert result["status"] == "HOLDOUT_DATA_NOT_VERIFIED" and "scenario_summaries" not in result and result["performance"] == "NOT COMPUTED"
    env2 = {**env, "out": tmp_path / "ho2"}
    short = go(env2, _provider(n_total=1400))  # 400 bars where 500 are expected: not the complete year
    assert short["status"] == "HOLDOUT_DATA_NOT_VERIFIED" and any("complete expected year" in p for p in short["problems"])
    with pytest.raises(ho.HoldoutAlreadyEvaluated):
        go(env, _provider())


# ------------------------------------------------------------------ the real provider on fake archives (guard + verification)
def _line(open_ms):
    return ",".join(str(x) for x in (open_ms * 1000, 100.0, 105.0, 95.0, 101.0, 10.0, (open_ms + H4 - 1) * 1000, 1010.0, 50, 5.0, 505.0, 0))


def _zip(name, lines):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(name.replace(".zip", ".csv"), "\n".join(lines) + "\n")
    return buf.getvalue()


def _fake_2026(monkeypatch, *, skip=()):
    files = {}
    for m in range(1, 13):
        start = datetime(2026, m, 1, tzinfo=timezone.utc)
        end = datetime(2026 + (m == 12), m % 12 + 1, 1, tzinfo=timezone.utc)
        lines, t = [], start
        while t < end:
            if int((t - EPOCH).total_seconds() * 1000) not in skip:
                lines.append(_line(int((t - EPOCH).total_seconds() * 1000)))
            t += timedelta(hours=4)
        name = f"BTCUSDT-4h-2026-{m:02d}.zip"
        content = _zip(name, lines)
        files[f"{it.BASE}/BTCUSDT/4h/{name}"] = content
        files[f"{it.BASE}/BTCUSDT/4h/{name}.CHECKSUM"] = f"{hashlib.sha256(content).hexdigest()}  {name}\n".encode()
    monkeypatch.setattr(it.http, "get", lambda url, **k: SimpleNamespace(status=200 if url in files else 404, body=files.get(url, b""), error=None))


def _view_csv(tmp_path, last="2025-12-31T20:00:00"):
    t = datetime.strptime(last, "%Y-%m-%dT%H:%M:%S")
    lines = ["timestamp,open,high,low,close,volume"] + [(t - timedelta(hours=4 * k)).strftime("%Y-%m-%dT%H:%M:%S") + ",100,105,95,101,10" for k in range(5, -1, -1)]
    path = tmp_path / "view.csv"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_the_real_provider_is_guarded_and_verifies_the_complete_year(tmp_path, monkeypatch):
    _fake_2026(monkeypatch)
    monkeypatch.setattr(it, "CLOSURE_MARKER", tmp_path / "no_closure.json")  # simulate a future protocol; Protocol v2 is closed
    provide = ho.real_provider(view_csv=_view_csv(tmp_path), raw_root=tmp_path / "raw", work_dir=tmp_path / "w")
    with pytest.raises(it.HoldoutAccessError):  # no freeze + approval markers: the guard blocks the fetch before any 2026 byte is requested
        provide()
    for name in ("FREEZE_MARKER", "APPROVAL_MARKER"):
        marker = tmp_path / f"{name}.json"
        marker.write_text("{}", encoding="utf-8")
        monkeypatch.setattr(it, name, marker)
    data = provide()
    assert data["validation"]["status"] == "PASS" and data["n_dev"] == 6 and data["n_total"] == 6 + ho.EXPECTED["bars"]
    assert (data["first_ts"], data["last_ts"]) == (ho.EXPECTED["first_ts"], ho.EXPECTED["last_ts"]) and len(data["dataset"].bars) == data["n_total"]


def test_the_real_provider_flags_gaps_and_non_contiguous_views(tmp_path, monkeypatch):
    monkeypatch.setattr(it, "CLOSURE_MARKER", tmp_path / "no_closure.json")
    for name in ("FREEZE_MARKER", "APPROVAL_MARKER"):
        marker = tmp_path / f"{name}.json"
        marker.write_text("{}", encoding="utf-8")
        monkeypatch.setattr(it, name, marker)
    skip = {int((datetime(2026, 5, 10, 8, tzinfo=timezone.utc) - EPOCH).total_seconds() * 1000)}
    _fake_2026(monkeypatch, skip=skip)
    gap = ho.real_provider(view_csv=_view_csv(tmp_path), raw_root=tmp_path / "raw1", work_dir=tmp_path / "w1")()
    assert gap["validation"]["status"] == "FAIL" and "I11" in gap["validation"]["failed_checks"]  # a missing 2026 bar is never repaired
    _fake_2026(monkeypatch)
    apart = ho.real_provider(view_csv=_view_csv(tmp_path, last="2025-12-31T16:00:00"), raw_root=tmp_path / "raw2", work_dir=tmp_path / "w2")()
    assert apart["validation"]["status"] == "FAIL" and "contiguity_with_view" in apart["validation"]["failed_checks"]


# ------------------------------------------------------------------ real repository state
def test_the_real_holdout_has_never_been_touched():
    assert not ho.HOLDOUT_DIR.exists() and not it.APPROVAL_MARKER.exists() and it.holdout_unlocked() is False
    assert not ho.RAW_HOLDOUT_ROOT.exists() and not [p for p in it.RAW_4H_ROOT.rglob("*") if "-2026-" in p.name]
    assert not (ROOT / "results" / "lockbox_registry.json").exists() and "evaluate_lockbox" not in dir(ho)
    assert ho.verify_addendum()["ok"] is True and run.verify_candidate()["ok"] is True
