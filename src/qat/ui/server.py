"""Local HTTP server for the QAT UI (stdlib only, binds 127.0.0.1 by default).

Routes (JSON unless noted)
  GET  /                      static single-page UI
  GET  /api/status | /api/overview | /api/portfolio | /api/orders | /api/risk
  GET  /api/operations        system health summary + Paper graduation / Live readiness (Live is always BLOCKED)
  GET  /api/audit?offset&limit&session_uid&event_type&order   durable audit log, READ-ONLY, whitelisted filters
  GET  /api/recovery          recovery state, restart assessment, flatten availability
  GET  /api/settings | /api/datasets | /api/runs | /api/runs/<run_id>
  POST /api/paper/session     {dataset_id, initial_cash, start_bar}
  POST /api/paper/proposals   {side, quantity, order_type, limit_price, expected_gross_return,
                               reason_code, client_request_id}
  POST /api/paper/orders/<id>/cancel
  POST /api/paper/advance     {bars}
  POST /api/paper/kill-switch {reason}           (engage only - there is no release route)
  POST /api/recovery/flatten  {operator, reason, confirm:"FLATTEN"}   operator-only emergency flatten (Paper session)
  POST /api/research/backtest | /api/research/walkforward | /api/research/stress

Audit fix (D10): when bound to a loopback address every request must carry a
loopback ``Host`` header, and a POST's ``Origin`` (if present) must be loopback
too. This stops a DNS-rebinding page (evil.example -> 127.0.0.1) from driving the
local paper API as "same origin".

POSTs must carry ``Content-Type: application/json`` and ``X-QAT-Client: ui`` (a
custom header a cross-site form cannot send). Bodies are limited to 64 KiB.
"""

from __future__ import annotations

import json
import pathlib
import re
import traceback
from http import HTTPStatus
from urllib.parse import parse_qs, urlsplit
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from qat.ui.service import ConflictError, QATService, ServiceError

STATIC_DIR = pathlib.Path(__file__).resolve().parent / "static"
STATIC_FILES = {"/": ("index.html", "text/html; charset=utf-8"),
                "/app.css": ("app.css", "text/css; charset=utf-8"),
                "/app.js": ("app.js", "application/javascript; charset=utf-8")}
MAX_BODY = 64 * 1024
DRAIN_LIMIT = 1024 * 1024
_CANCEL_RE = re.compile(r"^/api/paper/orders/([A-Za-z0-9_-]{1,80})/cancel$")
_RUN_RE = re.compile(r"^/api/runs/([A-Za-z0-9_.-]{1,120})$")


LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]", "::1"})


def _hostname(value: str) -> str:
    value = (value or "").strip().lower()
    if value.startswith("["):
        return value.split("]", 1)[0] + "]"
    return value.rsplit(":", 1)[0] if value.count(":") == 1 else value


def make_handler(service: QATService, enforce_loopback: bool = True):
    class Handler(BaseHTTPRequestHandler):
        server_version = "QAT-UI/0.1"

        def log_message(self, fmt, *args):  # keep test output quiet
            pass

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; img-src 'self' data:; style-src 'self'; "
                             "script-src 'self'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, obj) -> None:
            body = json.dumps(obj, ensure_ascii=False, default=str, allow_nan=False).encode("utf-8")
            self._send(status, body, "application/json; charset=utf-8")

        def _error(self, status: int, message: str) -> None:
            self._json(status, {"error": message, "status": status})

        def _dispatch(self, fn, *args) -> None:
            try:
                self._json(200, fn(*args))
            except ServiceError as exc:
                self._error(400, str(exc))
            except ConflictError as exc:
                self._error(409, str(exc))
            except Exception as exc:  # noqa: BLE001 - never leak a stack trace to the page
                traceback.print_exc()
                self._error(500, f"{type(exc).__name__}: {exc}")

        def _origin_ok(self, post: bool) -> bool:
            if not enforce_loopback:
                return True
            if _hostname(self.headers.get("Host", "")) not in LOOPBACK_HOSTS:
                return False
            origin = self.headers.get("Origin")
            if post and origin is not None:
                if origin == "null" or _hostname(urlsplit(origin).netloc) not in LOOPBACK_HOSTS:
                    return False
            return True

        def do_GET(self):  # noqa: N802
            if not self._origin_ok(post=False):
                self._error(403, "host not allowed")
                return
            path, _, query = self.path.partition("?")
            if path in STATIC_FILES:
                name, ctype = STATIC_FILES[path]
                self._send(200, (STATIC_DIR / name).read_bytes(), ctype)
                return
            routes = {
                "/api/status": service.status, "/api/overview": service.overview,
                "/api/portfolio": service.portfolio, "/api/orders": service.orders,
                "/api/risk": service.risk, "/api/settings": service.settings_view,
                "/api/datasets": service.list_datasets, "/api/runs": service.runs,
            }
            if path in routes:
                self._dispatch(routes[path])
                return
            if path == "/api/operations":
                self._dispatch(service.operations)
                return
            if path == "/api/recovery":
                self._dispatch(service.recovery_view)
                return
            if path == "/api/audit":
                parsed = parse_qs(query, keep_blank_values=True)
                if any(len(v) != 1 for v in parsed.values()):
                    self._error(400, "repeated query parameters are not accepted")
                    return
                self._dispatch(service.audit_view, {k: v[0] for k, v in parsed.items()})
                return
            match = _RUN_RE.match(path)
            if match:
                self._dispatch(service.run_detail, match.group(1))
                return
            self._error(404, "not found")

        def _read_body(self):
            """Consume the request body BEFORE any verdict. Answering 403/415/413 with an
            unread body makes the OS reset the connection, so the client sees
            ConnectionAbortedError instead of the status (seen in ~1/120 requests).
            Bounded: at most DRAIN_LIMIT bytes are read for an oversized request.
            Returns (bytes | None when the declared length is invalid or too large)."""

            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return None
            if length < 0:
                return None
            wanted = length if length <= MAX_BODY else min(length, DRAIN_LIMIT)
            chunks, remaining = [], wanted
            while remaining > 0:
                chunk = self.rfile.read(min(remaining, 65536))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            return b"".join(chunks) if length <= MAX_BODY else None

        def do_POST(self):  # noqa: N802
            raw = self._read_body()
            if not self._origin_ok(post=True):
                self._error(403, "host or origin not allowed")
                return
            path = self.path.split("?", 1)[0]
            if self.headers.get("X-QAT-Client") != "ui":
                self._error(403, "missing X-QAT-Client header")
                return
            if not (self.headers.get("Content-Type") or "").startswith("application/json"):
                self._error(415, "Content-Type must be application/json")
                return
            if raw is None:
                self._error(413, "request body too large or invalid length")
                return
            try:
                payload = json.loads(raw or b"{}")
            except ValueError:
                self._error(400, "invalid JSON")
                return
            if not isinstance(payload, dict):
                self._error(400, "JSON object expected")
                return
            routes = {
                "/api/paper/session": service.start_session,
                "/api/paper/proposals": service.submit_proposal,
                "/api/paper/advance": service.advance,
                "/api/paper/kill-switch": service.engage_kill_switch,
                "/api/recovery/flatten": service.emergency_flatten,
                "/api/research/backtest": service.run_backtest,
                "/api/research/walkforward": service.run_walkforward,
                "/api/research/stress": service.run_stress,
            }
            if path in routes:
                self._dispatch(routes[path], payload)
                return
            match = _CANCEL_RE.match(path)
            if match:
                self._dispatch(service.cancel_order, match.group(1))
                return
            self._error(404, "not found")

        def do_PUT(self):  # noqa: N802
            self._error(HTTPStatus.METHOD_NOT_ALLOWED, "method not allowed")

        do_DELETE = do_PATCH = do_PUT

    return Handler


def make_server(host: str = "127.0.0.1", port: int = 8765, settings_path: str | None = None,
                service: QATService | None = None) -> ThreadingHTTPServer:
    service = service or QATService(settings_path)
    server = ThreadingHTTPServer(
        (host, port), make_handler(service, enforce_loopback=host.lower() in LOOPBACK_HOSTS))
    server.daemon_threads = True
    server.qat_service = service
    return server
