"""Shared test helpers for the QAT Phase 0 / Core Batch #1 suite."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from qat.compliance.gate import ComplianceGate
from qat.core.models import Market, OrderType, Side, TradeProposal

FIXED_NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def make_proposal(
    market: Market,
    symbol: str,
    side: Side,
    quantity: float,
    *,
    order_type: OrderType = OrderType.MARKET,
    limit_price: float | None = None,
    reason_code: str = "unit-test",
    strategy_id: str = "test-strategy",
    confidence: float = 0.6,
    expected_gross_return: float = 0.0,
    signal_timestamp: datetime | None = None,
) -> TradeProposal:
    return TradeProposal(
        market=market,
        symbol=symbol,
        side=side,
        quantity=quantity,
        order_type=order_type,
        limit_price=limit_price,
        reason_code=reason_code,
        strategy_id=strategy_id,
        confidence=confidence,
        expected_gross_return=expected_gross_return,
        signal_timestamp=signal_timestamp,
    )


@pytest.fixture(autouse=True)
def _unit_test_universe_default(request, monkeypatch):
    """OD-02: production default is fail-closed (no allowlist => UNKNOWN). The legacy
    accounting / state-machine unit tests build bare stacks without a universe, so
    they opt in to an unrestricted universe HERE, explicitly, for test scaffolding
    only. UI and research entry points always pass an explicit universe and are not
    affected. Tests marked ``fail_closed_universe`` keep the production default."""

    if request.node.get_closest_marker("fail_closed_universe") is None:
        monkeypatch.setattr(ComplianceGate, "DEFAULT_UNRESTRICTED_UNIVERSE", True)
