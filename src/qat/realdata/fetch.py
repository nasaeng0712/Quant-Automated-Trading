"""Network acquisition of raw artifacts (Batch #3A). Each function downloads, parses ONLY to
record the returned range, and stores the original bytes unchanged via ``store_raw``.

Nothing is written when the HTTP call or the parse fails (the failure is returned/raised;
no partial/"repaired" artifact). The data.go.kr serviceKey is read from a local file at
call time and is scrubbed from every stored or returned text.
"""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone

from qat.realdata import http
from qat.realdata.provenance import RawArtifact, sha256_bytes, store_raw, utc_now_iso
from qat.realdata.secrets import encode_for_query, read_key, redact
from qat.realdata.sources import (
    ParseError, parse_binance_checksum, parse_binance_klines_zip, parse_coinbase_candles,
    parse_datagokr_stock_price, parse_naver_fchart, parse_nasdaq_historical, parse_yahoo_chart,
)

BINANCE_BASE = "https://data.binance.vision/data/spot/monthly/klines"
YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart"
NASDAQ_HIST = "https://api.nasdaq.com/api/quote/{symbol}/historical"
NAVER_FCHART = "https://fchart.stock.naver.com/sise.nhn"
COINBASE = "https://api.exchange.coinbase.com/products/{product}/candles"
DATAGOKR_STOCK_PRICE = "https://apis.data.go.kr/1160100/GetStockSecuritiesInfoService_V2/getStockPriceInfo_V2"
DATAGOKR_STOCK_PRICE_LEGACY = "https://apis.data.go.kr/1160100/service/GetStockSecuritiesInfoService/getStockPriceInfo"


class FetchError(RuntimeError):
    """An acquisition step failed. The message never contains a URL query or a key."""


def _require_ok(result: http.HttpResult, what: str) -> bytes:
    if not result.ok:
        raise FetchError(f"{what}: HTTP status {result.status} {result.error or ''}".strip())
    return result.body


def month_iter(first: date, last: date):
    year, month = first.year, first.month
    while (year, month) <= (last.year, last.month):
        yield year, month
        month += 1
        if month == 13:
            year, month = year + 1, 1


# --------------------------------------------------------------------------- Binance (crypto)
def fetch_binance_month(symbol: str, year: int, month: int, *, interval: str = "1d", root=None) -> tuple[RawArtifact, RawArtifact]:
    """Monthly kline archive + the official ``.CHECKSUM`` file; the archive's SHA-256 must
    equal the published checksum (recorded in the sidecar either way; a mismatch raises)."""

    name = f"{symbol}-{interval}-{year:04d}-{month:02d}.zip"
    base = f"{BINANCE_BASE}/{symbol}/{interval}/{name}"
    zip_bytes = _require_ok(http.get(base), name)
    checksum_bytes = _require_ok(http.get(base + ".CHECKSUM"), name + ".CHECKSUM")
    official = parse_binance_checksum(checksum_bytes)
    actual = sha256_bytes(zip_bytes)
    if official["sha256"] != actual or official["name"] != name:
        raise FetchError(f"{name}: SHA-256 differs from the official CHECKSUM file")
    parsed = parse_binance_klines_zip(zip_bytes)
    last_day = (date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)).isoformat()
    tz = {"exchange_timezone": "UTC", "timestamp_semantics": "kline open time, UTC; date = UTC calendar date"}
    art = store_raw(zip_bytes, name=name, provider="binance-vision", service="spot/monthly/klines", market="CRYPTO",
                    symbol=symbol, requested_range={"start": f"{year:04d}-{month:02d}-01", "end": last_day},
                    returned_range=parsed.date_range(), fmt="zip(csv)", endpoint=base, timezone_info=tz,
                    extra={"official_checksum": official["sha256"], "official_checksum_match": True,
                           "timestamp_units": parsed.meta["timestamp_units"], "http_status": 200})
    chk = store_raw(checksum_bytes, name=name + ".CHECKSUM", provider="binance-vision",
                    service="spot/monthly/klines CHECKSUM", market="CRYPTO", symbol=symbol,
                    requested_range={"start": f"{year:04d}-{month:02d}-01", "end": last_day},
                    returned_range={"start": None, "end": None}, fmt="text", endpoint=base + ".CHECKSUM",
                    timezone_info=tz, extra={"covers_artifact": name, "http_status": 200})
    return art, chk


# --------------------------------------------------------------------------- Yahoo
def _epoch(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp())


def fetch_yahoo_daily(yahoo_symbol: str, *, market: str, symbol: str, start: date, end: date,
                      utc_offset_seconds: int, exchange_tz: str, root=None) -> RawArtifact:
    """One chart request for [start, end] (period2 is exclusive, so end+1 day)."""

    url = (f"{YAHOO_CHART}/{yahoo_symbol}?period1={_epoch(start)}&period2={_epoch(end + timedelta(days=1))}"
           f"&interval=1d&events=div%7Csplit&includeAdjustedClose=true")
    body = _require_ok(http.get(url), f"yahoo {yahoo_symbol}")
    parsed = parse_yahoo_chart(body, utc_offset_seconds=utc_offset_seconds)
    return store_raw(body, name=f"{symbol}-1d-{start:%Y%m%d}-{end:%Y%m%d}.json", provider="yahoo-chart",
                     service="v8/finance/chart (unofficial)", market=market, symbol=symbol,
                     requested_range={"start": start.isoformat(), "end": end.isoformat()},
                     returned_range=parsed.date_range(), fmt="json", endpoint=url,
                     timezone_info={"exchange_timezone": exchange_tz, "utc_offset_seconds_used_for_date": utc_offset_seconds},
                     extra={"reported_symbol": parsed.meta["reported_symbol"], "http_status": 200}, root=root)


def fetch_nasdaq_daily(symbol: str, *, start: date, end: date, root=None) -> RawArtifact:
    url = f"{NASDAQ_HIST.format(symbol=symbol)}?assetclass=stocks&fromdate={start}&todate={end}&limit=9999"
    body = _require_ok(http.get(url, headers={"Accept": "application/json, text/plain, */*",
                                              "Origin": "https://www.nasdaq.com", "Referer": "https://www.nasdaq.com/"}),
                       f"nasdaq {symbol}")
    parsed = parse_nasdaq_historical(body)
    return store_raw(body, name=f"{symbol}-1d-{start:%Y%m%d}-{end:%Y%m%d}.json", provider="nasdaq-historical",
                     service="api/quote/historical (unofficial)", market="US", symbol=symbol,
                     requested_range={"start": start.isoformat(), "end": end.isoformat()},
                     returned_range=parsed.date_range(), fmt="json", endpoint=url,
                     timezone_info={"exchange_timezone": "America/New_York", "date_semantics": "exchange session date"},
                     extra={"reported_symbol": parsed.meta["reported_symbol"], "http_status": 200}, root=root)


# --------------------------------------------------------------------------- Naver
def fetch_naver_daily(symbol: str, *, count: int, start: date, end: date, root=None) -> RawArtifact:
    url = f"{NAVER_FCHART}?symbol={symbol}&timeframe=day&count={count}&requestType=0"
    body = _require_ok(http.get(url), f"naver {symbol}")
    parsed = parse_naver_fchart(body)
    return store_raw(body, name=f"{symbol}-day-count{count}.xml", provider="naver-fchart",
                     service="sise.nhn (unofficial)", market="KR", symbol=symbol,
                     requested_range={"start": start.isoformat(), "end": end.isoformat(),
                                      "note": f"provider returns the latest {count} sessions; window selected at normalization"},
                     returned_range=parsed.date_range(), fmt="xml", endpoint=url,
                     timezone_info={"exchange_timezone": "Asia/Seoul", "date_semantics": "KST session date"},
                     extra={"reported_symbol": parsed.meta["reported_symbol"], "http_status": 200}, root=root)


# --------------------------------------------------------------------------- Coinbase
def fetch_coinbase_window(product: str, *, symbol: str, start: date, end: date, root=None) -> RawArtifact:
    """<= 300 daily candles per request; the caller windows deterministically."""

    s = datetime(start.year, start.month, start.day, tzinfo=timezone.utc).isoformat()
    e = datetime(end.year, end.month, end.day, 23, 59, 59, tzinfo=timezone.utc).isoformat()
    url = f"{COINBASE.format(product=product)}?granularity=86400&start={s}&end={e}"
    body = _require_ok(http.get(url), f"coinbase {product} {start}")
    parsed = parse_coinbase_candles(body)
    return store_raw(body, name=f"{product}-1d-{start:%Y%m%d}-{end:%Y%m%d}.json", provider="coinbase-exchange",
                     service="products/candles", market="CRYPTO", symbol=symbol,
                     requested_range={"start": start.isoformat(), "end": end.isoformat()},
                     returned_range=parsed.date_range(), fmt="json", endpoint=url,
                     timezone_info={"exchange_timezone": "UTC", "timestamp_semantics": "UTC bucket start"},
                     extra={"product": product, "http_status": 200}, root=root)


# --------------------------------------------------------------------------- data.go.kr (serviceKey)
def datagokr_url(key: str, *, page: int, rows: int, begin: str, end: str, isin: str | None = None,
                 srtn: str | None = None, key_mode: str = "auto") -> str:
    parts = [f"serviceKey={encode_for_query(key, mode=key_mode)}", f"numOfRows={rows}", f"pageNo={page}",
             "resultType=json", f"beginBasDt={begin}", f"endBasDt={end}"]
    if isin:
        parts.append(f"isinCd={isin}")
    if srtn:
        parts.append(f"likeSrtnCd={srtn}")
    return DATAGOKR_STOCK_PRICE + "?" + "&".join(parts)


def datagokr_call(key: str, **kwargs) -> tuple[http.HttpResult, str]:
    """Returns (result, url-with-key). The URL must never be printed or stored unredacted."""

    url = datagokr_url(key, **kwargs)
    return http.get(url), url


def datagokr_error(result: http.HttpResult, key: str) -> dict:
    """Redacted, structural description of a failed call (codes and messages only)."""

    text = redact(result.body.decode("utf-8", errors="replace"), key)
    import re

    code = re.search(r'"returnReasonCode"\s*:\s*"?(\d+)', text) or re.search(r'"resultCode"\s*:\s*"?(\d+)', text)
    msg = re.search(r'"(?:errMsg|resultMsg)"\s*:\s*"([^"]*)"', text)
    return {"http_status": result.status, "code": code.group(1) if code else None,
            "message": msg.group(1) if msg else None, "network_error": result.error}


def fetch_datagokr_page(key: str, *, page: int, rows: int, begin: str, end: str, isin: str | None,
                        srtn: str | None, symbol: str, key_mode: str = "auto", root=None) -> RawArtifact:
    result, url = datagokr_call(key, page=page, rows=rows, begin=begin, end=end, isin=isin, srtn=srtn, key_mode=key_mode)
    if not result.ok:
        raise FetchError(f"data.go.kr page {page}: {datagokr_error(result, key)}")
    try:
        parsed = parse_datagokr_stock_price(result.body)
    except ParseError as exc:
        raise FetchError(redact(f"data.go.kr page {page}: {exc}", key)) from exc
    # official guide: beginBasDt is inclusive (>=), endBasDt is EXCLUSIVE (<)
    from datetime import datetime as _dt
    inclusive_end = (_dt.strptime(end, "%Y%m%d").date() - timedelta(days=1)).isoformat()
    begin_iso = _dt.strptime(begin, "%Y%m%d").date().isoformat()
    return store_raw(result.body, name=f"{symbol}-getStockPriceInfo_V2-{begin}-{end}-p{page:03d}.json",
                     provider="data.go.kr", service="금융위원회_주식시세정보 GetStockSecuritiesInfoService_V2/getStockPriceInfo_V2",
                     market="KR", symbol=symbol,
                     requested_range={"start": begin_iso, "end": inclusive_end,
                                      "note": f"endBasDt={end} is exclusive per the official guide"},
                     returned_range=parsed.date_range(), fmt="json", endpoint=url,
                     timezone_info={"exchange_timezone": "Asia/Seoul", "date_semantics": "basDt KST session date"},
                     extra={"page": page, "num_of_rows": rows, "total_count": parsed.meta["total_count"],
                            "result_code": parsed.meta["result_code"], "http_status": result.status,
                            "request_params": {"numOfRows": rows, "pageNo": page, "resultType": "json",
                                               "beginBasDt": begin, "endBasDt": end, "isinCd": isin, "likeSrtnCd": srtn,
                                               "serviceKey": "<REDACTED>"}},
                     key=key, root=root)


__all__ = [n for n in dir() if n.startswith(("fetch_", "datagokr_", "month_iter"))] + ["FetchError", "time", "utc_now_iso", "read_key"]
