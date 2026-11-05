"""Exercise the grading entry point from outside the repository."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]


@pytest.mark.parametrize("select_none", [False, True])
def test_runner_selects_t3_without_pythonpath_and_preserves_exit_code(tmp_path, select_none):
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    command = [
        sys.executable,
        str(ROOT / "tests/run_tests.py"),
        "--scenario",
        "T3",
        "--",
        "--collect-only",
        "-q",
        "-p",
        "no:cacheprovider",
    ]
    if select_none:
        command += ["-k", "no_tests_match_this_name"]
    result = subprocess.run(
        command, cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30, check=False
    )
    assert result.returncode == (5 if select_none else 0), result.stdout + result.stderr
    if not select_none:
        assert "test_t3_invalid_signature_wrong_domain" in result.stdout
        assert "1 test collected" in result.stdout


def test_runner_collects_t3_in_standalone_layout_without_project_metadata(tmp_path):
    standalone = tmp_path / "standalone"
    standalone.mkdir()
    for directory in ("src", "tests", "config"):
        shutil.copytree(
            ROOT / directory,
            standalone / directory,
            ignore=shutil.ignore_patterns("__pycache__", "*.egg-info"),
        )
    (standalone / "logs").mkdir()
    assert not (standalone / "pyproject.toml").exists()
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    result = subprocess.run(
        [
            sys.executable,
            str(standalone / "tests/run_tests.py"),
            "--scenario",
            "T3",
            "--",
            "--collect-only",
            "-q",
            "-p",
            "no:cacheprovider",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "test_t3_invalid_signature_wrong_domain" in result.stdout
    assert "1 test collected" in result.stdout
    assert "PytestUnknownMarkWarning" not in result.stdout + result.stderr
