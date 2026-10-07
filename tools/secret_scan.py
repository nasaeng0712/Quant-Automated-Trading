"""Credential leak scan. Reads the serviceKey from the local key file at run time and
searches the repository for ANY textual form of it (raw, decoded, percent-encoded). It
prints only counts and relative file paths - never the key, a prefix, a suffix or a length.

    python tools/secret_scan.py [--key-file PATH] [--root .]
Exit code 0 = no leak found, 1 = leak found, 2 = key file unavailable.
"""

from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from qat.realdata.secrets import CredentialError, key_variants, read_key  # noqa: E402

SKIP_DIRS = {".venv", ".git", "__pycache__", ".pytest_cache", "node_modules"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--key-file", default=None)
    parser.add_argument("--root", default=".")
    args = parser.parse_args()
    try:
        key = read_key(args.key_file)
    except CredentialError:
        print("key file unavailable: leak scan could not run")
        return 2
    needles = [v.encode("utf-8") for v in key_variants(key)]
    root = pathlib.Path(args.root).resolve()
    scanned, leaks = 0, []
    for path in root.rglob("*"):
        if not path.is_file() or any(part in SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        scanned += 1
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if any(n in data for n in needles):
            leaks.append(path.relative_to(root).as_posix())
    print(f"files scanned: {scanned}")
    print(f"credential leak found: {'YES' if leaks else 'NO'}")
    for rel in leaks:
        print("  LEAK in:", rel)
    return 1 if leaks else 0


if __name__ == "__main__":
    raise SystemExit(main())
