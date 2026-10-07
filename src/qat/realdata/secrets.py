"""Credential handling for real-data acquisition (Batch #3A).

The data.go.kr serviceKey lives in a local file OUTSIDE the repository. It is read only
at the moment of an API call, kept in memory, and never printed, logged, stored or
copied. Everything that leaves this module as text goes through ``redact`` first, and
stored request identities use ``redact_url``.

Nothing here writes the key anywhere.
"""

from __future__ import annotations

import pathlib
import re
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit

DEFAULT_KEY_FILE = pathlib.Path.home() / "Desktop" / "key.txt"
REDACTED = "<REDACTED>"
_SECRET_PARAMS = ("servicekey", "apikey", "api_key", "key", "token", "authkey")


class CredentialError(RuntimeError):
    """The credential file is missing or empty. Never contains the key."""


def key_file_status(path: str | pathlib.Path | None = None) -> dict:
    """Existence / non-emptiness only - safe to print."""

    target = pathlib.Path(path) if path is not None else DEFAULT_KEY_FILE
    exists = target.is_file()
    return {"key_file_exists": exists, "key_non_empty": bool(exists and target.read_bytes().strip())}


def read_key(path: str | pathlib.Path | None = None) -> str:
    """Read the key from the local file (trailing whitespace trimmed in memory only)."""

    target = pathlib.Path(path) if path is not None else DEFAULT_KEY_FILE
    if not target.is_file():
        raise CredentialError("credential file not found")
    value = target.read_text(encoding="utf-8-sig").strip()
    if not value:
        raise CredentialError("credential file is empty")
    return value


def key_variants(key: str) -> set[str]:
    """Every textual form the key may take inside a URL or message (raw, decoded,
    percent-encoded) - used to scrub output and to scan for leaks."""

    decoded = unquote(key)
    return {v for v in (key, decoded, quote(decoded, safe=""), quote(key, safe="")) if v}


def redact(text: str, key: str | None) -> str:
    if not key:
        return text
    for variant in sorted(key_variants(key), key=len, reverse=True):
        text = text.replace(variant, REDACTED)
    return text


def redact_url(url: str, key: str | None = None) -> str:
    """Replace the value of any secret query parameter (and the key itself) with
    ``<REDACTED>``. Safe to store as an endpoint identity."""

    parts = urlsplit(url)
    query = [(k, REDACTED if k.lower() in _SECRET_PARAMS else v) for k, v in parse_qsl(parts.query, keep_blank_values=True)]
    rebuilt = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query, safe="<>"), parts.fragment))
    return redact(rebuilt, key)


def encode_for_query(key: str, *, mode: str = "auto") -> str:
    """Value to place after ``serviceKey=``.

    data.go.kr issues an "Encoding" key (already percent-encoded: contains %2B, %2F, %3D)
    and a "Decoding" key (contains + / =). Sending the encoded form twice-encoded fails.
    mode: ``as_is`` (use verbatim), ``quote`` (percent-encode), ``auto`` (verbatim if it
    already contains a percent escape, else percent-encode).
    """

    if mode == "as_is":
        return key
    if mode == "quote":
        return quote(key, safe="")
    return key if re.search(r"%[0-9A-Fa-f]{2}", key) else quote(key, safe="")
