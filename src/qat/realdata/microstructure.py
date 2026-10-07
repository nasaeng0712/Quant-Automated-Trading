"""Batch #3D - microstructure SOURCE assessment (evidence about what the data can and cannot show).

* ``sample_day``: preserves one daily ``aggTrades`` and ``trades`` archive (with the official CHECKSUM) and
  derives descriptive statistics: trade counts, size distribution, intrabar turnover, consecutive-trade price
  variation. These are NOT bid-ask spreads: a trade print is an execution, ``is_buyer_maker`` only says which side
  was the resting order, and no quote (bid/ask) is observed. Nothing here is used as a cost parameter.
* ``endpoint_schema``: availability and FIELD NAMES of the public spot ``bookTicker`` / ``depth`` endpoints. Values
  are discarded on purpose (they would be live 2026 prices); no snapshot is stored and no collector is started.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import statistics
import zipfile
from collections import defaultdict
from datetime import date, datetime, timezone

from qat.realdata import http
from qat.realdata.intraday import HOLDOUT_START, RAW_4H_ROOT, IntradayError, _get_ok, assert_not_holdout
from qat.realdata.provenance import store_raw
from qat.realdata.sources import parse_binance_checksum

SAMPLE_DAY = date(2023, 6, 11)  # fixed in advance (small file, inside the development window, before 2025)
BASE = "https://data.binance.vision/data/spot/daily"
COLUMNS = {"aggTrades": ["agg_trade_id", "price", "quantity", "first_trade_id", "last_trade_id", "transact_time", "is_buyer_maker", "is_best_match"],
           "trades": ["id", "price", "qty", "quote_qty", "time", "is_buyer_maker", "is_best_match"]}
ENDPOINTS = ("https://api.binance.com/api/v3/ticker/bookTicker?symbol=BTCUSDT", "https://data-api.binance.vision/api/v3/ticker/bookTicker?symbol=BTCUSDT",
             "https://api.binance.com/api/v3/depth?symbol=BTCUSDT&limit=5", "https://data-api.binance.vision/api/v3/depth?symbol=BTCUSDT&limit=5")


def fetch_day(kind: str, day: date = SAMPLE_DAY, *, root=None):
    if kind not in COLUMNS:
        raise ValueError(kind)
    if day >= HOLDOUT_START:
        assert_not_holdout(day.year, day.month)
    name = f"BTCUSDT-{kind}-{day.isoformat()}.zip"
    url = f"{BASE}/{kind}/BTCUSDT/{name}"
    body, checksum = _get_ok(url, name), _get_ok(url + ".CHECKSUM", name + ".CHECKSUM")
    official = parse_binance_checksum(checksum)
    if official["sha256"] != hashlib.sha256(body).hexdigest() or official["name"] != name:
        raise IntradayError(f"{name}: SHA-256 differs from the official CHECKSUM file")
    tz = {"exchange_timezone": "UTC", "timestamp_semantics": "trade time, epoch ms (this day is before 2025)"}
    rng = {"start": day.isoformat(), "end": day.isoformat()}
    art = store_raw(body, name=name, provider="binance-vision", service=f"spot/daily/{kind}", market="CRYPTO", symbol="BTCUSDT", requested_range=rng,
                    returned_range=rng, fmt="zip(csv)", endpoint=url, timezone_info=tz, root=root or RAW_4H_ROOT,
                    extra={"official_checksum": official["sha256"], "official_checksum_match": True, "purpose": "microstructure source assessment sample", "http_status": 200})
    store_raw(checksum, name=name + ".CHECKSUM", provider="binance-vision", service=f"spot/daily/{kind} CHECKSUM", market="CRYPTO", symbol="BTCUSDT",
              requested_range=rng, returned_range={"start": None, "end": None}, fmt="text", endpoint=url + ".CHECKSUM", timezone_info=tz,
              root=root or RAW_4H_ROOT, extra={"covers_artifact": name, "http_status": 200})
    return art


def _rows(path, kind):
    with zipfile.ZipFile(path) as zf:
        member = [n for n in zf.namelist() if n.endswith(".csv")][0]
        for row in csv.reader(io.TextIOWrapper(zf.open(member), encoding="utf-8")):
            if row:
                yield row


def _pct(values, q):
    values = sorted(values)
    return values[min(len(values) - 1, int(q / 100 * len(values)))]


def describe(path, kind: str) -> dict:
    """Descriptive statistics of one daily trade file. No spread, no cost, no return."""

    price_i, qty_i, time_i, maker_i = (1, 2, 5, 6) if kind == "aggTrades" else (1, 2, 4, 5)
    n = 0
    prices, qtys, notionals, makers, bar_count, bar_notional, bar_qty = [], [], [], 0, defaultdict(int), defaultdict(float), defaultdict(float)
    first_ts = last_ts = None
    for row in _rows(path, kind):
        p, q, t = float(row[price_i]), float(row[qty_i]), int(row[time_i])
        if t > 10 ** 14:
            t //= 1000  # microsecond files (2025+); this sample is milliseconds
        n += 1
        prices.append(p)
        qtys.append(q)
        notionals.append(p * q)
        makers += row[maker_i].strip().lower() == "true"
        bar = datetime.fromtimestamp(t / 1000, tz=timezone.utc)
        key = bar.replace(hour=bar.hour - bar.hour % 4, minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M")
        bar_count[key] += 1
        bar_notional[key] += p * q
        bar_qty[key] += q
        first_ts, last_ts = (t if first_ts is None else first_ts), t
    moves = [abs(b / a - 1) * 1e4 for a, b in zip(prices, prices[1:]) if a > 0]
    unchanged = sum(1 for a, b in zip(prices, prices[1:]) if a == b)
    return {"kind": kind, "rows": n, "first_time_utc": datetime.fromtimestamp(first_ts / 1000, tz=timezone.utc).isoformat(),
            "last_time_utc": datetime.fromtimestamp(last_ts / 1000, tz=timezone.utc).isoformat(),
            "buyer_is_maker_share": makers / n, "quantity_btc": {"median": statistics.median(qtys), "p90": _pct(qtys, 90), "p99": _pct(qtys, 99), "max": max(qtys)},
            "notional_usdt": {"median": statistics.median(notionals), "p90": _pct(notionals, 90), "p99": _pct(notionals, 99), "max": max(notionals)},
            "per_4h_bar": {k: {"trades": bar_count[k], "notional_usdt": bar_notional[k], "quantity_btc": bar_qty[k]} for k in sorted(bar_count)},
            "consecutive_price_change_bps": {"share_unchanged": unchanged / max(1, len(prices) - 1), "median": statistics.median(moves) if moves else None,
                                             "p90": _pct(moves, 90) if moves else None, "p99": _pct(moves, 99) if moves else None},
            "interpretation_limits": ["descriptive statistics of executed trades only", "NOT a bid-ask spread: no quote is observed", "is_buyer_maker is a trade attribute, not a bid/ask",
                                      "no market-impact or slippage estimate is derived"]}


def reconcile_with_klines(agg: dict, klines_csv: str, extra_csv: str) -> dict:
    """aggTrades per-4h-bar traded quantity / quote volume against the kline row of the same bar (a data-consistency check)."""

    extra = {r["open_time_utc"][:16]: r for r in csv.DictReader(io.StringIO(extra_csv))}
    base = {r["timestamp"][:16]: r for r in csv.DictReader(io.StringIO(klines_csv))}
    diffs = []
    for key, stats in agg["per_4h_bar"].items():
        if key in extra and key in base:
            diffs.append({"bar": key, "base_volume_rel_diff": abs(stats["quantity_btc"] / float(base[key]["volume"]) - 1),
                          "quote_volume_rel_diff": abs(stats["notional_usdt"] / float(extra[key]["quote_volume"]) - 1),
                          "trades_rel_diff": abs(stats["trades"] / float(extra[key]["trades"]) - 1)})
    return {"bars_compared": len(diffs), "max_base_volume_rel_diff": max((d["base_volume_rel_diff"] for d in diffs), default=None),
            "max_quote_volume_rel_diff": max((d["quote_volume_rel_diff"] for d in diffs), default=None), "details": diffs}


def endpoint_schema() -> dict:
    """Availability + field names ONLY. Response values are discarded (live 2026 prices)."""

    out = {}
    for url in ENDPOINTS:
        res = http.get(url, timeout=20)
        shape = None
        if res.status == 200:
            try:
                data = json.loads(res.body)
                shape = {k: type(v).__name__ for k, v in data.items()} if isinstance(data, dict) else type(data).__name__
                if isinstance(data, dict):
                    for k, v in list(data.items()):
                        if isinstance(v, list) and v:
                            shape[k] = f"list[{type(v[0]).__name__}] of {type(v[0][0]).__name__ if isinstance(v[0], list) and v[0] else type(v[0]).__name__}"
            except ValueError:
                shape = "not json"
        out[url.split("?")[0].split("//")[1]] = {"http_status": res.status, "error": res.error, "response_fields": shape}
    return {"endpoints": out, "values_stored": False, "snapshot_format": {
        "file": "one JSON object per snapshot", "fields": ["captured_utc (client clock, ISO-8601)", "source_url (no credentials)", "http_status", "response_sha256", "bookTicker{bidPrice,bidQty,askPrice,askQty}",
                                                          "depth{lastUpdateId,bids[[price,qty]],asks[[price,qty]]}"],
        "reproducibility": "append-only, content-hashed, one file per capture; the capture schedule is part of a future protocol, not started here"},
        "collector_started": False}

