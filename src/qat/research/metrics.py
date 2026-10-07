"""Backtest metrics (Batch #2).

Definitions (all in the instrument's account currency):
  net_pnl        = ending_equity - starting_equity   (ledger truth: realized +
                   unrealized, after every cost)
  explicit_costs = commission + exchange_fee + tax + fx_cost   (sum over fills)
  gross_pnl      = net_pnl + explicit_costs
                   i.e. PnL at executed prices before explicit fees/taxes.
                   Slippage and half-spread are inside executed prices and are
                   NOT added back; ``slippage_estimate`` is reported separately
                   for analysis and never subtracted a second time.
Trades are round trips flat -> flat reconstructed from fills; trade net PnL is
the signed cash flow of its fills (buy costs are in the buy cash flow, sell
costs in the sell cash flow - each counted once). An open trade at the end is
reported with its mark-to-market value and excluded from win statistics.

Undefined values are ``None`` with a reason in ``notes`` - never 0 or infinity
disguised as a result (no trades, no losing trades, short samples).
"""

from __future__ import annotations

MIN_TRADES_FOR_STATS = 30
_EPS = 1e-9


def reconstruct_trades(fills: list[dict], final_mark: float | None) -> list[dict]:
    trades: list[dict] = []
    current: dict | None = None
    qty = 0.0
    for f in fills:
        if current is None:
            current = {"entry_ts": f["timestamp"], "exit_ts": None, "fills": 0, "buy_qty": 0.0,
                       "sell_qty": 0.0, "buy_gross": 0.0, "sell_gross": 0.0, "costs": 0.0,
                       "slippage_estimate": 0.0, "status": "OPEN"}
        cost = f["commission"] + f["exchange_fee"] + f["tax"] + f["fx_cost"]
        current["fills"] += 1
        current["costs"] += cost
        current["slippage_estimate"] += f["slippage_estimate"]
        if f["side"] == "BUY":
            qty += f["quantity"]
            current["buy_qty"] += f["quantity"]
            current["buy_gross"] += f["quantity"] * f["price"]
        else:
            qty -= f["quantity"]
            current["sell_qty"] += f["quantity"]
            current["sell_gross"] += f["quantity"] * f["price"]
        if qty <= _EPS:
            current["status"] = "CLOSED"
            current["exit_ts"] = f["timestamp"]
            current["gross_pnl"] = current["sell_gross"] - current["buy_gross"]
            current["net_pnl"] = current["gross_pnl"] - current["costs"]
            current["return"] = current["net_pnl"] / (current["buy_gross"] or float("nan"))
            trades.append(current)
            current, qty = None, 0.0
    if current is not None:
        open_value = qty * final_mark if final_mark is not None else None
        current["open_quantity"] = qty
        current["mark"] = final_mark
        if open_value is None:
            current["gross_pnl"] = current["net_pnl"] = current["return"] = None
        else:
            current["gross_pnl"] = current["sell_gross"] + open_value - current["buy_gross"]
            current["net_pnl"] = current["gross_pnl"] - current["costs"]
            current["return"] = current["net_pnl"] / current["buy_gross"] if current["buy_gross"] else None
        trades.append(current)
    return trades


def max_drawdown(equity: list[float]) -> tuple[float | None, float | None]:
    if not equity:
        return None, None
    peak, mdd_abs, mdd_pct = equity[0], 0.0, 0.0
    for value in equity:
        peak = max(peak, value)
        mdd_abs = max(mdd_abs, peak - value)
        if peak > 0:
            mdd_pct = max(mdd_pct, (peak - value) / peak)
    return mdd_abs, mdd_pct


def compute_metrics(*, starting_equity: float, equity_curve: list[dict], fills: list[dict],
                    trades: list[dict], rejections: list[dict], currency: str) -> dict:
    notes: list[str] = []
    values = [p["equity"] for p in equity_curve if p["equity"] is not None]
    ending = equity_curve[-1]["equity"] if equity_curve else None
    if ending is None:
        notes.append("ending_equity unavailable (missing mark) - PnL not computed")
    commission = sum(f["commission"] for f in fills)
    exchange_fee = sum(f["exchange_fee"] for f in fills)
    tax = sum(f["tax"] for f in fills)
    fx_cost = sum(f["fx_cost"] for f in fills)
    slippage = sum(f["slippage_estimate"] for f in fills)
    explicit = commission + exchange_fee + tax + fx_cost
    net_pnl = None if ending is None else ending - starting_equity
    gross_pnl = None if net_pnl is None else net_pnl + explicit

    closed = [t for t in trades if t["status"] == "CLOSED"]
    wins = [t["net_pnl"] for t in closed if t["net_pnl"] > 0]
    losses = [t["net_pnl"] for t in closed if t["net_pnl"] <= 0]
    if not closed:
        notes.append("no closed trades: win rate / expectancy / profit factor undefined")
    if closed and not losses:
        notes.append("no losing trades: profit factor undefined (not infinity)")
    if closed and len(closed) < MIN_TRADES_FOR_STATS:
        notes.append(f"short sample: {len(closed)} closed trades < {MIN_TRADES_FOR_STATS}")

    traded_notional = sum(f["quantity"] * f["price"] for f in fills)
    mean_equity = sum(values) / len(values) if values else None
    exposed = sum(1 for p in equity_curve if p["position_qty"] > _EPS)
    mdd_abs, mdd_pct = max_drawdown([starting_equity] + values)

    stages: dict[str, int] = {}
    for r in rejections:
        stages[r["stage"]] = stages.get(r["stage"], 0) + 1

    return {
        "currency": currency,
        "bars": len(equity_curve),
        "starting_equity": starting_equity,
        "ending_equity": ending,
        "net_pnl": net_pnl,
        "net_return": None if net_pnl is None else net_pnl / starting_equity,
        "gross_pnl": gross_pnl,
        "gross_return": None if gross_pnl is None else gross_pnl / starting_equity,
        "commission": commission,
        "exchange_fee": exchange_fee,
        "tax": tax,
        "fx_cost": fx_cost,
        "explicit_costs": explicit,
        "slippage_estimate": slippage,
        "fills": len(fills),
        "trades_closed": len(closed),
        "trades_open": len(trades) - len(closed),
        "win_rate": len(wins) / len(closed) if closed else None,
        "avg_win": sum(wins) / len(wins) if wins else None,
        "avg_loss": sum(losses) / len(losses) if losses else None,
        "expectancy": sum(t["net_pnl"] for t in closed) / len(closed) if closed else None,
        "profit_factor": (sum(wins) / abs(sum(losses))) if closed and losses and sum(losses) < 0 else None,
        "max_drawdown_abs": mdd_abs,
        "max_drawdown_pct": mdd_pct,
        "turnover": traded_notional / mean_equity if mean_equity else None,
        "traded_notional": traded_notional,
        "exposure_time": exposed / len(equity_curve) if equity_curve else None,
        "rejections": len(rejections),
        "rejections_by_stage": stages,
        "sample_warning": (not closed) or len(closed) < MIN_TRADES_FOR_STATS,
        "notes": notes,
    }
