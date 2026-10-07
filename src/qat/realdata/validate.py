"""Real-data validation (Batch #3A, Phase 6). It REPORTS; it never repairs.

Status per check: PASS | FAIL | WARN | UNKNOWN. Overall: FAIL if any FAIL, else UNKNOWN if
any UNKNOWN, else PASS (WARN does not block). A dataset is research-admissible only at PASS.

Checks (ids stable, used by tests and evidence):
 V01 schema_header          V02 required_fields        V03 numeric_validity
 V04 positive_prices        V05 ohlc_invariants        V06 negative_volume (+ zero-volume WARN)
 V07 duplicate_timestamps   V08 timestamp_order        V09 overlapping_bars
 V10 symbol_consistency     V11 market_consistency     V12 timezone_consistency
 V13 trading_calendar       V14 source_metadata        V15 synthetic_source
 V16 adjustment_semantics   V17 corporate_action_discontinuity
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from qat.realdata.calendars import Calendar, CalendarError, MARKET_CALENDAR
from qat.realdata.normalize import FIELDS, HEADER, parse_csv_rows

TZ_LABEL = {"KR": "+09:00", "US": "UTC", "CRYPTO": "UTC"}
_MAX_EXAMPLES = 8


@dataclass
class ValidationContext:
    market: str
    symbol: str
    provider: str
    timezone_label: str
    exchange_timezone: str
    calendar: Calendar
    window_start: str
    window_end: str
    synthetic_flag: bool = False
    meta_source: str | None = None
    expected_reported_symbols: tuple = ()
    expected_utc_times: tuple = ()
    reported: dict = field(default_factory=dict)
    aux: list | None = None
    raw: list = field(default_factory=list)          # raw sidecars (dicts)
    adjustment: dict | None = None
    requested_start: str | None = None
    coverage_evidence: dict | None = None   # {"windows": [...], "explains_shortfall": bool}


def _check(cid, name, status, summary, count=0, examples=None, **extra):
    return {"id": cid, "name": name, "status": status, "summary": summary, "count": count,
            "examples": (examples or [])[:_MAX_EXAMPLES], **extra}


def _token(value: str):
    """(float | None, kind) with kind in ok/missing/non_finite/invalid."""
    text = value.strip()
    if text == "":
        return None, "missing"
    low = text.lower()
    if low in ("nan", "inf", "-inf", "+inf"):
        return float(low), "non_finite"
    try:
        number = float(text)
    except ValueError:
        return None, "invalid"
    return (number, "ok") if math.isfinite(number) else (number, "non_finite")


def validate(csv_bytes: bytes, ctx: ValidationContext) -> dict:
    header, rows = parse_csv_rows(csv_bytes)
    checks = []

    # V01 schema
    ok_header = ",".join(header) == HEADER
    checks.append(_check("V01", "schema_header", "PASS" if ok_header else "FAIL",
                         "header matches canonical schema" if ok_header else f"header {header!r} != {HEADER!r}"))
    short = [r["line"] for r in rows if "tokens" not in r]
    parsed = []
    for r in rows:
        if "tokens" not in r:
            continue
        values, kinds = {}, {}
        for f in FIELDS:
            values[f], kinds[f] = _token(r["tokens"].get(f, ""))
        try:
            day = datetime.strptime(r["timestamp"], "%Y-%m-%dT%H:%M:%S")
            day_ok = day.hour == day.minute == day.second == 0
            d = day.date().isoformat()
        except ValueError:
            day_ok, d = False, None
        parsed.append({"line": r["line"], "date": d, "date_ok": day_ok, "ts": r["timestamp"], "values": values, "kinds": kinds})

    # V02 required fields
    missing = [{"line": p["line"], "field": f} for p in parsed for f in FIELDS if p["kinds"][f] == "missing"]
    bad_ts = [{"line": p["line"], "timestamp": p["ts"]} for p in parsed if not p["date_ok"]]
    checks.append(_check("V02", "required_fields", "FAIL" if (missing or bad_ts or short) else "PASS",
                         f"{len(missing)} missing values, {len(bad_ts)} unparseable/non-midnight timestamps, {len(short)} short rows",
                         len(missing) + len(bad_ts) + len(short), missing + bad_ts + [{"line": n} for n in short]))

    # V03 numeric validity
    nonfinite = [{"line": p["line"], "field": f, "token": rows[p["line"] - 1]["tokens"][f]}
                 for p in parsed for f in FIELDS if p["kinds"][f] in ("non_finite", "invalid")]
    checks.append(_check("V03", "numeric_validity", "FAIL" if nonfinite else "PASS",
                         f"{len(nonfinite)} NaN/inf/invalid numeric values", len(nonfinite), nonfinite))

    # V04 positive prices (zero or negative)
    nonpos = [{"line": p["line"], "date": p["date"], "field": f, "value": p["values"][f]}
              for p in parsed for f in ("open", "high", "low", "close")
              if p["kinds"][f] == "ok" and p["values"][f] <= 0]
    checks.append(_check("V04", "positive_prices", "FAIL" if nonpos else "PASS",
                         f"{len(nonpos)} zero/negative price values", len(nonpos), nonpos))

    # V05 OHLC invariants (only rows whose four prices are finite)
    viol = []
    for p in parsed:
        v = p["values"]
        if any(p["kinds"][f] != "ok" for f in ("open", "high", "low", "close")):
            continue
        if not (v["low"] <= v["open"] <= v["high"] and v["low"] <= v["close"] <= v["high"] and v["high"] >= v["low"]):
            viol.append({"line": p["line"], "date": p["date"], "open": v["open"], "high": v["high"], "low": v["low"], "close": v["close"]})
    checks.append(_check("V05", "ohlc_invariants", "FAIL" if viol else "PASS",
                         f"{len(viol)} rows violate low<=open<=high / low<=close<=high", len(viol), viol))

    # V06 volume
    negative = [{"line": p["line"], "date": p["date"], "volume": p["values"]["volume"]} for p in parsed
                if p["kinds"]["volume"] == "ok" and p["values"]["volume"] < 0]
    zero = [{"line": p["line"], "date": p["date"]} for p in parsed if p["kinds"]["volume"] == "ok" and p["values"]["volume"] == 0]
    checks.append(_check("V06", "negative_volume", "FAIL" if negative else "PASS", f"{len(negative)} negative volumes", len(negative), negative))
    if zero:
        checks.append(_check("V06b", "zero_volume", "WARN", f"{len(zero)} sessions with zero volume (possible suspension / no trades)",
                             len(zero), zero))

    # V07 duplicates, V08 order, V09 overlap
    seen, dups = {}, []
    for p in parsed:
        if p["date"] is None:
            continue
        if p["date"] in seen:
            dups.append({"line": p["line"], "date": p["date"], "first_line": seen[p["date"]]})
        else:
            seen[p["date"]] = p["line"]
    checks.append(_check("V07", "duplicate_timestamps", "FAIL" if dups else "PASS", f"{len(dups)} duplicate timestamps", len(dups), dups))
    dated = [p for p in parsed if p["date"] is not None]
    unordered = [{"line": b["line"], "date": b["date"], "after": a["date"]} for a, b in zip(dated, dated[1:]) if b["date"] < a["date"]]
    checks.append(_check("V08", "timestamp_order", "FAIL" if unordered else "PASS", f"{len(unordered)} out-of-order timestamps", len(unordered), unordered))
    checks.append(_check("V09", "overlapping_bars", "FAIL" if (dups or unordered) else "PASS",
                         "daily session bars cannot overlap unless timestamps repeat or run backwards (see V07/V08)",
                         len(dups) + len(unordered)))

    # V10 symbol consistency
    problems = []
    reported = set(ctx.reported.get("symbols", []))
    expected = set(ctx.expected_reported_symbols)
    if expected and reported and not reported <= expected:
        problems.append(f"provider reports symbol(s) {sorted(reported)} not in expected {sorted(expected)}")
    if expected and not reported and ctx.aux is None:
        problems.append("provider-reported symbol unavailable")
    for key, want in (("srtnCd", ctx.symbol), ("isinCd", None)):
        if ctx.aux:
            vals = {a.get(key) for a in ctx.aux if a.get(key) is not None}
            if want is not None and vals and vals != {want}:
                problems.append(f"{key} values {sorted(vals)} != {want!r}")
            if want is None and len(vals) > 1:
                problems.append(f"multiple {key} values {sorted(vals)}")
    wrong_raw = [r["artifact"] for r in ctx.raw if r.get("symbol") not in (ctx.symbol, ctx.symbol.replace("/", ""))]
    if wrong_raw:
        problems.append(f"raw artifacts declare a different symbol: {wrong_raw[:3]}")
    checks.append(_check("V10", "symbol_consistency", "FAIL" if problems else "PASS",
                         "; ".join(problems) if problems else f"symbol {ctx.symbol} consistent across metadata, provenance and provider fields",
                         len(problems), problems))

    # V11 market consistency
    problems = []
    if ctx.market not in MARKET_CALENDAR:
        problems.append(f"unknown market {ctx.market!r}")
    elif MARKET_CALENDAR[ctx.market] != ctx.calendar.code:
        problems.append(f"market {ctx.market} requires calendar {MARKET_CALENDAR[ctx.market]}, got {ctx.calendar.code}")
    wrong_market = [r["artifact"] for r in ctx.raw if r.get("market") != ctx.market]
    if wrong_market:
        problems.append(f"raw artifacts declare a different market: {wrong_market[:3]}")
    checks.append(_check("V11", "market_consistency", "FAIL" if problems else "PASS",
                         "; ".join(problems) if problems else f"market {ctx.market} <-> calendar {ctx.calendar.code} <-> raw provenance agree",
                         len(problems), problems))

    # V12 timezone consistency
    problems = []
    if ctx.timezone_label != TZ_LABEL.get(ctx.market):
        problems.append(f"metadata timezone {ctx.timezone_label!r} != expected {TZ_LABEL.get(ctx.market)!r} for {ctx.market}")
    wrong_tz = sorted({(r.get("timezone") or {}).get("exchange_timezone") for r in ctx.raw
                       if (r.get("timezone") or {}).get("exchange_timezone") != ctx.exchange_timezone})
    if wrong_tz:
        problems.append(f"raw provenance exchange_timezone {wrong_tz} != {ctx.exchange_timezone}")
    if ctx.expected_utc_times and ctx.aux:
        odd = sorted({a["ts_utc"][11:16] for a in ctx.aux if a.get("ts_utc") and a["ts_utc"][11:16] not in ctx.expected_utc_times})
        if odd:
            problems.append(f"provider timestamps at unexpected UTC time-of-day {odd} (expected {list(ctx.expected_utc_times)})")
    checks.append(_check("V12", "timezone_consistency", "FAIL" if problems else "PASS",
                         "; ".join(problems) if problems else f"timezone label {ctx.timezone_label}, exchange {ctx.exchange_timezone}, provider stamps consistent",
                         len(problems), problems))

    # V13 trading calendar
    try:
        expected_sessions = ctx.calendar.sessions_between(date.fromisoformat(ctx.window_start), date.fromisoformat(ctx.window_end))
        present = {p["date"] for p in dated}
        missing_sessions = [d.isoformat() for d in expected_sessions if d.isoformat() not in present]
        extra_dates = sorted(d for d in present if not ctx.calendar.is_session(date.fromisoformat(d))) if not ctx.calendar.twenty_four_seven else []
        status = "FAIL" if (missing_sessions or extra_dates) else "PASS"
        checks.append(_check("V13", "trading_calendar", status,
                             f"{len(expected_sessions)} expected {ctx.calendar.code} sessions; {len(missing_sessions)} missing; {len(extra_dates)} rows on non-session dates",
                             len(missing_sessions) + len(extra_dates), missing_sessions + extra_dates,
                             calendar=ctx.calendar.identity(), expected_sessions=len(expected_sessions),
                             missing_sessions=missing_sessions, extra_dates=extra_dates))
    except CalendarError as exc:
        checks.append(_check("V13", "trading_calendar", "UNKNOWN", f"calendar cannot judge the window: {exc}", 1))

    # V14 source metadata
    problems = []
    for r in ctx.raw:
        if r.get("provider") != ctx.provider:
            problems.append(f"{r.get('artifact')}: provider {r.get('provider')!r} != {ctx.provider!r}")
    if ctx.meta_source is not None and ctx.meta_source != ctx.provider:
        problems.append(f"dataset metadata source {ctx.meta_source!r} != provenance provider {ctx.provider!r}")
    if not ctx.raw:
        problems.append("no raw provenance recorded")
    checks.append(_check("V14", "source_metadata", "FAIL" if problems else "PASS",
                         "; ".join(problems[:3]) if problems else f"{len(ctx.raw)} raw artifacts, provider {ctx.provider} consistent",
                         len(problems), problems))

    # V15 synthetic / source contradiction
    problems = []
    if ctx.synthetic_flag:
        problems.append("dataset declares synthetic=true but claims real-data provenance")
    if any(r.get("synthetic") is not False for r in ctx.raw):
        problems.append("a raw artifact is not declared synthetic=false")
    if str(ctx.meta_source or "").upper().startswith("SYNTHETIC") or ctx.provider.upper().startswith("SYNTHETIC"):
        problems.append("source claims SYNTHETIC for a real dataset")
    checks.append(_check("V15", "synthetic_source", "FAIL" if problems else "PASS",
                         "; ".join(problems) if problems else "synthetic=false verified on dataset and every raw artifact", len(problems), problems))

    # V16 adjustment semantics, V17 discontinuity
    adj = ctx.adjustment
    if adj is None:
        checks.append(_check("V16", "adjustment_semantics", "UNKNOWN", "adjustment semantics not assessed", 1))
    elif adj["final"] == "UNKNOWN":
        checks.append(_check("V16", "adjustment_semantics", "UNKNOWN", f"final semantics UNKNOWN: {adj['final_reason']}", 1,
                             observed=adj["observed"]["state"]))
    else:
        checks.append(_check("V16", "adjustment_semantics", "PASS", f"{adj['final']}: {adj['final_reason']}", 0, final=adj["final"]))
    if adj is not None and ctx.market != "CRYPTO":
        bad = adj["unadjusted_discontinuities"] if adj["final"] == "UNADJUSTED" else []
        checks.append(_check("V17", "corporate_action_discontinuity", "FAIL" if bad else "PASS",
                             (f"UNADJUSTED series has {len(bad)} corporate-action discontinuities inside the window"
                              if bad else "no unadjusted corporate-action discontinuity inside the window"),
                             len(bad), bad, observed_events=adj["observed"]["events"]))

    # V18 requested-period coverage
    if ctx.requested_start and ctx.requested_start < ctx.window_start:
        gap_sessions = 0
        try:
            gap_sessions = len(ctx.calendar.sessions_between(
                date.fromisoformat(ctx.requested_start), date.fromisoformat(ctx.window_start) - timedelta(days=1)))
        except CalendarError:
            pass
        ev = ctx.coverage_evidence or {}
        explained = bool(ev.get("explains_shortfall"))
        checks.append(_check("V18", "requested_period_coverage", "WARN" if explained else "FAIL",
                             (f"provider cannot supply {ctx.requested_start}..{ctx.window_start}: {gap_sessions} expected sessions are NOT in this dataset; "
                              + ("explained by preserved probe responses that all returned 0 rows" if explained else "NOT explained by evidence")),
                             gap_sessions, [], requested_start=ctx.requested_start, covered_start=ctx.window_start,
                             shortfall_sessions=gap_sessions, evidence=ev))
    statuses = {c["status"] for c in checks}
    overall = "FAIL" if "FAIL" in statuses else ("UNKNOWN" if "UNKNOWN" in statuses else "PASS")
    return {"status": overall, "rows": len(rows), "checks": checks,
            "first_date": dated[0]["date"] if dated else None, "last_date": dated[-1]["date"] if dated else None,
            "failed_checks": [c["id"] for c in checks if c["status"] == "FAIL"],
            "unknown_checks": [c["id"] for c in checks if c["status"] == "UNKNOWN"],
            "warnings": [c["id"] for c in checks if c["status"] == "WARN"]}
