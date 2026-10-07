import os, sys, tempfile, pathlib, math, random
from dataclasses import replace
tmp = pathlib.Path(tempfile.mkdtemp())
os.environ["QAT_RESULTS_DIR"] = str(tmp / "results"); os.environ["QAT_STATE_DIR"] = str(tmp / "state")
sys.path[:0] = ["src", "tests"]
from conftest import make_proposal as mk
from qat.data.loader import load_dataset
from qat.data.bars import Bar
from qat.research.backtest import BacktestConfig, run_backtest
from qat.research.strategies import make_strategy
from qat.app import build_paper_stack
from qat.core.models import *
from qat.core.fx import StaticFXRateProvider

ds = load_dataset("data/fixtures/SYN_KR1_1d.csv")
fx = {"alpha_mode": "fixture", "fixture_expected_return": 0.01}
cfg = BacktestConfig()
scaled = replace(ds, bars=[Bar(b.ts, b.open * 2, b.high * 2, b.low * 2, b.close * 2, b.volume) for b in ds.bars], data_version="x2")
for name in ("ma_trend", "breakout", "mean_reversion"):
    reused = make_strategy(name, **fx)
    run_backtest(ds, reused, replace(cfg, trade_start=10, trade_end=300))   # n ends at ~299
    r_reused = run_backtest(scaled, reused, replace(cfg, trade_start=320, trade_end=500))  # first len 321 >= n
    r_fresh = run_backtest(scaled, make_strategy(name, **fx), replace(cfg, trade_start=320, trade_end=500))
    print(name, "reused-instance == fresh:", r_reused.fills == r_fresh.fills and r_reused.orders == r_fresh.orders)

print("== accounting invariants (random multi-currency sequences)")
rnd = random.Random(7)
bad = 0
for trial in range(60):
    fxp = StaticFXRateProvider({(Currency.USD, Currency.KRW): 1300.0})
    st = build_paper_stack(starting_cash={Currency.KRW: 5_000_000, Currency.USD: 5_000}, base_currency=Currency.KRW, fx_provider=fxp,
                           commission_rate=0.001, tax_rate_sell=0.002, slippage_bps=9, reservation_execution_buffer=0.02)
    init = {Currency.KRW: 5_000_000.0, Currency.USD: 5_000.0}
    marks = {}
    price = {"KR": 1000.0, "US": 100.0}
    sym = {"KR": "005930", "US": "AAPL"}
    cash_flow = {Currency.KRW: 0.0, Currency.USD: 0.0}
    fees = {Currency.KRW: 0.0, Currency.USD: 0.0}
    for step in range(25):
        mkt = rnd.choice(["KR", "US"]); market = Market(mkt); ccy = currency_for(market, sym[mkt])
        price[mkt] *= 1 + rnd.uniform(-0.03, 0.03); marks[(mkt, sym[mkt])] = price[mkt]
        side = rnd.choice([Side.BUY, Side.SELL]); q = rnd.randint(1, 30)
        res = st.submit_trade_proposal(mk(market, sym[mkt], side, q, strategy_id=f"s{step}", expected_gross_return=0.05), price[mkt])
        if not res.accepted: continue
        r = rnd.random()
        if r < 0.15:
            st.cancel_order(res.order_id); continue
        part = rnd.randint(1, q) if r < 0.5 and q > 1 else None
        fill = st.broker.simulate_fill(res.order_id, price[mkt] * (1 + rnd.uniform(-0.002, 0.002)), part)
        st.settle(fill)
        sign = -1 if side is Side.BUY else 1
        cash_flow[ccy] += sign * fill.gross - fill.total_cost
        fees[ccy] += fill.commission + fill.exchange_fee
        if r >= 0.85:
            try: st.cancel_order(res.order_id)
            except Exception: pass
    val = st.ledger.valuation(marks, fxp)
    for c in (Currency.KRW, Currency.USD):
        row = val.by_currency[c]
        cash_ok = abs(st.ledger.cash[c] - (init[c] + cash_flow[c])) < 1e-6
        pos_val = sum(p["market_value"] or 0 for p in val.positions if p["currency"] == c.value)
        eq_ok = abs(row["equity"] - (st.ledger.cash[c] + pos_val)) < 1e-6
        pnl_ok = abs((row["equity"] - init[c]) - (row["realized"] + row["unrealized"])) < 1e-6
        fee_ok = abs(st.ledger.fees[c] - fees[c]) < 1e-6
        res_ok = abs(st.ledger.reserved_cash[c] - sum(o.reservation.cash_remaining for o in st.broker.orders.values() if o.reservation and o.reservation.kind == "CASH" and o.reservation.currency == c and not o.is_terminal)) < 1e-6
        if not (cash_ok and eq_ok and pnl_ok and fee_ok and res_ok):
            bad += 1; print("VIOLATION", trial, c, cash_ok, eq_ok, pnl_ok, fee_ok, res_ok)
    base_total = sum(val.by_currency[c]["equity"] * fxp.rate(c, Currency.KRW) for c in (Currency.KRW, Currency.USD))
    if abs(val.total_base - base_total) > 1e-6: bad += 1; print("BASE TOTAL VIOLATION")
    # reserved positions vs open sell orders
    for (mk_, s_), pos in st.ledger.positions.items():
        exp = sum(o.reservation.quantity_remaining for o in st.broker.orders.values() if o.reservation and o.reservation.kind == "POSITION" and o.symbol == s_ and not o.is_terminal)
        if abs(pos.reserved_quantity - exp) > 1e-6: bad += 1; print("POS RESERVATION VIOLATION", trial)
print("violations:", bad)
