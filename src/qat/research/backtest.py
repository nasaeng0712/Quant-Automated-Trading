"""Backtest engine (Batch #2) - runs strategies through the unchanged Core pipeline.

Timeline for bar ``t`` (Level-1 model, recorded in every manifest):
  1. clock := open(t). Orders accepted at close(t-1) are filled at open(t) via
     ``PaperBroker.simulate_fill(order, open(t))`` - MARKET orders pay configured
     half-spread + slippage; an unfilled LIMIT expires (cancelled, reservation
     released). Every fill goes through ``SettlementService.apply_fill``.
  2. clock := close(t) = open(t) + timeframe. Positions are marked at close(t);
     the equity point is recorded (strict valuation, no avg-cost fallback).
  3. The strategy sees bars[0..t] only and may return a Signal. The runner
     builds a TradeProposal (signal_timestamp = close(t), reference price =
     close(t)) and submits it through ``StrategyGateway.submit_trade_proposal``;
     every gate decision is kept. The earliest fill is open(t+1).
  No intrabar high/low path is assumed; the last bar of the window emits no
  signal (it could not be filled inside the window).

End policy ``mark_to_market``: open positions are valued at the final close and
reported as open (not liquidated); any pending order is cancelled and its
reservation released; both are listed in ``end_state``.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import datetime

from qat.app import build_paper_stack
from qat.config import (
    cost_models_from_settings,
    enabled_markets_from_settings,
    fx_provider_from_settings,
    load_research_settings,
)
from qat.cost.engine import CostModel
from qat.core.models import Currency, Market, Side, TradeProposal
from qat.data.bars import DataRejected
from qat.data.loader import Dataset
from qat.realdata.admission import require_admitted
from qat.research.manifest import research_universe
from qat.research.metrics import compute_metrics, reconstruct_trades
from qat.research.strategies import ENTER_LONG, EXIT_LONG, History, Strategy

_EPS = 1e-9
LOT_SIZE = {Market.KR: 1.0, Market.US: 1.0, Market.CRYPTO: 0.0001}


class SimClock:
    def __init__(self, start: datetime) -> None:
        self._now = start

    def set(self, ts: datetime) -> None:
        self._now = ts

    def now(self) -> datetime:
        return self._now


class DeterministicIds:
    def __init__(self) -> None:
        self._counters: dict[str, int] = {}

    def __call__(self, prefix: str) -> str:
        self._counters[prefix] = self._counters.get(prefix, 0) + 1
        return f"{prefix}{self._counters[prefix]:06d}"


@dataclass
class BacktestConfig:
    initial_cash: float = 10_000_000.0
    settings_path: str | None = None
    cost_multiplier: float = 1.0
    position_fraction: float = 0.95
    # OD-03: 2% is a PROVISIONAL policy value, not empirically validated (no real
    # market data yet). Do not tune it automatically or per market; a gap above it
    # records a reservation breach and blocks later orders (design 5.3).
    reservation_buffer_pct: float = 0.02
    trade_start: int = 0  # first bar index whose close may emit a signal
    trade_end: int | None = None  # exclusive; later bars are never given to the engine
    seed: int = 0  # recorded; the engine itself draws no random numbers
    zero_cost_fixture: bool = False  # explicit test mode, labelled in results
    end_policy: str = "mark_to_market"
    risk_overrides: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("initial_cash", "cost_multiplier", "position_fraction", "reservation_buffer_pct"):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be a finite number >= 0")
        if self.initial_cash <= 0:
            raise ValueError("initial_cash must be > 0")
        if not 0 < self.position_fraction <= 1:
            raise ValueError("position_fraction must be in (0, 1]")
        if self.end_policy != "mark_to_market":
            raise ValueError("only end_policy='mark_to_market' is implemented")


@dataclass
class BacktestResult:
    config: dict
    strategy: dict
    dataset: dict
    settings_meta: dict
    cost_models: dict
    labels: dict
    equity: list = field(default_factory=list)
    fills: list = field(default_factory=list)
    orders: list = field(default_factory=list)
    rejections: list = field(default_factory=list)
    skipped: list = field(default_factory=list)
    breaches: list = field(default_factory=list)
    trades: list = field(default_factory=list)
    end_state: dict = field(default_factory=dict)
    metrics: dict = field(default_factory=dict)
    audit: list = field(default_factory=list)
    warnings: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _scaled(models: dict, multiplier: float) -> dict:
    return {
        market: CostModel(**{k: v * multiplier for k, v in asdict(model).items()})
        for market, model in models.items()
    }


def _round_lot(qty: float, lot: float) -> float:
    return round(math.floor(qty / lot + 1e-9) * lot, 8)


def run_backtest(dataset: Dataset, strategy: Strategy, config: BacktestConfig) -> BacktestResult:
    if not dataset.usable:
        raise DataRejected(f"dataset {dataset.data_version} failed validation: research BLOCKED")
    require_admitted(dataset)  # Batch #3A: real datasets need verified provenance + PASS validation
    bars = dataset.bars[: config.trade_end] if config.trade_end is not None else list(dataset.bars)
    if len(bars) < 2 or config.trade_start >= len(bars) - 1:
        raise ValueError("backtest window needs at least two bars after trade_start")

    strategy.reset()  # Audit fix (D7): no state from a previous run/dataset
    meta = dataset.meta
    market, symbol = meta.market_enum, meta.symbol
    ccy = Currency(meta.currency)
    key = (market.value, symbol)
    duration = meta.duration

    settings = load_research_settings(config.settings_path)
    base_models = cost_models_from_settings(settings)
    if config.zero_cost_fixture:
        models = {m: CostModel() for m in Market}
    else:
        models = _scaled(base_models, config.cost_multiplier)
    risk_cfg = {k: v for k, v in (settings.get("risk") or {}).items() if v is not None}
    risk_cfg.update(config.risk_overrides)
    fx = fx_provider_from_settings(settings)
    base = Currency(settings["base_currency"])

    marks: dict = {}
    clock = SimClock(bars[0].ts)
    ids = DeterministicIds()
    stack = build_paper_stack(
        starting_cash={ccy: float(config.initial_cash)},
        base_currency=base,
        fx_provider=fx,
        mode="PAPER",
        cost_models=models,
        broker_cost_models=models,
        min_net_alpha_bps=float((settings.get("net_alpha") or {}).get("min_net_alpha_bps", 0.0)),
        reservation_execution_buffer=config.reservation_buffer_pct,
        risk_kwargs={**risk_cfg, "mark_prices_fn": lambda: dict(marks)},
        enabled_markets=enabled_markets_from_settings(settings),
        # OD-02: universe = this run's validated dataset symbol, nothing else, and no
        # allow-all. The gate instance lives only inside this run.
        compliance_kwargs={"tradable_symbols": {symbol}, "unrestricted_universe": False,
                           "universe_label": f"research_run:{dataset.data_version}"},
        now_fn=clock.now,
        id_fn=ids,
    )
    ledger, broker = stack.ledger, stack.broker
    rates = broker.rates_for(market)
    lot = LOT_SIZE[market]

    result = BacktestResult(
        config=asdict(config),
        strategy=strategy.describe(),
        dataset=dataset.summary(),
        settings_meta=settings["_meta"],
        cost_models={m.value: asdict(cm) for m, cm in models.items()},
        labels={
            "data_source": meta.source,
            "synthetic_data": meta.synthetic,
            "alpha_mode": strategy.params["alpha_mode"],
            "zero_cost_fixture": config.zero_cost_fixture,
            "cost_numbers": "PLACEHOLDER (config/settings.yaml, not calibrated)",
            "execution_model": "Level-1 paper: next-bar-open fill, fixed half-spread+slippage bps, "
                               "no order book / queue / latency / market impact",
            "timestamp_semantics": "bar ts = open time (UTC); signal at close(t); earliest fill open(t+1)",
            "research_universe": research_universe(dataset),
            "net_alpha_exit_policy": "OD-01: exposure-reducing SELL is exempt from the Net Alpha threshold only",
            "reservation_buffer": f"{config.reservation_buffer_pct} (provisional policy value, not empirically validated)",
        },
    )
    if meta.synthetic:
        result.warnings.append("SYNTHETIC data: results say nothing about real Net Alpha")
    if strategy.params["alpha_mode"] == "fixture":
        result.warnings.append("FIXTURE expected return: not an alpha estimate")
    if config.zero_cost_fixture:
        result.warnings.append("ZERO-COST fixture run: costs deliberately disabled")

    pending: list[tuple[str, int]] = []
    orders_by_id: dict[str, dict] = {}
    prev_day = None
    start_equity = float(config.initial_cash)
    last_index = len(bars) - 1

    for t, bar in enumerate(bars):
        # ---- 1. fills at the open of bar t ------------------------------
        clock.set(bar.ts)
        if bar.ts.date() != prev_day:
            stack.risk.start_new_day()
            prev_day = bar.ts.date()
        marks[key] = bar.open
        for order_id, signal_t in pending:
            order = broker.orders[order_id]
            fill = broker.simulate_fill(order_id, bar.open)
            rec = orders_by_id[order_id]
            if fill is None:
                stack.cancel_order(order_id)
                rec["end_reason"] = "expired_unfilled_next_bar"
            else:
                outcome = stack.settle(fill)
                result.fills.append({
                    "fill_id": fill.fill_id, "order_id": order_id, "bar": t, "signal_bar": signal_t,
                    "timestamp": fill.timestamp.isoformat(), "side": fill.side.value,
                    "quantity": fill.quantity, "reference_price": bar.open, "price": fill.price,
                    "commission": fill.commission, "exchange_fee": fill.exchange_fee, "tax": fill.tax,
                    "fx_cost": fill.fx_cost, "slippage_estimate": fill.slippage_estimate,
                    "outcome": outcome.value,
                })
            rec["status"] = order.status.value
            rec["status_history"] = [s.value for s in order.status_history] + [order.status.value]
            rec["filled_quantity"] = order.filled_quantity
            rec["avg_fill_price"] = order.avg_fill_price
        pending = []

        # ---- 2. mark at the close of bar t -------------------------------
        close_time = bar.ts + duration
        clock.set(close_time)
        marks[key] = bar.close
        if t >= config.trade_start:
            val = ledger.valuation(marks, fx, base)
            row = val.by_currency.get(ccy, {})
            position = ledger.get_position(market, symbol)
            if val.total_base is not None:
                stack.risk.observe_equity(val.total_base)
            result.equity.append({
                "bar": t, "timestamp": close_time.isoformat(), "close": bar.close,
                "cash": row.get("cash"), "reserved": row.get("reserved"),
                "position_qty": position.quantity, "position_value": row.get("positions_value"),
                "equity": row.get("equity"), "equity_base": val.total_base,
            })

        # ---- 3. signal after close(t) ------------------------------------
        if t < config.trade_start or t >= last_index:
            continue
        position = ledger.get_position(market, symbol)
        holding = position.quantity > _EPS
        signal = strategy.decide(History(bars, t + 1), holding)
        if signal is None:
            continue
        if signal.action == ENTER_LONG:
            side = Side.BUY
            unit = bar.close * (1 + config.reservation_buffer_pct) * (1 + rates["slippage_bps"] / 1e4) \
                * (1 + rates["commission_rate"] + rates["exchange_fee_rate"])
            qty = _round_lot(config.position_fraction * ledger.available_cash(ccy) / unit, lot)
        elif signal.action == EXIT_LONG:
            side = Side.SELL
            qty = _round_lot(ledger.available_quantity(market, symbol), lot)
        else:  # pragma: no cover - strategies only emit the two actions
            continue
        base_record = {"bar": t, "timestamp": close_time.isoformat(), "action": signal.action,
                       "expected_gross_return": signal.expected_gross_return,
                       "alpha_source": signal.alpha_source, "evidence": signal.evidence}
        if qty < lot - _EPS:
            result.skipped.append({**base_record, "reason": "quantity_below_one_lot"})
            continue
        proposal = TradeProposal(
            market=market, symbol=symbol, side=side, quantity=qty,
            strategy_id=strategy.name, reason_code=signal.reason_code,
            confidence=signal.confidence, expected_gross_return=signal.expected_gross_return,
            expected_holding_period_seconds=int(duration.total_seconds() * strategy.params["horizon"]),
            created_at=close_time, signal_timestamp=close_time,
            strategy_version=strategy.version, config_version=settings["_meta"]["sha256"][:12],
            feature_snapshot_id=f"{dataset.data_version}@bar{t}", proposal_id=ids("P"),
        )
        res = stack.submit_trade_proposal(proposal, reference_price=bar.close)
        if res.accepted:
            pending.append((res.order_id, t))
            orders_by_id[res.order_id] = {
                "order_id": res.order_id, "proposal_id": proposal.proposal_id, "signal_bar": t,
                "timestamp": close_time.isoformat(), "side": side.value, "quantity": qty,
                "reference_price": bar.close, "order_type": proposal.order_type.value,
                "reservation": asdict(res.order.reservation) if res.order.reservation else None,
                "expected_gross_return": signal.expected_gross_return,
                "alpha_source": signal.alpha_source, "status": res.order.status.value,
            }
            result.orders.append(orders_by_id[res.order_id])
        else:
            result.rejections.append({**base_record, "side": side.value, "quantity": qty,
                                      "stage": res.stage, "reason": res.reason})

    # ---- end of window ------------------------------------------------------
    cancelled = []
    for order_id, _ in pending:  # defensive: the last bar never emits a signal
        stack.cancel_order(order_id)
        orders_by_id[order_id]["end_reason"] = "cancelled_at_end"
        cancelled.append(order_id)
    final_close = bars[-1].close
    position = ledger.get_position(market, symbol)
    result.breaches = list(ledger.reservation_breaches)
    result.end_state = {
        "policy": config.end_policy,
        "final_close": final_close,
        "open_position_qty": position.quantity,
        "open_position_avg_cost": position.avg_cost,
        "open_position_unrealized": (final_close - position.avg_cost) * position.quantity
        if position.quantity > _EPS else 0.0,
        "cash": ledger.cash[ccy],
        "reserved_cash": ledger.reserved_cash[ccy],
        "reserved_quantity": position.reserved_quantity,
        "realized_pnl": ledger.realized_pnl[ccy],
        "pending_orders_cancelled": cancelled,
        "reservation_breaches": len(ledger.reservation_breaches),
        "trading_halted_by_breach": bool(ledger.reservation_breaches),
    }
    if ledger.reservation_breaches:
        result.warnings.append("reservation breach recorded: Risk blocked all later orders (design 5.3)")
    result.trades = reconstruct_trades(result.fills, final_close)
    result.metrics = compute_metrics(
        starting_equity=start_equity, equity_curve=result.equity, fills=result.fills,
        trades=result.trades, rejections=result.rejections, currency=ccy.value,
    )
    result.metrics["skipped_signals"] = len(result.skipped)
    result.metrics["exit_rejections"] = sum(1 for r in result.rejections if r["action"] == EXIT_LONG)
    result.audit = stack.audit.dump()
    return result
