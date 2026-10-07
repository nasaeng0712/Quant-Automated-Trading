"""Real-data CLI.

  python -m qat.realdata acquire crypto|crypto-crosscheck|us|kr-crosscheck
  python -m qat.realdata acquire kr-official        (reads the serviceKey file; never prints it)
  python -m qat.realdata key-status
  python -m qat.realdata verify-raw
"""

from __future__ import annotations

import argparse
import sys

from qat.realdata import acquire
from qat.realdata.provenance import RAW_ROOT, SIDECAR_SUFFIX, ProvenanceError, verify_artifact
from qat.realdata.secrets import CredentialError, key_file_status, read_key, redact


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m qat.realdata")
    sub = parser.add_subparsers(dest="cmd", required=True)
    acq = sub.add_parser("acquire")
    acq.add_argument("target", choices=["crypto", "crypto-crosscheck", "us", "kr-crosscheck", "kr-official"])
    acq.add_argument("--key-file", default=None)
    acq.add_argument("--key-mode", default="auto", choices=["auto", "as_is", "quote"])
    ks = sub.add_parser("key-status")
    ks.add_argument("--key-file", default=None)
    sub.add_parser("verify-raw")
    sub.add_parser("evidence")
    args = parser.parse_args(argv)

    if args.cmd == "key-status":
        status = key_file_status(args.key_file)
        print(f"key file exists: {'YES' if status['key_file_exists'] else 'NO'}")
        print(f"non-empty: {'YES' if status['key_non_empty'] else 'NO'}")
        return 0
    if args.cmd == "evidence":
        import json

        from qat.realdata.evidence import SecretLeak, export_all

        try:
            print(json.dumps(export_all(), indent=1, ensure_ascii=False))
        except SecretLeak as exc:
            print("ABORTED:", exc)
            return 4
        return 0
    if args.cmd == "verify-raw":
        bad = 0
        for sidecar in sorted(RAW_ROOT.rglob("*" + SIDECAR_SUFFIX)):
            artifact = sidecar.with_name(sidecar.name[: -len(SIDECAR_SUFFIX)])
            try:
                verify_artifact(artifact)
            except ProvenanceError as exc:
                bad += 1
                print("FAIL", exc)
        print("raw artifacts verified; failures:", bad)
        return 1 if bad else 0

    key = None
    if args.target == "kr-official":
        try:
            key = read_key(args.key_file)
        except CredentialError as exc:
            print(f"credential error: {exc}")
            return 2

    def log(message: str) -> None:
        print(redact(message, key))
        sys.stdout.flush()

    if args.target == "crypto":
        acquire.acquire_crypto(log)
    elif args.target == "crypto-crosscheck":
        acquire.acquire_crypto_crosscheck(log)
    elif args.target == "us":
        acquire.acquire_us(log)
    elif args.target == "kr-crosscheck":
        acquire.acquire_kr_crosscheck(log)
    else:
        from qat.realdata.fetch import FetchError

        try:
            summary = acquire.acquire_kr_official(log, key, key_mode=args.key_mode)
            log(f"kr-official done: {summary}")
        except FetchError as exc:
            log(f"kr-official FAILED: {exc}")
            return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
