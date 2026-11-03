"""End-to-end test for T8 (Determinism & Reproducibility)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from tests.check_reproducible import compare_result_dirs
from tests.check_safety import check_safety

pytestmark = pytest.mark.e2e


def test_t8_reproducibility(tmp_path: Path):
    """Run t1_normal twice with same seed and assert byte-identical results."""
    run1_dir = tmp_path / "run1"
    run2_dir = tmp_path / "run2"
    run1_dir.mkdir()
    run2_dir.mkdir()

    cmd = [
        sys.executable,
        "-X",
        "utf8",
        "devnet/run.py",
        "--scenario",
        "config/t1_normal.json",
        "--headless",
    ]

    res1 = subprocess.run(cmd, capture_output=True, text=True)
    assert res1.returncode == 0, f"Run 1 failed: {res1.stderr}"

    results_dir = Path("logs/results/t1_normal")
    for f in results_dir.glob("*.json"):
        (run1_dir / f.name).write_bytes(f.read_bytes())

    res2 = subprocess.run(cmd, capture_output=True, text=True)
    assert res2.returncode == 0, f"Run 2 failed: {res2.stderr}"

    for f in results_dir.glob("*.json"):
        (run2_dir / f.name).write_bytes(f.read_bytes())

    identical, diffs = compare_result_dirs(run1_dir, run2_dir)
    assert identical is True, "T8 Reproducibility failure:\n" + "\n".join(diffs)

    verdict = check_safety("t1_normal", results_root="logs/results")
    assert verdict["pass"] is True
