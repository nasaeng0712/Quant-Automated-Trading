"""Batch #2 - Historical Data Engine: loading, validation, fixtures."""

from __future__ import annotations

import hashlib
import json
import pathlib

import pytest

from qat.data.bars import DataBlocked, DataError, DatasetMeta
from qat.data.loader import discover_datasets, load_dataset
from qat.data.synthetic import FIXTURES, write_all

ROOT = pathlib.Path(__file__).resolve().parents[1]
FIXTURE_DIR = ROOT / "data" / "fixtures"
HEADER = "timestamp,open,high,low,close,volume\n"


def _write(tmp_path, body, meta=None, name="d.csv"):
    path = tmp_path / name
    path.write_text(HEADER + body, encoding="utf-8")
    meta = meta or {"market": "CRYPTO", "symbol": "TST/KRW", "timeframe": "1h", "timezone": "UTC",
                    "timestamp_label": "open", "source": "LOCAL_FILE", "synthetic": False}
    (tmp_path / (name + ".meta.json")).write_text(json.dumps(meta), encoding="utf-8")
    return path


def _codes(ds):
    return {i["code"]: i for i in ds.validation.issues}


GOOD = (
    "2024-01-01T00:00:00,100,101,99,100.5,10\n"
    "2024-01-01T01:00:00,100.5,102,100,101,12\n"
    "2024-01-01T02:00:00,101,101.5,100.2,100.8,9\n"
)


def test_committed_fixtures_validate_and_are_marked_synthetic():
    paths = discover_datasets(FIXTURE_DIR)
    assert len(paths) == len(FIXTURES)
    for path in paths:
        ds = load_dataset(path)
        assert ds.validation.status == "PASS", ds.validation.issues
        assert ds.meta.synthetic is True and ds.meta.source == "SYNTHETIC"
        assert ds.meta.symbol.startswith("SYN")
        assert ds.validation.valid_rows == ds.validation.rows


def test_fixture_generation_is_deterministic(tmp_path):
    write_all(tmp_path)
    for spec in FIXTURES:
        a = (FIXTURE_DIR / f"{spec[0]}.csv").read_bytes()
        b = (tmp_path / f"{spec[0]}.csv").read_bytes()
        assert hashlib.sha256(a).hexdigest() == hashlib.sha256(b).hexdigest()


def test_good_file_passes_and_raw_file_untouched(tmp_path):
    path = _write(tmp_path, GOOD)
    before = path.read_bytes()
    ds = load_dataset(path)
    assert ds.validation.status == "PASS"
    assert len(ds.bars) == 3
    assert ds.bars[0].ts.isoformat() == "2024-01-01T00:00:00+00:00"
    assert path.read_bytes() == before
    assert ds.sha256 == hashlib.sha256(before).hexdigest()
    assert ds.sha256[:12] in ds.data_version


@pytest.mark.parametrize(
    "body,code",
    [
        (GOOD.replace("100.5,102,100,101", "100.5,100,100,101"), "ohlc_inconsistent"),
        (GOOD.replace(",12\n", ",-1\n"), "negative_volume"),
        (GOOD.replace("100.5,102,100,101", "100.5,102,0,101"), "non_positive_price"),
        (GOOD.replace("100.5,102,100,101", "100.5,102,100,nan"), "non_finite_value"),
        (GOOD.replace("100.5,102,100,101", "100.5,102,100,"), "missing_value"),
        (GOOD.replace("2024-01-01T01:00:00", "not-a-date"), "unparseable_timestamp"),
        (GOOD.replace("2024-01-01T02:00:00", "2024-01-01T01:00:00"), "duplicate_timestamp"),
        (GOOD.replace("2024-01-01T02:00:00", "2023-12-31T23:00:00"), "unsorted_timestamp"),
    ],
)
def test_bad_rows_fail_and_are_not_silently_dropped(tmp_path, body, code):
    ds = load_dataset(_write(tmp_path, body))
    assert ds.validation.status == "FAIL"
    assert code in _codes(ds)
    assert ds.validation.rows == 3  # every input row is accounted for
    assert not ds.usable


def test_zero_volume_is_a_warning(tmp_path):
    ds = load_dataset(_write(tmp_path, GOOD.replace(",9\n", ",0\n")))
    assert ds.validation.status == "WARN"
    assert _codes(ds)["zero_volume"]["severity"] == "WARN"


def test_crypto_gap_is_missing_data(tmp_path):
    ds = load_dataset(_write(tmp_path, GOOD.replace("2024-01-01T02:00:00", "2024-01-01T05:00:00")))
    assert ds.validation.status == "WARN"
    assert ds.validation.gap_summary == {"missing_data_gap": 1}


def test_kr_daily_weekend_vs_weekday_gap(tmp_path):
    meta = {"market": "KR", "symbol": "TEST", "timeframe": "1d", "timezone": "+09:00",
            "timestamp_label": "open", "source": "LOCAL_FILE", "synthetic": False}
    body = (
        "2024-01-04,100,101,99,100,10\n"   # Thu
        "2024-01-05,100,101,99,100,10\n"   # Fri
        "2024-01-08,100,101,99,100,10\n"   # Mon (weekend gap -> INFO)
        "2024-01-10,100,101,99,100,10\n"   # Wed (Tue missing -> WARN)
    )
    ds = load_dataset(_write(tmp_path, body, meta))
    assert ds.validation.gap_summary == {"session_closed_weekend": 1, "unexplained_weekday_gap": 1}
    assert ds.validation.status == "WARN"
    assert "holidays NOT modelled" in ds.validation.calendar_note


def test_close_label_is_normalised_to_open_time(tmp_path):
    meta = {"market": "CRYPTO", "symbol": "TST/KRW", "timeframe": "1h", "timezone": "+09:00",
            "timestamp_label": "close", "source": "LOCAL_FILE", "synthetic": False}
    ds = load_dataset(_write(tmp_path, GOOD, meta))
    # 00:00 KST close-label -> open 23:00 KST (prev day) -> 2023-12-31T14:00Z
    assert ds.bars[0].ts.isoformat() == "2023-12-31T14:00:00+00:00"


def test_missing_metadata_is_an_error(tmp_path):
    path = tmp_path / "x.csv"
    path.write_text(HEADER + GOOD, encoding="utf-8")
    with pytest.raises(DataError):
        load_dataset(path)


def test_missing_columns_is_an_error(tmp_path):
    path = tmp_path / "x.csv"
    path.write_text("timestamp,open,close\n2024-01-01,1,1\n", encoding="utf-8")
    with pytest.raises(DataError):
        load_dataset(path, DatasetMeta("CRYPTO", "A/KRW", "1h", "UTC"))


def test_parquet_without_pyarrow_is_blocked(tmp_path):
    import importlib.util

    if importlib.util.find_spec("pyarrow") is not None:
        pytest.skip("pyarrow installed - BLOCKED path not applicable")
    path = tmp_path / "x.parquet"
    path.write_bytes(b"PAR1")
    with pytest.raises(DataBlocked):
        load_dataset(path, DatasetMeta("CRYPTO", "A/KRW", "1h", "UTC"))


def test_synthetic_meta_must_declare_synthetic_source():
    with pytest.raises(DataError):
        DatasetMeta("KR", "X", "1d", "UTC", source="LOCAL_FILE", synthetic=True)
