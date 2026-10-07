import os, sys, json, math, tempfile, pathlib, threading, traceback
tmp = pathlib.Path(tempfile.mkdtemp())
os.environ["QAT_RESULTS_DIR"] = str(tmp / "results")
os.environ["QAT_STATE_DIR"] = str(tmp / "state")
sys.path[:0] = ["src", "tests"]
from conftest import make_proposal as mk
from qat.core.models import *
from qat.portfolio.ledger import PortfolioLedger
from qat.portfolio.reconciliation import reconcile
from qat.ui.service import QATService, ServiceError, ConflictError

KR = "data/fixtures/SYN_KR1_1d.csv"
def attempt(label, fn):
    try:
        out = fn()
        print(f"{label}: NO ERROR -> {str(out)[:110]}")
    except (ServiceError, ConflictError) as e:
        print(f"{label}: clean {type(e).__name__}: {str(e)[:80]}")
    except Exception as e:
        print(f"{label}: UNCLEAN {type(e).__name__}: {str(e)[:80]}")

print("== P1 reconcile non-finite snapshot")
l = PortfolioLedger(1000)
print("nan cash ->", reconcile(l, broker_cash={Currency.KRW: math.nan}, broker_positions={}).ok)
l.positions[("KR", "X")] = Position(5, 100)
print("nan pos ->", reconcile(l, broker_cash={Currency.KRW: 1000}, broker_positions={("KR", "X"): math.nan}).ok)
print("inf pos ->", reconcile(l, broker_cash={Currency.KRW: 1000}, broker_positions={("KR", "X"): math.inf}).ok)
print("nan tol ->", reconcile(l, broker_cash={Currency.KRW: 999}, broker_positions={("KR", "X"): 5}, tol=math.nan).ok)

print("== P2 payload robustness")
svc = QATService()
for label, body in [("start_bar inf", {"dataset_id": KR, "start_bar": math.inf}),
                    ("start_bar nan", {"dataset_id": KR, "start_bar": math.nan}),
                    ("cash inf", {"dataset_id": KR, "initial_cash": math.inf}),
                    ("cash str", {"dataset_id": KR, "initial_cash": "abc"}),
                    ("start_bar neg", {"dataset_id": KR, "start_bar": -1})]:
    attempt("session " + label, lambda b=body: svc.start_session(b))
svc.start_session({"dataset_id": KR, "start_bar": 60})
attempt("advance inf", lambda: svc.advance({"bars": math.inf}))
attempt("advance nan", lambda: svc.advance({"bars": math.nan}))
for label, extra in [("qty nan", {"quantity": math.nan}), ("qty inf", {"quantity": math.inf}), ("qty -1", {"quantity": -1}),
                     ("qty str", {"quantity": "x"}), ("qty bool", {"quantity": True}), ("exp nan", {"expected_gross_return": math.nan}),
                     ("limit nan MARKET", {"limit_price": math.nan}), ("limit inf LIMIT", {"order_type": "LIMIT", "limit_price": math.inf}),
                     ("side bad", {"side": "HOLD"}), ("type bad", {"order_type": "STOP"})]:
    body = {"side": "BUY", "quantity": 1, "expected_gross_return": 0.05, "client_request_id": "x-" + label, **extra}
    attempt("proposal " + label, lambda b=body: svc.submit_proposal(b))
print("orders after garbage:", len(svc.session.stack.broker.orders))
base = {"dataset_id": KR, "strategy": "ma_trend"}
for label, extra in [("train inf", {"train_bars": math.inf}), ("train nan", {"train_bars": math.nan}), ("cash 1e308", {"initial_cash": 1e308}),
                     ("params fast 0", {"params": {"fast": 0}}), ("params slow 0", {"params": {"slow": 0}}),
                     ("params fast nan", {"params": {"fast": math.nan}}), ("params fast neg", {"params": {"fast": -5}}),
                     ("params horizon 0", {"params": {"horizon": 0}}), ("params fast str", {"params": {"fast": "abc"}}),
                     ("params horizon 1e9", {"params": {"horizon": 10**9}}), ("params fast 1.5", {"params": {"fast": 1.5}}),
                     ("params fast huge", {"params": {"fast": 10**12, "slow": 10**13}})]:
    attempt("backtest " + label, lambda e=extra: svc.run_backtest({**base, **e}))
    if "train" in label:
        attempt("walkforward " + label, lambda e=extra: svc.run_walkforward({**base, **e}))

print("== P4 risk hooks in UI session")
sp = tmp / "dd.yaml"
sp.write_text(pathlib.Path("config/settings.yaml").read_text(encoding="utf-8").replace("max_drawdown: null", "max_drawdown: 0.001").replace("daily_loss_limit: null", "daily_loss_limit: 1000"), encoding="utf-8")
s2 = QATService(str(sp))
s2.start_session({"dataset_id": KR, "start_bar": 100})
r = s2.submit_proposal({"side": "BUY", "quantity": 100, "expected_gross_return": 0.05, "client_request_id": "d1"})
print("buy accepted", r["accepted"], r["reason"])
worst = None
for i in range(40):
    s2.advance({"bars": 1})
    ov = s2.overview()["session"]
    if ov["drawdown"] and ov["drawdown"] > 0.02:
        break
print("drawdown displayed", ov["drawdown"], "net_pnl", ov["net_pnl"], "realized", ov["realized"])
r2 = s2.submit_proposal({"side": "BUY", "quantity": 1, "expected_gross_return": 0.05, "client_request_id": "d2"})
print("2nd order with drawdown 0.1% limit ->", r2["accepted"], r2["reason"])
from qat.risk.gate import RiskGate
rg = RiskGate(PortfolioLedger(1000), max_drawdown=0.01)
print("RiskGate drawdown configured, no baseline ->", rg.evaluate(mk(Market.KR, "X", Side.BUY, 1), 10).describe())
