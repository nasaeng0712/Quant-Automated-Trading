"""Dataset specifications for the Batch #3A instruments + identity + persistence.

``SPECS`` lists, for each instrument, the primary dataset and the independent sources used
for cross-checks. A spec is pure data (raw file names, rules); everything derived from it
is recomputed from the preserved raw bytes - that is what makes identity reproducible.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import re

from qat.realdata import adjustment as adj_mod
from qat.realdata.calendars import load_calendar
from qat.realdata.normalize import DatasetSpec, NormalizedResult, normalize, to_number
from qat.realdata.provenance import artifact_dir, list_artifacts, load_artifact, verify_artifact
from qat.realdata.sources import parse_datagokr_stock_price
from qat.realdata.validate import ValidationContext, validate

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[3]
PROCESSED_DIR = PROJECT_ROOT / "data" / "processed" / "real"
START, END = "2018-01-01", "2025-12-31"


def _raw(provider: str, market: str, symbol: str, suffix: tuple = (".zip", ".json", ".xml")) -> tuple:
    return tuple(p for p in list_artifacts(provider, market, symbol) if p.name.endswith(suffix))


def build_specs() -> dict[str, DatasetSpec]:
    """Specs are built from whatever raw artifacts are on disk (files are looked up, never created)."""

    specs = {
        "crypto-binance": DatasetSpec(
            key="crypto-binance", parser="binance", provider="binance-vision", market="CRYPTO", symbol="BTC/USDT",
            provider_symbol="BTCUSDT", calendar_code="24X7", timezone_label="UTC", exchange_timezone="UTC",
            window_start=START, window_end=END, raw_files=_raw("binance-vision", "CRYPTO", "BTCUSDT", (".zip",))),
        "crypto-coinbase": DatasetSpec(
            key="crypto-coinbase", parser="coinbase", provider="coinbase-exchange", market="CRYPTO", symbol="BTC/USDT",
            provider_symbol="BTCUSDT", calendar_code="24X7", timezone_label="UTC", exchange_timezone="UTC",
            # cross-check source only: BTC-USDT candles exist from 2021-05-04 (earlier windows return no rows)
            window_start="2021-05-04", window_end=END, raw_files=_raw("coinbase-exchange", "CRYPTO", "BTCUSDT", (".json",))),
        "us-yahoo": DatasetSpec(
            key="us-yahoo", parser="yahoo", provider="yahoo-chart", market="US", symbol="AAPL", provider_symbol="AAPL",
            calendar_code="XNYS", timezone_label="UTC", exchange_timezone="America/New_York",
            window_start=START, window_end=END, raw_files=_raw("yahoo-chart", "US", "AAPL"),
            parser_kwargs={"utc_offset_seconds": 0}, expected_reported_symbols=("AAPL",), expected_utc_times=("13:30", "14:30")),
        "us-nasdaq": DatasetSpec(
            key="us-nasdaq", parser="nasdaq", provider="nasdaq-historical", market="US", symbol="AAPL", provider_symbol="AAPL",
            calendar_code="XNYS", timezone_label="UTC", exchange_timezone="America/New_York",
            window_start=START, window_end=END, raw_files=_raw("nasdaq-historical", "US", "AAPL"),
            expected_reported_symbols=("AAPL",)),
        "kr-yahoo": DatasetSpec(
            key="kr-yahoo", parser="yahoo", provider="yahoo-chart", market="KR", symbol="005930", provider_symbol="005930",
            calendar_code="XKRX", timezone_label="+09:00", exchange_timezone="Asia/Seoul",
            window_start=START, window_end=END, raw_files=_raw("yahoo-chart", "KR", "005930"),
            parser_kwargs={"utc_offset_seconds": 9 * 3600}, expected_reported_symbols=("005930.KS",), expected_utc_times=("00:00",)),
        "kr-naver": DatasetSpec(
            key="kr-naver", parser="naver", provider="naver-fchart", market="KR", symbol="005930", provider_symbol="005930",
            calendar_code="XKRX", timezone_label="+09:00", exchange_timezone="Asia/Seoul",
            window_start=START, window_end=END, raw_files=_raw("naver-fchart", "KR", "005930"),
            expected_reported_symbols=("005930",)),
        "kr-official": DatasetSpec(
            key="kr-official", parser="datagokr", provider="data.go.kr", market="KR", symbol="005930", provider_symbol="005930",
            calendar_code="XKRX", timezone_label="+09:00", exchange_timezone="Asia/Seoul",
            # coverage evidence: the official V2 service returns 0 rows for 005930 before 2020-01-02
            # (9 probe responses preserved under data/raw/data.go.kr/KR/005930-coverage-probe)
            window_start="2020-01-02", window_end=END, requested_start=START,
            raw_files=_raw("data.go.kr", "KR", "005930", (".json",)), paging="descending_list",
            declared_adjustment=_official_guide_declaration()),
    }
    return specs


def _official_guide_declaration() -> dict:
    """data.go.kr guide: prices are defined as formed trade prices; no adjustment statement exists."""

    guide = artifact_dir("data.go.kr", "KR", "005930-docs") / "guide.docx"
    digest = load_artifact(guide).sha256 if guide.is_file() else None
    return {"state": "UNADJUSTED", "kind": "official_field_definitions",
            "document": {"title": "금융위원회_주식시세정보_활용자가이드.docx", "sha256": digest,
                         "url": "https://www.data.go.kr/data/15094808/openapi.do",
                         "retrieved_as": "raw artifact 005930-docs/guide.docx"},
            "basis": ("guide field definitions: clpr = last price formed until the end of the regular session, "
                      "mkp = first price formed after the regular session opens, trqu = cumulative traded quantity "
                      "(as-traded values). The guide contains no adjustment / split / rights statement."),
            "explicit_adjustment_statement": False}


def _portable(path: pathlib.Path) -> str:
    """Repo-relative POSIX path when inside the project, else the absolute POSIX path (tests)."""

    try:
        return pathlib.Path(path).resolve().relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return pathlib.Path(path).resolve().as_posix()


def spec_to_dict(spec: DatasetSpec) -> dict:
    return {"key": spec.key, "parser": spec.parser, "provider": spec.provider, "market": spec.market, "symbol": spec.symbol,
            "provider_symbol": spec.provider_symbol, "calendar_code": spec.calendar_code,
            "timezone_label": spec.timezone_label, "exchange_timezone": spec.exchange_timezone,
            "window_start": spec.window_start, "window_end": spec.window_end,
            "raw_files": [_portable(p) for p in spec.raw_files],
            "parser_kwargs": spec.parser_kwargs, "expected_reported_symbols": list(spec.expected_reported_symbols),
            "expected_utc_times": list(spec.expected_utc_times), "declared_adjustment": spec.declared_adjustment,
            "paging": spec.paging, "requested_start": spec.requested_start}


def spec_from_dict(d: dict) -> DatasetSpec:
    return DatasetSpec(
        key=d["key"], parser=d["parser"], provider=d["provider"], market=d["market"], symbol=d["symbol"],
        provider_symbol=d["provider_symbol"], calendar_code=d["calendar_code"], timezone_label=d["timezone_label"],
        exchange_timezone=d["exchange_timezone"], window_start=d["window_start"], window_end=d["window_end"],
        raw_files=tuple(PROJECT_ROOT / p for p in d["raw_files"]), parser_kwargs=d.get("parser_kwargs") or {},
        expected_reported_symbols=tuple(d.get("expected_reported_symbols") or ()),
        expected_utc_times=tuple(d.get("expected_utc_times") or ()), declared_adjustment=d.get("declared_adjustment"),
        paging=d.get("paging", "independent"), requested_start=d.get("requested_start"))


def _dividend_evidence(result: NormalizedResult) -> dict | None:
    """Yahoo supplies an adjusted close next to the close: if they differ, the CLOSE is not
    dividend-adjusted (adjclose is a separate field)."""

    pairs = [(r["close"], a.get("adjclose")) for r, a in zip(result.rows, result.aux)
             if a.get("adjclose") is not None and r["close"] is not None]
    if not pairs:
        return None
    differing = sum(1 for c, a in pairs if abs(c - a) / max(abs(c), 1e-12) > 1e-4)
    return {"status": "CLOSE_NOT_DIVIDEND_ADJUSTED" if differing else "NO_DIFFERENCE_OBSERVED",
            "rows_with_adjclose": len(pairs), "rows_where_adjclose_differs_from_close": differing,
            "note": "adjusted close is a separate provider field; OHLC columns are what is normalized"}


def _day_before(iso: str) -> str:
    from datetime import date as _d, timedelta as _t

    return (_d.fromisoformat(iso) - _t(days=1)).isoformat()


def _coverage_evidence(spec: DatasetSpec) -> dict | None:
    """Preserved probe responses showing the provider returns no rows before the covered start."""

    if not spec.requested_start or spec.provider != "data.go.kr":
        return None
    windows = []
    for path in list_artifacts("data.go.kr", "KR", spec.provider_symbol + "-coverage-probe"):
        art = verify_artifact(path)
        total = parse_datagokr_stock_price(path.read_bytes()).meta["total_count"]
        windows.append({"artifact": path.name, "sha256": art.sha256, "requested": art.sidecar["requested_range"],
                        "total_count": int(total)})
    starts = [w["requested"]["start"] for w in windows]
    ends = [w["requested"]["end"] for w in windows]
    reaches = bool(windows) and min(starts) <= spec.requested_start and any(e >= _day_before(spec.window_start) for e in ends)
    return {"windows": windows, "explains_shortfall": bool(windows) and all(w["total_count"] == 0 for w in windows) and reaches,
            "note": "each probe is a raw API response with totalCount 0; endBasDt is exclusive so the last window ends the day before the covered start"}


def _base_price_consistency(result: NormalizedResult) -> dict | None:
    """Official fields only: close - vs must equal the previous session's close unless the base price
    was adjusted (corporate action). Detects any such event in the series."""

    if not result.aux or "vs" not in result.aux[0]:
        return None
    mismatches, checked = [], 0
    for prev, row, aux in zip(result.rows, result.rows[1:], result.aux[1:]):
        vs_value, how = to_number(aux.get("vs"))
        if vs_value is None or how == "invalid" or prev["close"] is None or row["close"] is None:
            continue
        checked += 1
        if abs((row["close"] - vs_value) - prev["close"]) > 0.5:
            mismatches.append({"date": row["date"], "prev_close": prev["close"], "close": row["close"], "vs": vs_value})
    return {"rows_checked": checked, "mismatch_count": len(mismatches), "mismatches": mismatches}


def assess_and_validate(result: NormalizedResult, *, meta_source: str | None = None, synthetic_flag: bool = False,
                        timezone_label: str | None = None, calendar_dir=None) -> tuple[dict, dict]:
    spec = result.spec
    calendar = load_calendar(spec.calendar_code, calendar_dir)
    adjustment = adj_mod.assess(result.rows, market=spec.market, symbol=spec.symbol, declared=spec.declared_adjustment,
                                dividend_evidence=_dividend_evidence(result), base_price=_base_price_consistency(result))
    coverage = _coverage_evidence(spec)
    sidecars = [load_artifact(p).sidecar for p in spec.raw_files]
    ctx = ValidationContext(
        market=spec.market, symbol=spec.symbol, provider=spec.provider,
        timezone_label=timezone_label or spec.timezone_label, exchange_timezone=spec.exchange_timezone, calendar=calendar,
        window_start=spec.window_start, window_end=spec.window_end, synthetic_flag=synthetic_flag,
        meta_source=meta_source if meta_source is not None else spec.provider,
        expected_reported_symbols=spec.expected_reported_symbols, expected_utc_times=spec.expected_utc_times,
        reported=result.reported, aux=result.aux, raw=sidecars, adjustment=adjustment,
        requested_start=spec.requested_start, coverage_evidence=coverage)
    return validate(result.csv_bytes, ctx), adjustment


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-")


def build_identity(result: NormalizedResult, report: dict, adjustment: dict, calendar_identity: dict, spec: DatasetSpec) -> dict:
    """Deterministic identity: contains no retrieval time, no run id, no path outside the repo."""

    first, last = report["first_date"], report["last_date"]
    identity = {
        "schema": 1, "provider": spec.provider, "market": spec.market, "symbol": spec.symbol, "timeframe": "1d",
        "timezone": spec.timezone_label, "exchange_timezone": spec.exchange_timezone,
        "date_range": {"first": first, "last": last, "requested": [spec.window_start, spec.window_end]},
        "rows": report["rows"], "adjustment_semantics": adjustment["final"],
        "adjustment": {"final": adjustment["final"], "final_reason": adjustment["final_reason"],
                       "assurance": adjustment["assurance"], "provider_explicit_statement": adjustment["provider_explicit_statement"],
                       "declared": adjustment["declared"], "observed_state": adjustment["observed"]["state"],
                       "dividend": adjustment["dividend"]},
        "raw": [{"name": r["name"], "sha256": r["sha256"], "size_bytes": r["size_bytes"]} for r in sorted(result.raw, key=lambda x: x["name"])],
        "raw_set_sha256": result.raw_set_sha256, "normalized_sha256": result.normalized_sha256,
        "transformation_manifest_sha256": result.manifest["manifest_sha256"],
        "calendar": calendar_identity, "validation_status": report["status"],
        "validation_failed_checks": report["failed_checks"], "validation_unknown_checks": report["unknown_checks"],
        "synthetic": False, "spec": spec_to_dict(spec),
    }
    identity["data_version"] = (f"{spec.market}-{_slug(spec.symbol)}-1d-{_slug(spec.provider)}-"
                                f"{result.normalized_sha256[:12]}-{result.raw_set_sha256[:6]}")
    return identity


def identity_bytes(identity: dict) -> bytes:
    return (json.dumps(identity, indent=1, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def evaluate(spec: DatasetSpec, *, calendar_dir=None) -> dict:
    """Normalize + validate + identity from raw bytes. Pure function of the preserved raw
    artifacts, the spec and the calendar snapshot - run it twice and compare."""

    result = normalize(spec)
    report, adjustment = assess_and_validate(result, calendar_dir=calendar_dir)
    calendar = load_calendar(spec.calendar_code, calendar_dir)
    identity = build_identity(result, report, adjustment, calendar.identity(), spec)
    return {"result": result, "report": report, "adjustment": adjustment, "identity": identity}


def persist(evaluation: dict, *, name: str, directory: pathlib.Path | None = None) -> dict:
    """Write the canonical CSV, its DatasetMeta sidecar and the identity file."""

    directory = directory or PROCESSED_DIR
    directory.mkdir(parents=True, exist_ok=True)
    spec: DatasetSpec = evaluation["result"].spec
    identity = evaluation["identity"]
    csv_path = directory / f"{name}.csv"
    csv_path.write_bytes(evaluation["result"].csv_bytes)
    identity_path = directory / f"{name}.identity.json"
    identity_path.write_bytes(identity_bytes(identity))
    meta = {
        "market": spec.market, "symbol": spec.symbol, "timeframe": "1d", "timezone": spec.timezone_label,
        "timestamp_label": "open", "source": spec.provider, "synthetic": False,
        "description": f"Real market data from {spec.provider}; see identity file. NOT synthetic.",
        "extra": {"real_data": {"identity_file": identity_path.name,
                                "identity_sha256": hashlib.sha256(identity_path.read_bytes()).hexdigest(),
                                "data_version": identity["data_version"]}},
    }
    meta_path = directory / f"{name}.csv.meta.json"
    meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return {"csv": csv_path, "meta": meta_path, "identity": identity_path}
