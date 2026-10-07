"""Market-condition rules R-07 / R-08 / R-09 (Risk) and MI-05 / MI-06 (Market Integrity).

Exact meanings (design doc sections 7 and 8.2 give only the names; this module fixes the semantics and says what data each needs):

  R-07  Liquidity shortage block          input: reference-bar volume. volume <= 0 or < configured minimum -> BLOCK; volume unknown -> UNKNOWN.
  R-08  Abnormal spread block             input: QUOTED bid-ask spread (bps). OHLCV bars carry none -> UNKNOWN while configured (never a PASS).
  R-09  Volatility shock response         input: reference-bar (high-low)/close. above the configured limit -> BLOCK; bar unusable -> UNKNOWN.
  MI-05 Cancel/replace pattern            input: the server's own cancel events per symbol inside a rolling window. more cancels than allowed -> BLOCK.
  MI-06 Liquidity participation           input: order quantity / reference-bar volume. above the limit -> BLOCK; volume unknown -> UNKNOWN.

An unconfigured threshold (``None``) means NOT_CONFIGURED: the rule is inert and every view says so; it is never reported as a PASS.
R-07/R-08/R-09 are market-condition blocks for RISK-INCREASING orders; an exposure-reducing order on trustworthy books is let through with an
``exit_allowed:<rule>`` note (frozen exit hierarchy: only the Kill Switch and untrustworthy accounting trap an exit). MI-05/MI-06 limit order
size / behaviour and apply to every order, as the other MI rules do. The context is supplied by the SERVER from the validated dataset: a client
cannot forge it. All functions are pure and deterministic.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

PASS, BLOCK, UNKNOWN = "PASS", "BLOCK", "UNKNOWN"


@dataclass(frozen=True)
class MarketContext:
    volume: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    spread_bps: float | None = None  # a QUOTED spread; None whenever the data source has no quotes
    as_of: str | None = None
    source: str = "unknown"


def _finite(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def liquidity_shortage(ctx: MarketContext | None, min_bar_volume: float | None) -> tuple[str, str]:  # R-07
    if min_bar_volume is None:
        return PASS, "not_configured"
    if ctx is None:
        return UNKNOWN, "liquidity_context_unavailable"
    if not _finite(ctx.volume):
        return UNKNOWN, "bar_volume_unavailable"
    if ctx.volume <= 0 or ctx.volume < min_bar_volume:
        return BLOCK, f"liquidity_shortage:{ctx.volume:g}<{max(min_bar_volume, 0):g}"
    return PASS, "ok"


def abnormal_spread(ctx: MarketContext | None, max_spread_bps: float | None) -> tuple[str, str]:  # R-08
    if max_spread_bps is None:
        return PASS, "not_configured"
    if ctx is None or not _finite(ctx.spread_bps):
        return UNKNOWN, "spread_unavailable:no_quote_data"
    if ctx.spread_bps < 0:
        return UNKNOWN, "spread_invalid:negative"
    if ctx.spread_bps > max_spread_bps:
        return BLOCK, f"abnormal_spread:{ctx.spread_bps:.2f}bps>{max_spread_bps:.2f}bps"
    return PASS, "ok"


def volatility_shock(ctx: MarketContext | None, range_pct_limit: float | None) -> tuple[str, str]:  # R-09
    if range_pct_limit is None:
        return PASS, "not_configured"
    if ctx is None or not all(_finite(v) for v in (ctx.high, ctx.low, ctx.close)) or ctx.close <= 0 or ctx.high < ctx.low:
        return UNKNOWN, "volatility_context_unavailable"
    rng = (ctx.high - ctx.low) / ctx.close
    if rng > range_pct_limit:
        return BLOCK, f"volatility_shock:{rng:.4f}>{range_pct_limit:.4f}"
    return PASS, "ok"


def liquidity_participation(ctx: MarketContext | None, quantity: float, max_rate: float | None) -> tuple[str, str]:  # MI-06
    if max_rate is None:
        return PASS, "not_configured"
    if ctx is None or not _finite(ctx.volume):
        return UNKNOWN, "participation_volume_unavailable"
    if ctx.volume <= 0:
        return BLOCK, "liquidity_participation:no_volume"
    rate = quantity / ctx.volume
    if rate > max_rate:
        return BLOCK, f"liquidity_participation:{rate:.4f}>{max_rate:.4f}"
    return PASS, "ok"


def cancel_replace_pattern(cancel_times: list[float], now: float, window_seconds: float, max_cancels: float | None) -> tuple[str, str]:  # MI-05
    if max_cancels is None:
        return PASS, "not_configured"
    recent = sum(1 for t in cancel_times if 0 <= now - t <= window_seconds)
    if recent > max_cancels:
        return BLOCK, f"cancel_replace_pattern:{recent}>{max_cancels:g}"
    return PASS, "ok"
