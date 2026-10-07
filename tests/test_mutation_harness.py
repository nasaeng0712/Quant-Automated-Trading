"""The mutation harness must restore source bytes exactly (no Windows line-ending drift)."""

import hashlib
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "tools"))
import mutation_harness as mh  # noqa: E402


def _sha(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize("eol", [b"\n", b"\r\n", b"a\r\nb\nc\r\n"])
def test_restore_is_byte_exact_for_lf_crlf_and_mixed_files(tmp_path, eol):
    target = tmp_path / "mod.py"
    original = b"x = 1" + (eol if eol != b"a\r\nb\nc\r\n" else b"\r\n") + b"if flag:" + (b"\r\n" if b"\r\n" in eol else b"\n") + b"    y = 2\n"
    target.write_bytes(original)
    before = _sha(target)
    seen = {}

    def runner():
        seen["mutated"] = target.read_bytes()
        return True, []

    status, _ = mh.run_mutation(target, "if flag:", "if False:", runner)
    assert status == "RAN"
    assert b"if False:" in seen["mutated"] and seen["mutated"] != original
    assert target.read_bytes() == original and _sha(target) == before


def test_multiline_pattern_adapts_to_file_line_endings(tmp_path):
    for eol in (b"\n", b"\r\n"):
        target = tmp_path / ("m%d.py" % len(eol))
        original = b"a = 1" + eol + b"b = 2" + eol + b"c = 3" + eol
        target.write_bytes(original)
        status, _ = mh.run_mutation(target, "a = 1\nb = 2", "a = 9\nb = 2", lambda: (True, []))
        assert status == "RAN" and target.read_bytes() == original


def test_restore_happens_even_when_the_runner_raises(tmp_path):
    target = tmp_path / "m.py"
    original = b"v = 1\r\n"
    target.write_bytes(original)

    def boom():
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        mh.run_mutation(target, "v = 1", "v = 2", boom)
    assert target.read_bytes() == original


def test_missing_pattern_reports_not_found_and_leaves_file_untouched(tmp_path):
    target = tmp_path / "m.py"
    target.write_bytes(b"v = 1\n")
    status, result = mh.run_mutation(target, "nope", "x", lambda: (True, []))
    assert (status, result) == ("NOT_FOUND", None) and target.read_bytes() == b"v = 1\n"


def test_noop_mutation_is_refused(tmp_path):
    target = tmp_path / "m.py"
    target.write_bytes(b"v = 1\n")
    with pytest.raises(ValueError):
        mh.run_mutation(target, "v = 1", "v = 1", lambda: (True, []))


def test_tree_hash_detects_any_byte_change(tmp_path):
    (tmp_path / "src").mkdir()
    f = tmp_path / "src" / "a.py"
    f.write_bytes(b"x\n")
    h1 = mh.tree_sha256(tmp_path)
    f.write_bytes(b"x\r\n")
    assert mh.tree_sha256(tmp_path) != h1
    f.write_bytes(b"x\n")
    assert mh.tree_sha256(tmp_path) == h1
