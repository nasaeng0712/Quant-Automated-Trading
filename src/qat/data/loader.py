"""Local OHLCV loader (Batch #2 Historical Data Engine).

Inputs are local files only (CSV always; Parquet when ``pyarrow`` is installed -
otherwise ``DataBlocked``). The source file is read-only: nothing is rewritten,
repaired, interpolated or dropped. Every load returns the raw-file SHA-256, a
``data_version`` and the validation report.

File contract
  CSV header (case-insensitive): timestamp, open, high, low, close, volume
  timestamp: ISO-8601 (``2024-01-02``, ``2024-01-02T09:00:00``, ``...+09:00``, ``...Z``)
  metadata: explicit ``DatasetMeta`` argument or a sidecar ``<file>.meta.json``
  with market, symbol, timeframe, timezone, timestamp_label, source, synthetic.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import pathlib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from qat.data.bars import Bar, DataBlocked, DataError, DatasetMeta, resolve_timezone, utc
from qat.data.validation import RawRow, ValidationReport, validate_rows

_COLUMNS = ("timestamp", "open", "high", "low", "close", "volume")


@dataclass
class Dataset:
    meta: DatasetMeta
    bars: list[Bar]
    path: str
    sha256: str
    data_version: str
    validation: ValidationReport

    @property
    def usable(self) -> bool:
        return self.validation.usable

    def summary(self) -> dict:
        return {
            "path": self.path,
            "sha256": self.sha256,
            "data_version": self.data_version,
            "meta": self.meta.to_dict(),
            "currency": self.meta.currency,
            "rows": self.validation.rows,
            "valid_rows": self.validation.valid_rows,
            "first_ts": self.validation.first_ts,
            "last_ts": self.validation.last_ts,
            "validation_status": self.validation.status,
        }


def read_meta(path: pathlib.Path) -> DatasetMeta:
    sidecar = path.with_name(path.name + ".meta.json")
    if not sidecar.exists():
        raise DataError(
            f"metadata missing: pass DatasetMeta or create {sidecar.name} "
            "(market, symbol, timeframe, timezone, timestamp_label, source, synthetic)"
        )
    try:
        data = json.loads(sidecar.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:  # invalid JSON / encoding
        raise DataError(f"unreadable metadata {sidecar.name}: {exc}") from exc
    if not isinstance(data, dict):
        raise DataError(f"metadata {sidecar.name} must be a JSON object")
    known = {k: data[k] for k in DatasetMeta.__dataclass_fields__ if k in data}
    return DatasetMeta(**known)


def _parse_float(text, name: str, errors: list) -> float | None:
    if text is None or str(text).strip() == "":
        errors.append(("missing_value", f"{name} empty"))
        return None
    try:
        value = float(text)
    except ValueError:
        errors.append(("unparseable_value", f"{name}={text!r}"))
        return None
    if not math.isfinite(value):
        errors.append(("non_finite_value", f"{name}={text!r}"))
        return None
    return value


def _parse_ts(text: str, tz, label: str, duration: timedelta, errors: list) -> datetime | None:
    if text is None or str(text).strip() == "":
        errors.append(("missing_timestamp", "timestamp empty"))
        return None
    try:
        dt = datetime.fromisoformat(str(text).strip())
    except ValueError:
        errors.append(("unparseable_timestamp", repr(text)))
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    dt = utc(dt)
    return dt - duration if label == "close" else dt


def _rows_from_records(records, meta: DatasetMeta) -> list[RawRow]:
    tz = resolve_timezone(meta.timezone)
    duration = meta.duration
    rows: list[RawRow] = []
    for idx, rec in enumerate(records, start=1):
        errors: list = []
        ts_text = rec.get("timestamp")
        ts = _parse_ts(ts_text, tz, meta.timestamp_label, duration, errors)
        values = {name: _parse_float(rec.get(name), name, errors) for name in _COLUMNS[1:]}
        rows.append(RawRow(row=idx, ts_text=str(ts_text), ts=ts, values=values, parse_errors=errors))
    return rows


def _read_csv(raw: bytes) -> list[dict]:
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise DataError(f"CSV is not valid UTF-8 text: {exc}") from exc
    try:
        reader = csv.DictReader(io.StringIO(text))
        if reader.fieldnames is None:
            raise DataError("CSV has no header")
        mapping = {name.strip().lower(): name for name in reader.fieldnames if name is not None}
        missing = [c for c in _COLUMNS if c not in mapping]
        if missing:
            raise DataError(f"CSV missing required columns: {missing}")
        return [{c: rec.get(mapping[c]) for c in _COLUMNS} for rec in reader]
    except csv.Error as exc:
        raise DataError(f"CSV is malformed: {exc}") from exc


def _read_parquet(path: pathlib.Path) -> list[dict]:
    try:
        import pyarrow.parquet as pq  # optional
    except ImportError as exc:
        raise DataBlocked("Parquet input needs 'pyarrow', which is not installed; use CSV") from exc
    table = pq.read_table(path)
    cols = {name.lower(): name for name in table.column_names}
    missing = [c for c in _COLUMNS if c not in cols]
    if missing:
        raise DataError(f"Parquet missing required columns: {missing}")
    data = {c: table.column(cols[c]).to_pylist() for c in _COLUMNS}
    out = []
    for i in range(table.num_rows):
        ts = data["timestamp"][i]
        out.append({"timestamp": ts.isoformat() if isinstance(ts, datetime) else ts,
                    **{c: data[c][i] for c in _COLUMNS[1:]}})
    return out


def compute_data_version(meta: DatasetMeta, sha256: str) -> str:
    meta_hash = hashlib.sha256(
        json.dumps(meta.to_dict(), sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    safe_symbol = re.sub(r"[^A-Za-z0-9]+", "-", meta.symbol).strip("-")
    return f"{meta.market}-{safe_symbol}-{meta.timeframe}-{sha256[:12]}-{meta_hash[:6]}"


def load_dataset(path: str | pathlib.Path, meta: DatasetMeta | None = None) -> Dataset:
    source = pathlib.Path(path)
    if not source.exists():
        raise FileNotFoundError(f"dataset not found: {source}")
    meta = meta or read_meta(source)
    raw = source.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    if source.suffix.lower() == ".parquet":
        records = _read_parquet(source)
    else:
        records = _read_csv(raw)
    rows = _rows_from_records(records, meta)
    report, bars = validate_rows(meta, rows)
    return Dataset(
        meta=meta,
        bars=bars,
        path=str(source.resolve()),
        sha256=sha,
        data_version=compute_data_version(meta, sha),
        validation=report,
    )


def discover_datasets(*directories: str | pathlib.Path) -> list[pathlib.Path]:
    """Data files with a sidecar meta.json under the given directories."""

    found = []
    for directory in directories:
        root = pathlib.Path(directory)
        if not root.exists():
            continue
        for candidate in sorted(root.rglob("*")):
            if candidate.suffix.lower() in (".csv", ".parquet") and candidate.with_name(
                candidate.name + ".meta.json"
            ).exists():
                found.append(candidate)
    return found
