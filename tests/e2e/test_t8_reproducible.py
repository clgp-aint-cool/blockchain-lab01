"""End-to-end test for T8 (Determinism & Reproducibility)."""

from __future__ import annotations

from pathlib import Path

import pytest

from l1sim.launch_local import available_base_port, launch_local
from tests.check_reproducible import compare_result_dirs
from tests.check_safety import check_safety

ROOT = Path(__file__).parents[2]

pytestmark = pytest.mark.e2e


def test_t8_reproducibility(tmp_path: Path):
    """Run t1_normal twice with same seed and assert byte-identical results."""
    run1_dir = tmp_path / "run1"
    run2_dir = tmp_path / "run2"
    run1_dir.mkdir()
    run2_dir.mkdir()

    base_port1 = available_base_port(count=10, start=19700, end=25000)
    verdict1 = launch_local(
        scenario_path=ROOT / "config/t1_normal.json",
        genesis_path=ROOT / "config/genesis.json",
        base_port=base_port1,
        timeout=30.0,
        results_root=run1_dir,
        quiet=True,
    )
    assert verdict1["pass"] is True

    base_port2 = available_base_port(count=10, start=19800, end=25000)
    verdict2 = launch_local(
        scenario_path=ROOT / "config/t1_normal.json",
        genesis_path=ROOT / "config/genesis.json",
        base_port=base_port2,
        timeout=30.0,
        results_root=run2_dir,
        quiet=True,
    )
    assert verdict2["pass"] is True

    identical, diffs = compare_result_dirs(run1_dir, run2_dir)
    assert identical is True, "T8 Reproducibility failure:\n" + "\n".join(diffs)

    verdict = check_safety("t1_normal", results_root=run1_dir)
    assert verdict["pass"] is True
