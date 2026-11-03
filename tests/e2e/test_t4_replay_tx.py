"""End-to-end test for T4 (Replay Transaction Prevention)."""

from __future__ import annotations

import subprocess
import sys

import pytest

from tests.check_safety import check_safety

pytestmark = pytest.mark.e2e


def test_t4_replay_tx():
    """Verify that transactions replayed across nodes only finalize once."""
    cmd = [
        sys.executable,
        "-X",
        "utf8",
        "devnet/run.py",
        "--scenario",
        "config/t4_replay_tx.json",
        "--headless",
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    assert res.returncode == 0, f"Devnet run failed:\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}"

    verdict = check_safety("t4_replay_tx", results_root="logs/results")
    assert verdict["safety_ok"] is True
    assert verdict["same_state_at_common_height"] is True
    assert verdict["pass"] is True
