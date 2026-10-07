"""Dataset validation (Batch #2 Historical Data Engine).

Checks ordering, duplicates, missing / non-finite values, non-positive prices,
OHLC relations, volume, and gaps. Nothing is repaired: there is no
interpolation and no row is dropped. ``status`` is

  FAIL - at least one ERROR issue; the dataset must not feed a backtest
  WARN - only warnings (e.g. unexplained gaps, zero volume)
  PASS - no issues beyond informational session gaps

Gap classification is evidence, not certainty: KR/US use a weekday session
model without an exchange holiday calendar, so a missing weekday is reported
as ``unexplained_weekday_gap`` (holiday or missing data - UNKNOWN). US intraday
sessions need an IANA tz database; without one those gaps are
``unclassified_gap``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta, timezone

from qat.data.bars import Bar, DatasetMeta, resolve_timezone

_MAX_EXAMPLES = 20

# weekday session model (local exchange time); holidays are NOT modelled
_SESSIONS = {
    "KR": {"tz": timezone(timedelta(hours=9)), "open": time(9, 0), "close": time(15, 30)},
    "US": {"tz_name": "America/New_York", "open": time(9, 30), "close": time(16, 0)},
}


@dataclass
class RawRow:
    row: int  # 1-based data row number (header excluded)
    ts_text: str
    ts: datetime | None
    values: dict  # open/high/low/close/volume -> float | None
    parse_errors: list = field(default_factory=list)


@dataclass
class ValidationReport:
    status: str
    rows: int
    valid_rows: int
    first_ts: str | None
    last_ts: str | None
    issues: list = field(default_factory=list)
    gap_summary: dict = field(default_factory=dict)
    calendar_note: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def usable(self) -> bool:
        return self.status != "FAIL"


class _Issues:
    def __init__(self) -> None:
        self._by_code: dict[str, dict] = {}

    def add(self, code: str, severity: str, row: int | None, ts: str | None, detail: str = "") -> None:
        entry = self._by_code.setdefault(code, {"code": code, "severity": severity, "count": 0, "examples": []})
        entry["count"] += 1
        if len(entry["examples"]) < _MAX_EXAMPLES:
            entry["examples"].append({"row": row, "ts": ts, "detail": detail})

    def all(self) -> list[dict]:
        order = {"ERROR": 0, "WARN": 1, "INFO": 2}
        return sorted(self._by_code.values(), key=lambda e: (order[e["severity"]], e["code"]))


def _us_tz():
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo("America/New_York")
    except Exception:  # noqa: BLE001 - tz database not installed (e.g. Windows w/o tzdata)
        return None


def _weekdays_between(a: date, b: date) -> list[date]:
    out, day = [], a + timedelta(days=1)
    while day < b:
        if day.weekday() < 5:
            out.append(day)
        day += timedelta(days=1)
    return out


def validate_rows(meta: DatasetMeta, rows: list[RawRow]) -> tuple[ValidationReport, list[Bar]]:
    issues = _Issues()
    bars: list[Bar] = []
    for raw in rows:
        for err in raw.parse_errors:
            issues.add(err[0], "ERROR", raw.row, raw.ts_text, err[1])
        if raw.parse_errors:
            continue
        v = raw.values
        o, h, lo, c, vol = v["open"], v["high"], v["low"], v["close"], v["volume"]
        bad = False
        if min(o, h, lo, c) <= 0:
            issues.add("non_positive_price", "ERROR", raw.row, raw.ts_text, f"o={o} h={h} l={lo} c={c}")
            bad = True
        if lo > h or h < max(o, c) or lo > min(o, c):
            issues.add("ohlc_inconsistent", "ERROR", raw.row, raw.ts_text, f"o={o} h={h} l={lo} c={c}")
            bad = True
        if vol < 0:
            issues.add("negative_volume", "ERROR", raw.row, raw.ts_text, f"volume={vol}")
            bad = True
        elif vol == 0:
            issues.add("zero_volume", "WARN", raw.row, raw.ts_text, "")
        if not bad:
            bars.append(Bar(raw.ts, o, h, lo, c, vol))

    # ordering / duplicates on every parsed timestamp (independent of value errors)
    stamped = [r for r in rows if r.ts is not None]
    seen: dict[datetime, int] = {}
    for prev, cur in zip(stamped, stamped[1:]):
        if cur.ts < prev.ts:
            issues.add("unsorted_timestamp", "ERROR", cur.row, cur.ts_text,
                       f"after row {prev.row} ({prev.ts_text})")
    for r in stamped:
        if r.ts in seen:
            issues.add("duplicate_timestamp", "ERROR", r.row, r.ts_text, f"same as row {seen[r.ts]}")
        else:
            seen[r.ts] = r.row

    gap_summary = _classify_gaps(meta, sorted({r.ts for r in stamped}), issues)
    if not rows:
        issues.add("empty_dataset", "ERROR", None, None, "no data rows")

    all_issues = issues.all()
    if any(i["severity"] == "ERROR" for i in all_issues):
        status = "FAIL"
    elif any(i["severity"] == "WARN" for i in all_issues):
        status = "WARN"
    else:
        status = "PASS"
    report = ValidationReport(
        status=status,
        rows=len(rows),
        valid_rows=len(bars),
        first_ts=stamped[0].ts.isoformat() if stamped else None,
        last_ts=stamped[-1].ts.isoformat() if stamped else None,
        issues=all_issues,
        gap_summary=gap_summary,
        calendar_note=_calendar_note(meta),
    )
    return report, sorted(bars, key=lambda b: b.ts)


def _calendar_note(meta: DatasetMeta) -> str:
    if meta.market == "CRYPTO":
        return "CRYPTO: continuous 24/7 model; every gap is treated as missing data"
    note = f"{meta.market}: weekday session model, exchange holidays NOT modelled (UNKNOWN)"
    if meta.market == "US" and meta.duration < timedelta(days=1) and _us_tz() is None:
        note += "; US intraday session classification unavailable (no IANA tz database)"
    return note


def _classify_gaps(meta: DatasetMeta, stamps: list[datetime], issues: _Issues) -> dict:
    duration = meta.duration
    summary: dict[str, int] = {}

    def record(kind: str, severity: str, ts: datetime, detail: str) -> None:
        summary[kind] = summary.get(kind, 0) + 1
        issues.add(kind, severity, None, ts.isoformat(), detail)

    session = _SESSIONS.get(meta.market)
    tz = None
    if duration >= timedelta(days=1):
        # daily bars: the file's declared timezone defines the exchange date
        tz = resolve_timezone(meta.timezone)
    elif session is not None:
        tz = session.get("tz") or _us_tz()

    # daily bars may legitimately differ by an hour across a DST change
    min_step = duration - timedelta(hours=1) if duration >= timedelta(days=1) else duration
    for prev, cur in zip(stamps, stamps[1:]):
        delta = cur - prev
        if delta < min_step:
            # Audit fix (D6): bars closer than the timeframe overlap; the old code
            # treated any delta <= duration as "no gap" and reported PASS.
            record("overlapping_bars", "ERROR", cur,
                   f"{prev.isoformat()} -> {cur.isoformat()} is shorter than timeframe {meta.timeframe}")
            continue
        if delta <= duration:
            continue
        missing_bars = int(delta / duration) - 1
        detail = f"{prev.isoformat()} -> {cur.isoformat()} (~{missing_bars} bars)"
        if meta.market == "CRYPTO":
            record("missing_data_gap", "WARN", cur, detail)
            continue
        if tz is None:
            record("unclassified_gap", "WARN", cur, detail + "; no tz database for session model")
            continue
        lp, lc = prev.astimezone(tz), cur.astimezone(tz)
        if duration >= timedelta(days=1):
            weekdays = _weekdays_between(lp.date(), lc.date())
            if weekdays:
                record("unexplained_weekday_gap", "WARN", cur,
                       detail + f"; missing weekdays {[d.isoformat() for d in weekdays[:5]]}"
                       " (holiday or missing data - no exchange calendar)")
            else:
                record("session_closed_weekend", "INFO", cur, detail)
            continue
        if lp.date() == lc.date():
            record("intraday_missing_data", "WARN", cur, detail)
            continue
        weekdays = _weekdays_between(lp.date(), lc.date())
        prev_close = (prev + duration).astimezone(tz).time()
        edge_missing = prev_close < session["close"] or lc.time() > session["open"]
        if weekdays:
            record("unexplained_weekday_gap", "WARN", cur,
                   detail + f"; missing weekdays {[d.isoformat() for d in weekdays[:5]]}")
        elif edge_missing:
            record("session_edge_missing", "WARN", cur, detail + "; bars missing near session open/close")
        else:
            record("session_closed_overnight", "INFO", cur, detail)
    return summary
