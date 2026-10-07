"""Batch #3D - BTCUSDT Binance Spot 4h verified-data foundation (minimal intraday extension).

Deliberately NOT a generic timeframe framework: one interval (4h), one venue, one symbol.
It reuses the existing provenance (immutable raw + sidecar, SHA-256) and checksum conventions.

Contract
  * raw: monthly kline zips + official ``.CHECKSUM`` files under ``data/raw/_intraday_4h/`` (a separate
    root so the daily dataset's raw-file discovery is untouched)
  * canonical ``timestamp,open,high,low,close,volume`` CSV: timestamp = UTC OPEN time of the bar,
    ``YYYY-MM-DDTHH:MM:SS`` (meta timezone UTC); engine-loadable with ``timeframe="4h"``
  * extra CSV (same row order): UTC open/close time, raw open/close time, timestamp unit (ms|us), quote
    volume, trade count, taker-buy volumes, source file
  * crypto is 24/7: no exchange calendar; the expected sequence is every 4h open from the first to the last bar
  * validation never repairs: a missing bar is a FAIL (pre-registered policy), nothing is interpolated

Timestamp units: Binance spot archives use epoch milliseconds (13 digits) and, from 2025-01-01, epoch
MICROseconds (16 digits). The unit is detected per row from the digit count, converted to UTC explicitly and
recorded per row; a unit that contradicts the expected one for the file's month is a WARN (it is parsed
correctly regardless), an unparseable value is a FAIL.

2026 HOLDOUT: data with an open time on/after ``HOLDOUT_START`` is never fetched, parsed or read until a
Protocol v2 freeze marker exists. Listing archive file names / sizes (metadata) is allowed; contents are not.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import pathlib
import re
import zipfile
from collections import Counter
from datetime import date, datetime, timedelta, timezone

from qat.realdata import http
from qat.realdata.normalize import fmt, to_number
from qat.realdata.provenance import RAW_ROOT, list_artifacts, store_raw, verify_artifact
from qat.realdata.sources import ParseError, parse_binance_checksum

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[3]
INTERVAL = "4h"
INTERVAL_MS = 4 * 3600 * 1000
SYMBOL = "BTCUSDT"
PROVIDER = "binance-vision"
BASE = "https://data.binance.vision/data/spot/monthly/klines"
LISTING = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision?delimiter=/&prefix="
DEV_FIRST = date(2018, 1, 1)
DEV_LAST = date(2025, 12, 31)
HOLDOUT_START = date(2026, 1, 1)
FREEZE_MARKER = PROJECT_ROOT / "artifacts" / "verification" / "protocol_v2" / "freeze.json"  # created only by the v2 freeze
APPROVAL_MARKER = PROJECT_ROOT / "artifacts" / "verification" / "protocol_v2" / "holdout_evaluation_approval.json"  # created only by a user-approved one-shot Batch
CLOSURE_MARKER = PROJECT_ROOT / "artifacts" / "verification" / "protocol_v2" / "protocol_v2_closure.json"  # Protocol v2 is CLOSED / NOT_HOLDOUT_ELIGIBLE
RAW_4H_ROOT = RAW_ROOT / "_intraday_4h"
PROCESSED_DIR = PROJECT_ROOT / "data" / "processed" / "real_4h"
NAME = "BTCUSDT_binance-vision_4h"
HEADER = "timestamp,open,high,low,close,volume"
EXTRA_HEADER = "open_time_utc,close_time_utc,open_time_raw,close_time_raw,timestamp_unit,quote_volume,trades,taker_buy_base,taker_buy_quote,source_file"
CLOSE_TOLERANCE_MS = 1  # unit-conversion rounding only


class HoldoutAccessError(RuntimeError):
    """Raised for any attempt to read 2026+ market data before the Protocol v2 freeze."""


class IntradayError(RuntimeError):
    pass


def holdout_unlocked() -> bool:
    """Reading 2026 values needs BOTH the Protocol v2 freeze marker (a technical permission only) AND a separate
    holdout-evaluation approval marker that only a user-approved one-shot Batch may create."""

    if CLOSURE_MARKER.exists():  # Protocol v2 is closed: no approval, however valid-looking, unlocks 2026 values for it
        return False
    return FREEZE_MARKER.exists() and APPROVAL_MARKER.exists()


def assert_not_holdout(year: int, month: int | None = None) -> None:
    first = date(year, month or 1, 1)
    if first >= HOLDOUT_START and not holdout_unlocked():
        raise HoldoutAccessError(f"{first:%Y-%m} is inside the untouched 2026 holdout: it needs the Protocol v2 freeze marker AND a holdout-evaluation approval marker")


# ------------------------------------------------------------------ parsing
def _open_dt(token: str) -> tuple[datetime, str, int]:
    text = token.strip()
    if not text.isdigit():
        raise ParseError(f"kline open_time is not an integer: {token!r}")
    if len(text) == 13:
        unit, ms = "ms", int(text)
    elif len(text) == 16:
        unit, ms = "us", int(text) // 1000
    else:
        raise ParseError(f"kline open_time has {len(text)} digits (expected 13 = ms or 16 = us)")
    return datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=ms), unit, ms


def _to_ms(token: str, unit: str) -> int | None:
    text = token.strip()
    if not text.isdigit():
        return None
    return int(text) // 1000 if unit == "us" else int(text)


def parse_klines(content: bytes, *, source_file: str) -> list[dict]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile as exc:
        raise ParseError("kline archive is not a zip file") from exc
    members = [n for n in archive.namelist() if n.lower().endswith(".csv")]
    if len(members) != 1:
        raise ParseError(f"kline zip must contain exactly one csv, found {len(members)}")
    rows = []
    for line in io.StringIO(archive.read(members[0]).decode("utf-8")):
        line = line.strip()
        if not line:
            continue
        cells = next(csv.reader([line]))
        if len(cells) < 9:
            raise ParseError(f"kline row has {len(cells)} columns (need >= 9)")
        dt, unit, open_ms = _open_dt(cells[0])
        close_ms = _to_ms(cells[6], unit)
        rows.append({"open_dt": dt, "open_ms": open_ms, "unit": unit, "open_raw": cells[0], "close_raw": cells[6], "close_ms": close_ms,
                     "open": cells[1], "high": cells[2], "low": cells[3], "close": cells[4], "volume": cells[5], "quote_volume": cells[7],
                     "trades": cells[8], "taker_buy_base": cells[9] if len(cells) > 9 else "", "taker_buy_quote": cells[10] if len(cells) > 10 else "",
                     "source_file": source_file, "member": members[0]})
    return rows


# ------------------------------------------------------------------ acquisition
def _get_ok(url: str, what: str) -> bytes:
    result = http.get(url)
    if result.status != 200:
        raise IntradayError(f"{what}: HTTP {result.status} {result.error or ''}".strip())
    return result.body


def fetch_month(year: int, month: int, *, symbol: str = SYMBOL, root: pathlib.Path | None = None):
    """Monthly 4h kline archive + official CHECKSUM, preserved immutably. The archive hash must equal the published one."""

    assert_not_holdout(year, month)
    name = f"{symbol}-{INTERVAL}-{year:04d}-{month:02d}.zip"
    url = f"{BASE}/{symbol}/{INTERVAL}/{name}"
    zip_bytes, checksum_bytes = _get_ok(url, name), _get_ok(url + ".CHECKSUM", name + ".CHECKSUM")
    official = parse_binance_checksum(checksum_bytes)
    if official["sha256"] != hashlib.sha256(zip_bytes).hexdigest() or official["name"] != name:
        raise IntradayError(f"{name}: SHA-256 differs from the official CHECKSUM file")
    rows = parse_klines(zip_bytes, source_file=name)
    if any(r["open_dt"].date() >= HOLDOUT_START and not holdout_unlocked() for r in rows):
        raise HoldoutAccessError(f"{name}: contains bars on/after {HOLDOUT_START}")
    last_day = (date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)).isoformat()
    units = sorted({r["unit"] for r in rows})
    tz = {"exchange_timezone": "UTC", "timestamp_semantics": "kline open time, UTC; unit detected per row (ms 13 digits / us 16 digits)"}
    rng = {"start": f"{year:04d}-{month:02d}-01", "end": last_day}
    art = store_raw(zip_bytes, name=name, provider=PROVIDER, service="spot/monthly/klines", market="CRYPTO", symbol=symbol, requested_range=rng,
                    returned_range={"start": rows[0]["open_dt"].isoformat() if rows else None, "end": rows[-1]["open_dt"].isoformat() if rows else None},
                    fmt="zip(csv)", endpoint=url, timezone_info=tz, root=root or RAW_4H_ROOT,
                    extra={"interval": INTERVAL, "official_checksum": official["sha256"], "official_checksum_match": True, "timestamp_units": units,
                           "rows": len(rows), "http_status": 200})
    chk = store_raw(checksum_bytes, name=name + ".CHECKSUM", provider=PROVIDER, service="spot/monthly/klines CHECKSUM", market="CRYPTO", symbol=symbol,
                    requested_range=rng, returned_range={"start": None, "end": None}, fmt="text", endpoint=url + ".CHECKSUM", timezone_info=tz,
                    root=root or RAW_4H_ROOT, extra={"interval": INTERVAL, "covers_artifact": name, "http_status": 200})
    return art, chk


def months(first: date = DEV_FIRST, last: date = DEV_LAST):
    y, m = first.year, first.month
    while (y, m) <= (last.year, last.month):
        yield y, m
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def acquire(log=print, *, first: date = DEV_FIRST, last: date = DEV_LAST, root: pathlib.Path | None = None) -> dict:
    ok = failed = 0
    for y, m in months(first, last):
        try:
            fetch_month(y, m, root=root)
            ok += 1
        except (IntradayError, ParseError) as exc:
            failed += 1
            log(f"4h {y}-{m:02d} FAILED: {exc}")
    return {"stored": ok, "failed": failed}


def archive_metadata(year_from: int = 2026) -> dict:
    """File names / sizes / modification times of the archive (metadata ONLY, no content)."""

    xml = _get_ok(f"{LISTING}data/spot/monthly/klines/{SYMBOL}/{INTERVAL}/", "listing").decode("utf-8")
    entries = []
    for key, modified, size in re.findall(r"<Key>([^<]+\.zip)</Key><LastModified>([^<]+)</LastModified>.*?<Size>(\d+)</Size>", xml, re.S):
        entries.append({"key": key, "last_modified": modified, "size_bytes": int(size)})
    return {"count": len(entries), "entries": entries}


# ------------------------------------------------------------------ normalization
def raw_artifacts(root: pathlib.Path | None = None) -> list[pathlib.Path]:
    prefix = f"{SYMBOL}-{INTERVAL}-"  # the same directory also holds the aggTrades/trades samples
    return [p for p in list_artifacts(PROVIDER, "CRYPTO", SYMBOL, root or RAW_4H_ROOT) if p.name.endswith(".zip") and p.name.startswith(prefix)]


def checksum_artifact(zip_path: pathlib.Path) -> pathlib.Path:
    return zip_path.with_name(zip_path.name + ".CHECKSUM")


def canonical_lines(rows: list[dict]) -> tuple[list[str], list[str]]:
    """Header + canonical CSV lines and header + extra CSV lines (same row order)."""

    canon, extra = [HEADER], [EXTRA_HEADER]
    for r in rows:
        ts = r["open_dt"].strftime("%Y-%m-%dT%H:%M:%S")
        values = [fmt(to_number(r[k])[0]) for k in ("open", "high", "low", "close", "volume")]
        canon.append(",".join([ts, *values]))
        close_dt = (datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=r["close_ms"])).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] if r["close_ms"] is not None else ""
        extra.append(",".join([r["open_dt"].strftime("%Y-%m-%dT%H:%M:%S"), close_dt, r["open_raw"], r["close_raw"], r["unit"],
                               fmt(to_number(r["quote_volume"])[0]), fmt(to_number(r["trades"])[0]), fmt(to_number(r["taker_buy_base"])[0]),
                               fmt(to_number(r["taker_buy_quote"])[0]), r["source_file"]]))
    return canon, extra


def normalize(root: pathlib.Path | None = None) -> dict:
    """Deterministic: verifies every raw artifact first, then parses/normalizes in file-name order."""

    paths = sorted(raw_artifacts(root), key=lambda p: p.name)
    if not paths:
        raise IntradayError("no raw 4h artifacts")
    raw_meta, rows = [], []
    for path in paths:
        art = verify_artifact(path)
        raw_meta.append({"name": path.name, "sha256": art.sidecar["sha256"], "size_bytes": art.sidecar["size_bytes"], "sidecar": art.sidecar})
        rows.extend(parse_klines(path.read_bytes(), source_file=path.name))
    canon, extra = canonical_lines(rows)
    csv_bytes, extra_bytes = ("\n".join(canon) + "\n").encode("utf-8"), ("\n".join(extra) + "\n").encode("utf-8")
    raw_set = hashlib.sha256("\n".join(f"{m['name']}:{m['sha256']}" for m in raw_meta).encode("utf-8")).hexdigest()
    steps = {"performed": ["verified each raw artifact against its sidecar SHA-256", "parsed kline CSV rows in archive/file-name order",
                           "converted open time to UTC (ms or us detected per row) and formatted YYYY-MM-DDTHH:MM:SS",
                           "formatted numbers deterministically (qat.realdata.normalize.fmt)"],
             "not_performed": ["interpolation", "gap filling", "row deletion", "deduplication", "OHLC repair", "resampling", "price adjustment"]}
    return {"rows": rows, "raw": raw_meta, "csv_bytes": csv_bytes, "extra_bytes": extra_bytes, "raw_set_sha256": raw_set,
            "normalized_sha256": hashlib.sha256(csv_bytes).hexdigest(), "extra_sha256": hashlib.sha256(extra_bytes).hexdigest(), "manifest": steps}


# ------------------------------------------------------------------ validation
def _check(cid, name, status, summary, count=0, examples=None, **extra):
    return {"id": cid, "name": name, "status": status, "summary": summary, "count": count, "examples": (examples or [])[:5], **extra}


def _expected_unit(dt: datetime) -> str:
    return "us" if dt.date() >= date(2025, 1, 1) else "ms"


def validate(rows: list[dict], raw_meta: list[dict], *, first_expected: datetime | None = None, last_expected: datetime | None = None,
             holdout_phase: bool = False) -> dict:
    """Never repairs. Missing bars FAIL (pre-registered)."""

    checks = []
    bad_num, bad_price, bad_ohlc, bad_vol, zero_vol = [], [], [], [], 0
    for r in rows:
        o, h, lo, c, v = (to_number(r[k])[0] for k in ("open", "high", "low", "close", "volume"))
        q, n = to_number(r["quote_volume"])[0], to_number(r["trades"])[0]
        vals = [o, h, lo, c, v, q, n]
        if any(x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))) for x in vals):
            bad_num.append(r["open_dt"].isoformat())
            continue
        if min(o, h, lo, c) <= 0:
            bad_price.append(r["open_dt"].isoformat())
        if not (lo <= o <= h and lo <= c <= h):
            bad_ohlc.append(r["open_dt"].isoformat())
        if v < 0 or q < 0 or n < 0:
            bad_vol.append(r["open_dt"].isoformat())
        zero_vol += v == 0
    checks.append(_check("I01", "numeric_finite", "FAIL" if bad_num else "PASS", f"{len(bad_num)} rows with NaN/inf/missing/invalid numbers", len(bad_num), bad_num))
    checks.append(_check("I02", "positive_prices", "FAIL" if bad_price else "PASS", f"{len(bad_price)} rows with price <= 0", len(bad_price), bad_price))
    checks.append(_check("I03", "ohlc_invariants", "FAIL" if bad_ohlc else "PASS", f"{len(bad_ohlc)} rows violate low<=open/close<=high", len(bad_ohlc), bad_ohlc))
    checks.append(_check("I04", "negative_volume", "FAIL" if bad_vol else ("WARN" if zero_vol else "PASS"),
                         f"{len(bad_vol)} negative; {zero_vol} zero-volume bars", len(bad_vol), bad_vol))
    opens = [r["open_ms"] for r in rows]
    dups = sorted(x for x, n in Counter(opens).items() if n > 1)
    checks.append(_check("I05", "duplicate_timestamps", "FAIL" if dups else "PASS", f"{len(dups)} duplicate open times", len(dups), [str(x) for x in dups]))
    unordered = [rows[i]["open_dt"].isoformat() for i in range(1, len(rows)) if opens[i] < opens[i - 1]]
    checks.append(_check("I06", "timestamp_order", "FAIL" if unordered else "PASS", f"{len(unordered)} out-of-order bars", len(unordered), unordered))
    overlap = [rows[i]["open_dt"].isoformat() for i in range(1, len(rows)) if 0 < opens[i] - opens[i - 1] < INTERVAL_MS]
    checks.append(_check("I07", "overlap", "FAIL" if overlap else "PASS", f"{len(overlap)} bars start less than 4h after the previous bar", len(overlap), overlap))
    misaligned = [r["open_dt"].isoformat() for r in rows if r["open_ms"] % INTERVAL_MS != 0]
    checks.append(_check("I08", "alignment", "FAIL" if misaligned else "PASS", f"{len(misaligned)} open times not on a 4h UTC boundary", len(misaligned), misaligned))
    wrong = [r["open_dt"].isoformat() for r in rows if r["close_ms"] is None or abs(r["close_ms"] - (r["open_ms"] + INTERVAL_MS - 1)) > CLOSE_TOLERANCE_MS]
    checks.append(_check("I09", "interval", "FAIL" if wrong else "PASS", f"{len(wrong)} bars whose close-open is not 4h-1ms (+-{CLOSE_TOLERANCE_MS}ms)", len(wrong), wrong))
    contradicting = sorted({(r["source_file"], r["unit"]) for r in rows if r["unit"] != _expected_unit(r["open_dt"])})
    units = sorted({r["unit"] for r in rows})
    checks.append(_check("I10", "timestamp_unit", "WARN" if contradicting else "PASS",
                         f"units seen {units}; {len(contradicting)} files contradict the expected unit (ms before 2025-01-01, us from it); each row is converted by its own unit",
                         len(contradicting), [f"{a}:{b}" for a, b in contradicting], units=units))
    # continuity: every expected 4h open between the first and last bar must exist
    present = set(opens)
    first_ms = int(first_expected.timestamp() * 1000) if first_expected else (min(opens) if opens else 0)
    last_ms = int(last_expected.timestamp() * 1000) if last_expected else (max(opens) if opens else 0)
    missing = [ms for ms in range(first_ms - first_ms % INTERVAL_MS, last_ms + 1, INTERVAL_MS) if ms not in present] if opens else []
    iso = lambda ms: (datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=ms)).strftime("%Y-%m-%dT%H:%M")  # noqa: E731
    checks.append(_check("I11", "continuity_24x7", "FAIL" if missing else "PASS", f"{len(missing)} expected 4h bars missing (no calendar; 24/7)", len(missing),
                         [iso(x) for x in missing], missing_bars=[iso(x) for x in missing]))
    checks.append(_check("I12", "coverage", "FAIL" if (first_expected and opens and min(opens) > first_ms) or (last_expected and opens and max(opens) < last_ms) else "PASS",
                         f"first {iso(min(opens)) if opens else None}, last {iso(max(opens)) if opens else None}"))
    wrong_symbol = [m["name"] for m in raw_meta if m["sidecar"].get("symbol") != SYMBOL or not m["name"].startswith(f"{SYMBOL}-{INTERVAL}-")]
    wrong_member = sorted({r["source_file"] for r in rows if not r["member"].startswith(f"{SYMBOL}-{INTERVAL}-")})
    checks.append(_check("I13", "symbol_consistency", "FAIL" if (wrong_symbol or wrong_member) else "PASS",
                         f"{len(wrong_symbol)} artifacts / {len(wrong_member)} archive members name a different symbol or interval", len(wrong_symbol) + len(wrong_member), wrong_symbol + wrong_member))
    contradict = [m["name"] for m in raw_meta if m["sidecar"].get("provider") != PROVIDER or m["sidecar"].get("extra", m["sidecar"]).get("interval", INTERVAL) != INTERVAL]
    checks.append(_check("I14", "source_metadata", "FAIL" if contradict else "PASS", f"{len(contradict)} artifacts contradict provider/interval", len(contradict), contradict))
    synth = [m["name"] for m in raw_meta if m["sidecar"].get("synthetic") is not False]
    checks.append(_check("I15", "synthetic_false", "FAIL" if synth else "PASS", f"{len(synth)} artifacts not declared synthetic=false", len(synth), synth))
    unmatched = [m["name"] for m in raw_meta if m["sidecar"].get("official_checksum") != m["sha256"] or m["sidecar"].get("official_checksum_match") is not True]
    checks.append(_check("I16", "official_checksum", "FAIL" if unmatched else "PASS", f"{len(unmatched)} archives whose SHA-256 differs from the published checksum", len(unmatched), unmatched))
    late = [] if holdout_phase else [r["open_dt"].isoformat() for r in rows if r["open_dt"].date() >= HOLDOUT_START]  # holdout_phase: only inside the approved one-shot evaluation
    checks.append(_check("I17", "holdout_boundary", "FAIL" if late else "PASS", f"{len(late)} bars on/after {HOLDOUT_START} (untouched holdout)", len(late), late))
    failed = [c["id"] for c in checks if c["status"] == "FAIL"]
    warned = [c["id"] for c in checks if c["status"] == "WARN"]
    return {"status": "FAIL" if failed else ("WARN" if warned else "PASS"), "rows": len(rows), "failed_checks": failed, "warnings": warned, "checks": checks}


def verify_checksums(root: pathlib.Path | None = None) -> dict:
    """Independent re-check: each zip's bytes vs the content of its preserved official CHECKSUM artifact."""

    bad = []
    for path in raw_artifacts(root):
        official = parse_binance_checksum(checksum_artifact(path).read_bytes())
        if official["sha256"] != hashlib.sha256(path.read_bytes()).hexdigest() or official["name"] != path.name:
            bad.append(path.name)
    return {"archives": len(raw_artifacts(root)), "mismatches": bad}


# ------------------------------------------------------------------ identity + persistence
def build(root: pathlib.Path | None = None) -> dict:
    norm = normalize(root)
    first, last = datetime(DEV_FIRST.year, DEV_FIRST.month, DEV_FIRST.day, tzinfo=timezone.utc), datetime(DEV_LAST.year, DEV_LAST.month, DEV_LAST.day, 20, tzinfo=timezone.utc)
    report = validate(norm["rows"], norm["raw"], first_expected=first, last_expected=last)
    manifest_sha = hashlib.sha256(json.dumps(norm["manifest"], sort_keys=True).encode("utf-8")).hexdigest()
    ident = {"schema": 1, "provider": PROVIDER, "market": "CRYPTO", "symbol": "BTC/USDT", "provider_symbol": SYMBOL, "interval": INTERVAL,
             "timezone": "UTC", "timestamp_semantics": "open time UTC; ms/us detected per row", "calendar": "24x7 (no exchange calendar); continuity checked",
             "development_window": [DEV_FIRST.isoformat(), DEV_LAST.isoformat()], "holdout_start": HOLDOUT_START.isoformat(),
             "rows": report["rows"], "first_open": norm["rows"][0]["open_dt"].isoformat(), "last_open": norm["rows"][-1]["open_dt"].isoformat(),
             "raw": [{"name": m["name"], "sha256": m["sha256"], "size_bytes": m["size_bytes"]} for m in norm["raw"]], "raw_set_sha256": norm["raw_set_sha256"],
             "normalized_sha256": norm["normalized_sha256"], "extra_sha256": norm["extra_sha256"], "transformation_manifest_sha256": manifest_sha,
             "validation_status": report["status"], "validation_failed_checks": report["failed_checks"], "validation_warnings": report["warnings"],
             "synthetic": False, "adjustment_semantics": "UNADJUSTED (spot trading pair: no corporate-action mechanism, structural)",
             "performance_data_accessed": False}
    ident["data_version"] = f"CRYPTO-BTCUSDT-4h-binance-vision-{norm['normalized_sha256'][:12]}-{norm['raw_set_sha256'][:6]}"
    return {"norm": norm, "report": report, "identity": ident}


def identity_bytes(identity: dict) -> bytes:
    return (json.dumps(identity, indent=1, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def persist(built: dict, directory: pathlib.Path | None = None) -> dict:
    directory = directory or PROCESSED_DIR
    directory.mkdir(parents=True, exist_ok=True)
    norm, ident = built["norm"], built["identity"]
    csv_path, extra_path = directory / f"{NAME}.csv", directory / f"{NAME}.klines_extra.csv"
    csv_path.write_bytes(norm["csv_bytes"])
    extra_path.write_bytes(norm["extra_bytes"])
    (directory / f"{NAME}.identity.json").write_bytes(identity_bytes(ident))
    meta = {"market": "CRYPTO", "symbol": "BTC/USDT", "timeframe": INTERVAL, "timezone": "UTC", "timestamp_label": "open", "source": PROVIDER,
            "synthetic": False, "description": "Binance Spot BTCUSDT 4h klines (Batch #3D foundation); NOT admitted for research until Protocol v2 is frozen",
            "extra": {"intraday_identity_file": f"{NAME}.identity.json", "data_version": ident["data_version"]}}
    (directory / f"{NAME}.csv.meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return {"csv": csv_path, "extra": extra_path, "identity": directory / f"{NAME}.identity.json"}


# ------------------------------------------------------------------ continuous research view (Batch #3E)
VIEW_NAME = f"{NAME}_continuous"
VIEW_RULE_ID = "trailing-continuous-segment-v1"
PARENT_STATUS_FAIL = "VALIDATION_FAIL_INCOMPLETE"
VIEW_STATUS_ADMITTED = "ADMITTED_CONTINUOUS_SUBPERIOD"
VIEW_RULE_TEXT = ("Scan backward from the last bar on/before 2025-12-31. A bar belongs to the segment if the open-time spacing to the next kept bar is exactly 4h "
                  "and the bar has the full 4h duration (close = open + 4h - 1ms, +-1ms). The first discontinuity (gap, duplicate, overlap, unordered or shortened bar) "
                  "ends the scan; continuous_start is the first regular bar after it. Only timestamps, bar durations and integrity checks are used - never prices, returns, "
                  "trade counts or strategy output.")


def is_regular_bar(row: dict) -> bool:
    return row["close_ms"] is not None and abs(row["close_ms"] - (row["open_ms"] + INTERVAL_MS - 1)) <= CLOSE_TOLERANCE_MS


def trailing_continuous_segment(rows: list[dict]) -> dict:
    """Maximal trailing continuous segment as an index range [start, end] (inclusive). Deterministic, timestamp-only."""

    if not rows:
        raise IntradayError("no rows")
    end = len(rows) - 1
    if not is_regular_bar(rows[end]):
        raise IntradayError("the last bar is not a regular full-duration 4h bar")
    start = end
    while start > 0:
        prev, cur = rows[start - 1], rows[start]
        if cur["open_ms"] - prev["open_ms"] != INTERVAL_MS or not is_regular_bar(prev):
            break
        start -= 1
    return {"start_index": start, "end_index": end}


def admission_status(kind: str, validation_status: str, *, derived_by_rule: bool = False) -> str:
    """Two different states: the parent artifact is judged on its own strict validation; a continuous view is admitted only if
    it was derived by the deterministic rule AND passes every check. A FAIL parent is never turned into a PASS."""

    if kind == "PARENT":
        return "ADMITTED_FULL_PERIOD" if validation_status == "PASS" else PARENT_STATUS_FAIL
    if kind == "CONTINUOUS_VIEW":
        return VIEW_STATUS_ADMITTED if (validation_status == "PASS" and derived_by_rule) else "NOT_ADMITTED"
    raise ValueError(kind)


def _episodes_of(missing: list[str]) -> list[dict]:
    out, step = [], timedelta(hours=4)
    for ts in missing:
        t = datetime.strptime(ts, "%Y-%m-%dT%H:%M")
        if out and t - out[-1]["_last"] == step:
            out[-1]["bars"] += 1
            out[-1]["_last"] = t
        else:
            out.append({"first_missing": ts, "bars": 1, "_last": t})
    return [{k: v for k, v in e.items() if k != "_last"} for e in out]


def derive_view(built: dict) -> dict:
    """Continuous research view of the parent build. Rows are copied verbatim (byte-identical CSV lines); nothing is modified or synthesized."""

    norm, ident = built["norm"], built["identity"]
    rows = norm["rows"]
    seg = trailing_continuous_segment(rows)
    a, b = seg["start_index"], seg["end_index"]
    seg_rows = rows[a:b + 1]
    csv_lines, extra_lines = norm["csv_bytes"].decode("utf-8").split("\n"), norm["extra_bytes"].decode("utf-8").split("\n")
    view_csv = "\n".join([csv_lines[0], *csv_lines[1 + a:2 + b]]) + "\n"
    view_extra = "\n".join([extra_lines[0], *extra_lines[1 + a:2 + b]]) + "\n"
    names = {r["source_file"] for r in seg_rows}
    raw_subset = [m for m in norm["raw"] if m["name"] in names]
    first = seg_rows[0]["open_dt"]
    last = seg_rows[-1]["open_dt"]
    report = validate(seg_rows, raw_subset, first_expected=first, last_expected=last)
    parent_checks = {c["id"]: c for c in built["report"]["checks"]}
    short = [{"open": r["open_dt"].strftime("%Y-%m-%dT%H:%M"), "close_minus_open_ms": (r["close_ms"] - r["open_ms"]) if r["close_ms"] is not None else None}
             for r in rows if not is_regular_bar(r)]
    modified = sum(1 for x, y in zip(csv_lines[1 + a:2 + b], view_csv.split("\n")[1:]) if x != y)
    status = admission_status("CONTINUOUS_VIEW", report["status"], derived_by_rule=True)
    view_sha = hashlib.sha256(view_csv.encode("utf-8")).hexdigest()
    ident_v = {
        "schema": 1, "kind": "CONTINUOUS_VIEW", "view_status": status, "rule_id": VIEW_RULE_ID,
        "parent": {"data_version": ident["data_version"], "normalized_sha256": ident["normalized_sha256"], "raw_set_sha256": ident["raw_set_sha256"],
                   "rows": ident["rows"], "strict_validation_status": ident["validation_status"], "failed_checks": ident["validation_failed_checks"],
                   "admission_status": admission_status("PARENT", ident["validation_status"])},
        "derivation": {"rule": VIEW_RULE_TEXT, "direction": "backward from the last bar", "inputs": "timestamps, bar durations, integrity checks only",
                       "performance_data_used": False},
        "development_end": DEV_LAST.isoformat(), "continuous_start": first.strftime("%Y-%m-%dT%H:%M:%S"), "continuous_end": last.strftime("%Y-%m-%dT%H:%M:%S"),
        "rows": len(seg_rows), "expected_rows_in_range": int((last - first).total_seconds() // (4 * 3600)) + 1,
        "excluded_irregular_intervals": {"missing_episodes": _episodes_of(parent_checks["I11"]["missing_bars"]), "irregular_or_shortened_bars": short,
                                         "all_irregular_intervals_precede_continuous_start": all(x["open"] < first.strftime("%Y-%m-%dT%H:%M") for x in short)
                                                                                         and all(m < first.strftime("%Y-%m-%dT%H:%M") for m in parent_checks["I11"]["missing_bars"]),
                                         "regular_parent_bars_excluded_only_because_they_precede_the_start": a - len(short)},
        "rows_modified": modified, "rows_synthesized": 0, "view_is_byte_identical_slice_of_parent": modified == 0,
        "view_normalized_sha256": view_sha, "view_extra_sha256": hashlib.sha256(view_extra.encode("utf-8")).hexdigest(),
        "validation": {"status": report["status"], "failed_checks": report["failed_checks"], "warnings": report["warnings"], "rows": report["rows"]},
        "synthetic": False, "performance_data_accessed": False}
    ident_v["view_data_version"] = f"CRYPTO-BTCUSDT-4h-binance-vision-cont-{view_sha[:12]}-{ident['normalized_sha256'][:6]}"
    return {"identity": ident_v, "csv": view_csv, "extra": view_extra, "report": report, "segment": seg}


def persist_view(view: dict, directory: pathlib.Path | None = None) -> dict:
    directory = directory or PROCESSED_DIR
    directory.mkdir(parents=True, exist_ok=True)
    csv_path, extra_path = directory / f"{VIEW_NAME}.csv", directory / f"{VIEW_NAME}.klines_extra.csv"
    csv_path.write_bytes(view["csv"].encode("utf-8"))
    extra_path.write_bytes(view["extra"].encode("utf-8"))
    ident_path = directory / f"{VIEW_NAME}.view.json"
    ident_path.write_bytes(identity_bytes(view["identity"]))
    meta = {"market": "CRYPTO", "symbol": "BTC/USDT", "timeframe": INTERVAL, "timezone": "UTC", "timestamp_label": "open", "source": PROVIDER, "synthetic": False,
            "description": "Continuous research VIEW of the BTCUSDT 4h parent (derived by a deterministic timestamp-only rule); the parent remains VALIDATION_FAIL_INCOMPLETE",
            "extra": {"intraday_view_identity_file": ident_path.name, "view_data_version": view["identity"]["view_data_version"]}}
    (directory / f"{VIEW_NAME}.csv.meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return {"csv": csv_path, "extra": extra_path, "identity": ident_path}


def main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="python -m qat.realdata.intraday")
    parser.add_argument("cmd", choices=["acquire", "build", "metadata", "view"])
    args = parser.parse_args(argv)
    if args.cmd == "acquire":
        print(json.dumps(acquire(print)))
    elif args.cmd == "view":
        view = derive_view(build())
        persist_view(view)
        v = view["identity"]
        print(json.dumps({"status": v["view_status"], "start": v["continuous_start"], "end": v["continuous_end"], "rows": v["rows"], "modified": v["rows_modified"],
                          "view_data_version": v["view_data_version"]}, indent=1))
    elif args.cmd == "metadata":
        meta = archive_metadata()
        print(json.dumps({"count": meta["count"], "first": meta["entries"][0]["key"], "last": meta["entries"][-1]["key"]}))
    else:
        built = build()
        paths = persist(built)
        print(json.dumps({"status": built["report"]["status"], "rows": built["report"]["rows"], "failed": built["report"]["failed_checks"],
                          "warnings": built["report"]["warnings"], "data_version": built["identity"]["data_version"], "files": [str(v) for v in paths.values()]}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
