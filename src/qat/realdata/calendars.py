"""Exchange trading calendars as versioned SNAPSHOTS (Batch #3A).

No runtime dependency: a snapshot is a JSON file in ``data/calendars/`` listing the
sessions (trading dates) of an exchange for a range. Snapshots are generated offline by
``tools``-style scripts from a named source (e.g. ``exchange_calendars==4.13.2``) and
record source, version, generation time and a SHA-256 over their canonical content.

  KR  -> XKRX     US -> XNYS     CRYPTO -> 24/7 (every calendar day is a session)

Weekends / holidays / early closes are NOT guessed at runtime: a date is a trading
session iff the snapshot says so. A date outside the snapshot range is UNKNOWN, never
"trading" - validation fails closed.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
from dataclasses import dataclass
from datetime import date, timedelta

CALENDAR_DIR = pathlib.Path(__file__).resolve().parents[3] / "data" / "calendars"
MARKET_CALENDAR = {"KR": "XKRX", "US": "XNYS", "CRYPTO": "24X7"}


class CalendarError(ValueError):
    """Calendar snapshot missing, tampered or not covering a requested date."""


def _canonical(payload: dict) -> bytes:
    body = {k: v for k, v in payload.items() if k != "sha256"}
    return json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def snapshot_sha256(payload: dict) -> str:
    return hashlib.sha256(_canonical(payload)).hexdigest()


@dataclass(frozen=True)
class Calendar:
    code: str
    first: date
    last: date
    sessions: frozenset  # of date; empty for 24x7
    source: str
    version: str
    generated_utc: str
    sha256: str
    twenty_four_seven: bool = False

    def covers(self, day: date) -> bool:
        return self.first <= day <= self.last

    def is_session(self, day: date) -> bool:
        if not self.covers(day):
            raise CalendarError(f"{day.isoformat()} is outside calendar {self.code} snapshot "
                                f"[{self.first.isoformat()}, {self.last.isoformat()}]")
        return True if self.twenty_four_seven else day in self.sessions

    def sessions_between(self, start: date, end: date) -> list[date]:
        if not (self.covers(start) and self.covers(end)):
            raise CalendarError(f"range {start}..{end} outside calendar {self.code} "
                                f"[{self.first}, {self.last}]")
        out, day = [], start
        while day <= end:
            if self.twenty_four_seven or day in self.sessions:
                out.append(day)
            day += timedelta(days=1)
        return out

    def identity(self) -> dict:
        return {"code": self.code, "first": self.first.isoformat(), "last": self.last.isoformat(),
                "source": self.source, "version": self.version, "generated_utc": self.generated_utc,
                "sha256": self.sha256, "sessions": None if self.twenty_four_seven else len(self.sessions)}


def build_payload(code: str, sessions: list[date], first: date, last: date, *, source: str,
                  version: str, generated_utc: str, notes: str = "") -> dict:
    payload = {
        "code": code, "first": first.isoformat(), "last": last.isoformat(),
        "source": source, "version": version, "generated_utc": generated_utc, "notes": notes,
        "sessions": [d.isoformat() for d in sorted(sessions)],
    }
    payload["sha256"] = snapshot_sha256(payload)
    return payload


def load_calendar(code: str, directory: pathlib.Path | str | None = None) -> Calendar:
    """Load and integrity-check a snapshot. 24X7 is built in (no file)."""

    if code == "24X7":
        first, last = date(1970, 1, 1), date(2100, 12, 31)
        return Calendar(code, first, last, frozenset(), "built-in 24/7 rule", "1", "n/a",
                        hashlib.sha256(b"24X7:1").hexdigest(), twenty_four_seven=True)
    path = pathlib.Path(directory or CALENDAR_DIR) / f"{code}.json"
    if not path.is_file():
        raise CalendarError(f"calendar snapshot missing: {path.name}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("code") != code:
        raise CalendarError(f"snapshot {path.name} declares code {payload.get('code')!r}")
    if snapshot_sha256(payload) != payload.get("sha256"):
        raise CalendarError(f"calendar snapshot {path.name} fails its own SHA-256 (tampered or corrupt)")
    return Calendar(
        code=code, first=date.fromisoformat(payload["first"]), last=date.fromisoformat(payload["last"]),
        sessions=frozenset(date.fromisoformat(s) for s in payload["sessions"]),
        source=payload["source"], version=payload["version"], generated_utc=payload["generated_utc"],
        sha256=payload["sha256"],
    )


def calendar_for_market(market: str, directory=None) -> Calendar:
    try:
        return load_calendar(MARKET_CALENDAR[market], directory)
    except KeyError as exc:
        raise CalendarError(f"no calendar for market {market!r}") from exc
