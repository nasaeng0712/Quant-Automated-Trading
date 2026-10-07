"""Immutable raw artifacts + provenance sidecars (Batch #3A).

Every real response is stored byte-for-byte under ``data/raw/<provider>/<market>/<symbol>/``
with a ``<name>.provenance.json`` sidecar. Nothing here edits, filters, fills or
deduplicates content. An artifact that already exists may never be overwritten with
different bytes (``RawImmutableError``); re-storing identical bytes is a no-op.

The sidecar records where the bytes came from, never a credential: the stored endpoint is
passed through ``redact_url`` and request parameters named like a key are replaced.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from qat.realdata.secrets import redact, redact_url

RAW_ROOT = pathlib.Path(__file__).resolve().parents[3] / "data" / "raw"
SIDECAR_SUFFIX = ".provenance.json"
REQUIRED_FIELDS = (
    "schema", "provider", "service", "market", "symbol", "retrieved_utc", "requested_range",
    "returned_range", "sha256", "size_bytes", "format", "endpoint", "timezone", "synthetic", "artifact",
)


class RawImmutableError(RuntimeError):
    """Attempt to overwrite an existing raw artifact with different bytes."""


class ProvenanceError(ValueError):
    """Sidecar missing, malformed, or inconsistent with the artifact bytes."""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def safe_name(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-")


def artifact_dir(provider: str, market: str, symbol: str, root: pathlib.Path | None = None) -> pathlib.Path:
    return (root or RAW_ROOT) / safe_name(provider) / safe_name(market) / safe_name(symbol)


@dataclass(frozen=True)
class RawArtifact:
    path: pathlib.Path
    sidecar: dict

    @property
    def sha256(self) -> str:
        return self.sidecar["sha256"]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def store_raw(content: bytes, *, name: str, provider: str, service: str, market: str, symbol: str,
              requested_range: dict, returned_range: dict, fmt: str, endpoint: str, timezone_info: dict,
              extra: dict | None = None, key: str | None = None, root: pathlib.Path | None = None,
              retrieved_utc: str | None = None) -> RawArtifact:
    """Persist ``content`` unchanged plus its sidecar. ``key`` (if given) is scrubbed from
    every stored text field. ``synthetic`` is always recorded as False for a real fetch."""

    directory = artifact_dir(provider, market, symbol, root)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / safe_name(name)
    digest = sha256_bytes(content)
    sidecar_path = path.with_name(path.name + SIDECAR_SUFFIX)
    if path.exists():
        if sha256_bytes(path.read_bytes()) != digest:
            raise RawImmutableError(f"raw artifact {path.name} exists with different content; refusing to overwrite")
        return load_artifact(path)
    sidecar = {
        "schema": 1, "provider": provider, "service": service, "market": market, "symbol": symbol,
        "retrieved_utc": retrieved_utc or utc_now_iso(),
        "requested_range": requested_range, "returned_range": returned_range,
        "sha256": digest, "size_bytes": len(content), "format": fmt,
        "endpoint": redact(redact_url(endpoint, key), key), "timezone": timezone_info,
        "synthetic": False, "artifact": path.name, **(extra or {}),
    }
    with path.open("xb") as handle:  # exclusive create: never truncates an existing file
        handle.write(content)
    sidecar_path.write_text(json.dumps(sidecar, indent=1, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    return RawArtifact(path, sidecar)


def load_artifact(path: pathlib.Path | str) -> RawArtifact:
    path = pathlib.Path(path)
    sidecar_path = path.with_name(path.name + SIDECAR_SUFFIX)
    if not path.is_file():
        raise ProvenanceError(f"raw artifact missing: {path.name}")
    if not sidecar_path.is_file():
        raise ProvenanceError(f"provenance sidecar missing for {path.name}")
    try:
        sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise ProvenanceError(f"provenance sidecar unreadable for {path.name}") from exc
    return RawArtifact(path, sidecar)


def verify_artifact(path: pathlib.Path | str) -> RawArtifact:
    """Re-hash the bytes and check the sidecar. Raises ``ProvenanceError`` on any mismatch,
    missing field, a non-False ``synthetic`` flag, or a stored endpoint that still carries
    a credential-looking query parameter."""

    artifact = load_artifact(path)
    sidecar = artifact.sidecar
    missing = [f for f in REQUIRED_FIELDS if f not in sidecar]
    if missing:
        raise ProvenanceError(f"{artifact.path.name}: sidecar missing fields {missing}")
    data = artifact.path.read_bytes()
    if sha256_bytes(data) != sidecar["sha256"]:
        raise ProvenanceError(f"{artifact.path.name}: SHA-256 mismatch (raw bytes changed)")
    if len(data) != sidecar["size_bytes"]:
        raise ProvenanceError(f"{artifact.path.name}: byte size mismatch")
    if sidecar["synthetic"] is not False:
        raise ProvenanceError(f"{artifact.path.name}: raw real-data artifact must declare synthetic=false")
    if sidecar["artifact"] != artifact.path.name:
        raise ProvenanceError(f"{artifact.path.name}: sidecar names a different artifact")
    if re.search(r"(?i)(service_?key|api_?key|token)=(?!<REDACTED>)[^&\s]+", str(sidecar["endpoint"])):
        raise ProvenanceError(f"{artifact.path.name}: endpoint identity carries an unredacted credential")
    return artifact


def list_artifacts(provider: str, market: str, symbol: str, root: pathlib.Path | None = None) -> list[pathlib.Path]:
    directory = artifact_dir(provider, market, symbol, root)
    if not directory.is_dir():
        return []
    return sorted(p for p in directory.iterdir() if p.is_file() and not p.name.endswith(SIDECAR_SUFFIX))
