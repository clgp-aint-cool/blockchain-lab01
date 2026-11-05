"""End-to-end test for T5 (Faults before GST and liveness after GST)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from l1sim.launch_local import available_base_port, launch_local

ROOT = Path(__file__).parents[2]

# Event logs written by node processes (not tmp_path isolated, but scenario-name isolated).
_EVENTS_DIR = ROOT / "logs/events/t5_pre_gst"

pytestmark = pytest.mark.e2e


def test_t5_pre_gst(tmp_path: Path):
    """Verify that network faults before GST preserve safety and progress resumes after GST.

    Configuration: gst_height=2 means FaultInjector applies drop_rate=0.3 and
    duplicate_rate=0.1 to all consensus messages at height=1 (before_gst = height <
    gst_height).  After height 1 is committed (GST), the network becomes clean and
    heights 2-5 must finalize quickly.

    If gst_height is ever reduced below 2, no consensus messages will be dropped
    (proposals and votes always carry height >= 1), and the fault_drop assertion below
    will catch the misconfiguration immediately.
    """
    # Clear stale event logs so we only count drops from this run.
    shutil.rmtree(_EVENTS_DIR, ignore_errors=True)

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

    # T5-specific: at least one fault_drop event must have occurred at height=1
    # (before GST). This guards against accidentally setting gst_height < 2 and
    # silently turning T5 into a clean-network test with no drops at all.
    total_drops = 0
    if _EVENTS_DIR.is_dir():
        for jsonl_file in sorted(_EVENTS_DIR.glob("V*.jsonl")):
            for line in jsonl_file.read_text(encoding="utf-8").splitlines():
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if evt.get("event") == "fault_drop":
                    total_drops += 1

    assert total_drops > 0, (
        f"Expected fault_drop events before GST (gst_height=2, drop_rate=0.3) "
        f"but found {total_drops}. "
        f"Check that gst_height >= 2 in config/t5_pre_gst.json."
    )
