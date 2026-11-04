"""Unit tests for check_reproducible."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.check_reproducible import compare_result_dirs, main


def test_compare_result_dirs_identical(tmp_path: Path):
    d1 = tmp_path / "run1"
    d2 = tmp_path / "run2"
    d1.mkdir()
    d2.mkdir()

    content = b'{"node":"V0","blocks":[{"height":1,"hash":"abc"}]}\n'
    (d1 / "V0.json").write_bytes(content)
    (d2 / "V0.json").write_bytes(content)

    ok, diffs = compare_result_dirs(d1, d2)
    assert ok is True
    assert diffs == []


def test_compare_result_dirs_mismatch_content(tmp_path: Path):
    d1 = tmp_path / "run1"
    d2 = tmp_path / "run2"
    d1.mkdir()
    d2.mkdir()

    (d1 / "V0.json").write_bytes(b'{"hash":"abc"}\n')
    (d2 / "V0.json").write_bytes(b'{"hash":"xyz"}\n')

    ok, diffs = compare_result_dirs(d1, d2)
    assert ok is False
    assert len(diffs) == 1
    assert "Mismatch in V0.json" in diffs[0]


def test_compare_result_dirs_missing_file(tmp_path: Path):
    d1 = tmp_path / "run1"
    d2 = tmp_path / "run2"
    d1.mkdir()
    d2.mkdir()

    (d1 / "V0.json").write_bytes(b"{}")
    (d2 / "V1.json").write_bytes(b"{}")

    ok, diffs = compare_result_dirs(d1, d2)
    assert ok is False
    assert any("V0.json only present" in d for d in diffs)
    assert any("V1.json only present" in d for d in diffs)


def test_compare_result_dirs_empty(tmp_path: Path):
    d1 = tmp_path / "run1"
    d2 = tmp_path / "run2"
    d1.mkdir()
    d2.mkdir()

    ok, diffs = compare_result_dirs(d1, d2)
    assert ok is False
    assert any("No result files" in d for d in diffs)


def test_compare_result_dirs_non_existent(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        compare_result_dirs(tmp_path / "none1", tmp_path)

    with pytest.raises(FileNotFoundError):
        compare_result_dirs(tmp_path, tmp_path / "none2")


def test_check_reproducible_cli_positional(tmp_path: Path):
    d1 = tmp_path / "run1"
    d2 = tmp_path / "run2"
    d1.mkdir()
    d2.mkdir()

    content = b'{"state":"ok"}'
    (d1 / "V0.json").write_bytes(content)
    (d2 / "V0.json").write_bytes(content)

    assert main([str(d1), str(d2)]) == 0


def test_check_reproducible_cli_scenario_flags(tmp_path: Path):
    r1 = tmp_path / "r1"
    r2 = tmp_path / "r2"
    (r1 / "t1").mkdir(parents=True)
    (r2 / "t1").mkdir(parents=True)

    content = b'{"state":"ok"}'
    (r1 / "t1" / "V0.json").write_bytes(content)
    (r2 / "t1" / "V0.json").write_bytes(content)

    assert main(["--scenario", "t1", "--run1", str(r1), "--run2", str(r2)]) == 0


def test_check_reproducible_cli_failure(tmp_path: Path):
    d1 = tmp_path / "run1"
    d2 = tmp_path / "run2"
    d1.mkdir()
    d2.mkdir()

    (d1 / "V0.json").write_bytes(b'{"a":1}')
    (d2 / "V0.json").write_bytes(b'{"a":2}')

    assert main([str(d1), str(d2)]) == 1


def test_compare_result_dirs_mismatch_binary(tmp_path: Path):
    d1 = tmp_path / "run1"
    d2 = tmp_path / "run2"
    d1.mkdir()
    d2.mkdir()

    # Invalid UTF-8 bytes to trigger the binary fallback
    (d1 / "V0.json").write_bytes(b"\x80\x81\x82")
    (d2 / "V0.json").write_bytes(b"\x83\x84\x85")

    ok, diffs = compare_result_dirs(d1, d2)
    assert ok is False
    assert any("Byte mismatch in binary content" in d for d in diffs)


def test_check_reproducible_cli_error():
    assert main([]) == 2
    assert main(["only_one_dir"]) == 2
    assert main(["none1", "none2"]) == 2
