import sys, math; sys.path[:0]=['src','tests']
from conftest import make_proposal as mk
from qat.app import build_paper_stack, build_paper_stack_from_settings
from qat.config import load_settings
from qat.core.models import *
from qat.core.fx import StaticFXRateProvider
from qat.portfolio.ledger import PortfolioLedger, LedgerError
from qat.portfolio.reconciliation import reconcile
import pathlib, tempfile
print('a2 missing explicit path ->', load_settings('nope.yaml'))
s=build_paper_stack_from_settings(starting_cash=10_000_000)
r=s.submit_trade_proposal(mk(Market.KR,'005930',Side.BUY,10,expected_gross_return=0.05),1000)
f=s.broker.simulate_fill(r.order_id,1000); print('a3 settings stack fill price/commission', f.price, f.commission, 'router', s.router.mode)
p=pathlib.Path(tempfile.mkdtemp())/'s.yaml'; p.write_text(open('config/settings.yaml',encoding='utf-8').read().replace('mode: paper','mode: live').replace('US: true','US: false'),encoding='utf-8')
s=build_paper_stack_from_settings(p,starting_cash=10_000_000); print('a4/a5 mode live in yaml -> router', s.router.mode, 'compliance', s.compliance.mode)
r=s.submit_trade_proposal(mk(Market.US,'AAPL',Side.BUY,1,expected_gross_return=0.05),10); print('   US disabled -> accepted?', r.accepted, r.reason)
l=PortfolioLedger({Currency.KRW:0,Currency.USD:0}); l.realized_pnl[Currency.USD]=10; print('b1 realized_pnl_total({}) =', l.realized_pnl_total({}))
l=PortfolioLedger({Currency.KRW:1000,Currency.USD:10}); print('b2 equity({}) =', l.equity({}))
l.positions[('KR','X')]=Position(2,100); print('   equity_in w/o marks uses avg_cost:', l.equity_in(Currency.KRW, StaticFXRateProvider({(Currency.USD,Currency.KRW):1})))
s=build_paper_stack(starting_cash=1_000_000,commission_rate=0.01,slippage_bps=100)
r=s.submit_trade_proposal(mk(Market.KR,'005930',Side.BUY,100),100); res=r.order.reservation.cash_reserved
f=s.broker.simulate_fill(r.order_id,200); s.settle(f); print('c1 reserved',res,'actual',f.gross+f.total_cost,'avail',s.ledger.available_cash('KRW'),'breaches',len(s.ledger.reservation_breaches))
s=build_paper_stack(starting_cash=1_000_000,commission_rate=0.01,slippage_bps=100)
r=s.submit_trade_proposal(mk(Market.KR,'005930',Side.BUY,100),100)
f=s.broker.simulate_fill(r.order_id,150,40); s.settle(f); print('c2 partial overrun actual',f.gross+f.total_cost,'slice',res*0.4,'breaches',len(s.ledger.reservation_breaches))
l=PortfolioLedger(1000); l.positions[('KR','X')]=Position(5,100)
print('d1', reconcile(l,broker_cash={Currency.KRW:1000},broker_positions={}).ok, 'd3 empty', reconcile(l,broker_cash={},broker_positions={}).ok)
l=PortfolioLedger({Currency.KRW:1000,Currency.USD:50}); print('d2', reconcile(l,broker_cash={Currency.KRW:1000},broker_positions={}).ok)
s=build_paper_stack(starting_cash=1_000_000)
r=s.submit_trade_proposal(mk(Market.KR,'005930',Side.BUY,10),100); o=s.broker.orders[r.order_id]
bad=Fill(order_id=o.order_id,market=Market.KR,symbol='000660',side=Side.BUY,quantity=11,price=100,currency=Currency.KRW)
print('e1/e2 mismatched symbol qty11 ->', s.settlement.apply_fill(o,bad), s.ledger.get_position('KR','000660').quantity)
l=PortfolioLedger(1000)
try: l.apply_fill(Fill(order_id='x',market=Market.KR,symbol='X',side=Side.SELL,quantity=1,price=100,currency=Currency.KRW,commission=1,tax=2))
except LedgerError: print('e3 fees after failed sell', dict(l.fees), dict(l.taxes))
s=build_paper_stack(starting_cash=1_000_000)
b=s.submit_trade_proposal(mk(Market.KR,'005930',Side.BUY,10),100); s.settle(s.broker.simulate_fill(b.order_id,100))
se=s.submit_trade_proposal(mk(Market.KR,'005930',Side.SELL,10),100); f=s.broker.simulate_fill(se.order_id,100)
s.ledger.positions[('KR','005930')].quantity=5
try: s.settle(f)
except LedgerError: print('e4 reserved qty after ledger failure', s.ledger.get_position('KR','005930').reserved_quantity, '(was 10)')
p=mk(Market.KR,'005930',Side.BUY,math.nan); print('f1 NaN qty proposal constructed:', p.quantity)
print('f2 NaN fill:', Fill(order_id='x',market=Market.KR,symbol='X',side=Side.BUY,quantity=1,price=math.nan,currency=Currency.KRW).price)
print('f3 NaN fx:', StaticFXRateProvider({('USD','KRW'):math.nan}).rate('USD','KRW'))
s=build_paper_stack(starting_cash=1_000_000); r=s.submit_trade_proposal(mk(Market.KR,'005930',Side.BUY,1),math.nan); print('f4 NaN ref accepted:', r.accepted, r.reason)
r=s.submit_trade_proposal(mk(Market.KR,'005931',Side.BUY,1,expected_gross_return=math.inf),100); print('f5 inf expected return accepted:', r.accepted)
