"""Research admission for REAL datasets (Batch #3A, Phase 11) - fail-closed.

A dataset is "real" when its metadata carries ``extra.real_data`` (identity pointer) OR
when its ``source`` names a real provider. Real datasets are admitted for research only
if EVERYTHING re-verifies at admission time:

  1. identity file hash matches the pointer in the metadata (no edited identity)
  2. identity fields agree with the metadata (market, symbol, timezone, provider, synthetic=false)
  3. the dataset file bytes hash to the identity's normalized_sha256 (no row/OHLC edit)
  4. raw artifacts exist and re-hash to the recorded SHA-256 (provenance reproducible)
  5. re-normalizing the raw bytes reproduces the identical normalized hash / data_version
  6. the calendar snapshot hash is the one the identity was built against
  7. re-validation is PASS (adjustment semantics usable, no unexpected trading-day gap, ...)

Any failure raises ``DataRejected`` (a domain rejection). Datasets that are not real
(synthetic fixtures, legacy local files without a real-provider source) are unaffected.
"""

from __future__ import annotations

import hashlib
import json
import pathlib

from qat.data.bars import DataRejected
from qat.realdata.calendars import CALENDAR_DIR, CalendarError
from qat.realdata.provenance import ProvenanceError

REAL_PROVIDERS = frozenset({"binance-vision", "coinbase-exchange", "yahoo-chart", "nasdaq-historical", "naver-fchart", "data.go.kr"})
_CACHE: dict = {}


def is_real(meta) -> bool:
    return bool(meta.extra.get("real_data")) or str(meta.source).lower() in REAL_PROVIDERS


def _stat(path: pathlib.Path):
    try:
        st = path.stat()
        return (str(path), st.st_size, st.st_mtime_ns)
    except OSError:
        return (str(path), None, None)


def _evaluate_view_admission(dataset) -> dict:
    """Protocol v2 continuous research VIEW of the BTCUSDT 4h parent (Batch #3E/#3F). Admitted only if the view identity is intact, the
    dataset bytes are exactly the view, the metadata agrees, and a fresh derivation from the preserved raw archives reproduces the identity
    byte for byte with every check PASS. The failed parent itself (no view pointer) is never admitted by this path."""

    from qat.realdata import intraday  # local import: heavy module

    meta = dataset.meta
    reasons: list[str] = []
    path = pathlib.Path(dataset.path).parent / str(meta.extra.get("intraday_view_identity_file", ""))
    if not path.is_file():
        return {"applicable": True, "admitted": False, "reasons": ["view identity file missing"]}
    identity_bytes = path.read_bytes()
    identity = json.loads(identity_bytes)
    if identity.get("kind") != "CONTINUOUS_VIEW" or identity.get("view_status") != intraday.VIEW_STATUS_ADMITTED:
        reasons.append("view identity is not an ADMITTED_CONTINUOUS_SUBPERIOD")
    if dataset.sha256 != identity.get("view_normalized_sha256"):
        reasons.append("dataset bytes do not match the view identity (rows or values changed)")
    for field, got, want in (("market", meta.market, "CRYPTO"), ("symbol", meta.symbol, "BTC/USDT"), ("timezone", meta.timezone, "UTC"),
                             ("provider", meta.source, intraday.PROVIDER), ("timeframe", meta.timeframe, "4h")):
        if got != want:
            reasons.append(f"metadata {field} {got!r} contradicts the view ({want!r})")
    if meta.synthetic is not False or identity.get("synthetic") is not False:
        reasons.append("synthetic flag contradicts real-data identity")
    if meta.extra.get("view_data_version") != identity.get("view_data_version"):
        reasons.append("metadata view_data_version differs from the identity")
    if reasons:
        return {"applicable": True, "admitted": False, "reasons": reasons}
    try:
        fresh = intraday.derive_view(intraday.build())
    except Exception as exc:  # noqa: BLE001 - any failure to reproduce the view from raw is a rejection
        return {"applicable": True, "admitted": False, "reasons": [f"view cannot be reproduced from the preserved raw archives: {type(exc).__name__}: {exc}"]}
    if intraday.identity_bytes(fresh["identity"]) != identity_bytes:
        reasons.append("a fresh derivation from raw does not reproduce the view identity")
    if fresh["identity"]["validation"]["status"] != "PASS" or fresh["identity"]["view_status"] != intraday.VIEW_STATUS_ADMITTED:
        reasons.append(f"view validation is {fresh['identity']['validation']['status']}")
    return {"applicable": True, "admitted": not reasons, "reasons": reasons, "data_version": identity["view_data_version"], "validation_status": fresh["identity"]["validation"]["status"],
            "adjustment_semantics": "UNADJUSTED", "admission_status": identity["view_status"],
            "covered_start": identity["continuous_start"][:10], "covered_end": identity["continuous_end"][:10]}


def _evaluate_admission(dataset) -> dict:
    from qat.realdata.datasets import evaluate, spec_from_dict  # local import: heavy module

    meta = dataset.meta
    if meta.extra.get("intraday_view_identity_file"):
        return _evaluate_view_admission(dataset)
    reasons: list[str] = []
    marker = meta.extra.get("real_data")
    if not marker:
        return {"applicable": True, "admitted": False,
                "reasons": [f"source {meta.source!r} is a real provider but the dataset has no identity (extra.real_data)"]}
    folder = pathlib.Path(dataset.path).parent
    identity_path = folder / str(marker.get("identity_file", ""))
    if not identity_path.is_file():
        return {"applicable": True, "admitted": False, "reasons": ["identity file missing"]}
    identity_bytes = identity_path.read_bytes()
    if hashlib.sha256(identity_bytes).hexdigest() != marker.get("identity_sha256"):
        return {"applicable": True, "admitted": False, "reasons": ["identity file hash differs from the metadata pointer (identity edited)"]}
    identity = json.loads(identity_bytes)
    for field, got in (("market", meta.market), ("symbol", meta.symbol), ("timezone", meta.timezone), ("provider", meta.source)):
        if identity.get(field) != got:
            reasons.append(f"metadata {field} {got!r} contradicts identity {identity.get(field)!r}")
    if meta.synthetic or identity.get("synthetic") is not False:
        reasons.append("synthetic flag contradicts real-data identity")
    if marker.get("data_version") != identity.get("data_version"):
        reasons.append("metadata data_version differs from identity")
    if dataset.sha256 != identity.get("normalized_sha256"):
        reasons.append("dataset bytes do not match identity normalized_sha256 (rows or values changed)")
    if reasons:
        return {"applicable": True, "admitted": False, "reasons": reasons}
    try:
        evaluation = evaluate(spec_from_dict(identity["spec"]))
    except (ProvenanceError, CalendarError, ValueError, KeyError, OSError) as exc:
        return {"applicable": True, "admitted": False, "reasons": [f"raw provenance cannot be reproduced: {type(exc).__name__}: {exc}"]}
    fresh = evaluation["identity"]
    for field in ("normalized_sha256", "raw_set_sha256", "data_version", "transformation_manifest_sha256"):
        if fresh[field] != identity[field]:
            reasons.append(f"re-normalization from raw does not reproduce {field}")
    if fresh["calendar"]["sha256"] != identity["calendar"]["sha256"]:
        reasons.append("calendar snapshot differs from the one the identity was built against")
    if fresh["validation_status"] != "PASS":
        failed = fresh["validation_failed_checks"] + fresh["validation_unknown_checks"]
        reasons.append(f"validation is {fresh['validation_status']} (checks {failed})")
    if identity["validation_status"] != fresh["validation_status"]:
        reasons.append("stored validation status differs from the re-run")
    return {"applicable": True, "admitted": not reasons, "reasons": reasons,
            "data_version": fresh["data_version"], "validation_status": fresh["validation_status"],
            "adjustment_semantics": fresh["adjustment_semantics"],
            "covered_start": fresh["date_range"]["first"], "covered_end": fresh["date_range"]["last"]}


def check_admission(dataset) -> dict:
    """Cached by every input that could change the answer (file stats, not only content)."""

    if not is_real(dataset.meta):
        return {"applicable": False, "admitted": True, "reasons": []}
    marker = dataset.meta.extra.get("real_data") or {}
    stamp = (dataset.sha256, tuple(_stat(pathlib.Path(dataset.path).parent / str(marker.get("identity_file", "")))),
             tuple(_stat(p) for p in sorted(CALENDAR_DIR.glob("*.json"))), json.dumps(dataset.meta.to_dict(), sort_keys=True, default=str))
    # raw artifact stats are part of the cache key once the identity is readable
    try:
        identity = json.loads((pathlib.Path(dataset.path).parent / str(marker.get("identity_file", ""))).read_text(encoding="utf-8"))
        raw_stats = tuple(_stat(pathlib.Path(__file__).resolve().parents[3] / p) for p in identity["spec"]["raw_files"])
        raw_stats += tuple(_stat(pathlib.Path(__file__).resolve().parents[3] / (p + ".provenance.json")) for p in identity["spec"]["raw_files"])
    except (OSError, ValueError, KeyError):
        raw_stats = ()
    if dataset.meta.extra.get("intraday_view_identity_file"):
        from qat.realdata import intraday

        arts = intraday.raw_artifacts()
        raw_stats = tuple(_stat(a) for a in arts) + tuple(_stat(intraday.checksum_artifact(a)) for a in arts)
    key = (stamp, raw_stats)
    if key not in _CACHE:
        _CACHE.clear()
        _CACHE[key] = _evaluate_admission(dataset)
    return _CACHE[key]


def require_coverage(dataset, requested_start: str | None, requested_end: str | None, *, allow_subperiod: bool = False) -> None:
    """Explicit period guard (Batch #3A.2). A requested period is never truncated or extended:
    it must equal the verified coverage. Outside it -> ``DataRejected``; narrower than it ->
    ``DataRejected`` too unless ``allow_subperiod`` (the Batch #3B research protocol selects its
    development windows explicitly and passes True; the UI/API never does).
    Datasets without a verified identity cannot vouch for any period (fail-closed)."""

    if requested_start is None and requested_end is None:
        return
    require_admitted(dataset)
    result = check_admission(dataset)
    if not result["applicable"] or "covered_start" not in result:
        raise DataRejected("requested period cannot be verified: the dataset has no verified coverage record")
    lo, hi = result["covered_start"], result["covered_end"]
    start, end = requested_start or lo, requested_end or hi
    if start < lo or end > hi:
        raise DataRejected(f"requested period exceeds verified dataset coverage: requested {start}..{end}, "
                           f"verified {lo}..{hi}; no truncation, backfill or substitute source is used")
    if not allow_subperiod and (start, end) != (lo, hi):
        raise DataRejected(f"period selection inside the verified coverage is not supported yet: requested {start}..{end}, "
                           f"verified {lo}..{hi}")


def require_admitted(dataset) -> None:
    result = check_admission(dataset)
    if not result["admitted"]:
        raise DataRejected(f"real dataset {dataset.meta.symbol} ({dataset.meta.source}) not admitted for research: "
                           + "; ".join(result["reasons"]))
