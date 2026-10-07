"""Contract-test double for the broker boundary. In-memory, deterministic, with explicit fault injection. NOT a simulator of a real venue."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from qat.brokerage.contract import (
    BrokerAck, BrokerConnectionLost, BrokerFillReport, BrokerOrderState, BrokerRejected, BrokerSnapshot, BrokerTimeout,
)


def _utcnow():
    return datetime.now(timezone.utc)


class FakeBrokerAdapter:
    name = "fake-broker"

    def __init__(self, *, now_fn=_utcnow, cash=None, positions=None) -> None:
        self._now = now_fn
        self._cash = dict(cash or {})
        self._positions = dict(positions or {})
        self.orders: dict[str, dict] = {}  # client_order_id -> record
        self._pending_fills: list[BrokerFillReport] = []
        self._faults: dict[str, list] = {}
        self.connected = True
        self.snapshot_age_seconds = 0.0  # inject a stale snapshot by raising this
        self.snapshot_timestamp_override = "unset"
        self.submit_calls = 0
        self._fill_seq = 0

    # ------------------------------------------------------------ fault injection
    def inject(self, method: str, kind: str, **opts) -> None:
        """kind: timeout (opts accepted=True/False: did the broker actually take the order?), connection_lost, reject, unknown_state."""

        self._faults.setdefault(method, []).append((kind, opts))

    def _next_fault(self, method: str):
        queue = self._faults.get(method) or []
        return queue.pop(0) if queue else None

    def _check_connected(self) -> None:
        if not self.connected:
            raise BrokerConnectionLost("not connected")

    # ------------------------------------------------------------ contract
    def submit_order(self, order_view: dict, client_order_id: str) -> BrokerAck:
        self._check_connected()
        self.submit_calls += 1
        fault = self._next_fault("submit_order")
        if client_order_id in self.orders and not (fault and fault[0] == "timeout"):
            rec = self.orders[client_order_id]
            return BrokerAck(client_order_id, rec["broker_order_id"], rec["state"], self._now(), reason="idempotent_replay")
        if fault:
            kind, opts = fault
            if kind == "reject":
                raise BrokerRejected(opts.get("reason", "rejected by broker"))
            if kind == "connection_lost":
                self.connected = False
                raise BrokerConnectionLost("connection lost during submit")
            if kind == "timeout":
                if opts.get("accepted", False) and client_order_id not in self.orders:
                    self.orders[client_order_id] = {"broker_order_id": f"B-{len(self.orders) + 1}", "state": BrokerOrderState.ACCEPTED, "view": dict(order_view), "filled": 0.0}
                raise BrokerTimeout("submit timed out (the order may or may not exist)")
        rec = {"broker_order_id": f"B-{len(self.orders) + 1}", "state": BrokerOrderState.ACCEPTED, "view": dict(order_view), "filled": 0.0}
        self.orders[client_order_id] = rec
        return BrokerAck(client_order_id, rec["broker_order_id"], rec["state"], self._now())

    def cancel_order(self, client_order_id: str) -> BrokerAck:
        self._check_connected()
        fault = self._next_fault("cancel_order")
        if fault and fault[0] == "timeout":
            raise BrokerTimeout("cancel timed out")
        rec = self.orders.get(client_order_id)
        if rec is None:
            raise BrokerRejected("unknown order")
        if rec["state"] in (BrokerOrderState.FILLED, BrokerOrderState.CANCELLED):
            return BrokerAck(client_order_id, rec["broker_order_id"], rec["state"], self._now(), reason="already_terminal")
        rec["state"] = BrokerOrderState.CANCELLED
        return BrokerAck(client_order_id, rec["broker_order_id"], rec["state"], self._now())

    def order_status(self, client_order_id: str) -> BrokerAck:
        self._check_connected()
        fault = self._next_fault("order_status")
        if fault and fault[0] == "unknown_state":
            return BrokerAck(client_order_id, None, BrokerOrderState.UNKNOWN, self._now(), reason="broker cannot determine the order state")
        rec = self.orders.get(client_order_id)
        if rec is None:
            return BrokerAck(client_order_id, None, BrokerOrderState.UNKNOWN, self._now(), reason="order not found at broker")
        return BrokerAck(client_order_id, rec["broker_order_id"], rec["state"], self._now())

    def fills(self) -> list[BrokerFillReport]:
        self._check_connected()
        out, self._pending_fills = list(self._pending_fills), []
        return out

    def positions(self) -> dict:
        self._check_connected()
        return dict(self._positions)

    def cash(self) -> dict:
        self._check_connected()
        return dict(self._cash)

    def account_snapshot(self) -> BrokerSnapshot:
        self._check_connected()
        now = self._now()
        ts = now - timedelta(seconds=self.snapshot_age_seconds)
        if self.snapshot_timestamp_override != "unset":
            ts = self.snapshot_timestamp_override
        return BrokerSnapshot(cash=dict(self._cash), positions=dict(self._positions), snapshot_ts=ts, received_ts=now, complete=True, source=self.name)

    def reconnect(self) -> dict:
        self.connected = True
        return {"reconnected": True, "open_orders": [k for k, v in self.orders.items() if v["state"] in (BrokerOrderState.ACCEPTED, BrokerOrderState.PARTIALLY_FILLED)]}

    # ------------------------------------------------------------ test-side helpers (what the "venue" does)
    def report_fill(self, client_order_id: str, quantity: float, price: float, *, side: str, duplicate: bool = False, broker_fill_id: str | None = None,
                    commission: float = 0.0) -> BrokerFillReport:
        self._fill_seq += 1
        rec = self.orders[client_order_id]
        fid = broker_fill_id or f"BF-{self._fill_seq}"
        report = BrokerFillReport(fid, client_order_id, rec["broker_order_id"], side, quantity, price, self._now(), commission=commission)
        self._pending_fills.append(report)
        if duplicate:
            self._pending_fills.append(report)
        rec["filled"] += quantity
        rec["state"] = BrokerOrderState.PARTIALLY_FILLED
        return report
