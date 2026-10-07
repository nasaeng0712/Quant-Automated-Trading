"""QAT UI entry point.

  python -m qat.ui serve [--port 8765] [--settings config/settings.yaml]
  python -m qat.ui latches
  python -m qat.ui clear-latch --id KILL_SWITCH-1 --approver NAME --note "offline review done"
  python -m qat.ui ack-recovery --approver NAME --note "reviewed; books verified"

Clearing a safety latch is an offline administrative action on purpose: the UI
has no route for it.
"""

from __future__ import annotations

import argparse
import json
import sys

from qat.ui.service import SafetyLatches, ServiceError, acknowledge_recovery


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m qat.ui")
    sub = parser.add_subparsers(dest="cmd", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--settings", default=None)
    sub.add_parser("latches")
    clear = sub.add_parser("clear-latch")
    clear.add_argument("--id", required=True)
    clear.add_argument("--approver", required=True)
    clear.add_argument("--note", required=True)
    ack = sub.add_parser("ack-recovery")
    ack.add_argument("--approver", required=True)
    ack.add_argument("--note", required=True)
    args = parser.parse_args(argv)

    if args.cmd == "latches":
        sys.stdout.write(json.dumps(SafetyLatches().active(), indent=2, ensure_ascii=False) + "\n")
        return 0
    if args.cmd == "clear-latch":
        cleared = SafetyLatches().clear(args.id, args.approver, args.note)
        sys.stdout.write(json.dumps(cleared, indent=2, ensure_ascii=False) + "\n")
        return 0

    if args.cmd == "ack-recovery":
        try:
            out = acknowledge_recovery(args.approver, args.note)
        except ServiceError as exc:
            sys.stderr.write(f"refused: {exc}\n")
            return 2
        sys.stdout.write(json.dumps(out, indent=2, ensure_ascii=False) + "\n")
        return 0

    from qat.ui.server import make_server

    if args.host not in ("127.0.0.1", "localhost", "::1"):
        sys.stderr.write("warning: binding beyond localhost exposes the paper UI to the network\n")
    server = make_server(args.host, args.port, args.settings)
    sys.stdout.write(f"QAT UI on http://{args.host}:{args.port}  (Ctrl+C to stop)\n")
    sys.stdout.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.qat_service.close()
        server.server_close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
