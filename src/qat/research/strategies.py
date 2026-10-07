"""Benchmark strategies (Batch #2) - research-pipeline controls, not alpha claims.

A strategy sees only a causal ``History`` view (bars ``0..t`` after bar ``t``
closed) and returns a ``Signal`` or ``None``. It has no broker, ledger or
gateway handle; the backtest runner turns a Signal into a ``TradeProposal`` and
submits it through ``StrategyGateway`` like any other strategy.

Expected gross return (what the Net Alpha gate judges):
  * ``alpha_mode="empirical"`` (default): the mean forward return observed after
    the same event type in *completed* past episodes only
    (``close[i+h] / open[i+1] - 1`` with ``i + h <= t``). For an exit the value
    is the expected return of selling = ``-mean`` forward return after past exit
    events. Fewer than ``min_samples`` episodes -> ``0.0`` with alpha_source
    ``NO_EVIDENCE`` (the gate then decides; nothing is inflated to force a trade).
  * ``alpha_mode="fixture"``: a fixed ``fixture_expected_return`` for pipeline /
    accounting tests. Labelled ``FIXTURE`` everywhere - never an alpha estimate.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

ENTER_LONG = "ENTER_LONG"
EXIT_LONG = "EXIT_LONG"

# Audit fix (D3): parameters are validated instead of being coerced or crashing
# deep inside a run (window 0 -> ZeroDivisionError, negative window -> empty slice,
# NaN -> ValueError, 1.5 -> silently int()).
_INT_PARAMS = ("fast", "slow", "entry_lookback", "exit_lookback", "window", "horizon", "min_samples")
_FLOAT_PARAMS = ("entry_z", "exit_z", "fixture_expected_return")


def _validate_params(params: dict) -> dict:
    out = dict(params)
    for key in _INT_PARAMS:
        if key not in out:
            continue
        value = out[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)                 or value != int(value) or value < 1:
            raise ValueError(f"parameter {key} must be an integer >= 1, got {value!r}")
        out[key] = int(value)
    for key in _FLOAT_PARAMS:
        if key not in out:
            continue
        value = out[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"parameter {key} must be a finite number, got {value!r}")
        out[key] = float(value)
    if "entry_z" in out and out["entry_z"] <= 0:
        raise ValueError("parameter entry_z must be > 0")
    return out


class History:
    """Read-only causal view: bars[0:end]. Indexing at or beyond ``end`` raises."""

    __slots__ = ("_bars", "_end")

    def __init__(self, bars, end: int) -> None:
        self._bars = bars
        self._end = int(end)

    def __len__(self) -> int:
        return self._end

    def __getitem__(self, index: int):
        if index < 0:
            index += self._end
        if index < 0 or index >= self._end:
            raise IndexError("history index outside the causal window")
        return self._bars[index]

    @property
    def last(self):
        return self[self._end - 1]


@dataclass(frozen=True)
class Signal:
    action: str
    reason_code: str
    expected_gross_return: float
    alpha_source: str  # EMPIRICAL | NO_EVIDENCE | FIXTURE
    confidence: float = 0.5
    evidence: dict = field(default_factory=dict)


class Strategy:
    name = "base"
    version = "0.1.0"
    default_params: dict = {}
    param_grid: dict = {}  # deliberately small; no large sweeps (Batch #2 scope)

    def __init__(self, **params) -> None:
        unknown = set(params) - set(self.default_params) - {
            "alpha_mode", "fixture_expected_return", "horizon", "min_samples"}
        if unknown:
            raise ValueError(f"unknown parameters for {self.name}: {sorted(unknown)}")
        merged = {"alpha_mode": "empirical", "fixture_expected_return": 0.0,
                  "horizon": 10, "min_samples": 5, **self.default_params, **params}
        if merged["alpha_mode"] not in ("empirical", "fixture"):
            raise ValueError("alpha_mode must be 'empirical' or 'fixture'")
        self.params = _validate_params(merged)
        self._reset()

    def reset(self) -> None:
        """Drop all incremental state. ``run_backtest`` calls this so a strategy
        instance reused across runs/datasets can never carry bars of an earlier
        run into a new one (Audit fix D7)."""

        self._reset()

    # ---- incremental causal series ------------------------------------
    def _reset(self) -> None:
        self._n = 0
        self._close: list[float] = []
        self._open: list[float] = []
        self._high: list[float] = []
        self._low: list[float] = []
        self._cond: list[bool | None] = []  # long condition per bar
        self._entry_events: list[int] = []
        self._exit_events: list[int] = []

    def _sync(self, h: History) -> None:
        if len(h) < self._n:
            self._reset()
        for i in range(self._n, len(h)):
            bar = h[i]
            self._open.append(bar.open)
            self._high.append(bar.high)
            self._low.append(bar.low)
            self._close.append(bar.close)
            entry, exit_ = self._events_at(i)
            if entry:
                self._entry_events.append(i)
            if exit_:
                self._exit_events.append(i)
        self._n = len(h)

    def _events_at(self, i: int) -> tuple[bool, bool]:  # pragma: no cover - abstract
        raise NotImplementedError

    @property
    def warmup(self) -> int:  # pragma: no cover - abstract
        raise NotImplementedError

    # ---- expected return ------------------------------------------------
    def _edge(self, events: list[int], t: int, sign: float) -> tuple[float, str, dict]:
        p = self.params
        if p["alpha_mode"] == "fixture":
            return float(p["fixture_expected_return"]), "FIXTURE", {"fixture": True}
        horizon = int(p["horizon"])
        samples = [
            self._close[i + horizon] / self._open[i + 1] - 1.0
            for i in events
            if i + horizon <= t and i + 1 <= t
        ]
        evidence = {"samples": len(samples), "horizon": horizon}
        if len(samples) < int(p["min_samples"]):
            return 0.0, "NO_EVIDENCE", evidence
        mean = sum(samples) / len(samples)
        evidence["mean_forward_return"] = mean
        return sign * mean, "EMPIRICAL", evidence

    def decide(self, h: History, holding: bool) -> Signal | None:
        self._sync(h)
        t = len(h) - 1
        if t < self.warmup:
            return None
        if not holding and self._entry_events and self._entry_events[-1] == t:
            value, source, evidence = self._edge(self._entry_events[:-1], t, 1.0)
            return Signal(ENTER_LONG, f"{self.name}:entry", value, source, 0.5, evidence)
        if holding and self._exit_condition(t):
            value, source, evidence = self._edge(self._exit_events, t, -1.0)
            return Signal(EXIT_LONG, f"{self.name}:exit", value, source, 0.5, evidence)
        return None

    def _exit_condition(self, t: int) -> bool:  # pragma: no cover - abstract
        raise NotImplementedError

    def describe(self) -> dict:
        return {"name": self.name, "version": self.version, "params": dict(self.params)}


def _sma(values: list[float], end: int, n: int) -> float | None:
    if end + 1 < n:
        return None
    return sum(values[end + 1 - n: end + 1]) / n


class MovingAverageTrend(Strategy):
    name = "ma_trend"
    default_params = {"fast": 10, "slow": 30}
    param_grid = {"fast": [5, 10, 20], "slow": [30, 50]}

    @property
    def warmup(self) -> int:
        return int(self.params["slow"])

    def _cond_at(self, i: int) -> bool | None:
        fast = _sma(self._close, i, int(self.params["fast"]))
        slow = _sma(self._close, i, int(self.params["slow"]))
        return None if fast is None or slow is None else fast > slow

    def _events_at(self, i: int) -> tuple[bool, bool]:
        cond = self._cond_at(i)
        self._cond.append(cond)
        prev = self._cond[i - 1] if i > 0 else None
        if cond is None or prev is None:
            return False, False
        return (cond and not prev), (not cond and prev)

    def _exit_condition(self, t: int) -> bool:
        return self._cond[t] is False


class Breakout(Strategy):
    name = "breakout"
    default_params = {"entry_lookback": 20, "exit_lookback": 10}
    param_grid = {"entry_lookback": [20, 40], "exit_lookback": [10, 20]}

    @property
    def warmup(self) -> int:
        return int(max(self.params["entry_lookback"], self.params["exit_lookback"]))

    def _breaks(self, i: int) -> tuple[bool | None, bool | None]:
        n_in, n_out = int(self.params["entry_lookback"]), int(self.params["exit_lookback"])
        up = None if i < n_in else self._close[i] > max(self._high[i - n_in:i])
        down = None if i < n_out else self._close[i] < min(self._low[i - n_out:i])
        return up, down

    def _events_at(self, i: int) -> tuple[bool, bool]:
        up, down = self._breaks(i)
        self._cond.append(down)  # store exit condition per bar
        prev_up = self._breaks(i - 1)[0] if i > 0 else None
        entry = bool(up) and prev_up is False
        exit_ = bool(down) and (i == 0 or self._cond[i - 1] is False)
        return entry, exit_

    def _exit_condition(self, t: int) -> bool:
        return bool(self._cond[t])


class MeanReversion(Strategy):
    name = "mean_reversion"
    default_params = {"window": 20, "entry_z": 2.0, "exit_z": 0.0}
    param_grid = {"window": [20, 40], "entry_z": [1.5, 2.0]}

    @property
    def warmup(self) -> int:
        return int(self.params["window"])

    def _z(self, i: int) -> float | None:
        n = int(self.params["window"])
        if i + 1 < n:
            return None
        window = self._close[i + 1 - n: i + 1]
        mean = sum(window) / n
        var = sum((x - mean) ** 2 for x in window) / n
        if var <= 0:
            return None
        return (self._close[i] - mean) / math.sqrt(var)

    def _events_at(self, i: int) -> tuple[bool, bool]:
        z = self._z(i)
        self._cond.append(z)
        prev = self._cond[i - 1] if i > 0 else None
        if z is None or prev is None:
            return False, False
        lo, hi = -float(self.params["entry_z"]), float(self.params["exit_z"])
        return (z < lo <= prev), (z > hi >= prev)

    def _exit_condition(self, t: int) -> bool:
        z = self._cond[t]
        return z is not None and z > float(self.params["exit_z"])


STRATEGIES = {cls.name: cls for cls in (MovingAverageTrend, Breakout, MeanReversion)}


def make_strategy(name: str, **params) -> Strategy:
    if name not in STRATEGIES:
        raise ValueError(f"unknown strategy {name!r}; available: {sorted(STRATEGIES)}")
    return STRATEGIES[name](**params)
