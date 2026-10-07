"""Batch #3A real-data INTEGRATION tests: they use the preserved raw artifacts under data/raw
(no network). If the raw files are absent the tests SKIP with an explicit reason - the
rest of the suite never needs them.
"""

from __future__ import annotations

import json
import pathlib
import threading
import urllib.error
import urllib.request

import pytest

from qat.data.bars import DataRejected
from qat.data.loader import load_dataset
from qat.realdata import calendars as calmod
from qat.realdata.admission import check_admission, require_admitted
from qat.realdata.crosscheck import compare
from qat.realdata.datasets import PROCESSED_DIR, build_specs, evaluate, identity_bytes, persist
from qat.realdata.provenance import RAW_ROOT, SIDECAR_SUFFIX, sha256_bytes, verify_artifact

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPECS = build_specs()
HAVE_RAW = all(SPECS[k].raw_files for k in ("crypto-binance", "us-yahoo", "kr-official", "kr-yahoo", "kr-naver"))
pytestmark = pytest.mark.skipif(not HAVE_RAW, reason="preserved real raw artifacts not present under data/raw (BLOCKED, not failed)")


@pytest.fixture(scope="module")
def evals():
    return {k: evaluate(SPECS[k]) for k in ("crypto-binance", "us-yahoo", "kr-official", "kr-yahoo", "kr-naver", "us-nasdaq", "crypto-coinbase")
            if SPECS[k].raw_files}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("QAT_RESULTS_DIR", str(tmp_path / "results"))
    monkeypatch.setenv("QAT_STATE_DIR", str(tmp_path / "state"))


def check(report, cid):
    return next(c for c in report["checks"] if c["id"] == cid)


# ------------------------------------------------------------------ raw preservation
def test_every_raw_artifact_reverifies_and_carries_no_credential():
    sidecars = sorted(RAW_ROOT.rglob("*" + SIDECAR_SUFFIX))
    assert len(sidecars) > 200
    for sidecar in sidecars:
        artifact = sidecar.with_name(sidecar.name[: -len(SIDECAR_SUFFIX)])
        art = verify_artifact(artifact)
        assert art.sidecar["synthetic"] is False
        text = sidecar.read_text(encoding="utf-8")
        assert "serviceKey=<REDACTED>" in text or "serviceKey" not in text


def test_binance_archives_match_the_official_checksums():
    zips = [p for p in SPECS["crypto-binance"].raw_files]
    assert len(zips) == 96
    for path in zips:
        official = path.with_name(path.name + ".CHECKSUM").read_text(encoding="utf-8").split()[0]
        assert sha256_bytes(path.read_bytes()) == official
        assert verify_artifact(path).sidecar["official_checksum_match"] is True


def test_official_kr_raw_pages_record_the_documented_service(evals):
    pages = SPECS["kr-official"].raw_files
    assert len(pages) == 2
    for page in pages:
        s = verify_artifact(page).sidecar
        assert s["provider"] == "data.go.kr" and "GetStockSecuritiesInfoService_V2/getStockPriceInfo_V2" in s["service"]
        assert s["result_code"] == "00" and s["http_status"] == 200 and s["total_count"] == 1473
        assert "<REDACTED>" in s["endpoint"] and s["request_params"]["serviceKey"] == "<REDACTED>"
        assert "exclusive" in s["requested_range"]["note"]


# ------------------------------------------------------------------ datasets
def test_crypto_btcusdt_is_valid_over_the_full_period(evals):
    r = evals["crypto-binance"]["report"]
    assert r["status"] == "PASS" and r["rows"] == 2922 and (r["first_date"], r["last_date"]) == ("2018-01-01", "2025-12-31")
    assert evals["crypto-binance"]["identity"]["adjustment_semantics"] == "UNADJUSTED"
    assert "ms" in evals["crypto-binance"]["result"].reported["timestamp_units"] and "us" in evals["crypto-binance"]["result"].reported["timestamp_units"]


def test_us_aapl_is_valid_and_split_adjusted(evals):
    r = evals["us-yahoo"]["report"]
    assert r["status"] == "PASS" and r["rows"] == 2011 and check(r, "V13")["missing_sessions"] == []
    adj = evals["us-yahoo"]["adjustment"]
    assert adj["final"] == "SPLIT_ADJUSTED" and adj["observed"]["events"][0]["verdict"] == "ADJUSTED_AT_EVENT"
    assert adj["dividend"]["status"] == "CLOSE_NOT_DIVIDEND_ADJUSTED"
    assert evals["us-nasdaq"]["adjustment"]["final"] == "SPLIT_ADJUSTED" and evals["us-nasdaq"]["report"]["status"] == "PASS"


def test_kr_official_dataset_facts(evals):
    ev = evals["kr-official"]
    r, i, a = ev["report"], ev["identity"], ev["adjustment"]
    assert r["status"] == "PASS" and r["rows"] == 1473 and (r["first_date"], r["last_date"]) == ("2020-01-02", "2025-12-30")
    assert check(r, "V13")["missing_sessions"] == [] and check(r, "V13")["extra_dates"] == []
    assert r["failed_checks"] == [] and r["unknown_checks"] == [] and r["warnings"] == ["V18"]
    v18 = check(r, "V18")
    assert v18["status"] == "WARN" and v18["shortfall_sessions"] == 490 and v18["evidence"]["explains_shortfall"] is True
    assert all(w["total_count"] == 0 for w in v18["evidence"]["windows"]) and len(v18["evidence"]["windows"]) == 9
    assert a["final"] == "UNADJUSTED" and a["declared"]["explicit_adjustment_statement"] is False
    assert a["observed"]["base_price_consistency"]["mismatch_count"] == 0 and a["all_overnight_discontinuities"] == []
    assert i["calendar"]["code"] == "XKRX" and i["synthetic"] is False and i["timezone"] == "+09:00"


def test_xkrx_snapshot_agrees_with_the_official_session_list(evals):
    cal = calmod.load_calendar("XKRX")
    sessions = {d.isoformat() for d in cal.sessions_between(__import__("datetime").date(2020, 1, 2), __import__("datetime").date(2025, 12, 31))}
    assert sessions == {r["date"] for r in evals["kr-official"]["result"].rows}  # same trading days, none missing, none extra


def test_persisted_datasets_match_a_fresh_evaluation_and_are_admitted(evals):
    for key, name in (("crypto-binance", "BTCUSDT_binance-vision_1d"), ("us-yahoo", "AAPL_yahoo-chart_1d"), ("kr-official", "005930_data.go.kr_1d")):
        csv_path = PROCESSED_DIR / f"{name}.csv"
        if not csv_path.exists():
            pytest.skip(f"{name} not persisted yet (run: python -m qat.realdata evidence)")
        assert csv_path.read_bytes() == evals[key]["result"].csv_bytes
        assert (PROCESSED_DIR / f"{name}.identity.json").read_bytes() == identity_bytes(evals[key]["identity"])
        ds = load_dataset(csv_path)
        assert ds.meta.synthetic is False and ds.meta.source == SPECS[key].provider
        assert check_admission(ds)["admitted"] is True


def test_normalization_and_identity_are_reproducible(evals):
    for key in ("crypto-binance", "us-yahoo", "kr-official"):
        again = evaluate(SPECS[key])
        assert again["result"].csv_bytes == evals[key]["result"].csv_bytes
        assert identity_bytes(again["identity"]) == identity_bytes(evals[key]["identity"])
        assert again["identity"]["data_version"] == evals[key]["identity"]["data_version"]
        assert again["result"].manifest["manifest_sha256"] == evals[key]["result"].manifest["manifest_sha256"]


# ------------------------------------------------------------------ cross-checks and historical hypotheses
def test_official_vs_naver_matches_on_every_field(evals):
    c = compare(evals["kr-official"]["result"], evals["kr-naver"]["result"], label_a="official", label_b="naver", window=("2020-01-02", "2025-12-30"))
    assert c["verdict"] == "MATCH" and c["totals"] == {"MATCH": 7365, "EXPLAINED_DIFFERENCE": 0, "CONFLICT": 0, "UNKNOWN": 0}


def test_official_data_confirms_the_yahoo_anomalies(evals):
    c = compare(evals["kr-official"]["result"], evals["kr-yahoo"]["result"], label_a="official", label_b="yahoo", window=("2020-01-02", "2025-12-30"))
    assert c["verdict"] == "CONFLICT" and c["only_in_a"] == ["2022-01-03", "2022-05-09"] and c["only_in_b"] == []
    yahoo = evals["kr-yahoo"]["report"]
    assert check(yahoo, "V13")["missing_sessions"] == ["2022-01-03", "2022-05-09"]
    assert check(yahoo, "V02")["status"] == "FAIL" and check(yahoo, "V05")["status"] == "FAIL" and check(yahoo, "V12")["status"] == "FAIL"
    assert yahoo["status"] == "FAIL"


def test_naver_pre_coverage_anomalies_are_real_but_not_checkable_against_official(evals):
    naver = evals["kr-naver"]["report"]
    assert naver["status"] == "FAIL" and check(naver, "V04")["status"] == "FAIL" and check(naver, "V05")["status"] == "FAIL"
    zero_dates = sorted({e["date"] for e in check(naver, "V04")["examples"]})
    assert zero_dates and all(d < "2020-01-02" for d in zero_dates)  # all before the official window -> no official counterpart


def test_crypto_and_us_cross_checks(evals):
    btc = compare(evals["crypto-binance"]["result"], evals["crypto-coinbase"]["result"], label_a="binance", label_b="coinbase",
                  venue_tol=0.05, volume_comparable=False, window=("2021-05-04", "2025-12-31"))
    assert btc["verdict"] == "EXPLAINED_DIFFERENCE" and btc["totals"]["CONFLICT"] == 0
    aapl = compare(evals["us-yahoo"]["result"], evals["us-nasdaq"]["result"], label_a="yahoo", label_b="nasdaq", unknown_tol={"volume": 0.02})
    assert aapl["only_in_a"] == [] and aapl["only_in_b"] == [] and aapl["verdict"] == "CONFLICT"  # documented, unexplained vendor differences
    assert aapl["per_field"]["close"]["CONFLICT"] == 1


# ------------------------------------------------------------------ admission with real data
def _persist_copy(key, tmp_path):
    out = persist(evals_cache[key], name=f"{key}_copy", directory=tmp_path / "proc")
    return load_dataset(out["csv"])


evals_cache: dict = {}


def test_failed_real_datasets_are_not_admitted(evals, tmp_path):
    evals_cache.update(evals)
    for key in ("kr-yahoo", "kr-naver"):
        ds = _persist_copy(key, tmp_path)
        result = check_admission(ds)
        assert result["admitted"] is False and any("validation is FAIL" in r for r in result["reasons"])
        with pytest.raises(DataRejected):
            require_admitted(ds)


def test_tampered_copy_of_the_official_dataset_is_rejected_and_the_original_is_not_touched(evals, tmp_path):
    evals_cache.update(evals)
    ds = _persist_copy("kr-official", tmp_path)
    assert check_admission(ds)["admitted"] is True
    csv_path = pathlib.Path(ds.path)
    rows = csv_path.read_bytes().decode("utf-8").splitlines()
    csv_path.write_bytes(("\n".join(rows[:100] + rows[101:]) + "\n").encode("utf-8"))  # delete one session
    assert check_admission(load_dataset(csv_path))["admitted"] is False
    assert (PROCESSED_DIR / "005930_data.go.kr_1d.csv").read_bytes() == evals["kr-official"]["result"].csv_bytes


def test_backtest_runs_only_on_admitted_real_data(evals, tmp_path):
    from qat.research.backtest import BacktestConfig, run_backtest
    from qat.research.strategies import make_strategy

    evals_cache.update(evals)
    good = load_dataset(PROCESSED_DIR / "005930_data.go.kr_1d.csv") if (PROCESSED_DIR / "005930_data.go.kr_1d.csv").exists() else None
    if good is None:
        pytest.skip("official dataset not persisted yet")
    # pipeline execution only - no profitability claim; fixture alpha so trades actually flow
    res = run_backtest(good, make_strategy("ma_trend", alpha_mode="fixture", fixture_expected_return=0.01), BacktestConfig(initial_cash=10_000_000))
    assert res.dataset["validation_status"] in ("PASS", "WARN") and res.labels["synthetic_data"] is False
    assert res.labels["research_universe"]["symbols"] == ["005930"]
    bad = _persist_copy("kr-yahoo", tmp_path)
    with pytest.raises(DataRejected):
        run_backtest(bad, make_strategy("ma_trend"), BacktestConfig(initial_cash=10_000_000))


# ------------------------------------------------------------------ UI / HTTP: domain rejection is a 4xx, never a 500
def _req(url, method="GET", body=None):
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None, method=method,
                                 headers={"Content-Type": "application/json", "X-QAT-Client": "ui"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_ui_rejects_failed_real_datasets_with_409_not_500(evals, tmp_path):
    from qat.ui.server import make_server
    from qat.ui.service import QATService

    evals_cache.update(evals)
    bad = _persist_copy("kr-yahoo", tmp_path)
    service = QATService()
    service._dataset_ids = lambda: ["bad"]
    service.dataset = lambda _id: bad
    listing = service.list_datasets()
    assert listing[0]["real_data"] is True and listing[0]["admission"]["admitted"] is False
    server = make_server("127.0.0.1", 0, service=service)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        for path, body in (("/api/research/backtest", {"dataset_id": "bad", "strategy": "ma_trend"}),
                           ("/api/research/walkforward", {"dataset_id": "bad", "strategy": "ma_trend"}),
                           ("/api/research/stress", {"dataset_id": "bad", "strategy": "ma_trend"}),
                           ("/api/paper/session", {"dataset_id": "bad", "start_bar": 10})):
            status, payload = _req(base + path, "POST", body)
            assert status == 409, (path, status, payload)
            assert "not admitted" in payload["error"] or "validation" in payload["error"]
        assert service.session is None
    finally:
        server.shutdown()
        server.server_close()


def test_ui_lists_the_admitted_real_datasets():
    from qat.ui.service import QATService

    if not (PROCESSED_DIR / "005930_data.go.kr_1d.csv").exists():
        pytest.skip("official dataset not persisted yet")
    rows = {r["id"]: r for r in QATService().list_datasets()}
    for name in ("BTCUSDT_binance-vision_1d", "AAPL_yahoo-chart_1d", "005930_data.go.kr_1d"):
        row = rows[f"data/processed/real/{name}.csv"]
        assert row["real_data"] is True and row["admission"]["admitted"] is True and row["meta"]["synthetic"] is False


# ------------------------------------------------------------------ Batch #3A.2: KR research scope (CLOSED WITH SCOPE)
OFFICIAL_CSV = PROCESSED_DIR / "005930_data.go.kr_1d.csv"
OFFICIAL_NORMALIZED_SHA = "92e7ddb723e804373cadb66e3645ad386ca9da81a32548a76345a112838bd7ae"
OFFICIAL_RAW_SET_SHA = "83c7575a41599f625b5fea6adfbaa4c0b5d341e2f1def1dd109a50be4b8ffb93"


def test_official_kr_dataset_admits_only_its_verified_interval_and_never_truncates(evals):
    from qat.realdata.admission import require_coverage

    if not OFFICIAL_CSV.exists():
        pytest.skip("official dataset not persisted yet")
    ds = load_dataset(OFFICIAL_CSV)
    adm = check_admission(ds)
    assert adm["admitted"] is True and (adm["covered_start"], adm["covered_end"]) == ("2020-01-02", "2025-12-30")
    require_coverage(ds, "2020-01-02", "2025-12-30")  # T1
    for start, end in (("2018-01-01", "2025-12-31"), ("2019-12-31", "2025-12-30"), ("2019-01-01", "2019-12-31"), ("2020-01-02", "2025-12-31")):
        with pytest.raises(DataRejected, match="exceeds verified dataset coverage"):  # T2: no silent truncation
            require_coverage(ds, start, end)


def test_pre_2020_request_is_a_409_and_never_falls_back_to_another_source(evals, tmp_path):
    from qat.ui.service import ConflictError, QATService

    if not OFFICIAL_CSV.exists():
        pytest.skip("official dataset not persisted yet")
    service = QATService()
    official = f"data/processed/real/{OFFICIAL_CSV.name}"
    with pytest.raises(ConflictError, match="exceeds verified dataset coverage"):
        service._research_inputs({"dataset_id": official, "strategy": "ma_trend", "requested_start": "2018-01-01", "requested_end": "2025-12-31"})
    service._research_inputs({"dataset_id": official, "strategy": "ma_trend", "requested_start": "2020-01-02", "requested_end": "2025-12-30"})
    # T3: the admitted official dataset is built from data.go.kr raw pages only; Yahoo/Naver are never part of it
    ident = evals["kr-official"]["identity"]
    assert ident["provider"] == "data.go.kr"
    assert all("data.go.kr" in p.as_posix() for p in SPECS["kr-official"].raw_files)
    assert not any(tag in p.as_posix() for p in SPECS["kr-official"].raw_files for tag in ("yahoo", "naver"))


def test_official_kr_adjustment_is_evidence_supported_not_provider_stated(evals):
    adj = evals["kr-official"]["identity"]["adjustment"]  # T4
    assert adj["final"] == "UNADJUSTED" and adj["assurance"] == "UNADJUSTED_EVIDENCE_SUPPORTED"
    assert adj["provider_explicit_statement"] == "UNAVAILABLE" and adj["declared"]["explicit_adjustment_statement"] is False
    assert adj["declared"]["kind"] == "official_field_definitions"


def test_scope_policy_does_not_change_the_official_dataset_identity(evals):
    ident = evals["kr-official"]["identity"]  # T5: bytes/version are the pre-policy values
    assert ident["normalized_sha256"] == OFFICIAL_NORMALIZED_SHA and ident["raw_set_sha256"] == OFFICIAL_RAW_SET_SHA
    assert ident["data_version"] == "KR-005930-1d-data-go-kr-92e7ddb723e8-83c757"
    assert (ident["date_range"]["first"], ident["date_range"]["last"], ident["rows"]) == ("2020-01-02", "2025-12-30", 1473)


# ------------------------------------------------------------------ credential safety
def test_no_credential_text_anywhere_in_the_repository():
    from qat.realdata.secrets import CredentialError, key_variants, read_key

    try:
        key = read_key()
    except CredentialError:
        pytest.skip("credential file not available: leak scan not runnable here")
    needles = [v.encode() for v in key_variants(key)]
    skip = {".venv", "__pycache__", ".pytest_cache", ".git"}
    leaked = [str(p.relative_to(ROOT)) for p in ROOT.rglob("*")
              if p.is_file() and not (set(p.relative_to(ROOT).parts) & skip) and any(n in p.read_bytes() for n in needles)]
    assert leaked == []  # file names only; the key itself is never in the assertion message
