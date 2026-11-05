"""End-to-end test for T5 (Faults before GST and liveness after GST)."""

from __future__ import annotations

from pathlib import Path

import pytest

from l1sim.launch_local import available_base_port, launch_local

ROOT = Path(__file__).parents[2]

pytestmark = pytest.mark.e2e


def test_t5_pre_gst(tmp_path: Path):
    """Verify that network faults before GST preserve safety and progress resumes after GST.

    The scenario runs with 30% packet drop and 10% duplication before GST
    (height 2).  After GST the network stabilises and all 10 honest nodes must
    reach max_height=5 with identical state.
    """
    base_port = available_base_port(count=10, start=20500, end=25000)
    verdict = launch_local(
        scenario_path=ROOT / "config/t5_pre_gst.json",
        genesis_path=ROOT / "config/genesis.json",
        base_port=base_port,
        timeout=90.0,
        results_root=tmp_path / "results",
        quiet=True,
    )

    assert verdict["pass"] is True
    assert verdict["safety_ok"] is True
    assert verdict["same_state_at_common_height"] is True
    assert verdict["reached_max_height"] is True
