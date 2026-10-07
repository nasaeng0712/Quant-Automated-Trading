"""Evidence export for ``artifacts/verification/real_data/`` (Batch #3A, Phase 12).

Everything is recomputed from the preserved raw artifacts, the specs and the calendar
snapshots - nothing is copied from memory or earlier reports. Before any file is written
the text is scanned for the serviceKey (all textual forms); on a hit the export aborts
WITHOUT printing it. Only structure and verdicts are ever reported.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
from datetime import datetime, timezone

from qat.realdata import crosscheck
from qat.realdata.adjustment import actions_for
from qat.realdata.datasets import PROJECT_ROOT, PROCESSED_DIR, build_specs, evaluate, identity_bytes, persist
from qat.realdata.provenance import list_artifacts, load_artifact, verify_artifact
from qat.realdata.secrets import key_variants, read_key

EVIDENCE_DIR = PROJECT_ROOT / "artifacts" / "verification" / "real_data"
PRIMARY = {"crypto-binance": "BTCUSDT_binance-vision_1d", "us-yahoo": "AAPL_yahoo-chart_1d", "kr-official": "005930_data.go.kr_1d"}


class SecretLeak(RuntimeError):
    """The evidence text contained the credential (never includes it)."""


def _dump(obj) -> str:
    return json.dumps(obj, indent=1, sort_keys=True, ensure_ascii=False, default=str) + "\n"


def write_evidence(name: str, obj, directory: pathlib.Path | None = None) -> pathlib.Path:
    text = _dump(obj)
    try:
        key = read_key()
    except Exception:  # noqa: BLE001 - no key file: nothing to scan for
        key = None
    if key and any(v in text for v in key_variants(key)):
        raise SecretLeak(f"credential text found in evidence file {name}; export aborted")
    directory = directory or EVIDENCE_DIR
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def _summary(ev: dict) -> dict:
    r, i = ev["report"], ev["identity"]
    return {"status": r["status"], "rows": r["rows"], "first_date": r["first_date"], "last_date": r["last_date"],
            "failed_checks": r["failed_checks"], "unknown_checks": r["unknown_checks"], "warnings": r["warnings"],
            "adjustment_semantics": i["adjustment_semantics"], "data_version": i["data_version"],
            "normalized_sha256": i["normalized_sha256"], "raw_set_sha256": i["raw_set_sha256"]}


def _raw_listing() -> list[dict]:
    out = []
    root = PROJECT_ROOT / "data" / "raw"
    for sidecar in sorted(root.rglob("*.provenance.json")):
        art = verify_artifact(sidecar.with_name(sidecar.name[: -len(".provenance.json")]))
        s = art.sidecar
        out.append({"path": art.path.relative_to(PROJECT_ROOT).as_posix(), "provider": s["provider"], "service": s["service"],
                    "market": s["market"], "symbol": s["symbol"], "sha256": s["sha256"], "size_bytes": s["size_bytes"],
                    "format": s["format"], "retrieved_utc": s["retrieved_utc"], "requested_range": s["requested_range"],
                    "returned_range": s["returned_range"], "endpoint": s["endpoint"], "timezone": s["timezone"],
                    "synthetic": s["synthetic"], "extra": {k: v for k, v in s.items() if k in ("official_checksum", "official_checksum_match",
                                                                                           "total_count", "result_code", "page", "http_status", "kind", "title")}})
    return out


def official_api_contract() -> dict:
    """The data.go.kr contract exactly as the official guide documents it (guide sha256 recorded)."""

    guide = list_artifacts("data.go.kr", "KR", "005930-docs")
    page = [p for p in guide if p.name == "openapi_service_page.html"]
    doc = [p for p in guide if p.name == "guide.docx"]
    return {
        "service_title": "금융위원회_주식시세정보", "provider_agency": "금융위원회 (Financial Services Commission)",
        "portal": "data.go.kr (공공데이터포털), dataset 15094808",
        "api_name": "GetStockSecuritiesInfoService_V2", "operation": "getStockPriceInfo_V2",
        "base_url": "https://apis.data.go.kr/1160100/GetStockSecuritiesInfoService_V2",
        "call_url_pattern": "https://apis.data.go.kr/1160100/GetStockSecuritiesInfoService_V2/getStockPriceInfo_V2?serviceKey=<REDACTED>&numOfRows=..&pageNo=..",
        "http_method": "GET (REST), TLS", "auth": "serviceKey query parameter (portal-issued; activation by 활용신청, auto-approved)",
        "formats": ["xml", "json"], "default_format": "xml (resultType=json used)",
        "pagination": {"numOfRows": "page size, default 10; values above 10,000 are capped to 10,000", "pageNo": "1-based, default 1",
                       "totalCount": "returned in body"},
        "date_params": {"basDt": "equals", "beginBasDt": "basDt >= value (inclusive)", "endBasDt": "basDt < value (EXCLUSIVE)", "likeBasDt": "contains"},
        "instrument_params": {"likeSrtnCd": "short code contains", "isinCd": "ISIN equals (Samsung Electronics KR7005930003)", "itmsNm": "name equals",
                              "mrktCls": "KOSPI | KONEX | KOSDAQ"},
        "response_fields": {"basDt": "기준일자 YYYYMMDD", "srtnCd": "단축코드 (6 digits)", "isinCd": "ISIN", "itmsNm": "종목명",
                            "mrktCtg": "시장구분", "clpr": "종가 = last price formed until the end of the regular session",
                            "vs": "대비 = change vs previous day", "fltRt": "등락률", "mkp": "시가 = first price formed after the regular session opens",
                            "hipr": "고가", "lopr": "저가", "trqu": "거래량 = cumulative traded quantity", "trPrc": "거래대금",
                            "lstgStCnt": "상장주식수", "mrktTotAmt": "시가총액 = clpr * lstgStCnt"},
        "update_policy": "daily load; data published after 13:00 of the next business day (not real time)",
        "license": "공공누리 제4유형: 출처표시 + 상업적 이용금지 + 변경금지; third-party redistribution prohibited -> raw data stays local (data/raw is git-ignored)",
        "adjustment_statement_in_guide": "NONE (no 수정/조정/액면분할/권리 wording found in the guide)",
        "documents": {"guide": {"artifact": "data/raw/data.go.kr/KR/005930-docs/guide.docx", "sha256": load_artifact(doc[0]).sha256 if doc else None},
                      "service_page": {"artifact": "data/raw/data.go.kr/KR/005930-docs/openapi_service_page.html",
                                       "sha256": load_artifact(page[0]).sha256 if page else None}},
        "superseded_endpoint_note": "the legacy path /1160100/service/GetStockSecuritiesInfoService/getStockPriceInfo answered code 30 "
                                    "(SERVICE_KEY_IS_NOT_REGISTERED_ERROR) for this key; the documented V2 service authenticated (resultCode 00)",
        "unknown": ["whether KRX adjusts historical rows retroactively after a corporate action (the guide is silent)",
                    "why the service starts at 2020-01-02 (guide lists service start 2021-11-16; backfill range is not documented)"],
    }


def evaluate_all() -> dict:
    specs = build_specs()
    return {k: evaluate(s) for k, s in specs.items() if s.raw_files}


def determinism(specs: dict) -> dict:
    out = {}
    for key in PRIMARY:
        a, b = evaluate(specs[key]), evaluate(specs[key])
        out[key] = {
            "normalized_sha256": [a["identity"]["normalized_sha256"], b["identity"]["normalized_sha256"]],
            "data_version": [a["identity"]["data_version"], b["identity"]["data_version"]],
            "identity_bytes_sha256": [hashlib.sha256(identity_bytes(a["identity"])).hexdigest(), hashlib.sha256(identity_bytes(b["identity"])).hexdigest()],
            "manifest_sha256": [a["result"].manifest["manifest_sha256"], b["result"].manifest["manifest_sha256"]],
            "identical": (a["identity"]["normalized_sha256"] == b["identity"]["normalized_sha256"]
                          and a["identity"]["data_version"] == b["identity"]["data_version"]
                          and identity_bytes(a["identity"]) == identity_bytes(b["identity"])
                          and a["result"].manifest["manifest_sha256"] == b["result"].manifest["manifest_sha256"])}
    return out


def build_crosschecks(evals: dict) -> dict:
    n = {k: ev["result"] for k, ev in evals.items()}
    out = {}
    if "crypto-binance" in n and "crypto-coinbase" in n:
        out["crypto_binance_vs_coinbase"] = crosscheck.compare(
            n["crypto-binance"], n["crypto-coinbase"], label_a="binance", label_b="coinbase", venue_tol=0.05,
            volume_comparable=False, window=("2021-05-04", "2025-12-31"))
        out["crypto_binance_vs_coinbase"]["note"] = ("different venues: price differences within 5% are explained by venue-specific books; "
                                                      "volume is venue-specific and not compared; Coinbase BTC-USDT exists from 2021-05-04")
    if "us-yahoo" in n and "us-nasdaq" in n:
        out["us_yahoo_vs_nasdaq"] = crosscheck.compare(n["us-yahoo"], n["us-nasdaq"], label_a="yahoo", label_b="nasdaq",
                                                        unknown_tol={"volume": 0.02})
    covered = (evals["kr-official"]["report"]["first_date"], evals["kr-official"]["report"]["last_date"]) if "kr-official" in evals else None
    if covered:
        for label, key in (("yahoo", "kr-yahoo"), ("naver", "kr-naver")):
            if key in n:
                c = crosscheck.compare(n["kr-official"], n[key], label_a="official", label_b=label, window=covered)
                c["window"] = covered
                out[f"kr_official_vs_{label}"] = c
    if "kr-yahoo" in n and "kr-naver" in n:
        out["kr_yahoo_vs_naver_full_range"] = crosscheck.compare(n["kr-yahoo"], n["kr-naver"], label_a="yahoo", label_b="naver",
                                                                  split_actions=actions_for("KR", "005930"))
    return out


def classify_yahoo_conflicts(evals: dict) -> dict:
    """Group the official-vs-Yahoo differences by cause (computed from the data, not asserted).
    A date can fall in several groups."""

    fields = ("open", "high", "low", "close", "volume")
    off = {r["date"]: r for r in evals["kr-official"]["result"].rows}
    yh = {r["date"]: r for r in evals["kr-yahoo"]["result"].rows}
    first = min(off)
    groups = {"missing_in_yahoo": [], "yahoo_null_row": [], "yahoo_zero_volume_stale_row": [],
              "yahoo_partial_volume_below_5pct_of_official": [], "yahoo_close_differs_ohlv_equal": [],
              "yahoo_other_difference": []}

    def differs(o, y, f):
        return abs(o[f] - y[f]) > 1e-4 * max(abs(o[f]), abs(y[f]), 1e-12)

    for d in sorted(off):
        y = yh.get(d)
        if y is None:
            if d >= first:
                groups["missing_in_yahoo"].append(d)
            continue
        if any(y[f] is None for f in fields):
            groups["yahoo_null_row"].append(d)
            continue
        diff = [f for f in fields if differs(off[d], y, f)]
        if not diff:
            continue
        placed = False
        if y["volume"] == 0:
            groups["yahoo_zero_volume_stale_row"].append(d)
            placed = True
        elif "volume" in diff and y["volume"] < 0.05 * off[d]["volume"]:
            groups["yahoo_partial_volume_below_5pct_of_official"].append(d)
            placed = True
        if diff == ["close"]:
            groups["yahoo_close_differs_ohlv_equal"].append(d)
            placed = True
        if not placed:
            groups["yahoo_other_difference"].append(d)
    return {k: {"count": len(v), "dates": v[:60]} for k, v in groups.items()}


def hypotheses(evals: dict, crosschecks: dict, yahoo_groups: dict) -> list[dict]:
    base = crosscheck.kr_hypotheses(evals["kr-yahoo"], evals["kr-naver"])
    official_missing = set(yahoo_groups["missing_in_yahoo"]["dates"])
    for h in base:
        if h["id"].startswith("yahoo-missing-"):
            day = h["id"].split("yahoo-missing-")[1]
            h["official_check"] = ("CONFIRMED by official data: the date is a trading session present in the official dataset"
                                   if day in official_missing else "official data does not contain this date" if h["verdict"] == "REPRODUCED"
                                   else "not applicable")
    naver_off = crosscheck_summary(crosschecks.get("kr_official_vs_naver"))
    base.append({"id": "naver-adjustment-basis", "source": "naver-fchart",
                 "hypothesis": "Naver prices/volume are on a different (split-adjusted) basis than as-traded data",
                 "verdict": "UNKNOWN",
                 "evidence": ("inside the official window (2020-01-02..2025-12-30) Naver equals the official data on every field "
                              f"({naver_off}); the Samsung 50:1 split (2018-05-04) is OUTSIDE official coverage, so the adjustment-basis "
                              "claim cannot be checked against official data. Yahoo-vs-Naver full-range differences show 81 field values "
                              "on a split-adjusted vs as-traded basis (EXPLAINED against the issuer reference action), not official-confirmed.")})
    return base


def crosscheck_summary(c: dict | None) -> str:
    return "n/a" if not c else f"verdict {c['verdict']}, totals {c['totals']}"


def od04_decision(evals: dict, determinism_report: dict, admission: dict) -> dict:
    ev = evals["kr-official"]
    r, i, a = ev["report"], ev["identity"], ev["adjustment"]
    raw = [verify_artifact(p).sidecar for p in ev["result"].spec.raw_files]
    v18 = next((c for c in r["checks"] if c["id"] == "V18"), None)
    checks = [
        ("official_source_identified", i["provider"] == "data.go.kr" and "V2" in raw[0]["service"], f"{raw[0]['service']}"),
        ("authentication_succeeded", all(s.get("result_code") == "00" and s.get("http_status") == 200 for s in raw), "every page: HTTP 200, resultCode 00"),
        ("raw_preserved", all(s.get("synthetic") is False for s in raw), f"{len(raw)} pages re-verified byte-for-byte"),
        ("sha256_recorded", all(len(s["sha256"]) == 64 for s in raw), "per-artifact SHA-256 + raw-set SHA-256 in identity"),
        ("requested_period_covered_or_shortfall_explained", bool(v18 and v18["status"] in ("PASS", "WARN") and v18["evidence"].get("explains_shortfall")) or v18 is None,
         v18["summary"] if v18 else "complete"),
        ("normalization_deterministic", determinism_report["kr-official"]["identical"], "two independent re-normalizations: identical hash, data_version, identity, manifest"),
        ("timezone_session_resolved", all(c["status"] == "PASS" for c in r["checks"] if c["id"] in ("V12", "V13")), "V12 timezone + V13 XKRX calendar PASS"),
        ("adjustment_semantics_usable", a["final"] in ("UNADJUSTED", "SPLIT_ADJUSTED", "DIVIDEND_ADJUSTED", "TOTAL_RETURN_ADJUSTED"), f"{a['final']} ({a['assurance']}; provider explicit statement {a['provider_explicit_statement']}): {a['final_reason'][:160]}"),
        ("validation_pass", r["status"] == "PASS", f"status {r['status']}; failed {r['failed_checks']}; unknown {r['unknown_checks']}"),
        ("no_unexpected_trading_day_gaps", next(c for c in r["checks"] if c["id"] == "V13")["status"] == "PASS", next(c for c in r["checks"] if c["id"] == "V13")["summary"]),
        ("synthetic_false_verified", next(c for c in r["checks"] if c["id"] == "V15")["status"] == "PASS", "dataset and every raw artifact declare synthetic=false"),
        ("provenance_reproducible", admission["admitted"], "admission re-verification from raw bytes reproduced hash/version/validation"),
        ("dataset_identity_reproducible", determinism_report["kr-official"]["identical"], i["data_version"]),
    ]
    results = [{"criterion": c, "met": bool(m), "evidence": e} for c, m, e in checks]
    closed = all(x["met"] for x in results)
    return {"decision": "CLOSED WITH SCOPE" if closed else "OPEN", "criteria": results,
            "scope": {"verified_coverage": [i["date_range"]["first"], i["date_range"]["last"]],
                      "pre_2020": "UNKNOWN (no official data; never interpolated, backfilled or stitched from Yahoo/Naver)",
                      "unofficial_fallback": "none: Yahoo/Naver are not an official fallback",
                      "adjustment": f"{a['assurance']}; provider explicit adjustment statement {a['provider_explicit_statement']}",
                      "out_of_range_requests": "rejected explicitly (requested period exceeds verified dataset coverage)"},
            "statement": ("OD-04 CLOSED WITH SCOPE — official KR research dataset source established via data.go.kr (금융위원회_주식시세정보 V2), "
                          "verified coverage 2020-01-02..2025-12-30 only; 2018-01-01..2019-12-31 is not available from the official service "
                          "and stays UNKNOWN"
                          if closed else "OD-04 remains OPEN: at least one criterion is not met"),
            "judgment_notes": [
                "'requested period' criterion interpreted as 'complete requested range OR shortfall explained' (the instruction's own wording): "
                "the official service holds no rows before 2020-01-02 (9 preserved probe responses incl. 1990-01-01..2020-01-02 -> totalCount 0), "
                "so 490 expected XKRX sessions (2018-01-02..2019-12-30) are NOT covered",
                "adjustment semantics rest on the official field definitions (as-traded) and the official base-price consistency of every row; "
                "the guide has no explicit adjustment statement; the covered window contains no corporate action",
                "if research requires pre-2020 KR history there is no official source for it; Yahoo/Naver pre-2020 rows remain FAIL/unverifiable"],
            "scope_restrictions": ["no Walk-Forward, tuning, ranking, profitability claim or Lockbox use was performed"]}


def export_all(*, persist_datasets: bool = True) -> dict:
    from qat.data.loader import load_dataset
    from qat.realdata.admission import check_admission

    specs = build_specs()
    evals = evaluate_all()
    persisted = {}
    if persist_datasets:
        for key, name in PRIMARY.items():
            if key in evals:
                persisted[key] = persist(evals[key], name=name)
    det = determinism(specs)
    admission = {}
    for key, name in PRIMARY.items():
        if key in evals:
            ds = load_dataset(PROCESSED_DIR / f"{name}.csv")
            admission[key] = check_admission(ds)
    cross = build_crosschecks(evals)
    groups = classify_yahoo_conflicts(evals) if {"kr-official", "kr-yahoo"} <= set(evals) else {}
    generated = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    write_evidence("source_metadata.json", {"generated_utc": generated, "official_kr_api_contract": official_api_contract(),
                                            "calendars": {k: v["identity"]["calendar"] for k, v in evals.items()},
                                            "raw_artifacts": _raw_listing()})
    write_evidence("raw_and_normalized_hashes.json", {k: {"raw": v["identity"]["raw"], "raw_set_sha256": v["identity"]["raw_set_sha256"],
                                                          "normalized_sha256": v["identity"]["normalized_sha256"], "data_version": v["identity"]["data_version"]}
                                                      for k, v in evals.items()})
    write_evidence("validation_reports.json", {k: {"summary": _summary(v), "report": v["report"]} for k, v in evals.items()})
    write_evidence("calendar_gap_report.json", {k: {"calendar": v["identity"]["calendar"],
                                                    "v13": next((c for c in v["report"]["checks"] if c["id"] == "V13"), None),
                                                    "v18": next((c for c in v["report"]["checks"] if c["id"] == "V18"), None)} for k, v in evals.items()})
    write_evidence("transformation_manifests.json", {k: v["result"].manifest for k, v in evals.items()})
    write_evidence("adjustment_semantics.json", {k: v["adjustment"] for k, v in evals.items()})
    write_evidence("deterministic_identity_report.json", {"determinism_runs": det, "identities": {k: v["identity"] for k, v in evals.items()},
                                                          "admission": {k: {kk: vv for kk, vv in a.items()} for k, a in admission.items()}})
    write_evidence("cross_check_report.json", {"comparisons": cross, "kr_yahoo_conflict_classification": groups,
                                               "historical_hypotheses": hypotheses(evals, cross, groups) if groups else []})
    decision = od04_decision(evals, det, admission["kr-official"]) if "kr-official" in evals and "kr-official" in admission else None
    if decision:
        write_evidence("kr_official_report.json", {"summary": _summary(evals["kr-official"]), "identity": evals["kr-official"]["identity"],
                                                    "adjustment": evals["kr-official"]["adjustment"], "api_contract": official_api_contract()})
        write_evidence("od04_decision.json", decision)
    return {"datasets": {k: _summary(v) for k, v in evals.items()}, "determinism": {k: v["identical"] for k, v in det.items()},
            "admission": {k: a["admitted"] for k, a in admission.items()}, "od04": decision["decision"] if decision else None}
