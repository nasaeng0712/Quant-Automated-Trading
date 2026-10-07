import sys, threading
sys.path[:0] = ["src", "tests"]
sys.setswitchinterval(1e-6)
from conftest import make_proposal as mk
from qat.app import build_paper_stack
from qat.core.models import *

dup = 0
trials = 300
for _ in range(trials):
    st = build_paper_stack(starting_cash=1_000_000)
    r = st.submit_trade_proposal(mk(Market.KR, "005930", Side.BUY, 10), 100)
    fill = st.broker.simulate_fill(r.order_id, 100)
    outs = []
    def go(): outs.append(st.settle(fill))
    ts = [threading.Thread(target=go) for _ in range(6)]
    [t.start() for t in ts]; [t.join() for t in ts]
    applied = sum(1 for o in outs if o.value == "APPLIED")
    qty = st.ledger.get_position(Market.KR, "005930").quantity
    if applied != 1 or abs(qty - 10) > 1e-9:
        dup += 1
print(f"concurrent settle of one fill: {dup}/{trials} trials double-applied")
