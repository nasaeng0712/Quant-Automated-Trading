"""Atomic state writes and corrupt-state detection.

Single-process safety only: QAT's state files (latches, recovery state, registries) assume ONE writer process.
Multi-process / multi-host coordination is NOT supported and is not simulated here (see the runbook).
"""

from __future__ import annotations

import json
import os
import pathlib
import tempfile


class StateCorrupt(RuntimeError):
    """A persisted state file exists but cannot be trusted (invalid JSON / wrong shape)."""


def atomic_write_text(path: pathlib.Path, text: str) -> None:
    """Write to a temp file in the same directory, fsync, then ``os.replace`` (readers see the old or the new file, never a partial one)."""

    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_json(path: pathlib.Path, obj, *, indent: int | None = 2) -> None:
    atomic_write_text(path, json.dumps(obj, indent=indent, ensure_ascii=False, sort_keys=True) + "\n")


def read_json_strict(path: pathlib.Path, *, default=None, expect=dict):
    """Missing file -> ``default``. Present but unreadable / wrong type -> ``StateCorrupt`` (never a silent reset)."""

    path = pathlib.Path(path)
    if not path.exists():
        return default
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise StateCorrupt(f"{path.name}: {type(exc).__name__}: {exc}") from exc
    if expect is not None and not isinstance(data, expect):
        raise StateCorrupt(f"{path.name}: expected {expect.__name__}, found {type(data).__name__}")
    return data
