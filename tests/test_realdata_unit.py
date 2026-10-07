"""Batch #3A real-data foundation - UNIT tests (no network, no real data files).

Fixtures are tiny hand-made payloads in the providers' documented shapes. They are test
scaffolding, NOT market data: every artifact lives in a pytest tmp dir and is never
mistaken for the preserved real raw files under data/raw.
"""

from __future__ import annotations

import io
import json
import math
import pathlib
import zipfile
from datetime import date, timedelta

import pytest

from qat.data.bars import DataRejected
from qat.data.loader import load_dataset
from qat.realdata import adjustment, crosscheck, secrets, sources
from qat.realdata import calendars as calmod
from qat.realdata.admission import check_admission, is_real, require_admitted
from qat.realdata.calendars import CalendarError, build_payload, load_calendar
from qat.realdata.datasets import evaluate, identity_bytes, persist, spec_from_dict, spec_to_dict
from qat.realdata.normalize import DatasetSpec, fmt, normalize, to_number
from qat.realdata.provenance import (
    ProvenanceError, RawImmutableError, sha256_bytes, store_raw, verify_artifact,
)
from qat.realdata.validate import ValidationContext, validate

FAKE_KEY = "unit-test-FAKE-key-0a1b2c3d4e5f6a7b8c9d"  # obviously not a real credential


# ------------------------------------------------------------------ helpers
def raw_store(tmp_path, content: bytes, name: str, *, provider="naver-fchart", market="KR", symbol="005930", fmt_="xml",
              endpoint="https://example.invalid/x?serviceKey=" + FAKE_KEY, key=FAKE_KEY, **extra):
    return store_raw(content, name=name, provider=provider, service="unit", market=market, symbol=symbol,
                     requested_range={"start": "2024-01-01", "end": "2024-01-31"}, returned_range={"start": None, "end": None},
                     fmt=fmt_, endpoint=endpoint, timezone_info={"exchange_timezone": "Asia/Seoul"}, key=key, root=tmp_path / "raw", **extra)


def naver_xml(rows, symbol="005930") -> bytes:
    items = "\n".join(f'<item data="{d}|{o}|{h}|{l}|{c}|{v}" />' for d, o, h, l, c, v in rows)
    text = f'<?xml version="1.0" encoding="EUC-KR" ?><protocol><chartdata symbol="{symbol}" name="삼성전자" count="{len(rows)}">{items}</chartdata></protocol>'
    return text.encode("cp949")


def snapshot(tmp_path, sessions, code="XKRX", first="2024-01-01", last="2024-12-31"):
    cal_dir = tmp_path / "cal"
    cal_dir.mkdir(exist_ok=True)
    payload = build_payload(code, [date.fromisoformat(s) for s in sessions], date.fromisoformat(first), date.fromisoformat(last),
                            source="unit-test", version="0", generated_utc="2026-01-01T00:00:00+00:00")
    (cal_dir / f"{code}.json").write_text(json.dumps(payload), encoding="utf-8")
    return cal_dir


SESSIONS = ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05", "2024-01-08", "2024-01-09", "2024-01-10", "2024-01-11"]
ROWS = [("20240102", 100, 105, 99, 104, 1000), ("20240103", 104, 106, 103, 105, 1100), ("20240104", 105, 107, 104, 106, 1200),
        ("20240105", 106, 108, 105, 107, 900), ("20240108", 107, 109, 106, 108, 950), ("20240109", 108, 110, 107, 109, 970),
        ("20240110", 109, 111, 108, 110, 980), ("20240111", 110, 112, 109, 111, 990)]
OFFICIAL_DEF = {"state": "UNADJUSTED", "kind": "official_field_definitions", "document": {"title": "unit"}, "basis": "unit",
                "explicit_adjustment_statement": False}


def make_spec(tmp_path, rows=ROWS, art_name="005930-unit.xml", **over) -> DatasetSpec:
    art = raw_store(tmp_path, naver_xml(rows), art_name)
    base = dict(key="kr-unit", parser="naver", provider="naver-fchart", market="KR", symbol="005930", provider_symbol="005930",
                calendar_code="XKRX", timezone_label="+09:00", exchange_timezone="Asia/Seoul", window_start="2024-01-02",
                window_end="2024-01-11", raw_files=(art.path,), expected_reported_symbols=("005930",), declared_adjustment=OFFICIAL_DEF)
    base.update(over)
    return DatasetSpec(**base)


# ================================================================== secrets
def test_key_file_status_and_reading_never_expose_content(tmp_path):
    path = tmp_path / "key.txt"
    assert secrets.key_file_status(path) == {"key_file_exists": False, "key_non_empty": False}
    path.write_text("  \n", encoding="utf-8")
    assert secrets.key_file_status(path)["key_non_empty"] is False
    with pytest.raises(secrets.CredentialError) as err:
        secrets.read_key(path)
    path.write_bytes((FAKE_KEY + "\r\n").encode())
    assert secrets.read_key(path) == FAKE_KEY  # trailing whitespace trimmed in memory only
    assert path.read_bytes() == (FAKE_KEY + "\r\n").encode()  # the file itself is untouched (byte-exact)
    assert FAKE_KEY not in str(err.value)


def test_redaction_of_urls_and_text():
    url = f"https://api.example/x?serviceKey={FAKE_KEY}&numOfRows=3&pageNo=1"
    redacted = secrets.redact_url(url, FAKE_KEY)
    assert FAKE_KEY not in redacted and "serviceKey=<REDACTED>" in redacted and "numOfRows=3" in redacted
    assert FAKE_KEY not in secrets.redact(f"error for {FAKE_KEY} and {secrets.quote(FAKE_KEY, safe='')}", FAKE_KEY)
    plus_key = "ab+cd/ef=="
    assert plus_key not in secrets.redact(f"x {plus_key} y {secrets.quote(plus_key, safe='')}", plus_key)


def test_encode_for_query_modes():
    assert secrets.encode_for_query("ab%2Bcd", mode="auto") == "ab%2Bcd"          # already encoded: verbatim
    assert secrets.encode_for_query("ab+cd/ef==", mode="auto") == "ab%2Bcd%2Fef%3D%3D"  # decoded form: encoded once
    assert secrets.encode_for_query("ab+cd", mode="as_is") == "ab+cd"
    assert secrets.encode_for_query("ab+cd", mode="quote") == "ab%2Bcd"


# ================================================================== provenance
def test_raw_artifacts_are_immutable_and_credential_free(tmp_path):
    art = raw_store(tmp_path, b"<a/>", "x.xml")
    assert FAKE_KEY not in art.sidecar["endpoint"] and "<REDACTED>" in art.sidecar["endpoint"]
    assert art.sidecar["synthetic"] is False and art.sidecar["sha256"] == sha256_bytes(b"<a/>")
    assert raw_store(tmp_path, b"<a/>", "x.xml").sha256 == art.sha256  # identical bytes: no-op
    with pytest.raises(RawImmutableError):
        raw_store(tmp_path, b"<b/>", "x.xml")  # different bytes: refused
    assert art.path.read_bytes() == b"<a/>"
    verify_artifact(art.path)


def test_verify_detects_every_tamper(tmp_path):
    art = raw_store(tmp_path, b"original", "y.xml")
    sidecar_path = art.path.with_name(art.path.name + ".provenance.json")
    good = sidecar_path.read_text(encoding="utf-8")
    art.path.write_bytes(b"original-edited")
    with pytest.raises(ProvenanceError, match="SHA-256"):
        verify_artifact(art.path)
    art.path.write_bytes(b"original")
    for patch, match in ((lambda d: d.update(synthetic=True), "synthetic"),
                         (lambda d: d.update(size_bytes=1), "size"),
                         (lambda d: d.pop("provider"), "missing fields"),
                         (lambda d: d.update(endpoint="https://x/?serviceKey=abc123"), "credential")):
        data = json.loads(good)
        patch(data)
        sidecar_path.write_text(json.dumps(data), encoding="utf-8")
        with pytest.raises(ProvenanceError, match=match):
            verify_artifact(art.path)
    sidecar_path.write_text(good, encoding="utf-8")
    verify_artifact(art.path)
    sidecar_path.unlink()
    with pytest.raises(ProvenanceError, match="sidecar missing"):
        verify_artifact(art.path)


# ================================================================== calendars
def test_calendar_snapshot_integrity_and_fail_closed_range(tmp_path):
    cal_dir = snapshot(tmp_path, SESSIONS)
    cal = load_calendar("XKRX", cal_dir)
    assert cal.is_session(date(2024, 1, 2)) and not cal.is_session(date(2024, 1, 6))
    assert [d.isoformat() for d in cal.sessions_between(date(2024, 1, 1), date(2024, 1, 5))] == SESSIONS[:4]
    with pytest.raises(CalendarError, match="outside"):
        cal.is_session(date(2025, 1, 2))  # outside the snapshot is UNKNOWN, never "trading"
    path = cal_dir / "XKRX.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["sessions"].remove("2024-01-04")  # delete a trading day without fixing the hash
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(CalendarError, match="SHA-256"):
        load_calendar("XKRX", cal_dir)
    with pytest.raises(CalendarError, match="missing"):
        load_calendar("XNYS", cal_dir)
    assert load_calendar("24X7").is_session(date(2024, 1, 6))  # crypto: every day


# ================================================================== parsers
def _zip(rows: list[str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("BTCUSDT-1d.csv", "\n".join(rows) + "\n")
    return buf.getvalue()


def test_binance_parser_handles_ms_and_us_timestamps():
    parsed = sources.parse_binance_klines_zip(_zip([
        "1704067200000,42283.58,44184.1,42180,44179.55,27676.5,1704153599999,1,2,3,4,0",
        "1735689600000000,93576,95151,92888,95791,10000,1735775999999999,1,2,3,4,0"]))
    assert [r["date"] for r in parsed.rows] == ["2024-01-01", "2025-01-01"]
    assert parsed.meta["timestamp_units"] == ["ms", "us"]
    with pytest.raises(sources.ParseError):
        sources.parse_binance_klines_zip(_zip(["17040672,1,1,1,1,1"]))  # 8 digits: neither unit
    with pytest.raises(sources.ParseError):
        sources.parse_binance_klines_zip(b"not a zip")
    assert sources.parse_binance_checksum(b"%s  BTCUSDT-1d-2024-01.zip\n" % (b"a" * 64))["name"] == "BTCUSDT-1d-2024-01.zip"


def test_yahoo_parser_derives_session_date_and_keeps_nulls():
    doc = {"chart": {"error": None, "result": [{"meta": {"symbol": "AAPL", "currency": "USD", "exchangeTimezoneName": "America/New_York"},
                                                  "timestamp": [1704205800, 1704292200],
                                                  "events": {"splits": {"1": {"splitRatio": "4:1"}}},
                                                  "indicators": {"quote": [{"open": [1.0, None], "high": [2.0, None], "low": [0.5, None],
                                                                            "close": [1.5, None], "volume": [10, None]}],
                                                                 "adjclose": [{"adjclose": [1.4, None]}]}}]}}
    parsed = sources.parse_yahoo_chart(json.dumps(doc).encode(), utc_offset_seconds=0)
    assert [r["date"] for r in parsed.rows] == ["2024-01-02", "2024-01-03"]  # 14:30Z -> same exchange date
    assert parsed.rows[1]["close"] is None and parsed.rows[0]["extras"]["ts_utc"].endswith("14:30:00+00:00")
    assert parsed.meta["reported_symbol"] == "AAPL" and parsed.meta["splits"]
    with pytest.raises(sources.ParseError):
        sources.parse_yahoo_chart(b"{}", utc_offset_seconds=0)
    broken = json.loads(json.dumps(doc))
    broken["chart"]["result"][0]["indicators"]["quote"][0]["open"] = [1.0]
    with pytest.raises(sources.ParseError, match="length"):
        sources.parse_yahoo_chart(json.dumps(broken).encode(), utc_offset_seconds=0)


def test_nasdaq_naver_coinbase_parsers():
    n = sources.parse_nasdaq_historical(json.dumps({"data": {"symbol": "AAPL", "totalRecords": 1, "tradesTable": {"headers": {}, "rows": [
        {"date": "01/10/2024", "close": "$185.92", "volume": "58,414,460", "open": "$186.06", "high": "$186.40", "low": "$183.92"}]}}}).encode())
    assert n.rows[0]["date"] == "2024-01-10" and n.rows[0]["close"] == "$185.92"  # text kept as given
    with pytest.raises(sources.ParseError):
        sources.parse_nasdaq_historical(json.dumps({"data": {"tradesTable": {"rows": [{"date": "2024-01-10"}]}}}).encode())
    k = sources.parse_naver_fchart(naver_xml([("20240102", 1, 2, 1, 2, 5)]))
    assert k.rows[0]["date"] == "2024-01-02" and k.meta["reported_symbol"] == "005930"
    with pytest.raises(sources.ParseError):
        sources.parse_naver_fchart(b"<protocol></protocol>")
    c = sources.parse_coinbase_candles(json.dumps([[1704412800, 42401.97, 44368.38, 44153.69, 44128.22, 1361.4]]).encode())
    assert c.rows[0]["open"] == 44153.69 and c.rows[0]["low"] == 42401.97 and c.rows[0]["date"] == "2024-01-05"


def test_datagokr_parser_success_error_and_single_item():
    item = {"basDt": "20240102", "srtnCd": "005930", "isinCd": "KR7005930003", "itmsNm": "삼성전자", "mrktCtg": "KOSPI", "clpr": "79600",
            "vs": "-100", "fltRt": "-.13", "mkp": "78800", "hipr": "79800", "lopr": "78500", "trqu": "17142847", "trPrc": "1", "lstgStCnt": "2", "mrktTotAmt": "3"}
    ok = {"response": {"header": {"resultCode": "00", "resultMsg": "NORMAL SERVICE."}, "body": {"numOfRows": 1, "pageNo": 1, "totalCount": 1, "items": {"item": item}}}}
    parsed = sources.parse_datagokr_stock_price(json.dumps(ok).encode())
    assert parsed.rows[0]["date"] == "2024-01-02" and parsed.rows[0]["close"] == "79600" and parsed.rows[0]["extras"]["srtnCd"] == "005930"
    ok["response"]["body"]["items"] = ""  # empty page
    assert sources.parse_datagokr_stock_price(json.dumps(ok).encode()).rows == []
    ok["response"]["header"]["resultCode"] = "30"
    with pytest.raises(sources.ParseError, match="error code 30"):
        sources.parse_datagokr_stock_price(json.dumps(ok).encode())  # an API error is never an empty series
    with pytest.raises(sources.ParseError):
        sources.parse_datagokr_stock_price(b'{"OpenAPI_ServiceResponse": {}}')


# ================================================================== normalization
def test_number_conversion_and_formatting():
    assert to_number("$1,234.50") == (1234.5, "converted") and to_number("12") == (12.0, "number")
    assert to_number(None)[1] == "missing" and to_number("")[1] == "missing" and to_number("abc")[1] == "invalid"
    assert math.isinf(to_number("inf")[0]) and to_number(True)[1] == "invalid"
    assert fmt(55200.0) == "55200" and fmt(0.1) == "0.1" and fmt(None) == "" and fmt(math.nan) == "nan" and fmt(-math.inf) == "-inf"


def test_normalization_carries_bad_values_and_records_every_step(tmp_path):
    rows = [("20240103", 104, 106, 103, 105, 1100), ("20240102", 100, 105, 99, 104, 1000)]
    spec = make_spec(tmp_path, rows=rows, window_end="2024-01-03")
    res = normalize(spec)
    assert res.csv_bytes.decode().splitlines()[0] == "timestamp,open,high,low,close,volume"
    assert [r["date"] for r in res.rows] == ["2024-01-02", "2024-01-03"]  # strictly descending artifact was reversed
    steps = {s["step"]: s for s in res.manifest["steps"]}
    assert steps["ordering"]["artifacts_reversed"] == 1 and steps["window_selection"]["rows_outside_window_not_carried"] == 0
    assert "interpolation" in res.manifest["not_performed"] and "row deletion for quality" in res.manifest["not_performed"]
    # unordered (not strictly descending) input is kept as given so validation can see it
    spec2 = make_spec(tmp_path, rows=[("20240102", 1, 2, 1, 2, 5), ("20240104", 1, 2, 1, 2, 5), ("20240103", 1, 2, 1, 2, 5)],
                      art_name="unordered.xml", window_end="2024-01-11")
    assert [r["date"] for r in normalize(spec2).rows] == ["2024-01-02", "2024-01-04", "2024-01-03"]


def test_normalization_window_selection_is_counted_not_silent(tmp_path):
    spec = make_spec(tmp_path, window_start="2024-01-03", window_end="2024-01-09")
    res = normalize(spec)
    step = next(s for s in res.manifest["steps"] if s["step"] == "window_selection")
    assert step["rows_outside_window_not_carried"] == 3 and len(res.rows) == 5  # 01-02, 01-10, 01-11 are outside


def test_normalization_is_deterministic_and_verifies_raw_first(tmp_path):
    spec = make_spec(tmp_path)
    a, b = normalize(spec), normalize(spec)
    assert a.csv_bytes == b.csv_bytes and a.normalized_sha256 == b.normalized_sha256 and a.manifest["manifest_sha256"] == b.manifest["manifest_sha256"]
    spec.raw_files[0].write_bytes(spec.raw_files[0].read_bytes() + b" ")
    with pytest.raises(ProvenanceError):
        normalize(spec)


# ================================================================== validation
def ctx_for(tmp_path, *, market="CRYPTO", symbol="BTC/USDT", cal="24X7", ws="2024-01-01", we="2024-01-05", adjustment=None, raw=None, **over):
    calendar = load_calendar(cal, snapshot(tmp_path, SESSIONS) if cal == "XKRX" else None)
    base = dict(market=market, symbol=symbol, provider="p", timezone_label="UTC", exchange_timezone="UTC", calendar=calendar,
                window_start=ws, window_end=we, meta_source="p",
                raw=raw if raw is not None else [{"artifact": "a", "provider": "p", "market": market, "symbol": symbol.replace("/", ""),
                                                   "synthetic": False, "timezone": {"exchange_timezone": "UTC"}}],
                adjustment=adjustment or {"final": "UNADJUSTED", "final_reason": "unit", "observed": {"state": "UNKNOWN", "events": []},
                                          "unadjusted_discontinuities": [], "declared": {}})
    base.update(over)
    return ValidationContext(**base)


def csvb(lines: list[str]) -> bytes:
    return ("timestamp,open,high,low,close,volume\n" + "\n".join(lines) + "\n").encode()


GOOD = [f"2024-01-0{d}T00:00:00,100,105,99,104,1000" for d in range(1, 6)]


def failed(report, cid):
    return next(c for c in report["checks"] if c["id"] == cid)["status"]


def test_validation_passes_a_clean_dataset(tmp_path):
    report = validate(csvb(GOOD), ctx_for(tmp_path))
    assert report["status"] == "PASS" and report["rows"] == 5 and not report["failed_checks"]


@pytest.mark.parametrize("lines,check", [
    (GOOD[:2] + [GOOD[1]] + GOOD[2:], "V07"),                                                       # duplicate timestamp
    ([GOOD[1], GOOD[0]] + GOOD[2:], "V08"),                                                         # unordered
    (GOOD[:1] + ["2024-01-02T00:00:00,100,99,101,104,1000"] + GOOD[2:], "V05"),                    # high < low
    (GOOD[:1] + ["2024-01-02T00:00:00,100,105,99,106,1000"] + GOOD[2:], "V05"),                    # close > high
    (GOOD[:1] + ["2024-01-02T00:00:00,nan,105,99,104,1000"] + GOOD[2:], "V03"),                    # NaN
    (GOOD[:1] + ["2024-01-02T00:00:00,100,inf,99,104,1000"] + GOOD[2:], "V03"),                    # +inf
    (GOOD[:1] + ["2024-01-02T00:00:00,100,105,-inf,104,1000"] + GOOD[2:], "V03"),                  # -inf
    (GOOD[:1] + ["2024-01-02T00:00:00,100,abc,99,104,1000"] + GOOD[2:], "V03"),                    # invalid numeric
    (GOOD[:1] + ["2024-01-02T00:00:00,100,105,99,104,-5"] + GOOD[2:], "V06"),                      # negative volume
    (GOOD[:1] + ["2024-01-02T00:00:00,0,0,0,104,1000"] + GOOD[2:], "V04"),                         # zero prices
    (GOOD[:1] + ["2024-01-02T00:00:00,100,105,99,,1000"] + GOOD[2:], "V02"),                       # missing field
    (GOOD[:1] + ["2024-01-02T09:30:00,100,105,99,104,1000"] + GOOD[2:], "V02"),                    # non-midnight timestamp
])
def test_validation_flags_each_data_defect_without_repairing(tmp_path, lines, check):
    raw = csvb(lines)
    report = validate(raw, ctx_for(tmp_path))
    assert failed(report, check) == "FAIL" and report["status"] == "FAIL"
    assert raw == csvb(lines)  # input untouched


def test_validation_header_symbol_market_timezone_source_synthetic(tmp_path):
    assert failed(validate(b"timestamp,open,high,low,close\n", ctx_for(tmp_path)), "V01") == "FAIL"
    assert failed(validate(csvb(GOOD), ctx_for(tmp_path, reported={"symbols": ["ETH"]}, expected_reported_symbols=("BTC",))), "V10") == "FAIL"
    assert failed(validate(csvb(GOOD), ctx_for(tmp_path, raw=[{"artifact": "a", "provider": "p", "market": "CRYPTO", "symbol": "ETHUSDT",
                                                               "synthetic": False, "timezone": {"exchange_timezone": "UTC"}}])), "V10") == "FAIL"
    assert failed(validate(csvb(GOOD), ctx_for(tmp_path, raw=[{"artifact": "a", "provider": "p", "market": "US", "symbol": "BTCUSDT",
                                                               "synthetic": False, "timezone": {"exchange_timezone": "UTC"}}])), "V11") == "FAIL"
    assert failed(validate(csvb(GOOD), ctx_for(tmp_path, timezone_label="+09:00")), "V12") == "FAIL"
    assert failed(validate(csvb(GOOD), ctx_for(tmp_path, exchange_timezone="Asia/Seoul")), "V12") == "FAIL"
    assert failed(validate(csvb(GOOD), ctx_for(tmp_path, meta_source="someone-else")), "V14") == "FAIL"
    assert failed(validate(csvb(GOOD), ctx_for(tmp_path, synthetic_flag=True)), "V15") == "FAIL"
    assert failed(validate(csvb(GOOD), ctx_for(tmp_path, meta_source="SYNTHETIC-fixture", provider="SYNTHETIC-fixture")), "V15") == "FAIL"
    forged = [{"artifact": "a", "provider": "p", "market": "CRYPTO", "symbol": "BTCUSDT", "synthetic": True, "timezone": {"exchange_timezone": "UTC"}}]
    assert failed(validate(csvb(GOOD), ctx_for(tmp_path, raw=forged)), "V15") == "FAIL"
    assert failed(validate(csvb(GOOD), ctx_for(tmp_path, market="KR", symbol="005930", timezone_label="+09:00")), "V11") == "FAIL"  # 24x7 calendar for KR


def test_validation_trading_calendar_missing_and_extra_sessions(tmp_path):
    lines = [f"{d}T00:00:00,100,105,99,104,1000" for d in SESSIONS]
    ok = ctx_for(tmp_path, market="KR", symbol="005930", cal="XKRX", ws="2024-01-02", we="2024-01-11", timezone_label="+09:00",
                 exchange_timezone="Asia/Seoul", raw=[{"artifact": "a", "provider": "p", "market": "KR", "symbol": "005930", "synthetic": False,
                                                        "timezone": {"exchange_timezone": "Asia/Seoul"}}])
    assert validate(csvb(lines), ok)["status"] == "PASS"
    gap = validate(csvb([l for l in lines if not l.startswith("2024-01-04")]), ok)  # deleted trading day
    assert failed(gap, "V13") == "FAIL" and "2024-01-04" in next(c for c in gap["checks"] if c["id"] == "V13")["missing_sessions"]
    extra = validate(csvb(lines + ["2024-01-06T00:00:00,100,105,99,104,1000"]), ok)  # Saturday
    assert "2024-01-06" in next(c for c in extra["checks"] if c["id"] == "V13")["extra_dates"] and failed(extra, "V13") == "FAIL"
    outside = ctx_for(tmp_path, market="KR", symbol="005930", cal="XKRX", ws="2024-01-02", we="2030-01-01", timezone_label="+09:00",
                      exchange_timezone="Asia/Seoul", raw=ok.raw)
    assert failed(validate(csvb(lines), outside), "V13") == "UNKNOWN"  # calendar cannot judge: fail closed


def test_validation_adjustment_unknown_blocks_pass(tmp_path):
    unknown = {"final": "UNKNOWN", "final_reason": "no basis", "observed": {"state": "UNKNOWN", "events": []}, "unadjusted_discontinuities": [], "declared": {}}
    report = validate(csvb(GOOD), ctx_for(tmp_path, adjustment=unknown))
    assert failed(report, "V16") == "UNKNOWN" and report["status"] == "UNKNOWN"
    unadj = {"final": "UNADJUSTED", "final_reason": "x", "observed": {"state": "UNADJUSTED", "events": [{"ex_date": "2024-01-03"}]},
             "unadjusted_discontinuities": [{"date": "2024-01-03"}], "declared": {}}
    equity = ctx_for(tmp_path, market="KR", symbol="005930", cal="XKRX", ws="2024-01-02", we="2024-01-11", timezone_label="+09:00", exchange_timezone="Asia/Seoul",
                     adjustment=unadj, raw=[{"artifact": "a", "provider": "p", "market": "KR", "symbol": "005930", "synthetic": False,
                                             "timezone": {"exchange_timezone": "Asia/Seoul"}}])
    assert failed(validate(csvb([f"{d}T00:00:00,100,105,99,104,1000" for d in SESSIONS]), equity), "V17") == "FAIL"


def test_requested_period_shortfall_needs_evidence(tmp_path):
    lines = [f"{d}T00:00:00,100,105,99,104,1000" for d in SESSIONS]
    kw = dict(market="KR", symbol="005930", cal="XKRX", ws="2024-01-02", we="2024-01-11", timezone_label="+09:00", exchange_timezone="Asia/Seoul",
              requested_start="2024-01-01", raw=[{"artifact": "a", "provider": "p", "market": "KR", "symbol": "005930", "synthetic": False,
                                                   "timezone": {"exchange_timezone": "Asia/Seoul"}}])
    unexplained = validate(csvb(lines), ctx_for(tmp_path, **kw))
    assert failed(unexplained, "V18") == "FAIL" and unexplained["status"] == "FAIL"
    explained = validate(csvb(lines), ctx_for(tmp_path, coverage_evidence={"explains_shortfall": True, "windows": []}, **kw))
    assert failed(explained, "V18") == "WARN" and explained["status"] == "PASS"


# ================================================================== adjustment
def _rows(closes, start="2024-01-02"):
    d = date.fromisoformat(start)
    return [{"date": (d + timedelta(days=i)).isoformat(), "close": c} for i, c in enumerate(closes)]


def test_adjustment_split_observation_uses_the_reference_action_not_the_pattern_alone(monkeypatch):
    action = {"market": "KR", "symbol": "TST", "type": "SPLIT", "ex_date": "2024-01-06", "ratio": 50.0}
    monkeypatch.setattr(adjustment, "CORPORATE_ACTIONS", [action])
    unadjusted = adjustment.assess(_rows([50000, 50000, 50000, 50000, 1000, 1000]), market="KR", symbol="TST", declared=None)
    assert unadjusted["observed"]["state"] == "UNADJUSTED" and unadjusted["final"] == "UNADJUSTED" and unadjusted["unadjusted_discontinuities"]
    adjusted = adjustment.assess(_rows([1000, 1000, 1000, 1000, 1000, 1000]), market="KR", symbol="TST", declared=None)
    assert adjusted["final"] == "SPLIT_ADJUSTED" and not adjusted["unadjusted_discontinuities"]
    none_in_range = adjustment.assess(_rows([1000, 1000]), market="KR", symbol="TST", declared=None)
    assert none_in_range["final"] == "UNKNOWN" and not none_in_range["usable_for_research"]
    contradict = adjustment.assess(_rows([50000, 50000, 50000, 50000, 1000, 1000]), market="KR", symbol="TST",
                                   declared={"state": "SPLIT_ADJUSTED", "document": "doc", "basis": "claim"})
    assert contradict["final"] == "UNKNOWN" and "contradicts" in contradict["final_reason"]


def test_adjustment_official_definitions_need_clean_base_prices():
    ok = adjustment.assess(_rows([1000, 1010]), market="KR", symbol="ZZZ", declared=OFFICIAL_DEF, base_price={"rows_checked": 1, "mismatch_count": 0, "mismatches": []})
    assert ok["final"] == "UNADJUSTED" and "NO explicit adjustment statement" in ok["final_reason"]
    dirty = adjustment.assess(_rows([1000, 1010]), market="KR", symbol="ZZZ", declared=OFFICIAL_DEF, base_price={"rows_checked": 1, "mismatch_count": 1, "mismatches": [{}]})
    assert dirty["final"] == "UNKNOWN"
    assert adjustment.assess(_rows([1, 2]), market="CRYPTO", symbol="BTC/USDT", declared=None)["final"] == "UNADJUSTED"


# ================================================================== cross-check
class _Res:
    def __init__(self, rows):
        self.rows = rows


def _r(date_, o, h, l, c, v):
    return {"date": date_, "open": o, "high": h, "low": l, "close": c, "volume": v}


def test_crosscheck_verdicts():
    a = _Res([_r("2024-01-02", 100.0, 110.0, 90.0, 105.0, 1000.0), _r("2024-01-03", 100.0, 110.0, 90.0, 105.0, 1000.0)])
    assert crosscheck.compare(a, a, label_a="a", label_b="b")["verdict"] == "MATCH"
    b = _Res([_r("2024-01-02", 100.0, 110.0, 90.0, 105.0, 1000.0), _r("2024-01-03", 100.0, 110.0, 90.0, 120.0, 1000.0)])
    c = crosscheck.compare(a, b, label_a="a", label_b="b")
    assert c["verdict"] == "CONFLICT" and c["conflict_examples"][0]["field"] == "close"
    venue = _Res([_r("2024-01-02", 100.5, 110.0, 90.0, 105.2, 77.0), _r("2024-01-03", 100.0, 110.0, 90.0, 105.0, 55.0)])
    assert crosscheck.compare(a, venue, label_a="a", label_b="b", venue_tol=0.05, volume_comparable=False)["verdict"] == "EXPLAINED_DIFFERENCE"
    vol = _Res([_r("2024-01-02", 100.0, 110.0, 90.0, 105.0, 1010.0), _r("2024-01-03", 100.0, 110.0, 90.0, 105.0, 1000.0)])
    unknown = crosscheck.compare(a, vol, label_a="a", label_b="b", unknown_tol={"volume": 0.02})
    assert unknown["verdict"] == "UNKNOWN" and unknown["totals"]["CONFLICT"] == 0  # small unexplained difference is UNKNOWN, not "explained"
    nulls = _Res([_r("2024-01-02", None, None, None, None, None), _r("2024-01-03", 100.0, 110.0, 90.0, 105.0, 1000.0)])
    assert crosscheck.compare(a, nulls, label_a="a", label_b="b")["totals"]["UNKNOWN"] == 5
    missing = _Res([_r("2024-01-03", 100.0, 110.0, 90.0, 105.0, 1000.0)])
    assert crosscheck.compare(a, missing, label_a="a", label_b="b")["only_in_a"] == ["2024-01-02"]
    assert crosscheck.compare(a, _Res([]), label_a="a", label_b="b")["verdict"] == "UNKNOWN"
    split = _Res([_r("2024-01-02", 2.0, 2.2, 1.8, 2.1, 50000.0), _r("2024-01-03", 100.0, 110.0, 90.0, 105.0, 1000.0)])
    sp = crosscheck.compare(a, split, label_a="a", label_b="b", split_actions=[{"ex_date": "2024-01-03", "ratio": 50.0}])
    assert sp["totals"]["EXPLAINED_DIFFERENCE"] == 5 and sp["verdict"] == "EXPLAINED_DIFFERENCE"


# ================================================================== identity + admission (tmp real-shaped dataset)
@pytest.fixture
def real_ds(tmp_path, monkeypatch):
    cal_dir = snapshot(tmp_path, SESSIONS)
    monkeypatch.setattr(calmod, "CALENDAR_DIR", cal_dir)
    spec = make_spec(tmp_path)
    ev = evaluate(spec)
    assert ev["report"]["status"] == "PASS", ev["report"]["failed_checks"] + ev["report"]["unknown_checks"]
    out = persist(ev, name="KR_unit", directory=tmp_path / "proc")
    return {"spec": spec, "ev": ev, "paths": out, "dir": tmp_path / "proc", "tmp": tmp_path, "cal_dir": cal_dir}


def test_identity_is_deterministic_and_secret_free(real_ds):
    again = evaluate(real_ds["spec"])
    assert identity_bytes(again["identity"]) == identity_bytes(real_ds["ev"]["identity"])
    ident = real_ds["ev"]["identity"]
    for field in ("provider", "market", "symbol", "timezone", "adjustment_semantics", "validation_status", "data_version", "normalized_sha256",
                  "raw_set_sha256", "rows", "date_range", "synthetic"):
        assert field in ident
    assert FAKE_KEY not in json.dumps(ident) and ident["synthetic"] is False
    assert spec_to_dict(spec_from_dict(ident["spec"])) == ident["spec"]  # spec round-trips


def _admit(real_ds):
    ds = load_dataset(real_ds["paths"]["csv"])
    return check_admission(ds)


def _rewrite_meta(real_ds, **changes):
    meta_path = real_ds["paths"]["meta"]
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta.update(changes)
    meta_path.write_text(json.dumps(meta), encoding="utf-8")


def test_untouched_dataset_is_admitted(real_ds):
    ds = load_dataset(real_ds["paths"]["csv"])
    assert is_real(ds.meta) and _admit(real_ds)["admitted"] is True
    require_admitted(ds)  # does not raise


def test_tamper_row_deletion_addition_and_ohlc_edit_are_rejected(real_ds):
    csv_path = real_ds["paths"]["csv"]
    original = csv_path.read_bytes().decode("utf-8")  # byte-exact (no newline translation)
    lines = original.splitlines()
    for label, tampered in (("deleted row", "\n".join(lines[:3] + lines[4:]) + "\n"),
                            ("added row", original + "2024-01-12T00:00:00,1,2,1,2,3\n"),
                            ("ohlc value edited", original.replace(",104,1000", ",140,1000", 1))):
        csv_path.write_bytes(tampered.encode("utf-8"))
        result = _admit(real_ds)
        assert result["admitted"] is False and any("normalized_sha256" in r for r in result["reasons"]), label
        with pytest.raises(DataRejected):
            require_admitted(load_dataset(csv_path))
    csv_path.write_bytes(original.encode("utf-8"))
    assert _admit(real_ds)["admitted"] is True


@pytest.mark.parametrize("changes,expect", [
    ({"symbol": "000660"}, "symbol"),
    ({"source": "yahoo-chart"}, "provider"),
    ({"timezone": "UTC"}, "timezone"),
    ({"market": "US"}, "market"),
])
def test_tamper_metadata_is_rejected(real_ds, changes, expect):
    _rewrite_meta(real_ds, **changes)
    result = _admit(real_ds)
    assert result["admitted"] is False and any(expect in r for r in result["reasons"])


def test_tamper_synthetic_flag_forgery_is_rejected(real_ds):
    _rewrite_meta(real_ds, synthetic=True, source="SYNTHETIC-forged")
    with pytest.raises(Exception):  # DatasetMeta refuses at load, or admission refuses - either way no research input
        ds = load_dataset(real_ds["paths"]["csv"])
        require_admitted(ds)
    meta = json.loads(real_ds["paths"]["meta"].read_text(encoding="utf-8"))
    ident = json.loads(real_ds["paths"]["identity"].read_text(encoding="utf-8"))
    ident["synthetic"] = True
    real_ds["paths"]["identity"].write_text(json.dumps(ident), encoding="utf-8")
    _rewrite_meta(real_ds, synthetic=False, source="naver-fchart")
    assert _admit(real_ds)["admitted"] is False and "identity" in " ".join(_admit(real_ds)["reasons"])
    assert meta["extra"]["real_data"]["identity_sha256"] != sha256_bytes(real_ds["paths"]["identity"].read_bytes())


def test_tamper_identity_file_and_missing_identity(real_ds):
    ident_path = real_ds["paths"]["identity"]
    ident = json.loads(ident_path.read_text(encoding="utf-8"))
    ident["validation_status"] = "PASS"
    ident["rows"] = 999
    ident_path.write_text(json.dumps(ident), encoding="utf-8")
    assert "identity file hash differs" in " ".join(_admit(real_ds)["reasons"])
    ident_path.unlink()
    assert _admit(real_ds)["reasons"] == ["identity file missing"]


def test_tamper_raw_bytes_and_sidecar_are_rejected(real_ds):
    raw_path = real_ds["spec"].raw_files[0]
    original = raw_path.read_bytes()
    raw_path.write_bytes(original.replace(b"100|105", b"101|105", 1))
    result = _admit(real_ds)
    assert result["admitted"] is False and "provenance cannot be reproduced" in result["reasons"][0]
    raw_path.write_bytes(original)
    assert _admit(real_ds)["admitted"] is True


def test_tamper_calendar_trading_day_deletion_is_rejected(real_ds):
    path = real_ds["cal_dir"] / "XKRX.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["sessions"].remove("2024-01-04")
    path.write_text(json.dumps(payload), encoding="utf-8")  # hash now wrong
    assert _admit(real_ds)["admitted"] is False
    payload["sha256"] = calmod.snapshot_sha256(payload)  # an adversary who also fixes the hash
    path.write_text(json.dumps(payload), encoding="utf-8")
    result = _admit(real_ds)
    assert result["admitted"] is False and any("calendar" in r or "validation" in r for r in result["reasons"])


def test_real_provider_dataset_without_identity_is_rejected(tmp_path):
    csv_path = tmp_path / "x.csv"
    csv_path.write_text("timestamp,open,high,low,close,volume\n2024-01-02T00:00:00,1,2,1,2,3\n", encoding="utf-8")
    (tmp_path / "x.csv.meta.json").write_text(json.dumps({"market": "KR", "symbol": "005930", "timeframe": "1d", "timezone": "+09:00",
                                                          "timestamp_label": "open", "source": "yahoo-chart", "synthetic": False}), encoding="utf-8")
    ds = load_dataset(csv_path)
    result = check_admission(ds)
    assert result["admitted"] is False and "no identity" in result["reasons"][0]
    with pytest.raises(DataRejected):
        require_admitted(ds)


def test_non_real_datasets_are_not_affected(tmp_path):
    ds = load_dataset(pathlib.Path(__file__).resolve().parents[1] / "data" / "fixtures" / "SYN_KR1_1d.csv")
    assert check_admission(ds) == {"applicable": False, "admitted": True, "reasons": []}


def test_failed_validation_is_not_admitted_even_with_consistent_identity(tmp_path, monkeypatch):
    monkeypatch.setattr(calmod, "CALENDAR_DIR", snapshot(tmp_path, SESSIONS))
    rows = [r for r in ROWS if r[0] != "20240104"]  # a missing trading session
    ev = evaluate(make_spec(tmp_path, rows=rows))
    assert ev["report"]["status"] == "FAIL" and "V13" in ev["report"]["failed_checks"]
    out = persist(ev, name="KR_fail", directory=tmp_path / "proc")
    ds = load_dataset(out["csv"])
    result = check_admission(ds)
    assert result["admitted"] is False and any("validation is FAIL" in r for r in result["reasons"])
    with pytest.raises(DataRejected):
        require_admitted(ds)



# ================================================================== surviving-mutant closures (adversary also fixes the hashes)
def _forge_identity(real_ds, *, csv_bytes=None, **identity_changes):
    """An adversary who edits the identity AND repairs every hash pointer so the cheap
    integrity checks agree; only the semantic checks can still reject it."""

    paths = real_ds["paths"]
    ident = json.loads(paths["identity"].read_text(encoding="utf-8"))
    ident.update(identity_changes)
    if csv_bytes is not None:
        paths["csv"].write_bytes(csv_bytes)
        ident["normalized_sha256"] = sha256_bytes(csv_bytes)
    paths["identity"].write_bytes(json.dumps(ident, sort_keys=True).encode("utf-8"))
    meta = json.loads(paths["meta"].read_text(encoding="utf-8"))
    meta["extra"]["real_data"]["identity_sha256"] = sha256_bytes(paths["identity"].read_bytes())
    paths["meta"].write_bytes(json.dumps(meta).encode("utf-8"))


def test_forged_identity_synthetic_flag_is_rejected_even_with_repaired_hashes(real_ds):
    _forge_identity(real_ds, synthetic=True)
    result = _admit(real_ds)
    assert result["admitted"] is False and "synthetic flag contradicts" in " ".join(result["reasons"])


def test_consistently_forged_dataset_and_identity_fail_re_normalization_from_raw(real_ds):
    original = real_ds["paths"]["csv"].read_bytes()
    forged = original.replace(b",104,1000", b",103,1000", 1)  # still a valid OHLC bar: only the raw can disprove it
    assert forged != original
    _forge_identity(real_ds, csv_bytes=forged)
    result = _admit(real_ds)
    assert result["admitted"] is False and any("re-normalization from raw does not reproduce" in r for r in result["reasons"])
    with pytest.raises(DataRejected):
        require_admitted(load_dataset(real_ds["paths"]["csv"]))


def test_raw_artifact_provider_must_match_the_dataset_provider(tmp_path):
    other = [{"artifact": "a", "provider": "someone-else", "market": "CRYPTO", "symbol": "BTCUSDT",
              "synthetic": False, "timezone": {"exchange_timezone": "UTC"}}]
    report = validate(csvb(GOOD), ctx_for(tmp_path, raw=other))  # dataset metadata still says provider "p"
    assert failed(report, "V14") == "FAIL" and report["status"] == "FAIL"


def test_redact_url_removes_the_secret_parameter_without_knowing_the_key():
    stored = secrets.redact_url("https://api.example/x?serviceKey=" + FAKE_KEY + "&numOfRows=3")
    assert FAKE_KEY not in stored and "serviceKey=<REDACTED>" in stored and "numOfRows=3" in stored
    assert FAKE_KEY not in secrets.redact_url("https://api.example/x?api_key=" + FAKE_KEY)


def test_research_entry_points_refuse_a_non_admitted_real_dataset(real_ds):
    from qat.research.backtest import BacktestConfig, run_backtest
    from qat.research.strategies import make_strategy
    from qat.research.walkforward import WalkForwardConfig, run_cost_stress, run_walkforward

    real_ds["paths"]["csv"].write_bytes(real_ds["paths"]["csv"].read_bytes().replace(b",104,1000", b",103,1000", 1))
    ds = load_dataset(real_ds["paths"]["csv"])
    assert ds.usable and _admit(real_ds)["admitted"] is False  # it loads fine: only admission stops it
    with pytest.raises(DataRejected):
        run_backtest(ds, make_strategy("ma_trend"), BacktestConfig())
    with pytest.raises(DataRejected):
        run_walkforward(ds, "ma_trend", WalkForwardConfig(train_bars=3, test_bars=2), save=False)
    with pytest.raises(DataRejected):
        run_cost_stress(ds, "ma_trend", {}, BacktestConfig(), save=False)


# ================================================================== Batch #3A.2 - verified coverage + adjustment assurance
def test_requested_period_inside_verified_coverage_is_accepted_and_outside_is_rejected_without_truncation(real_ds):
    from qat.realdata.admission import require_coverage

    ds = load_dataset(real_ds["paths"]["csv"])
    first, last = _admit(real_ds)["covered_start"], _admit(real_ds)["covered_end"]
    assert (first, last) == ("2024-01-02", "2024-01-11")
    require_coverage(ds, None, None)
    require_coverage(ds, first, last)  # the verified interval itself: admitted
    for start, end, text in (("2023-12-01", last, "exceeds verified dataset coverage"),   # earlier than coverage
                             (first, "2024-02-01", "exceeds verified dataset coverage"),  # later than coverage
                             ("2019-01-01", "2024-02-01", "exceeds verified dataset coverage"),
                             ("2024-01-04", last, "not supported"),                       # narrower: no silent subset either
                             (first, "2024-01-09", "not supported")):
        with pytest.raises(DataRejected, match=text):
            require_coverage(ds, start, end)


def test_period_request_is_rejected_when_the_dataset_has_no_verified_coverage(tmp_path):
    from qat.realdata.admission import require_coverage

    ds = load_dataset(pathlib.Path(__file__).resolve().parents[1] / "data" / "fixtures" / "SYN_KR1_1d.csv")
    with pytest.raises(DataRejected, match="no verified coverage"):
        require_coverage(ds, "2024-01-01", "2024-02-01")


def test_adjustment_record_separates_provider_statement_from_evidence(real_ds):
    adj = real_ds["ev"]["identity"]["adjustment"]
    assert adj["final"] == "UNADJUSTED" and adj["assurance"] == "UNADJUSTED_EVIDENCE_SUPPORTED"
    assert adj["provider_explicit_statement"] == "UNAVAILABLE" and adj["declared"]["explicit_adjustment_statement"] is False
    stated = adjustment.assess(_rows([100, 101, 102]), market="KR", symbol="000000",
                               declared={"state": "UNADJUSTED", "document": {"title": "doc"}, "basis": "explicit"})
    assert stated["provider_explicit_statement"] == "AVAILABLE" and stated["assurance"] == "UNADJUSTED_PROVIDER_STATED"
    unknown = adjustment.assess(_rows([100, 101, 102]), market="KR", symbol="000000", declared=None)
    assert unknown["final"] == "UNKNOWN" and unknown["assurance"] == "UNKNOWN" and unknown["provider_explicit_statement"] == "UNAVAILABLE"
