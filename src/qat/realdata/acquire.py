"""Deterministic acquisition plans for the Batch #3A instruments.

Period for every instrument: 2018-01-01 .. 2025-12-31. Windows are fixed (calendar
months for Binance, 300-day windows for Coinbase, one request for Yahoo/Nasdaq/Naver,
fixed-size pages for data.go.kr) so a re-run asks for exactly the same things.
Existing identical artifacts are skipped; different bytes are refused (immutability).
"""

from __future__ import annotations

from datetime import date, timedelta

from qat.realdata import fetch
from qat.realdata.provenance import RawImmutableError

START = date(2018, 1, 1)
END = date(2025, 12, 31)


def acquire_crypto(log) -> None:
    for year, month in fetch.month_iter(START, END):
        try:
            fetch.fetch_binance_month("BTCUSDT", year, month)
            log(f"binance BTCUSDT {year}-{month:02d} stored")
        except (fetch.FetchError, RawImmutableError) as exc:
            log(f"binance BTCUSDT {year}-{month:02d} FAILED: {exc}")


def acquire_crypto_crosscheck(log, first: date = START) -> None:
    day = first
    while day <= END:
        stop = min(day + timedelta(days=299), END)
        try:
            fetch.fetch_coinbase_window("BTC-USDT", symbol="BTCUSDT", start=day, end=stop)
            log(f"coinbase BTC-USDT {day}..{stop} stored")
        except (fetch.FetchError, RawImmutableError) as exc:
            log(f"coinbase BTC-USDT {day}..{stop} FAILED: {exc}")
        day = stop + timedelta(days=1)


def acquire_us(log) -> None:
    for label, fn in (
        ("yahoo AAPL", lambda: fetch.fetch_yahoo_daily("AAPL", market="US", symbol="AAPL", start=START, end=END,
                                                       utc_offset_seconds=0, exchange_tz="America/New_York")),
        ("nasdaq AAPL", lambda: fetch.fetch_nasdaq_daily("AAPL", start=date(2018, 1, 2), end=END)),
    ):
        try:
            art = fn()
            log(f"{label} stored: {art.path.name} rows-range {art.sidecar['returned_range']}")
        except (fetch.FetchError, RawImmutableError) as exc:
            log(f"{label} FAILED: {exc}")


def acquire_kr_crosscheck(log) -> None:
    for label, fn in (
        ("yahoo 005930.KS", lambda: fetch.fetch_yahoo_daily("005930.KS", market="KR", symbol="005930", start=START, end=END,
                                                            utc_offset_seconds=9 * 3600, exchange_tz="Asia/Seoul")),
        ("naver 005930", lambda: fetch.fetch_naver_daily("005930", count=3500, start=START, end=END)),
    ):
        try:
            art = fn()
            log(f"{label} stored: {art.path.name} range {art.sidecar['returned_range']}")
        except (fetch.FetchError, RawImmutableError) as exc:
            log(f"{label} FAILED: {exc}")


def acquire_kr_official(log, key: str, *, rows: int = 1000, key_mode: str = "auto") -> dict:
    """Page through getStockPriceInfo for Samsung Electronics (isinCd KR7005930003).
    Returns a summary; raises ``fetch.FetchError`` (redacted) on the first failed page."""

    pages, total, page = [], None, 1
    while True:
        art = fetch.fetch_datagokr_page(key, page=page, rows=rows, begin="20180101", end="20260101",
                                        isin="KR7005930003", srtn=None, symbol="005930", key_mode=key_mode)
        pages.append(art.path.name)
        total = art.sidecar["total_count"]
        log(f"data.go.kr page {page} stored ({art.sidecar['returned_range']}, totalCount={total})")
        if total is None or page * rows >= int(total):
            break
        page += 1
    return {"pages": pages, "total_count": total}
