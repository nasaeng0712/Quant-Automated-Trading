"""Batch #3B - frozen Walk-Forward protocol: unit tests (pure) + one real-data integration run (KR+US).

Nothing here opens a Lockbox. Integration runs write to a pytest tmp results dir and tmp evidence dir.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from qat.data.bars import DataRejected
from qat.data.loader import load_dataset
from qat.realdata.admission import require_coverage
from qat.research import protocol as proto
from qat.research import walkforward as wf
from qat.research.walkforward import WalkForwardConfig

ROOT = pathlib.Path(__file__).resolve().parents[1]
HAVE_REAL = all((ROOT / proto.MARKETS[m]).exists() for m in ("KR", "US"))


# ------------------------------------------------------------------ pure / unit
def test_fold_generation_honours_start_bar_and_never_enters_the_lockbox():
    cfg = WalkForwardConfig(train_bars=100, test_bars=20, lockbox_bars=50, start_bar=40)
    folds = cfg.folds(400)
    assert folds[0]["train"] == [40, 140] and folds[0]["test"] == [140, 160]
    assert all(f["test"][0] == f["train"][1] and f["test"][1] <= 400 - 50 for f in folds)
    assert all(b["train"][0] - a["train"][0] == 20 for a, b in zip(folds, folds[1:]))  # step = test, non-overlapping OOS
    default = WalkForwardConfig(train_bars=100, test_bars=20, lockbox_bars=50).folds(400)
    assert default[0]["train"] == [0, 100]  # start_bar default keeps the existing behaviour
    with pytest.raises(ValueError):
        WalkForwardConfig(train_bars=100, test_bars=20, start_bar=-1).folds(400)
    with pytest.raises(ValueError):
        WalkForwardConfig(train_bars=100, test_bars=20, lockbox_bars=50, start_bar=250).folds(400)  # no fold is invented


@pytest.mark.parametrize("market,bars,expected_folds", [("KR", 1473, 5), ("US", 2011, 9), ("CRYPTO", 2922, 9)])
def test_market_specific_windows_use_each_markets_own_coverage(market, bars, expected_folds):
    train, test, lockbox = proto.WINDOWS[market]
    folds = WalkForwardConfig(train_bars=train, test_bars=test, lockbox_bars=lockbox).folds(bars)
    assert len(folds) == expected_folds and folds[-1]["test"][1] <= bars - lockbox


def test_common_period_is_the_intersection_and_disjoint_coverage_has_none():
    cov = {"KR": ("2020-01-02", "2025-12-30"), "US": ("2018-01-02", "2025-12-31"), "CRYPTO": ("2018-01-01", "2025-12-31")}
    assert proto.common_period(cov) == ("2020-01-02", "2025-12-30")
    assert proto.common_period({"A": ("2018-01-01", "2019-01-01"), "B": ("2020-01-01", "2021-01-01")}) is None
    assert proto.first_index_on_or_after(["2019-12-31", "2020-01-02", "2020-01-03"], "2020-01-01") == 1
    with pytest.raises(ValueError):
        proto.first_index_on_or_after(["2019-12-31"], "2020-01-01")


def _summary(**over):
    base = {"folds": 5, "folds_ok": 5, "stitched_net_return": 0.10, "positive_folds": 4, "active_folds": 5,
            "total_oos_trades": 40, "passive_stitched_return": 0.05}
    base.update(over)
    return base


def test_classification_follows_the_frozen_thresholds():
    ok = {2.0: 0.04}
    assert proto.classify(_summary(), ok)["status"] == "PASS"
    assert proto.classify(_summary(), ok)["lockbox_candidate"] is True
    assert proto.classify(_summary(passive_stitched_return=0.5), ok)["lockbox_candidate"] is False  # PASS but not better than passive
    assert proto.classify(_summary(total_oos_trades=5), ok)["status"] == "INSUFFICIENT_ACTIVITY"  # profit with ~no trades is not success
    assert proto.classify(_summary(active_folds=2), ok)["status"] == "INSUFFICIENT_ACTIVITY"
    assert proto.classify(_summary(stitched_net_return=-0.01), ok)["status"] == "FAIL"
    assert proto.classify(_summary(positive_folds=2), ok)["status"] == "UNSTABLE"
    assert proto.classify(_summary(), {2.0: -0.01})["status"] == "UNSTABLE"
    assert proto.classify(_summary(), {})["status"] == "UNSTABLE"  # no stress evidence is not a pass
    assert proto.classify(_summary(folds_ok=4), ok)["status"] == "UNKNOWN"  # a failed fold hides information
    assert proto.classify(_summary(folds=2, folds_ok=2), ok)["status"] == "UNKNOWN"
    assert proto.classify(_summary(), ok, admitted=False)["status"] == "UNKNOWN"
    assert proto.classify(_summary(stitched_net_return=None), ok)["status"] == "UNKNOWN"


def test_passive_benchmark_is_open_to_close_over_the_oos_window():
    class B:
        def __init__(self, o, c):
            self.open, self.close = o, c

    bars = [B(10, 10), B(10, 11), B(11, 12), B(12, 15)]
    assert proto.passive_return(bars, [1, 4]) == pytest.approx(15 / 10 - 1)


def test_protocol_is_frozen_hash_stable_and_a_changed_protocol_is_refused(tmp_path, monkeypatch):
    a, b = proto.protocol_definition(), proto.protocol_definition()
    assert a["protocol_sha256"] == b["protocol_sha256"] and a["version"] == proto.PROTOCOL_VERSION
    assert a["search_space"]["ma_trend"] == {"fast": [5, 10, 20], "slow": [30, 50]}  # the existing grids, not new ones
    assert "PLACEHOLDER" in a["cost_model"]["status"] and a["thresholds"]["min_total_oos_trades"] == 30
    assert proto.freeze_protocol(tmp_path)["protocol_sha256"] == a["protocol_sha256"]
    assert proto.freeze_protocol(tmp_path)["protocol_sha256"] == a["protocol_sha256"]  # idempotent
    monkeypatch.setitem(proto.THRESHOLDS, "min_folds", 4)
    with pytest.raises(RuntimeError, match="PROTOCOL_VERSION"):
        proto.freeze_protocol(tmp_path)


# ------------------------------------------------------------------ real-data integration (KR + US)
@pytest.mark.skipif(not HAVE_REAL, reason="persisted real datasets not present (run: python -m qat.realdata evidence)")
class TestRealProtocolRun:
    @pytest.fixture(scope="class")
    def run(self, tmp_path_factory):
        mp = pytest.MonkeyPatch()
        tmp = tmp_path_factory.mktemp("wf3b")
        mp.setenv("QAT_RESULTS_DIR", str(tmp / "results"))
        engine_trade_ends, lockbox_calls = [], []
        real_backtest = wf.run_backtest

        def spy(dataset, strategy, config):
            engine_trade_ends.append((dataset.meta.market, config.trade_end))
            return real_backtest(dataset, strategy, config)

        mp.setattr(wf, "run_backtest", spy)
        mp.setattr(wf, "evaluate_lockbox", lambda *a, **k: lockbox_calls.append(a) or (_ for _ in ()).throw(AssertionError("Lockbox opened")))
        out = tmp / "evidence"
        summary = proto.run_protocol(markets=["KR", "US"], out_dir=out)
        yield {"summary": summary, "out": out, "tmp": tmp, "trade_ends": engine_trade_ends, "lockbox_calls": lockbox_calls}
        mp.undo()

    def test_every_run_is_reproducible_and_the_lockbox_is_never_touched(self, run):
        s = run["summary"]
        assert s["all_runs_deterministic"] is True and run["lockbox_calls"] == []
        assert s["lockbox"]["opened"] is False and s["lockbox"]["registry_exists"] is False
        assert not (run["tmp"] / "results" / "lockbox_registry.json").exists()
        n = {"KR": 1473, "US": 2011}
        dev_end = {m: n[m] - proto.WINDOWS[m][2] for m in n}
        assert run["trade_ends"] and all(te is not None and te <= dev_end[m] for m, te in run["trade_ends"])  # no engine call reads lockbox bars

    def test_markets_keep_their_own_coverage_and_common_period_is_separate(self, run):
        s = run["summary"]
        assert s["verified_coverage"]["KR"] == ["2020-01-02", "2025-12-30"]
        assert s["common_period"] == ["2020-01-02", "2025-12-30"]  # KR is the narrowest of KR/US
        kr, us = s["results"]["KR/ma_trend"], s["results"]["US/ma_trend"]
        assert kr["common_period"]["same_as_market_specific"] is True  # not re-run: identical window
        assert us["market_specific"]["folds"] == 9 and us["common_period"]["folds"] < us["market_specific"]["folds"]
        assert us["common_period"]["rows"][0]["train_dates"][0] >= "2020-01-02"  # common-period training never reaches before the common start
        assert us["market_specific"]["rows"][0]["train_dates"][0] == "2018-01-02"  # primary period NOT cut for the comparison
        assert us["common_period"]["run_id"] != us["market_specific"]["run_id"]

    def test_run_manifests_carry_protocol_data_identity_and_fold_choices(self, run):
        from qat.research.store import load_run

        entry = run["summary"]["results"]["KR/breakout"]
        manifest = load_run(entry["market_specific"]["run_id"])["manifest"]
        assert manifest["research_protocol"]["sha256"] == run["summary"]["protocol_sha256"]
        assert manifest["research_protocol"]["market_scope"] == "market_specific"
        ident = manifest["data_identity"]
        assert ident["provider"] == "data.go.kr" and ident["symbol"] == "005930" and ident["data_version"].startswith("KR-005930-1d-data-go-kr-")
        assert ident["verified_coverage"] == ["2020-01-02", "2025-12-30"] and len(ident["normalized_sha256"]) == 64 and len(ident["raw_set_sha256"]) == 64
        window = manifest["research_window"]
        assert window["development_bars"] == [0, 1473 - 252] and window["lockbox_range_bars"] == [1473 - 252, 1473]
        assert manifest["grid"] == {"entry_lookback": [20, 40], "exit_lookback": [10, 20]} and manifest["base_config"]["cost_multiplier"] == 1.0
        assert manifest["walkforward"]["lockbox_status"] == "RESERVED_UNUSED"
        folds = load_run(entry["market_specific"]["run_id"])["result"]["folds"]
        assert all(f["status"] == "OK" and set(f["chosen_params"]) == {"entry_lookback", "exit_lookback"} for f in folds)
        for key, mult in (("2x", 2.0), ("3x", 3.0)):
            stressed = load_run(entry["cost_stress"][key]["run_id"])["manifest"]
            assert stressed["base_config"]["cost_multiplier"] == mult and stressed["research_protocol"]["role"] == f"cost_stress_{mult:g}x"

    def test_every_candidate_has_a_status_and_the_decision_is_consistent(self, run):
        s = run["summary"]
        assert s["candidates_tested"] == 6
        for entry in s["results"].values():
            d = entry["market_specific"]["decision"]
            assert d["status"] in ("PASS", "FAIL", "INSUFFICIENT_ACTIVITY", "UNSTABLE", "UNKNOWN")
            assert entry["market_specific"]["folds_failed"] == 0 and entry["determinism"]["identical"] is True
        assert (s["lockbox"]["decision"] == "LOCKBOX_ELIGIBLE") == bool(s["lockbox"]["eligible_candidates"])

    def test_rerunning_the_same_protocol_version_is_refused(self, run):
        with pytest.raises(RuntimeError, match="another OOS look"):
            proto.run_protocol(markets=["KR", "US"], out_dir=run["out"])
        assert json.loads((run["out"] / "protocol.json").read_text(encoding="utf-8"))["protocol_sha256"] == run["summary"]["protocol_sha256"]


@pytest.mark.skipif(not HAVE_REAL, reason="persisted real datasets not present")
def test_kr_requests_before_the_verified_start_are_still_rejected_by_the_protocol_path():
    ds = load_dataset(str(ROOT / proto.MARKETS["KR"]))
    cov = proto.verified_coverage(ds)
    assert cov == ("2020-01-02", "2025-12-30")
    require_coverage(ds, "2021-01-04", "2024-12-30", allow_subperiod=True)  # explicit sub-period inside coverage: allowed for the protocol
    with pytest.raises(DataRejected, match="exceeds verified dataset coverage"):
        require_coverage(ds, "2019-12-31", "2025-12-30", allow_subperiod=True)  # one day earlier than the verified start
    with pytest.raises(DataRejected, match="exceeds verified dataset coverage"):
        require_coverage(ds, "2018-01-01", "2025-12-31", allow_subperiod=True)
    with pytest.raises(DataRejected, match="not supported"):
        require_coverage(ds, "2021-01-04", "2024-12-30")  # the UI/API path still refuses sub-periods


def test_determinism_fingerprint_is_sensitive_to_every_compared_field():
    base = {"folds": [{"fold": 0, "status": "OK", "train": [0, 5], "test": [5, 7], "chosen_params": {"fast": 5},
                       "calibration": [], "oos_metrics": {"net_return": 0.1}, "oos_equity": [{"timestamp": "t", "equity": 1.0}]}]}
    same = json.loads(json.dumps(base))
    assert proto._comparable(base) == proto._comparable(same)
    for mutate in (lambda r: r["folds"][0].update(chosen_params={"fast": 10}), lambda r: r["folds"][0]["oos_metrics"].update(net_return=0.2),
                   lambda r: r["folds"][0]["oos_equity"][0].update(equity=1.1), lambda r: r["folds"][0].update(test=[5, 8])):
        other = json.loads(json.dumps(base))
        mutate(other)
        assert proto._comparable(other) != proto._comparable(base)
