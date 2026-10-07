"""Tiny stdlib HTTP GET used by the real-data fetchers.

It never raises with a URL in the message (a URL may carry a serviceKey) and returns the
response verbatim: status, headers and the exact body bytes.
"""

from __future__ import annotations

import urllib.error
import urllib.request
from dataclasses import dataclass

USER_AGENT = "Mozilla/5.0 (QAT research data acquisition; contact: local user)"


@dataclass
class HttpResult:
    status: int | None
    headers: dict
    body: bytes
    error: str | None = None  # sanitized class name only, never a URL

    @property
    def ok(self) -> bool:
        return self.status is not None and 200 <= self.status < 300


def get(url: str, *, headers: dict | None = None, timeout: float = 60.0) -> HttpResult:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return HttpResult(response.status, dict(response.headers), response.read())
    except urllib.error.HTTPError as exc:
        body = exc.read() if hasattr(exc, "read") else b""
        return HttpResult(exc.code, dict(exc.headers or {}), body)
    except Exception as exc:  # noqa: BLE001 - network failure; message deliberately dropped
        return HttpResult(None, {}, b"", error=type(exc).__name__)
