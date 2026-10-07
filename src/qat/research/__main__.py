"""Research CLI (Batch #2).

  python -m qat.research validate-data data/fixtures/SYN_KR1_1d.csv
  python -m qat.research backtest --data data/fixtures/SYN_KR1_1d.csv --strategy ma_trend -p fast=10 -p slow=30
  python -m qat.research walkforward --data ... --strategy ma_trend --train 250 --test 60 --lockbox 100
  python -m qat.research lockbox --data ... --run <walkforward_run_id>
  python -m qat.research stress --data ... --strategy breakout --mult 1 2 3
  python -m qat.research list
  python -m qat.research show <run_id>
"""

from __future__ import annotations

import argparse
import json
import sys

from qat.data.loader import load_dataset
from qat.research.backtest import BacktestConfig, run_backtest
from qat.research.store import list_runs, load_run, save_backtest
from qat.research.strategies import make_strategy
from qat.research.walkforward import WalkForwardConfig, evaluate_lockbox, run_cost_stress, run_walkforward


def _params(items) -> dict:
    out = {}
    for item in items or []:
        key, _, value = item.partition("=")
        try:
            out[key] = json.loads(value)
        except ValueError:
            out[key] = value
    return out


def _base(args) -> BacktestConfig:
    return BacktestConfig(initial_cash=args.cash, settings_path=args.settings,
                          cost_multiplier=args.cost_mult, position_fraction=args.fraction,
                          reservation_buffer_pct=args.buffer, seed=args.seed)


def _print(obj) -> None:
    sys.stdout.write(json.dumps(obj, indent=2, ensure_ascii=False, default=str) + "\n")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m qat.research")
    sub = parser.add_subparsers(dest="cmd", required=True)

    def add_common(p):
        p.add_argument("--data", required=True)
        p.add_argument("--strategy", required=True)
        p.add_argument("-p", "--param", action="append", help="key=value (JSON value)")
        p.add_argument("--cash", type=float, default=10_000_000.0)
        p.add_argument("--settings", default=None)
        p.add_argument("--cost-mult", type=float, default=1.0)
        p.add_argument("--fraction", type=float, default=0.95)
        p.add_argument("--buffer", type=float, default=0.02)
        p.add_argument("--seed", type=int, default=0)

    v = sub.add_parser("validate-data")
    v.add_argument("path")
    add_common(sub.add_parser("backtest"))
    w = sub.add_parser("walkforward")
    add_common(w)
    w.add_argument("--train", type=int, default=250)
    w.add_argument("--test", type=int, default=60)
    w.add_argument("--step", type=int, default=None)
    w.add_argument("--lockbox", type=int, default=0)
    lb = sub.add_parser("lockbox")
    lb.add_argument("--data", required=True)
    lb.add_argument("--run", required=True)
    lb.add_argument("--acknowledge-reuse", action="store_true")
    s = sub.add_parser("stress")
    add_common(s)
    s.add_argument("--mult", type=float, nargs="+", default=[1.0, 2.0, 3.0])
    sub.add_parser("list")
    sh = sub.add_parser("show")
    sh.add_argument("run_id")
    args = parser.parse_args(argv)

    if args.cmd == "validate-data":
        ds = load_dataset(args.path)
        _print({"summary": ds.summary(), "validation": ds.validation.to_dict()})
        return 0 if ds.usable else 2
    if args.cmd == "list":
        _print(list_runs())
        return 0
    if args.cmd == "show":
        _print(load_run(args.run_id))
        return 0
    ds = load_dataset(args.data)
    if args.cmd == "lockbox":
        out = evaluate_lockbox(ds, args.run, acknowledge_reuse=args.acknowledge_reuse)
        _print({"run_id": out["manifest"]["run_id"], "independent": out["independent"], "metrics": out["metrics"]})
        return 0
    params = _params(args.param)
    if args.cmd == "backtest":
        res = run_backtest(ds, make_strategy(args.strategy, **params), _base(args))
        manifest = save_backtest(res)
        _print({"run_id": manifest["run_id"], "warnings": res.warnings, "metrics": res.metrics})
    elif args.cmd == "walkforward":
        out = run_walkforward(ds, args.strategy, WalkForwardConfig(
            train_bars=args.train, test_bars=args.test, step_bars=args.step,
            lockbox_bars=args.lockbox, base=_base(args)), params)
        _print({"run_id": out["manifest"]["run_id"], "metrics": out["metrics"]})
    elif args.cmd == "stress":
        out = run_cost_stress(ds, args.strategy, params, _base(args), tuple(args.mult))
        _print({"run_id": out["manifest"]["run_id"], "metrics": out["metrics"]})
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
