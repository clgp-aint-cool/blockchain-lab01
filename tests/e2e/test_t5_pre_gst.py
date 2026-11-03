"""End-to-end test for T5 (Faults before GST and liveness after GST)."""

from __future__ import annotations

import subprocess
import sys

import pytest

from tests.check_safety import check_safety

pytestmark = pytest.mark.e2e


def test_t5_pre_gst():
    """Verify that network faults before GST preserve safety and progress resumes after GST."""
    cmd = [
        sys.executable,
        "-X",
        "utf8",
        "devnet/run.py",
        "--scenario",
        "config/t5_pre_gst.json",
        "--headless",
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    assert res.returncode == 0, f"Devnet run failed:\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}"

    verdict = check_safety("t5_pre_gst", results_root="logs/results")
    assert verdict["safety_ok"] is True
    assert verdict["same_state_at_common_height"] is True
    assert verdict["pass"] is True
