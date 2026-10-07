"""Byte-exact mutation harness (used by the mutation scripts under artifacts/verification).

A mutation temporarily edits ONE source file, runs the tests, and restores the file. The
restore is BYTE-EXACT: files are read and written as bytes (no newline translation), the
replacement pattern is adapted to the file's own line-ending style, and the restore is
verified (SHA-256 of the file and of the whole watched tree). On Windows a text-mode
``write_text`` silently turns LF into CRLF - this module never uses it.
"""

from __future__ import annotations

import hashlib
import pathlib
import subprocess
import sys

WATCHED = ("src", "tests", "tools", "config")


def tree_sha256(root: pathlib.Path, dirs=WATCHED) -> str:
    digest = hashlib.sha256()
    for name in dirs:
        base = root / name
        if not base.exists():
            continue
        for path in sorted(base.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
                digest.update(path.relative_to(root).as_posix().encode("utf-8"))
                digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def mutate_bytes(original: bytes, old: str, new: str) -> bytes | None:
    """Replace the first ``old`` with ``new`` keeping the file's line-ending style.
    ``None`` when the pattern is absent."""

    text = original.decode("utf-8")
    eol = "\r\n" if "\r\n" in text else "\n"
    old_n, new_n = old.replace("\n", eol), new.replace("\n", eol)
    if old_n not in text:
        return None
    return text.replace(old_n, new_n, 1).encode("utf-8")


class RestoreError(RuntimeError):
    pass


def run_mutation(target: pathlib.Path, old: str, new: str, runner):
    """Apply, run ``runner()``, restore byte-exactly. Returns (status, runner_result):
    status ``NOT_FOUND`` (pattern absent) or ``RAN``. Raises ``RestoreError`` if the file
    is not byte-identical afterwards (never silently)."""

    original = target.read_bytes()
    mutated = mutate_bytes(original, old, new)
    if mutated is None:
        return "NOT_FOUND", None
    if mutated == original:
        raise ValueError("mutation does not change the file")
    target.write_bytes(mutated)
    try:
        result = runner()
    finally:
        target.write_bytes(original)
        if target.read_bytes() != original:
            raise RestoreError(f"{target} was not restored byte-exactly")
    return "RAN", result


def pytest_runner(python: str, root: pathlib.Path, tests: list[str]):
    def run():
        r = subprocess.run([python, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-x", *tests],
                           capture_output=True, text=True, timeout=1800, cwd=root)
        fails = [ln for ln in r.stdout.splitlines() if ln.startswith("FAILED")][:1]
        return r.returncode != 0, fails
    return run


def run_all(muts, *, root: pathlib.Path, tests: list[str], only: list[str] | None = None, python: str | None = None) -> int:
    python = python or sys.executable
    before = tree_sha256(root)
    survivors = 0
    for entry in muts:
        label, path, old, new = entry[:4]
        entry_tests = entry[4] if len(entry) > 4 else tests
        if only and not any(o in label for o in only):
            continue
        status, result = run_mutation(root / path, old, new, pytest_runner(python, root, entry_tests))
        if status == "NOT_FOUND":
            survivors += 1
            print((label, "PATTERN NOT FOUND"))
            continue
        caught, fails = result
        survivors += 0 if caught else 1
        print((label, "CAUGHT" if caught else "SURVIVED", fails))
    after = tree_sha256(root)
    print(f"watched tree restored byte-exactly: {'YES' if before == after else 'NO'}")
    print(f"survivors/not-found: {survivors}")
    return 1 if (survivors or before != after) else 0
