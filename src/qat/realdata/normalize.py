"""Canonical normalization (Batch #3A, Phase 4).

raw artifacts --(verify hashes)--> provider parser --> canonical rows --> canonical CSV

Allowed transformations (each is written to the transformation manifest with counts):
  * field rename / selection into market, symbol, date, open, high, low, close, volume
  * string -> number conversion (strip ``$`` and thousands ``,`` for Nasdaq text)
  * date parsing / derivation under the provider's documented rule
  * concatenation of pages / months in artifact-name order
  * window selection to the requested range (rows outside are COUNTED, never silently lost)
  * ordering: ONLY a strictly descending provider order is reversed; any other order is kept
    so that validation can see it
  * serialization (fixed header, fixed number formatting)

NOT done here: interpolation, fill, row deletion for quality, duplicate removal, OHLC or
zero-price correction, split/dividend adjustment. Bad values are carried into the CSV as
``nan`` / empty so that validation reports them.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field

from qat.realdata import sources
from qat.realdata.provenance import RawArtifact, sha256_bytes, verify_artifact

HEADER = "timestamp,open,high,low,close,volume"
FIELDS = ("open", "high", "low", "close", "volume")

FIELD_MAPS = {
    "binance": "kline columns [open_time, open, high, low, close, volume(base asset), ...] -> timestamp/open/high/low/close/volume",
    "yahoo": "chart.result[0].timestamp + indicators.quote[0].{open,high,low,close,volume} (adjclose kept as auxiliary evidence only)",
    "nasdaq": "tradesTable.rows[].{date,open,high,low,close,volume} (text values '$x', 'n,nnn')",
    "naver": "item data 'YYYYMMDD|open|high|low|close|volume'",
    "datagokr": "item.{basDt->date, mkp->open, hipr->high, lopr->low, clpr->close, trqu->volume}; vs/fltRt/trPrc/... kept as auxiliary evidence",
    "coinbase": "candle [time, low, high, open, close, volume] -> open/high/low/close/volume",
}

PARSERS = {
    "binance": lambda raw, **kw: sources.parse_binance_klines_zip(raw),
    "yahoo": lambda raw, **kw: sources.parse_yahoo_chart(raw, utc_offset_seconds=kw["utc_offset_seconds"]),
    "nasdaq": lambda raw, **kw: sources.parse_nasdaq_historical(raw),
    "naver": lambda raw, **kw: sources.parse_naver_fchart(raw),
    "datagokr": lambda raw, **kw: sources.parse_datagokr_stock_price(raw),
    "coinbase": lambda raw, **kw: sources.parse_coinbase_candles(raw),
}


@dataclass(frozen=True)
class DatasetSpec:
    key: str                      # short id, e.g. "crypto-binance"
    parser: str                   # key into PARSERS
    provider: str                 # source slug recorded as meta.source
    market: str                   # KR | US | CRYPTO
    symbol: str                   # canonical symbol (BTC/USDT, AAPL, 005930)
    provider_symbol: str          # as the provider names it (BTCUSDT, AAPL, 005930)
    calendar_code: str            # XKRX | XNYS | 24X7
    timezone_label: str           # DatasetMeta.timezone (date-only bars are labelled in it)
    exchange_timezone: str        # IANA name, informational
    window_start: str
    window_end: str
    raw_files: tuple              # artifact paths (sorted by name)
    parser_kwargs: dict = field(default_factory=dict)
    expected_reported_symbols: tuple = ()
    expected_utc_times: tuple = ()  # allowed HH:MM of provider UTC stamps (Yahoo), else ()
    declared_adjustment: dict | None = None   # OFFICIAL statement, if one was retrieved
    paging: str = "independent"   # "descending_list": artifacts are consecutive pages of ONE descending list
    requested_start: str | None = None  # when the provider cannot supply the requested start (coverage shortfall)


@dataclass
class NormalizedResult:
    spec: DatasetSpec
    rows: list                    # canonical rows: date + FIELDS (float | None | nan)
    aux: list                     # per-row auxiliary evidence (same order as rows)
    csv_bytes: bytes
    manifest: dict
    raw: list                     # [{"name", "sha256", "size_bytes", "retrieved_utc", "returned_range"}]
    raw_set_sha256: str
    reported: dict                # reported symbols / provider meta collected from the raw files

    @property
    def normalized_sha256(self) -> str:
        return sha256_bytes(self.csv_bytes)


_NUM = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$")


def to_number(token) -> tuple:
    """Return (value, how). value: float | None (missing) | float('nan') (invalid text).
    how: 'number' | 'converted' (text with $ or ,) | 'missing' | 'invalid'."""

    if token is None:
        return None, "missing"
    if isinstance(token, bool):
        return math.nan, "invalid"
    if isinstance(token, (int, float)):
        return float(token), "number"
    text = str(token).strip()
    if text == "":
        return None, "missing"
    cleaned = text.replace("$", "").replace(",", "")
    if cleaned.lower() in ("nan", "inf", "-inf", "+inf", "infinity", "-infinity"):
        return float(cleaned), "number"
    if _NUM.match(cleaned):
        return float(cleaned), "converted" if cleaned != text else "number"
    return math.nan, "invalid"


def fmt(value) -> str:
    if value is None:
        return ""
    if math.isnan(value):
        return "nan"
    if math.isinf(value):
        return "inf" if value > 0 else "-inf"
    if value == int(value) and abs(value) < 1e15:
        return str(int(value))
    return repr(float(value))


def raw_set_hash(raw: list) -> str:
    return hashlib.sha256("\n".join(f"{r['name']}:{r['sha256']}" for r in sorted(raw, key=lambda r: r["name"])).encode()).hexdigest()


def _canonical_json(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def normalize(spec: DatasetSpec) -> NormalizedResult:
    steps, raw_info = [], []
    # 1. verify raw hashes (raises ProvenanceError on any tamper)
    artifacts: list[RawArtifact] = [verify_artifact(p) for p in spec.raw_files]
    for art in artifacts:
        raw_info.append({"name": art.path.name, "sha256": art.sha256, "size_bytes": art.sidecar["size_bytes"],
                         "retrieved_utc": art.sidecar["retrieved_utc"], "returned_range": art.sidecar["returned_range"]})
    steps.append({"step": "verify_raw_hashes", "artifacts": len(artifacts), "result": "all SHA-256 and sidecars verified"})

    # 2. parse every artifact in name order and concatenate (provider order kept)
    parsed_rows, reported, meta_collect = [], {"symbols": [], "currency": [], "timestamp_units": []}, []
    reversed_artifacts = 0
    for art in sorted(artifacts, key=lambda a: a.path.name):
        if art.path.name.endswith(".CHECKSUM"):
            continue
        parsed = PARSERS[spec.parser](art.path.read_bytes(), **spec.parser_kwargs)
        # ordering is judged PER ARTIFACT: only a strictly descending response is reversed
        art_dates = [r["date"] for r in parsed.rows]
        if (spec.paging == "independent" and len(art_dates) > 1
                and all(a > b for a, b in zip(art_dates, art_dates[1:]))):
            parsed.rows.reverse()
            reversed_artifacts += 1
        parsed_rows.extend(parsed.rows)
        if parsed.meta.get("reported_symbol") is not None:
            reported["symbols"].append(parsed.meta["reported_symbol"])
        if parsed.meta.get("currency"):
            reported["currency"].append(parsed.meta["currency"])
        reported["timestamp_units"].extend(parsed.meta.get("timestamp_units", []))
        meta_collect.append({k: v for k, v in parsed.meta.items() if k in ("splits", "dividends", "exchange_timezone", "date_rule")})
    if spec.paging == "descending_list":
        # pages are consecutive slices of one descending list: reverse the whole concatenation, once,
        # and only if it is strictly descending (otherwise keep it so validation reports the disorder)
        all_dates = [r["date"] for r in parsed_rows]
        if len(all_dates) > 1 and all(a > b for a, b in zip(all_dates, all_dates[1:])):
            parsed_rows.reverse()
            reversed_artifacts = 1
    reported["provider_meta"] = meta_collect[0] if meta_collect else {}
    reported["symbols"] = sorted(set(reported["symbols"]))
    reported["currency"] = sorted(set(reported["currency"]))
    reported["timestamp_units"] = sorted(set(reported["timestamp_units"]))
    steps.append({"step": f"parse_{spec.parser}", "field_map": FIELD_MAPS[spec.parser], "rows_parsed": len(parsed_rows),
                  "date_rule": reported["provider_meta"].get("date_rule")})

    # 3. numeric conversion (carry bad values through as nan / missing)
    counts = {"number": 0, "converted": 0, "missing": 0, "invalid": 0}
    canon, aux = [], []
    for r in parsed_rows:
        row = {"date": r["date"]}
        for f in FIELDS:
            value, how = to_number(r.get(f))
            counts[how] += 1
            row[f] = value
        canon.append(row)
        aux.append(dict(r.get("extras") or {}))
    steps.append({"step": "numeric_conversion", "values": counts,
                  "note": "'converted' = '$'/',' stripped from text; 'missing' written as empty; 'invalid' written as nan - NOT repaired"})

    # 4. window selection
    start, end = spec.window_start, spec.window_end
    keep = [i for i, r in enumerate(canon) if start <= r["date"] <= end]
    dropped = len(canon) - len(keep)
    canon, aux = [canon[i] for i in keep], [aux[i] for i in keep]
    steps.append({"step": "window_selection", "window": [start, end], "rows_out": len(canon),
                  "rows_outside_window_not_carried": dropped,
                  "note": "selection of the requested range only; no quality-based removal"})

    # 5. ordering was applied per artifact in step 2 (strictly descending responses only)
    steps.append({"step": "ordering", "artifacts_reversed": reversed_artifacts,
                  "rule": "an artifact whose rows are strictly descending is reversed; every other order is kept so validation can see it"})

    # 6. serialize
    lines = [HEADER]
    for r in canon:
        lines.append(f"{r['date']}T00:00:00," + ",".join(fmt(r[f]) for f in FIELDS))
    csv_bytes = ("\n".join(lines) + "\n").encode("utf-8")
    steps.append({"step": "serialize_csv", "header": HEADER, "timestamp_format": "YYYY-MM-DDT00:00:00 (session date; zone declared in metadata)",
                  "number_format": "integral -> integer text, else shortest round-trip repr; missing '', non-finite nan/inf",
                  "line_endings": "LF", "rows": len(canon), "bytes": len(csv_bytes)})
    manifest = {"schema": 1, "dataset": spec.key, "provider": spec.provider, "market": spec.market, "symbol": spec.symbol,
                "provider_symbol": spec.provider_symbol, "steps": steps,
                "not_performed": ["interpolation", "forward/backward fill", "row deletion for quality", "duplicate removal",
                                  "missing-row insertion", "OHLC correction", "zero-price correction", "split/dividend adjustment"]}
    manifest["manifest_sha256"] = hashlib.sha256(_canonical_json(manifest)).hexdigest()
    return NormalizedResult(spec, canon, aux, csv_bytes, manifest, raw_info, raw_set_hash(raw_info), reported)


def parse_csv_rows(csv_bytes: bytes) -> tuple[list[str], list[dict]]:
    """Strict reader used by validation: returns (header cells, rows with raw tokens and parsed values)."""

    lines = csv_bytes.decode("utf-8").splitlines()
    if not lines:
        return [], []
    header = lines[0].split(",")
    rows = []
    for number, line in enumerate(lines[1:], start=1):
        cells = line.split(",")
        row = {"line": number, "cells": cells}
        if len(cells) == len(header):
            row["timestamp"] = cells[0]
            row["tokens"] = dict(zip(header[1:], cells[1:]))
        rows.append(row)
    return header, rows
