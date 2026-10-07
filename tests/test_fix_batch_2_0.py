"""Fix Batch #2.0 - reproduction of the hand-over static-review candidates (가~사).

Each test asserts the EXPECTED contract. Run before the fix: a FAIL reproduces
the candidate defect; a PASS on the unmodified code means NOT REPRODUCED for the
tested condition. Results are recorded in docs/QAT_검증_이력.md (Batch #2.0).
"""

from __future__ import annotations

import math
import pathlib
import threading

import pytest

from conftest import FIXED_NOW, make_proposal as mkprop

from qat.app import build_paper_stack, build_paper_stack_from_settings
from qat.core.fx import InvalidFXRateError, MissingFXRateError, StaticFXRateProvider
from qat.core.models import (
    Currency,
    DomainError,
    Fill,
    Market,
    Position,
    Side,
)
from qat.execution.settlement import FillOutcome
from qat.portfolio.ledger import LedgerError, PortfolioLedger
from qat.portfolio.reconciliation import reconcile

ROOT = pathlib.Path(__file__).resolve().parents[1]

UNIQUE_SETTINGS = """
project: {name: QAT, mode: paper}
markets: {KR: true, US: false, CRYPTO: true}
execution: {live_enabled: false, deterministic_paper_mode: true}
base_currency: KRW
fx:
  rates:
    - {from: USD, to: KRW, rate: 1234.5}
costs:
  KR: {commission_rate: 0.0007, tax_rate_sell: 0.0031, half_spread_bps: 3, slippage_bps: 4, fx_cost_bps: 0}
  US: {commission_rate: 0.0011, tax_rate_sell: 0.0, half_spread_bps: 1, slippage_bps: 1, fx_cost_bps: 0}
  CRYPTO: {commission_rate: 0.0013, tax_rate_sell: 0.0, half_spread_bps: 2, slippage_bps: 6, fx_cost_bps: 0}
risk: {max_order_notional: 777777, max_symbol_exposure: null, max_market_exposure: null,
       max_total_exposure: null, daily_loss_limit: null, max_drawdown: null}
net_alpha: {min_net_alpha_bps: 0}
"""


@pytest.fixture
def unique_settings(tmp_path):
    path = tmp_path / "settings.yaml"
    path.write_text(UNIQUE_SETTINGS, encoding="utf-8")
    return path


# ============================================================ 가. settings wiring
def test_a1_default_settings_path_points_to_repo_config():
    """Candidate: parents[2] may resolve to src/config. Source layout check."""
    from qat import config

    assert config._DEFAULT_PATH == ROOT / "config" / "settings.yaml"
    assert config._DEFAULT_PATH.exists()


def test_a2_explicit_missing_settings_file_is_an_error(tmp_path):
    from qat.config import load_settings

    with pytest.raises(FileNotFoundError):
        load_settings(tmp_path / "does-not-exist.yaml")


def test_a3_settings_costs_reach_broker_reservation_and_fill(unique_settings):
    """Unique commission/tax/slippage values must drive the Gate, the
    reservation and the actual fill - not only the Net Alpha estimate."""
    stack = build_paper_stack_from_settings(unique_settings, starting_cash=10_000_000)
    kr = stack.net_alpha_gate.cost_engine.model_for(Market.KR)
    assert kr.commission_rate == pytest.approx(0.0007)

    res = stack.submit_trade_proposal(
        mkprop(Market.KR, "005930", Side.BUY, 10, expected_gross_return=0.05), 1000
    )
    assert res.accepted, res.reason
    # MARKET BUY pays half-spread + slippage from settings: (3 + 4) bps
    fill = stack.broker.simulate_fill(res.order_id, 1000)
    assert fill.price == pytest.approx(1000 * (1 + 7 / 1e4))
    assert fill.commission == pytest.approx(fill.gross * 0.0007)
    reserved = res.order.reservation.cash_reserved
    assert reserved >= fill.gross + fill.total_cost - 1e-9
    assert stack.settle(fill) is FillOutcome.APPLIED

    sell = stack.submit_trade_proposal(
        mkprop(Market.KR, "005930", Side.SELL, 10, expected_gross_return=0.05), 1000
    )
    assert sell.accepted, sell.reason
    sfill = stack.broker.simulate_fill(sell.order_id, 1000)
    assert sfill.tax == pytest.approx(sfill.gross * 0.0031)


def test_a4_settings_risk_mode_and_markets_are_applied(unique_settings):
    stack = build_paper_stack_from_settings(
        unique_settings, starting_cash={Currency.KRW: 10_000_000, Currency.USD: 10_000}
    )
    assert stack.risk.max_order_notional == pytest.approx(777777)
    assert stack.router.mode == "PAPER"
    # US disabled in this settings file -> compliance must not pass a US proposal
    res = stack.submit_trade_proposal(
        mkprop(Market.US, "AAPL", Side.BUY, 1, expected_gross_return=0.05), 10
    )
    assert not res.accepted
    assert "market_disabled" in res.reason


def test_a5_live_enabled_toggle_cannot_unlock_live(tmp_path):
    path = tmp_path / "live.yaml"
    path.write_text(
        UNIQUE_SETTINGS.replace("live_enabled: false", "live_enabled: true").replace(
            "mode: paper", "mode: live"
        ),
        encoding="utf-8",
    )
    stack = build_paper_stack_from_settings(path, starting_cash=10_000_000)
    res = stack.submit_trade_proposal(
        mkprop(Market.KR, "005930", Side.BUY, 1, expected_gross_return=0.05), 1000
    )
    assert not res.accepted
    assert stack.broker.orders == {}


def test_a6_research_settings_require_costs(tmp_path):
    from qat.config import load_research_settings

    path = tmp_path / "empty.yaml"
    path.write_text("project: {mode: paper}\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_research_settings(path)


# ============================================================ 나. FX / valuation
def test_b1_realized_pnl_total_never_assumes_rate_one():
    ledger = PortfolioLedger({Currency.KRW: 0, Currency.USD: 0}, base_currency=Currency.KRW)
    ledger.realized_pnl[Currency.USD] = 10.0
    with pytest.raises(MissingFXRateError):
        ledger.realized_pnl_total({})


def test_b2_equity_never_assumes_rate_one():
    ledger = PortfolioLedger({Currency.KRW: 1000, Currency.USD: 10}, base_currency=Currency.KRW)
    with pytest.raises(MissingFXRateError):
        ledger.equity({})
    assert ledger.equity({Currency.USD: 1300}) == pytest.approx(1000 + 13_000)


def test_b3_strict_valuation_reports_missing_marks_separately_from_fx():
    ledger = PortfolioLedger({Currency.KRW: 1000, Currency.USD: 10}, base_currency=Currency.KRW)
    ledger.positions[("KR", "005930")] = Position(quantity=2, avg_cost=100)
    ledger.positions[("US", "AAPL")] = Position(quantity=1, avg_cost=5)
    val = ledger.valuation(mark_prices={("KR", "005930"): 110}, fx_provider=StaticFXRateProvider({}))
    assert val.missing_marks == [("US", "AAPL")]
    assert val.missing_fx == ["USD"]
    assert val.total_base is None  # never synthesised
    assert val.by_currency[Currency.KRW]["positions_value"] == pytest.approx(220)


# ============================================================ 다. reservation breach
def _stack_breach(cash):
    return build_paper_stack(starting_cash=cash, commission_rate=0.01, slippage_bps=100)


def test_c1_overrun_with_spare_cash_is_still_a_breach():
    """Doc 5.3: actual cost > reservation -> record + block, even when the
    account has enough spare cash that available cash stays positive."""
    stack = _stack_breach(1_000_000)
    res = stack.submit_trade_proposal(mkprop(Market.KR, "005930", Side.BUY, 100), 100)
    fill = stack.broker.simulate_fill(res.order_id, 200)  # next-bar gap
    assert stack.settle(fill) is FillOutcome.APPLIED
    assert stack.ledger.available_cash(Currency.KRW) > 0
    assert len(stack.ledger.reservation_breaches) == 1
    nxt = stack.submit_trade_proposal(mkprop(Market.KR, "005930", Side.BUY, 1), 100)
    assert not nxt.accepted and "reservation_breach" in nxt.reason


def test_c2_partial_fill_overrun_is_a_breach():
    stack = _stack_breach(1_000_000)
    res = stack.submit_trade_proposal(mkprop(Market.KR, "005930", Side.BUY, 100), 100)
    fill = stack.broker.simulate_fill(res.order_id, 150, 40)
    assert stack.settle(fill) is FillOutcome.APPLIED
    assert len(stack.ledger.reservation_breaches) == 1
    assert stack.ledger.reservation_breaches[0]["overrun"] > 0


def test_c3_fill_within_reservation_is_not_a_breach():
    stack = _stack_breach(1_000_000)
    res = stack.submit_trade_proposal(mkprop(Market.KR, "005930", Side.BUY, 100), 100)
    stack.settle(stack.broker.simulate_fill(res.order_id, 99, 50))
    stack.settle(stack.broker.simulate_fill(res.order_id, 100, 50))
    assert stack.ledger.reservation_breaches == []
    assert stack.ledger.reserved_cash[Currency.KRW] == pytest.approx(0.0)


# ============================================================ 라. reconciliation
def test_d1_internal_only_position_is_a_mismatch():
    ledger = PortfolioLedger(1000)
    ledger.positions[("KR", "005930")] = Position(quantity=5, avg_cost=100)
    res = reconcile(ledger, broker_cash={Currency.KRW: 1000}, broker_positions={})
    assert not res.ok
    assert any("position_mismatch:KR:005930" in r for r in res.reasons)


def test_d2_internal_only_cash_is_a_mismatch():
    ledger = PortfolioLedger({Currency.KRW: 1000, Currency.USD: 50})
    res = reconcile(ledger, broker_cash={Currency.KRW: 1000}, broker_positions={})
    assert not res.ok
    assert any(r.startswith("cash_mismatch:USD") for r in res.reasons)


def test_d3_empty_snapshot_is_not_a_match():
    ledger = PortfolioLedger(1000)
    res = reconcile(ledger, broker_cash={}, broker_positions={})
    assert not res.ok


def test_d4_incomplete_snapshot_is_not_ok():
    ledger = PortfolioLedger(1000)
    res = reconcile(ledger, broker_cash={Currency.KRW: 1000}, broker_positions={}, complete=False)
    assert not res.ok
    assert "snapshot_incomplete" in res.reasons


def test_d5_breach_cannot_be_cleared_without_clean_reconciliation():
    stack = _stack_breach(1_000_000)
    res = stack.submit_trade_proposal(mkprop(Market.KR, "005930", Side.BUY, 100), 100)
    stack.settle(stack.broker.simulate_fill(res.order_id, 200))
    bad = reconcile(stack.ledger, broker_cash={Currency.KRW: 1}, broker_positions={})
    with pytest.raises(LedgerError):
        stack.ledger.resolve_reservation_breaches(bad, approver="tester", note="x")
    assert stack.ledger.reservation_breaches
    good = reconcile(
        stack.ledger,
        broker_cash={Currency.KRW: stack.ledger.cash[Currency.KRW]},
        broker_positions={(Market.KR, "005930"): 100},
    )
    with pytest.raises(LedgerError):
        stack.ledger.resolve_reservation_breaches(good, approver="", note="x")
    stack.ledger.resolve_reservation_breaches(good, approver="tester", note="reviewed")
    assert stack.ledger.reservation_breaches == []
    assert stack.ledger.resolved_breaches[0]["approver"] == "tester"


# ============================================================ 마. settlement integrity
@pytest.mark.parametrize(
    "field,value",
    [
        ("symbol", "000660"),
        ("side", Side.SELL),
        ("market", Market.US),
        ("currency", Currency.USD),
        ("order_id", "other-order"),
    ],
)
def test_e1_fill_must_match_order(field, value):
    stack = build_paper_stack(starting_cash=1_000_000)
    res = stack.submit_trade_proposal(mkprop(Market.KR, "005930", Side.BUY, 10), 100)
    order = stack.broker.orders[res.order_id]
    kwargs = dict(order_id=order.order_id, market=Market.KR, symbol="005930", side=Side.BUY,
                  quantity=5, price=100, currency=Currency.KRW)
    kwargs[field] = value
    if field == "market":
        kwargs["symbol"] = "AAPL"
    before = (dict(stack.ledger.cash), dict(stack.ledger.reserved_cash))
    assert stack.settlement.apply_fill(order, Fill(**kwargs)) is FillOutcome.REJECTED_MISMATCH
    assert (dict(stack.ledger.cash), dict(stack.ledger.reserved_cash)) == before


def test_e2_fill_quantity_cannot_exceed_order_across_settlements():
    stack = build_paper_stack(starting_cash=1_000_000)
    res = stack.submit_trade_proposal(mkprop(Market.KR, "005930", Side.BUY, 10), 100)
    order = stack.broker.orders[res.order_id]
    extra = Fill(order_id=order.order_id, market=Market.KR, symbol="005930", side=Side.BUY,
                 quantity=11, price=100, currency=Currency.KRW)
    assert stack.settlement.apply_fill(order, extra) is FillOutcome.REJECTED_MISMATCH
    assert stack.ledger.get_position(Market.KR, "005930").quantity == 0


def test_e3_failed_sell_leaves_costs_untouched():
    ledger = PortfolioLedger(1000)
    bad = Fill(order_id="x", market=Market.KR, symbol="005930", side=Side.SELL,
               quantity=1, price=100, currency=Currency.KRW, commission=1.0, tax=2.0)
    with pytest.raises(LedgerError):
        ledger.apply_fill(bad)
    assert ledger.fees[Currency.KRW] == 0.0
    assert ledger.taxes[Currency.KRW] == 0.0


def test_e4_ledger_failure_does_not_release_reservation():
    stack = build_paper_stack(starting_cash=1_000_000)
    buy = stack.submit_trade_proposal(mkprop(Market.KR, "005930", Side.BUY, 10), 100)
    stack.settle(stack.broker.simulate_fill(buy.order_id, 100))
    sell = stack.submit_trade_proposal(mkprop(Market.KR, "005930", Side.SELL, 10), 100)
    fill = stack.broker.simulate_fill(sell.order_id, 100)
    # corrupt the books between fill and settlement to force a ledger failure
    stack.ledger.positions[("KR", "005930")].quantity = 5
    before_reserved = stack.ledger.get_position(Market.KR, "005930").reserved_quantity
    with pytest.raises(LedgerError):
        stack.settle(fill)
    assert stack.ledger.get_position(Market.KR, "005930").reserved_quantity == before_reserved
    assert not stack.ledger.has_applied(fill.fill_id)


# ============================================================ 바. numerics
@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_f1_non_finite_quantity_rejected(bad):
    with pytest.raises(DomainError):
        mkprop(Market.KR, "005930", Side.BUY, bad)


@pytest.mark.parametrize("bad", [math.nan, math.inf])
def test_f2_non_finite_fill_rejected(bad):
    with pytest.raises(DomainError):
        Fill(order_id="x", market=Market.KR, symbol="005930", side=Side.BUY,
             quantity=1, price=bad, currency=Currency.KRW)
    with pytest.raises(DomainError):
        Fill(order_id="x", market=Market.KR, symbol="005930", side=Side.BUY,
             quantity=1, price=100, currency=Currency.KRW, commission=bad)


@pytest.mark.parametrize("bad", [math.nan, math.inf])
def test_f3_non_finite_fx_rate_rejected(bad):
    fx = StaticFXRateProvider({(Currency.USD, Currency.KRW): bad})
    with pytest.raises(InvalidFXRateError):
        fx.rate(Currency.USD, Currency.KRW)


@pytest.mark.parametrize("bad", [math.nan, math.inf])
def test_f4_non_finite_reference_price_not_approved(bad):
    stack = build_paper_stack(starting_cash=1_000_000)
    res = stack.submit_trade_proposal(mkprop(Market.KR, "005930", Side.BUY, 1), bad)
    assert not res.accepted
    assert stack.broker.orders == {}


def test_f5_non_finite_expected_return_blocked():
    # rejected at construction (DomainError) - never reaches the gates
    with pytest.raises(DomainError):
        mkprop(Market.KR, "005930", Side.BUY, 1, expected_gross_return=math.inf)
    with pytest.raises(DomainError):
        mkprop(Market.KR, "005930", Side.BUY, 1, expected_gross_return=math.nan)


def test_f6_deterministic_ids_and_clock_when_injected():
    def run():
        counter = iter(range(1, 1000))
        stack = build_paper_stack(
            starting_cash=1_000_000,
            now_fn=lambda: FIXED_NOW,
            id_fn=lambda prefix: f"{prefix}{next(counter):06d}",
        )
        res = stack.submit_trade_proposal(
            mkprop(Market.KR, "005930", Side.BUY, 1, signal_timestamp=FIXED_NOW), 100
        )
        fill = stack.broker.simulate_fill(res.order_id, 100)
        stack.settle(fill)
        return res.order_id, fill.fill_id, fill.broker_fill_id, [
            (r.stage, r.timestamp.isoformat()) for r in stack.audit.records
        ]

    assert run() == run()


# ============================================================ 사. audit / concurrency
def test_g1_settlement_is_audited():
    stack = build_paper_stack(starting_cash=1_000_000)
    res = stack.submit_trade_proposal(mkprop(Market.KR, "005930", Side.BUY, 1), 100)
    stack.settle(stack.broker.simulate_fill(res.order_id, 100))
    stages = [r.stage for r in stack.audit.records]
    assert "fill_settled" in stages


def test_g2_concurrent_submission_cannot_double_reserve():
    stack = build_paper_stack(starting_cash=1_000)
    results = []

    def submit(i):
        results.append(
            stack.submit_trade_proposal(
                mkprop(Market.KR, "005930", Side.BUY, 700, strategy_id=f"s{i}"), 1
            )
        )

    threads = [threading.Thread(target=submit, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(r.accepted for r in results) == 1
    assert stack.ledger.reserved_cash[Currency.KRW] == pytest.approx(700)
    assert stack.ledger.available_cash(Currency.KRW) >= 0
