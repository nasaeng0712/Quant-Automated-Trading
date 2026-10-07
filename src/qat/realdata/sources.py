"""Provider parsers: raw bytes -> rows, WITHOUT cleaning (Batch #3A).

A parser only reads. Values stay exactly as the provider gave them (strings or JSON
numbers, ``None`` for null) so that validation can see every anomaly. Nothing is skipped,
deduplicated, reordered or repaired here; row order is the provider's order.

Row keys: ``date`` (ISO session date derived per the documented rule), ``open`` ``high``
``low`` ``close`` ``volume`` (as given), ``extras`` (provider-specific fields).
"""

from __future__ import annotations

import csv
import io
import json
import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone


class ParseError(ValueError):
    """The raw bytes are not in the provider's documented/observed shape."""


@dataclass
class ParsedSeries:
    rows: list[dict]
    meta: dict = field(default_factory=dict)

    def date_range(self) -> dict:
        dates = [r["date"] for r in self.rows if r.get("date")]
        return {"start": min(dates), "end": max(dates)} if dates else {"start": None, "end": None}


def _row(date, o, h, lo, c, v, **extras) -> dict:
    return {"date": date, "open": o, "high": h, "low": lo, "close": c, "volume": v, "extras": extras}


# --------------------------------------------------------------------------- Binance
def _binance_open_date(open_time: str) -> tuple[str, str]:
    """data.binance.vision spot klines: open_time is epoch MILLISECONDS (13 digits) until
    2024 and MICROSECONDS (16 digits) from 2025-01-01. Returns (UTC date, unit)."""

    text = open_time.strip()
    if not re.fullmatch(r"\d+", text):
        raise ParseError(f"binance open_time is not an integer: {text!r}")
    digits = len(text)
    if digits == 13:
        seconds, unit = int(text) / 1_000, "ms"
    elif digits == 16:
        seconds, unit = int(text) / 1_000_000, "us"
    else:
        raise ParseError(f"binance open_time has {digits} digits (expected 13 ms or 16 us)")
    return datetime.fromtimestamp(seconds, tz=timezone.utc).date().isoformat(), unit


def parse_binance_klines_zip(content: bytes) -> ParsedSeries:
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile as exc:
        raise ParseError("binance archive is not a zip file") from exc
    members = [n for n in archive.namelist() if n.lower().endswith(".csv")]
    if len(members) != 1:
        raise ParseError(f"binance zip must contain exactly one csv, found {len(members)}")
    rows, units = [], set()
    for line in io.StringIO(archive.read(members[0]).decode("utf-8")):
        line = line.strip()
        if not line:
            continue
        cells = next(csv.reader([line]))
        if len(cells) < 6:
            raise ParseError(f"binance kline row has {len(cells)} columns")
        date, unit = _binance_open_date(cells[0])
        units.add(unit)
        rows.append(_row(date, cells[1], cells[2], cells[3], cells[4], cells[5], open_time=cells[0],
                         close_time=cells[6] if len(cells) > 6 else None,
                         quote_volume=cells[7] if len(cells) > 7 else None,
                         trades=cells[8] if len(cells) > 8 else None))
    return ParsedSeries(rows, {"member": members[0], "timestamp_units": sorted(units),
                               "date_rule": "UTC calendar date of kline open_time"})


def parse_binance_checksum(content: bytes) -> dict:
    """``<sha256>  <filename>`` as published next to each archive."""

    match = re.match(r"\s*([0-9a-fA-F]{64})\s+\*?(\S+)", content.decode("utf-8", errors="replace"))
    if not match:
        raise ParseError("binance CHECKSUM file not in '<sha256>  <name>' form")
    return {"sha256": match.group(1).lower(), "name": match.group(2)}


# --------------------------------------------------------------------------- Yahoo chart API
def parse_yahoo_chart(content: bytes, *, utc_offset_seconds: int) -> ParsedSeries:
    """Yahoo v8 chart JSON (interval=1d). Daily stamps are the exchange's session OPEN time:
    US 09:30 local = 13:30Z (EDT) / 14:30Z (EST) so the UTC date equals the exchange date
    (offset 0); KR stamps are 09:00 KST = 00:00Z so +9h gives the KST date. Whatever
    time-of-day arrives is kept in ``extras['ts_utc']`` so validation can check it."""

    try:
        doc = json.loads(content)
        chart = doc["chart"]
    except (ValueError, KeyError, TypeError) as exc:
        raise ParseError("yahoo response is not chart JSON") from exc
    if chart.get("error"):
        raise ParseError(f"yahoo chart error: {chart['error']}")
    result = (chart.get("result") or [None])[0]
    if not result:
        raise ParseError("yahoo chart has no result")
    stamps = result.get("timestamp") or []
    quote = (result.get("indicators", {}).get("quote") or [{}])[0]
    adj = (result.get("indicators", {}).get("adjclose") or [{}])[0].get("adjclose")
    lengths = {k: len(quote.get(k) or []) for k in ("open", "high", "low", "close", "volume")}
    if any(n != len(stamps) for n in lengths.values()):
        raise ParseError(f"yahoo arrays differ in length from timestamps: {lengths} vs {len(stamps)}")
    rows = []
    for i, stamp in enumerate(stamps):
        moment = datetime.fromtimestamp(stamp, tz=timezone.utc)
        date = (moment + timedelta(seconds=utc_offset_seconds)).date().isoformat()
        rows.append(_row(date, quote["open"][i], quote["high"][i], quote["low"][i], quote["close"][i],
                         quote["volume"][i], ts_utc=moment.isoformat(),
                         adjclose=None if adj is None else adj[i]))
    meta = dict(result.get("meta") or {})
    return ParsedSeries(rows, {
        "reported_symbol": meta.get("symbol"), "currency": meta.get("currency"),
        "exchange_timezone": meta.get("exchangeTimezoneName"), "instrument_type": meta.get("instrumentType"),
        "data_granularity": meta.get("dataGranularity"), "exchange_name": meta.get("exchangeName"),
        "splits": (result.get("events") or {}).get("splits"),
        "dividends": (result.get("events") or {}).get("dividends"),
        "date_rule": f"UTC date of session-open timestamp shifted by {utc_offset_seconds}s",
    })


# --------------------------------------------------------------------------- Nasdaq
def parse_nasdaq_historical(content: bytes) -> ParsedSeries:
    try:
        doc = json.loads(content)
        data = doc["data"]
        table = data["tradesTable"]
        rows_in = table["rows"]
    except (ValueError, KeyError, TypeError) as exc:
        raise ParseError("nasdaq response is not historical JSON") from exc
    rows = []
    for item in rows_in or []:
        text = item.get("date", "")
        try:
            date = datetime.strptime(text, "%m/%d/%Y").date().isoformat()
        except ValueError as exc:
            raise ParseError(f"nasdaq date not MM/DD/YYYY: {text!r}") from exc
        rows.append(_row(date, item.get("open"), item.get("high"), item.get("low"), item.get("close"),
                         item.get("volume"), source_date=text))
    return ParsedSeries(rows, {"reported_symbol": data.get("symbol"), "date_rule": "MM/DD/YYYY exchange session date",
                               "total_records": data.get("totalRecords"), "headers": table.get("headers")})


# --------------------------------------------------------------------------- Naver
def parse_naver_fchart(content: bytes) -> ParsedSeries:
    """fchart.stock.naver.com sise.nhn XML: ``<item data="YYYYMMDD|open|high|low|close|volume"/>``."""

    try:
        # the response declares EUC-KR, which expat cannot parse from bytes: decode (cp949 is a
        # superset of EUC-KR) and parse the text without its encoding declaration
        text = re.sub(r"^\s*<\?xml[^>]*\?>", "", content.decode("cp949"))
        root = ET.fromstring(text)
    except (ET.ParseError, UnicodeDecodeError) as exc:
        raise ParseError("naver response is not XML") from exc
    chart = root.find(".//chartdata")
    if chart is None:
        raise ParseError("naver XML has no chartdata")
    rows = []
    for item in chart.findall("item"):
        cells = (item.get("data") or "").split("|")
        if len(cells) != 6 or not re.fullmatch(r"\d{8}", cells[0]):
            raise ParseError(f"naver item not 'YYYYMMDD|o|h|l|c|v': {item.get('data')!r}")
        d = cells[0]
        rows.append(_row(f"{d[:4]}-{d[4:6]}-{d[6:]}", cells[1], cells[2], cells[3], cells[4], cells[5]))
    return ParsedSeries(rows, {"reported_symbol": chart.get("symbol"), "name": chart.get("name"),
                               "origintime": chart.get("origintime"), "count": chart.get("count"),
                               "date_rule": "YYYYMMDD KST session date"})


# --------------------------------------------------------------------------- data.go.kr
def parse_datagokr_stock_price(content: bytes) -> ParsedSeries:
    """금융위원회 주식시세정보 ``getStockPriceInfo`` JSON page. A non-success ``resultCode``
    is an error, never an empty series."""

    try:
        doc = json.loads(content)
        response = doc["response"]
        header = response["header"]
        body = response["body"]
    except (ValueError, KeyError, TypeError) as exc:
        raise ParseError("data.go.kr response is not the expected response/header/body JSON") from exc
    if str(header.get("resultCode")) != "00":
        raise ParseError(f"data.go.kr API error code {header.get('resultCode')}: {header.get('resultMsg')}")
    items = body.get("items") or {}
    item_list = items.get("item") if isinstance(items, dict) else None
    if item_list is None:
        item_list = []
    if isinstance(item_list, dict):  # single-item responses may be an object, not a list
        item_list = [item_list]
    rows = []
    for item in item_list:
        text = str(item.get("basDt", ""))
        if not re.fullmatch(r"\d{8}", text):
            raise ParseError(f"data.go.kr basDt not YYYYMMDD: {text!r}")
        rows.append(_row(f"{text[:4]}-{text[4:6]}-{text[6:]}", item.get("mkp"), item.get("hipr"), item.get("lopr"),
                         item.get("clpr"), item.get("trqu"), srtnCd=item.get("srtnCd"), isinCd=item.get("isinCd"),
                         itmsNm=item.get("itmsNm"), mrktCtg=item.get("mrktCtg"), vs=item.get("vs"),
                         fltRt=item.get("fltRt"), trPrc=item.get("trPrc"), lstgStCnt=item.get("lstgStCnt"),
                         mrktTotAmt=item.get("mrktTotAmt")))
    return ParsedSeries(rows, {
        "total_count": body.get("totalCount"), "page_no": body.get("pageNo"), "num_of_rows": body.get("numOfRows"),
        "result_code": header.get("resultCode"), "date_rule": "basDt YYYYMMDD KST session date"})


# --------------------------------------------------------------------------- Coinbase Exchange
def parse_coinbase_candles(content: bytes) -> ParsedSeries:
    """Coinbase Exchange ``/products/<id>/candles`` JSON: ``[time, low, high, open, close, volume]``
    with ``time`` = epoch seconds of the UTC bucket start (provider order: newest first)."""

    try:
        data = json.loads(content)
    except ValueError as exc:
        raise ParseError("coinbase response is not JSON") from exc
    if not isinstance(data, list):
        raise ParseError(f"coinbase response is not a candle list: {str(data)[:80]}")
    rows = []
    for cell in data:
        if not isinstance(cell, list) or len(cell) != 6:
            raise ParseError("coinbase candle is not [time, low, high, open, close, volume]")
        moment = datetime.fromtimestamp(cell[0], tz=timezone.utc)
        rows.append(_row(moment.date().isoformat(), cell[3], cell[2], cell[1], cell[4], cell[5],
                         ts_utc=moment.isoformat()))
    return ParsedSeries(rows, {"date_rule": "UTC date of candle bucket start (86400s granularity)"})
