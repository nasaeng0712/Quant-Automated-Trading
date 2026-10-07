"""Batch #2.2 - Risk-reducing exit hierarchy (Control Tower decision).

Every scenario here uses a Long-100 position in the authoritative Ledger. "Reducing" is
decided by the server from that Ledger (qat.core.exposure.reduces_exposure); nothing the
client says can claim it. Kill Switch is the top-level hard stop and is NOT relaxed.
Kill Switch does not imply automatic liquidation - no flatten / recovery feature exists.
"""

from __future__ import annotations

import math
import pathlib

import pytest

from conftest import make_proposal as mk

from qat.app import build_paper_stack
from qat.cost.engine import CostModel
from qat.core.fx import StaticFXRateProvider
from qat.core.models import Currency, Fill, Market, OrderStatus, Position, Side, TradeProposal
from qat.data.loader import load_dataset
from qat.portfolio.ledger import LedgerError
from qat.portfolio.reconciliation import reconcile
from qat.research.backtest import BacktestConfig, run_backtest
from qat.research.strategies import EXIT_LONG, make_strategy
from qat.ui.service import QATService, ServiceError

ROOT = pathlib.Path(__file__).resolve().parents[1]
COSTS = {Market.KR: CostModel(commission_rate=0.001, tax_rate_sell=0.002, half_spread_bps=5, slippage_bps=5)}
KRW = Currency.KRW


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("QAT_RESULTS_DIR", str(tmp_path / "results"))
    monkeypatch.setenv("QAT_STATE_DIR", str(tmp_path / "state"))


def held(qty=100, stack_kwargs=None, **risk_kwargs):
    stack = build_paper_stack(starting_cash=1_000_000, cost_models=COSTS, risk_kwargs=risk_kwargs,
                              **(stack_kwargs or {}))
    stack.ledger.positions[("KR", "005930")] = Position(quantity=qty, avg_cost=1000.0)
    return stack


def sell(stack, qty, exp=0.0, sid="exit"):
    return stack.submit_trade_proposal(
        mk(Market.KR, "005930", Side.SELL, qty, expected_gross_return=exp, strategy_id=sid), 1000)


def buy(stack, qty=1, exp=0.05, sid="add"):
    return stack.submit_trade_proposal(
        mk(Market.KR, "005930", Side.BUY, qty, expected_gross_return=exp, strategy_id=sid), 1000)


def risk_reasons(stack):
    return [r.payload["reasons"] for r in stack.audit.records if r.stage == "risk_decision"][-1]


def breach(stack, **detail):
    return stack.ledger.record_reservation_breach(
        KRW, 1.0, order_id="o", fill_id="f", actual_cost=110.0, reservation_allocated=100.0, overrun=10.0, **detail)


# ================================================================ Kill Switch: hard stop, no exception
def test_kill_switch_blocks_reducing_non_reducing_and_buy():
    stack = held(kill_switch=True)
    for res in (sell(stack, 50), sell(stack, 100), sell(stack, 101, exp=0.05), buy(stack)):
        assert not res.accepted and res.stage == "risk" and "kill_switch" in res.reason
    assert stack.broker.orders == {} and stack.ledger.get_position(Market.KR, "005930").reserved_quantity == 0


def test_kill_switch_engaged_after_a_reducing_order_was_accepted_blocks_the_next_one():
    stack = held()
    assert sell(stack, 30).accepted
    stack.risk.trip_kill_switch(True)
    assert "kill_switch" in sell(stack, 30, sid="exit2").reason


def test_kill_switch_does_not_imply_liquidation():
    stack = held(kill_switch=True)
    assert not hasattr(stack, "flatten") and not hasattr(stack.risk, "flatten")
    sell(stack, 100)
    assert stack.ledger.get_position(Market.KR, "005930").quantity == 100  # nothing was liquidated


# ================================================================ Daily Loss
def daily_loss_stack(**kw):
    stack = held(daily_loss_limit=1000, **kw)
    stack.risk.start_new_day()
    stack.ledger.realized_pnl[KRW] = -5000.0
    return stack


@pytest.mark.parametrize("qty", [50, 100])
def test_daily_loss_breach_does_not_block_a_reducing_sell(qty):
    stack = daily_loss_stack()
    res = sell(stack, qty)
    assert res.accepted, res.reason
    assert "exit_allowed:daily_loss_limit" in risk_reasons(stack)  # the relaxation is audited


def test_daily_loss_breach_still_blocks_buy_and_non_reducing_sell():
    stack = daily_loss_stack()
    blocked = buy(stack)
    assert not blocked.accepted and blocked.stage == "risk" and "daily_loss_limit" in blocked.reason
    over = sell(stack, 101)  # exp 0 -> no Net Alpha exemption either
    assert not over.accepted and over.stage == "net_alpha"
    over_strong = sell(stack, 101, exp=0.05)  # clears Net Alpha; NOT reducing -> daily loss still applies
    assert not over_strong.accepted and "daily_loss_limit" in over_strong.reason
    assert stack.ledger.get_position(Market.KR, "005930").quantity == 100


def test_daily_loss_with_unknown_fx_stays_unknown_for_a_reducing_sell():
    stack = held(daily_loss_limit=1000)
    stack.ledger.cash[Currency.USD] = 10.0
    stack.ledger.realized_pnl[Currency.USD] = -500.0  # needs USD->KRW, no rate configured
    stack.risk.start_new_day()
    res = sell(stack, 50)
    assert not res.accepted and res.stage == "risk" and "fx_unavailable" in res.reason


def test_non_finite_daily_loss_is_unknown_not_a_silent_pass():
    stack = held(daily_loss_limit=1000)
    stack.risk.start_new_day()
    stack.ledger.realized_pnl[KRW] = math.nan  # loss becomes NaN: `nan >= limit` is False
    for res in (sell(stack, 50), buy(stack)):
        assert not res.accepted and res.stage == "risk" and "daily_loss_not_finite" in res.reason
    assert {r.payload["status"] for r in stack.audit.records if r.stage == "risk_decision"} == {"UNKNOWN"}
    assert stack.broker.orders == {}


# ================================================================ Drawdown
def drawdown_stack(**kw):
    stack = held(max_drawdown=0.10, **kw)
    stack.risk.observe_equity(1_200_000)
    stack.ledger.cash[KRW] = 800_000.0  # equity 900,000 -> 25% below peak
    return stack


def test_drawdown_breach_does_not_block_a_reducing_sell_but_blocks_buy():
    stack = drawdown_stack()
    res = sell(stack, 50)
    assert res.accepted, res.reason
    assert "exit_allowed:max_drawdown" in risk_reasons(stack)
    blocked = buy(stack)
    assert not blocked.accepted and "max_drawdown" in blocked.reason


def test_drawdown_unknown_keeps_unknown_semantics_for_a_reducing_sell():
    no_baseline = held(max_drawdown=0.10)
    res = sell(no_baseline, 50)
    assert not res.accepted and res.stage == "risk" and "drawdown_baseline_unknown" in res.reason
    assert [r.payload["status"] for r in no_baseline.audit.records if r.stage == "risk_decision"] == ["UNKNOWN"]
    fx = held(max_drawdown=0.10)
    fx.ledger.cash[Currency.USD] = 10.0  # cannot value USD -> drawdown cannot be computed
    fx.risk.observe_equity(1_000_000)
    res = sell(fx, 50)
    assert not res.accepted and "fx_unavailable" in res.reason


def test_non_finite_drawdown_is_unknown_not_a_silent_pass():
    stack = held(max_drawdown=0.10)
    stack.risk.observe_equity(1_000_000)
    stack.ledger.positions[("KR", "005930")].avg_cost = math.nan  # equity becomes NaN
    res = sell(stack, 50)
    assert not res.accepted and res.stage == "risk"
    res = buy(stack)
    assert not res.accepted and res.stage == "risk"


# ================================================================ Absolute exposure limit
def test_absolute_exposure_limit_does_not_block_reducing_sells():
    # exposure 150,000 against a 100,000 limit
    for qty in (30, 70, 150):  # -> 120k, 80k, 0
        stack = held(qty=150, max_base_exposure=100_000)
        res = sell(stack, qty)
        assert res.accepted, (qty, res.reason)
        assert "exit_allowed:max_base_exposure" in risk_reasons(stack)
    stack = held(qty=150, max_base_exposure=100_000)
    blocked = buy(stack)  # 150k -> 151k
    assert not blocked.accepted and "max_base_exposure" in blocked.reason


def test_exposure_limit_relaxation_never_lets_a_sell_exceed_the_position():
    stack = held(qty=150, max_base_exposure=100_000)
    res = sell(stack, 151, exp=0.05)
    assert not res.accepted and "max_base_exposure" in res.reason  # not reducing -> limit applies
    assert stack.ledger.get_position(Market.KR, "005930").quantity == 150


# ================================================================ Reservation breach
def test_reservation_overrun_alone_allows_a_reducing_sell_and_blocks_buy():
    stack = held()
    breach(stack)
    res = sell(stack, 50)
    assert res.accepted, res.reason
    assert any(r.startswith("exit_allowed:reservation_overrun") for r in risk_reasons(stack))
    blocked = buy(stack)
    assert not blocked.accepted and "reservation_breach" in blocked.reason
    assert stack.ledger.reservation_breaches  # the breach is never hidden or cleared by an exit


def test_real_overrun_end_to_end_exit_then_still_halted_for_new_risk():
    stack = build_paper_stack(starting_cash=1_000_000, commission_rate=0.01, slippage_bps=100)
    entry = stack.submit_trade_proposal(mk(Market.KR, "005930", Side.BUY, 100, strategy_id="entry"), 100)
    stack.settle(stack.broker.simulate_fill(entry.order_id, 200))  # gap -> reservation overrun
    assert len(stack.ledger.reservation_breaches) == 1
    exit_ = stack.submit_trade_proposal(mk(Market.KR, "005930", Side.SELL, 100, strategy_id="exit"), 200)
    assert exit_.accepted, exit_.reason
    stack.settle(stack.broker.simulate_fill(exit_.order_id, 200))
    assert stack.ledger.get_position(Market.KR, "005930").quantity == 0
    assert stack.ledger.reservation_breaches  # still recorded
    again = stack.submit_trade_proposal(mk(Market.KR, "005930", Side.BUY, 1, strategy_id="re"), 200)
    assert not again.accepted and "reservation_breach" in again.reason


def test_overrun_with_reconciliation_mismatch_blocks_the_exit_until_a_clean_reconciliation():
    stack = held()
    breach(stack)
    bad = reconcile(stack.ledger, broker_cash={KRW: 1.0}, broker_positions={("KR", "005930"): 100})
    assert not bad.ok
    res = sell(stack, 50)
    assert not res.accepted and res.stage == "risk" and "reconciliation_mismatch" in res.reason
    good = reconcile(stack.ledger, broker_cash={KRW: stack.ledger.cash[KRW]},
                     broker_positions={("KR", "005930"): 100})
    assert good.ok
    assert sell(stack, 50, sid="exit2").accepted  # mismatch resolved by an actual reconciliation


def test_require_reconciliation_makes_a_never_reconciled_breach_unknown():
    stack = held(stack_kwargs=None, require_reconciliation=True)
    breach(stack)
    res = sell(stack, 50)
    assert not res.accepted and "reconciliation_not_performed" in res.reason
    assert [r.payload["status"] for r in stack.audit.records if r.stage == "risk_decision"] == ["UNKNOWN"]
    reconcile(stack.ledger, broker_cash={KRW: stack.ledger.cash[KRW]}, broker_positions={("KR", "005930"): 100})
    assert sell(stack, 50, sid="exit2").accepted


@pytest.mark.parametrize("corrupt,label", [
    (lambda s: s.ledger.cash.__setitem__(KRW, math.nan), "non_finite:cash"),
    (lambda s: s.ledger.realized_pnl.__setitem__(KRW, math.inf), "non_finite:realized_pnl"),
    (lambda s: setattr(s.ledger.positions[("KR", "005930")], "avg_cost", math.nan), "non_finite:position"),
    (lambda s: setattr(s.ledger.positions[("KR", "005930")], "reserved_quantity", 500.0),
     "reserved_quantity_inconsistent"),
    (lambda s: s.ledger.positions.__setitem__(("KR", "000660"), Position(quantity=-5.0, avg_cost=10.0)),
     "negative_position"),
    (lambda s: s.ledger.reserved_cash.__setitem__(KRW, -50.0), "negative_reserved_cash"),
    (lambda s: s.ledger.reservation_breaches[0].__setitem__("overrun", math.nan), "non_finite:breach"),
])
def test_overrun_with_untrustworthy_accounting_blocks_the_exit(corrupt, label):
    stack = held()
    breach(stack)
    corrupt(stack)
    assert any(label in p for p in stack.ledger.integrity_problems()), stack.ledger.integrity_problems()
    res = sell(stack, 50)
    assert not res.accepted and stack.broker.orders == {}
    if label != "reserved_quantity_inconsistent":
        # (an over-reserved position has no available quantity, so the SELL is not "reducing"
        # at all and is refused earlier; every other corruption reaches the breach rule)
        assert res.stage == "risk" and "accounting_untrusted" in res.reason


def test_overrun_with_unknown_fx_for_authoritative_valuation_is_unknown():
    stack = held()
    breach(stack)
    stack.ledger.cash[Currency.USD] = 10.0  # cannot be valued without a USD->KRW rate
    res = sell(stack, 50)
    assert not res.accepted and [r.payload["status"] for r in stack.audit.records if r.stage == "risk_decision"] == ["UNKNOWN"]
    assert "fx_unavailable" in res.reason
    ok = held(stack_kwargs={"fx_provider": StaticFXRateProvider({(Currency.USD, KRW): 1300.0})})
    breach(ok)
    ok.ledger.cash[Currency.USD] = 10.0
    assert sell(ok, 50).accepted  # with an explicit rate the valuation is authoritative


def test_a_breach_that_is_not_a_plain_overrun_blocks_the_exit():
    stack = held()
    stack.ledger.reservation_breaches.append({"kind": "something_else", "overrun": 1.0})
    res = sell(stack, 50)
    assert not res.accepted and "not_an_overrun" in res.reason


def test_settlement_integrity_problem_blocks_the_exit_until_resolved_with_a_clean_reconciliation():
    stack = build_paper_stack(starting_cash=1_000_000, commission_rate=0.01, slippage_bps=100)
    entry = stack.submit_trade_proposal(mk(Market.KR, "005930", Side.BUY, 100, strategy_id="entry"), 100)
    stack.settle(stack.broker.simulate_fill(entry.order_id, 200))  # overrun breach
    open_exit = stack.submit_trade_proposal(mk(Market.KR, "005930", Side.SELL, 50, strategy_id="exit"), 200)
    assert open_exit.accepted, open_exit.reason  # plain overrun, consistent books -> allowed
    order = stack.broker.orders[open_exit.order_id]
    foreign = Fill(order_id=order.order_id, market=Market.KR, symbol="000660", side=Side.SELL,
                   quantity=1, price=100, currency=KRW)  # does not belong to this order
    assert stack.settlement.apply_fill(order, foreign).value == "REJECTED_MISMATCH"
    assert any("settlement:fill_order_mismatch" in p for p in stack.ledger.integrity_problems())
    res = stack.submit_trade_proposal(mk(Market.KR, "005930", Side.SELL, 10, strategy_id="exit2"), 200)
    assert not res.accepted and "accounting_untrusted" in res.reason
    bad = reconcile(stack.ledger, broker_cash={KRW: 1.0}, broker_positions={})
    with pytest.raises(LedgerError):
        stack.ledger.resolve_integrity_issues(bad, approver="tester", note="x")
    good = reconcile(stack.ledger, broker_cash={KRW: stack.ledger.cash[KRW]},
                     broker_positions={("KR", "005930"): 100})
    assert good.ok
    with pytest.raises(LedgerError):
        stack.ledger.resolve_integrity_issues(good, approver="", note="x")
    stack.ledger.resolve_integrity_issues(good, approver="tester", note="reviewed")
    assert stack.submit_trade_proposal(mk(Market.KR, "005930", Side.SELL, 10, strategy_id="exit3"), 200).accepted


def test_limit_relaxations_are_not_granted_on_untrustworthy_accounting():
    stack = daily_loss_stack()
    stack.ledger.reserved_cash[KRW] = -50.0  # books inconsistent -> no relaxation, old blocking behaviour
    res = sell(stack, 50)
    assert not res.accepted and res.stage == "risk" and "daily_loss_limit" in res.reason


# ================================================================ order size / notional
def test_reducing_sell_over_the_order_notional_limit_is_blocked_and_never_split():
    stack = held(max_order_notional=30_000)
    big = sell(stack, 100)  # 100 x 1000 = 100,000 > 30,000
    assert not big.accepted and big.stage == "risk" and "max_order_notional" in big.reason
    assert stack.broker.orders == {}  # rejected, and no automatic split order was created
    small = sell(stack, 20, sid="exit2")
    assert small.accepted
    assert len(stack.broker.orders) == 1


def test_order_notional_limit_still_binds_when_daily_loss_is_breached():
    stack = held(daily_loss_limit=1000, max_order_notional=30_000)
    stack.risk.start_new_day()
    stack.ledger.realized_pnl[KRW] = -5000.0
    assert "max_order_notional" in sell(stack, 100).reason
    assert sell(stack, 20, sid="exit2").accepted


# ================================================================ other gates are never bypassed
def test_compliance_block_wins_over_a_relaxed_reducing_sell():
    stack = daily_loss_stack()
    stack.compliance.halted_symbols.add("005930")
    res = sell(stack, 50)
    assert not res.accepted and res.stage == "compliance" and "trading_halt" in res.reason
    assert stack.broker.orders == {}


def test_market_integrity_block_wins_over_a_relaxed_reducing_sell():
    stack = held(daily_loss_limit=1000)
    assert buy(stack, qty=1).accepted  # open BUY -> a SELL is an opposing order
    stack.risk.start_new_day()
    stack.ledger.realized_pnl[KRW] = -5000.0
    res = sell(stack, 50)
    assert not res.accepted and res.stage == "integrity" and "opposing_order" in res.reason


def test_relaxed_exit_still_flows_through_settlement_and_releases_reservations():
    stack = held(stack_kwargs={"commission_rate": 0.001, "tax_rate_sell": 0.002}, daily_loss_limit=1000)
    stack.risk.start_new_day()
    stack.ledger.realized_pnl[KRW] = -5000.0
    res = sell(stack, 100)
    assert res.accepted
    pos = stack.ledger.get_position(Market.KR, "005930")
    assert pos.reserved_quantity == 100 and pos.available_quantity == 0
    cash_before = stack.ledger.cash[KRW]
    fill = stack.broker.simulate_fill(res.order_id, 1000)
    assert stack.settle(fill).value == "APPLIED"
    pos = stack.ledger.get_position(Market.KR, "005930")
    assert pos.quantity == 0 and pos.reserved_quantity == 0
    assert stack.broker.orders[res.order_id].status is OrderStatus.FILLED
    # execution cost is charged exactly as configured - the relaxation never waives it
    assert fill.commission == pytest.approx(fill.gross * 0.001) and fill.tax == pytest.approx(fill.gross * 0.002)
    assert stack.ledger.cash[KRW] == pytest.approx(cash_before + fill.gross - fill.commission - fill.tax)
    assert any(r.stage == "fill_settled" for r in stack.audit.records)


def test_reserved_quantity_cannot_be_relaxed_twice():
    stack = daily_loss_stack()
    assert sell(stack, 100).accepted
    second = sell(stack, 1, sid="exit2")  # all 100 shares already reserved -> not reducing
    assert not second.accepted


# ================================================================ client cannot claim reducing status
def test_client_supplied_flags_are_never_trusted():
    with pytest.raises(TypeError):
        TradeProposal(market=Market.KR, symbol="005930", side=Side.BUY, quantity=1, reduce_only=True)  # type: ignore[call-arg]
    stack = daily_loss_stack()
    forged_buy = mk(Market.KR, "005930", Side.BUY, 1, expected_gross_return=0.05, strategy_id="forge")
    object.__setattr__(forged_buy, "reduce_only", True)  # bypassing the dataclass on purpose
    object.__setattr__(forged_buy, "risk_reducing", True)
    res = stack.submit_trade_proposal(forged_buy, 1000)
    assert not res.accepted and "daily_loss_limit" in res.reason
    forged_over = mk(Market.KR, "005930", Side.SELL, 101, expected_gross_return=0.05, strategy_id="forge2")
    object.__setattr__(forged_over, "exit", True)
    res = stack.submit_trade_proposal(forged_over, 1000)
    assert not res.accepted and "daily_loss_limit" in res.reason
    kill = held(kill_switch=True)
    forged_sell = mk(Market.KR, "005930", Side.SELL, 50, strategy_id="forge3")
    object.__setattr__(forged_sell, "reduce_only", True)
    assert "kill_switch" in kill.submit_trade_proposal(forged_sell, 1000).reason


def test_ui_rejects_reducing_claims():
    svc = QATService()
    svc.start_session({"dataset_id": "data/fixtures/SYN_KR1_1d.csv", "start_bar": 60})
    for field in ("exit", "reduce_only", "risk_reducing", "reducing", "flatten"):
        with pytest.raises(ServiceError):
            svc.submit_proposal({"side": "SELL", "quantity": 1, "client_request_id": f"r-{field}", field: True})


def test_ui_risk_view_reports_the_policy_and_integrity_state():
    svc = QATService()
    svc.start_session({"dataset_id": "data/fixtures/SYN_KR1_1d.csv", "start_bar": 60})
    view = svc.risk()
    rules = {r["rule"]: r["reducing_sell"] for r in view["exit_policy"]}
    assert rules["Kill Switch"] == "BLOCK" and rules["Daily Loss"] == "ALLOWED" and rules["Reservation breach"] == "CONDITIONAL"
    assert view["accounting_integrity"] == []
    assert "CLOSED" in svc.settings_view()["policies"]["risk_reducing_exit_hierarchy"]
    svc.session.stack.ledger.cash[KRW] = math.nan
    assert any("non_finite:cash" in p for p in svc.risk()["accounting_integrity"])


# ================================================================ end-to-end research
def test_backtest_exits_are_not_trapped_by_drawdown_limit(tmp_path):
    base = (ROOT / "config" / "settings.yaml").read_text(encoding="utf-8")
    path = tmp_path / "dd.yaml"
    path.write_text(base.replace("max_drawdown: null", "max_drawdown: 0.05"), encoding="utf-8")
    res = run_backtest(load_dataset(ROOT / "data/fixtures/SYN_KR1_1d.csv"),
                       make_strategy("ma_trend", alpha_mode="fixture", fixture_expected_return=0.01),
                       BacktestConfig(settings_path=str(path)))
    dd_blocked = [r for r in res.rejections if "max_drawdown" in r["reason"]]
    assert dd_blocked, "the drawdown limit must still bind risk-increasing entries"
    assert all(r["action"] != EXIT_LONG for r in dd_blocked)  # exits are never blocked by it
    assert res.end_state["open_position_qty"] == 0 or res.metrics["trades_closed"] > 0
