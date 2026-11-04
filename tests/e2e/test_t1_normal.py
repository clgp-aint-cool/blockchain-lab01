"""End-to-end test for T1 (Normal consensus progression using launch_local)."""

from __future__ import annotations

from pathlib import Path

import pytest

from l1sim.launch_local import available_base_port, launch_local

ROOT = Path(__file__).parents[2]

pytestmark = pytest.mark.e2e


def test_t1_normal(tmp_path: Path):
    """Run 10 validators on t1_normal using launch_local and verify finality."""
    base_port = available_base_port(count=10, start=19500, end=25000)
    verdict = launch_local(
        scenario_path=ROOT / "config/t1_normal.json",
        genesis_path=ROOT / "config/genesis.json",
        base_port=base_port,
        timeout=30.0,
        results_root=tmp_path / "custom_results",
        quiet=True,
    )

    assert verdict["pass"] is True
    assert verdict["safety_ok"] is True
    assert verdict["same_state_at_common_height"] is True
    assert verdict["common_height"] == 5
    assert verdict["reached_max_height"] is True
    assert verdict["safety_violations"] == []
