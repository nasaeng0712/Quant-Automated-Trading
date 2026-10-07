import os, sys, json, math, tempfile, pathlib, threading
tmp = pathlib.Path(tempfile.mkdtemp())
os.environ["QAT_RESULTS_DIR"] = str(tmp / "results"); os.environ["QAT_STATE_DIR"] = str(tmp / "state")
sys.path[:0] = ["src", "tests"]
from qat.ui.service import QATService
from qat.research.backtest import BacktestConfig, run_backtest
from qat.research.strategies import make_strategy
from qat.data.loader import load_dataset

KR = "data/fixtures/SYN_KR1_1d.csv"; US = "data/fixtures/SYN_US1_1d.csv"
print("== UI concurrent identical proposals, distinct request ids")
svc = QATService(); svc.start_session({"dataset_id": KR, "start_bar": 60})
res = []
def go(i): res.append(svc.submit_proposal({"side": "BUY", "quantity": 1, "expected_gross_return": 0.05, "client_request_id": f"c{i}"}))
ts = [threading.Thread(target=go, args=(i,)) for i in range(12)]
[t.start() for t in ts]; [t.join() for t in ts]
print("accepted:", sum(r["accepted"] for r in res), "orders:", len(svc.session.stack.broker.orders), "reserved:", svc.session.stack.ledger.reserved_cash["KRW"])

print("== FX missing (no rates) with total exposure limit, US dataset")
base_yaml = pathlib.Path("config/settings.yaml").read_text(encoding="utf-8")
nofx = base_yaml.replace("    - { from: USD, to: KRW, rate: 1350.0 }\n    - { from: USDT, to: KRW, rate: 1350.0 }\n", "").replace("rates:", "rates: []\n    #", 1)
p1 = tmp / "nofx.yaml"; p1.write_text(nofx.replace("max_total_exposure: null", "max_total_exposure: 0.9"), encoding="utf-8")
try:
    import yaml; print("fx rates parsed:", yaml.safe_load(p1.read_text(encoding="utf-8"))["fx"])
except Exception as e: print("yaml err", e)
s3 = QATService(str(p1)); s3.start_session({"dataset_id": US, "initial_cash": 10000, "start_bar": 60})
r = s3.submit_proposal({"side": "BUY", "quantity": 1, "expected_gross_return": 0.05, "client_request_id": "f1"})
print("limit configured + FX missing ->", r["accepted"], r["reason"][:90])
pf = s3.portfolio(); print("portfolio total_base:", pf["total_base"], "missing_fx:", pf["missing_fx"])
s4 = QATService(str(tmp / "nofx.yaml") if False else None)
p2 = tmp / "nofx2.yaml"; p2.write_text(nofx, encoding="utf-8")
s4 = QATService(str(p2)); s4.start_session({"dataset_id": US, "initial_cash": 10000, "start_bar": 60})
r = s4.submit_proposal({"side": "BUY", "quantity": 1, "expected_gross_return": 0.05, "client_request_id": "f2"})
print("no limits + FX missing ->", r["accepted"])
s4.advance({"bars": 2}); ov = s4.overview()["session"]
print("overview equity_base:", ov["equity_base"], "missing_fx:", ov["missing_fx"], "equity(ccy):", round(ov["equity"], 2))

print("== backtest drawdown limit, FX missing (US dataset, base KRW)")
p3 = tmp / "ddfx.yaml"; p3.write_text(nofx.replace("max_drawdown: null", "max_drawdown: 0.01"), encoding="utf-8")
ds = load_dataset(US)
res = run_backtest(ds, make_strategy("ma_trend", alpha_mode="fixture", fixture_expected_return=0.01), BacktestConfig(initial_cash=10000, settings_path=str(p3)))
print("fills:", res.metrics["fills"], "rejections by stage:", res.metrics["rejections_by_stage"], "min equity_base None?", all(p["equity_base"] is None for p in res.equity))
print("max drawdown observed", res.metrics["max_drawdown_pct"])

print("== daily loss reset (UI): realized loss on day N vs next day")
p5 = tmp / "dl.yaml"; p5.write_text(base_yaml.replace("daily_loss_limit: null", "daily_loss_limit: 5000"), encoding="utf-8")
s5 = QATService(str(p5)); s5.start_session({"dataset_id": KR, "start_bar": 100})
s5.submit_proposal({"side": "BUY", "quantity": 100, "expected_gross_return": 0.05, "client_request_id": "b1"}); s5.advance({"bars": 1})
for i in range(60):
    pos = s5.session.stack.ledger.get_position("KR", "SYNKR1")
    if pos.quantity and (s5.session.marks[s5.session.key] < pos.avg_cost * 0.97): break
    s5.advance({"bars": 1})
r = s5.submit_proposal({"side": "SELL", "quantity": 100, "expected_gross_return": 0.05, "client_request_id": "s1"}); s5.advance({"bars": 1})
print("realized after loss:", round(s5.session.stack.ledger.realized_pnl["KRW"]), "sell accepted:", r["accepted"])
s5.advance({"bars": 3})  # new days
r = s5.submit_proposal({"side": "BUY", "quantity": 1, "expected_gross_return": 0.05, "client_request_id": "b2"})
print("next-day BUY with daily limit 5000 after loss>5000 ->", r["accepted"], r["reason"][:70])
