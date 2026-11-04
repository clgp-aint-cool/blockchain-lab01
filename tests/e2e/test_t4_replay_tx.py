"""End-to-end test for T4 (Replay Transaction Prevention)."""

from __future__ import annotations

from pathlib import Path

import pytest

from l1sim.launch_local import available_base_port, launch_local

ROOT = Path(__file__).parents[2]

pytestmark = pytest.mark.e2e


def test_t4_replay_tx(tmp_path: Path):
    """Verify that transactions replayed across nodes only finalize once."""
    base_port = available_base_port(count=10, start=19600, end=25000)
    verdict = launch_local(
        scenario_path=ROOT / "config/t4_replay_tx.json",
        genesis_path=ROOT / "config/genesis.json",
        base_port=base_port,
        timeout=30.0,
        results_root=tmp_path / "results",
        quiet=True,
    )

    assert verdict["pass"] is True
    assert verdict["safety_ok"] is True
    assert verdict["same_state_at_common_height"] is True
    assert verdict["common_height"] == 5
    assert verdict["reached_max_height"] is True
    assert verdict["safety_violations"] == []
