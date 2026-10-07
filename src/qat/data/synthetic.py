"""Deterministic SYNTHETIC OHLCV fixtures (Batch #2).

These series are generated from a seeded PRNG with alternating drift regimes so
the research pipeline (data -> strategy -> gates -> paper fill -> settlement ->
ledger -> metrics -> UI) can be exercised offline. They are NOT market data and
any performance measured on them says nothing about real Net Alpha. Every file
is written with a sidecar ``meta.json`` carrying ``synthetic: true`` and
``source: SYNTHETIC``; symbols start with ``SYN`` so they cannot be mistaken for
real tickers.

Regenerate:  python -m qat.data.synthetic --out data/fixtures
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import pathlib
import random
from datetime import datetime, timedelta, timezone

# (name, market, symbol, timeframe, tz, start, bars, start_price, seed, skip_weekends)
FIXTURES = (
    ("SYN_KR1_1d", "KR", "SYNKR1", "1d", "+09:00", "2022-01-03T00:00:00", 750, 50_000.0, 11, True),
    ("SYN_US1_1d", "US", "SYNUS1", "1d", "UTC", "2022-01-03T00:00:00", 750, 150.0, 23, True),
    ("SYN_CRYPTO1_1h", "CRYPTO", "SYNBTC/KRW", "1h", "UTC", "2024-01-01T00:00:00", 1500, 50_000_000.0, 37, False),
)

# (bars, daily drift, daily vol) cycled: up-trend, range, down-trend, range
_REGIMES = ((120, 0.0025, 0.012), (80, 0.0, 0.010), (100, -0.0022, 0.013), (80, 0.0, 0.009))


def generate_bars(*, bars: int, start: datetime, step: timedelta, start_price: float,
                  seed: int, skip_weekends: bool, vol_scale: float = 1.0) -> list[dict]:
    rng = random.Random(seed)
    rows: list[dict] = []
    close = start_price
    ts = start
    regime_idx, left = 0, _REGIMES[0][0]
    while len(rows) < bars:
        if skip_weekends and ts.weekday() >= 5:
            ts += step
            continue
        if left == 0:
            regime_idx = (regime_idx + 1) % len(_REGIMES)
            left = _REGIMES[regime_idx][0]
        _, drift, vol = _REGIMES[regime_idx]
        vol *= vol_scale
        drift *= vol_scale
        left -= 1
        open_ = close * math.exp(rng.gauss(0.0, vol * 0.25))
        new_close = open_ * math.exp(drift + rng.gauss(0.0, vol))
        high = max(open_, new_close) * math.exp(abs(rng.gauss(0.0, vol * 0.5)))
        low = min(open_, new_close) * math.exp(-abs(rng.gauss(0.0, vol * 0.5)))
        volume = int(rng.uniform(50_000, 150_000))
        rows.append({
            "timestamp": ts.strftime("%Y-%m-%dT%H:%M:%S"),
            "open": f"{open_:.4f}", "high": f"{high:.4f}", "low": f"{low:.4f}",
            "close": f"{new_close:.4f}", "volume": str(volume),
        })
        close = new_close
        ts += step
    return rows


def write_fixture(out_dir: pathlib.Path, name, market, symbol, timeframe, tz, start, n,
                  start_price, seed, skip_weekends) -> pathlib.Path:
    from qat.data.bars import parse_timeframe

    step = parse_timeframe(timeframe)
    vol_scale = 1.0 if step >= timedelta(days=1) else 0.2  # hourly bars: smaller moves
    rows = generate_bars(bars=n, start=datetime.fromisoformat(start), step=step,
                         start_price=start_price, seed=seed, skip_weekends=skip_weekends,
                         vol_scale=vol_scale)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{name}.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["timestamp", "open", "high", "low", "close", "volume"],
                                lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    meta = {
        "market": market, "symbol": symbol, "timeframe": timeframe, "timezone": tz,
        "timestamp_label": "open", "source": "SYNTHETIC", "synthetic": True,
        "description": "Deterministic synthetic fixture for pipeline verification - NOT market data",
        "extra": {"generator": "qat.data.synthetic", "seed": seed, "bars": n,
                  "regimes": [list(r) for r in _REGIMES], "generated_utc_start": start,
                  "created": datetime(2026, 1, 1, tzinfo=timezone.utc).isoformat()},
    }
    path.with_name(path.name + ".meta.json").write_text(
        json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n"
    )
    return path


def write_all(out_dir: pathlib.Path) -> list[pathlib.Path]:
    return [write_fixture(out_dir, *spec) for spec in FIXTURES]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Write deterministic SYNTHETIC OHLCV fixtures")
    parser.add_argument("--out", default="data/fixtures")
    args = parser.parse_args(argv)
    for path in write_all(pathlib.Path(args.out)):
        print(path)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
