"""Offline generator for ``data/calendars/*.json`` (NOT a runtime dependency).

Run it with an interpreter that has ``exchange_calendars`` installed, e.g. a throw-away
venv:

    python -m venv calvenv && calvenv/Scripts/pip install exchange_calendars
    calvenv/Scripts/python tools/generate_calendar_snapshots.py --out data/calendars

It records the source package, its version and a SHA-256 over the snapshot content. The
runtime only reads the resulting JSON (``qat.realdata.calendars``).
"""

from __future__ import annotations

import argparse
import importlib.metadata as md
import pathlib
import sys
from datetime import date, datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from qat.realdata.calendars import build_payload  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="data/calendars")
    parser.add_argument("--first", default="2017-01-01")
    parser.add_argument("--last", default="2026-12-31")
    args = parser.parse_args()
    import json

    import exchange_calendars as xc

    version = md.version("exchange_calendars")
    first, last = date.fromisoformat(args.first), date.fromisoformat(args.last)
    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    for code in ("XKRX", "XNYS"):
        cal = xc.get_calendar(code, start=args.first, end=args.last)
        lo = max(first, cal.first_session.date())
        hi = min(last, cal.last_session.date())
        sessions = [s.date() for s in cal.sessions_in_range(lo.isoformat(), hi.isoformat())]
        payload = build_payload(
            code, sessions, first, last, source=f"exchange_calendars=={version}", version=version,
            generated_utc=stamp,
            notes="community-maintained calendar, NOT an official exchange publication; "
                  "cross-validated against raw price data by qat.realdata.validate")
        (out / f"{code}.json").write_text(json.dumps(payload, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        print(code, len(sessions), payload["sha256"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
