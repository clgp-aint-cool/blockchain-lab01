"""Unit tests for l1sim.launch_local."""

from __future__ import annotations

import json
import socket
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from l1sim.launch_local import (
    _get_node_height,
    available_base_port,
    launch_local,
    main,
    wait_for_port,
)

ROOT = Path(__file__).parents[2]


def test_available_base_port():
    port = available_base_port(count=2, start=21000, end=22000)
    assert 21000 <= port < 22000

    # Test error when range is empty
    with pytest.raises(RuntimeError, match="no free range"):
        available_base_port(count=10, start=22000, end=22000)


def test_wait_for_port_success():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    port = sock.getsockname()[1]
    try:
        assert wait_for_port("127.0.0.1", port, timeout=1.0) is True
    finally:
        sock.close()


def test_wait_for_port_timeout():
    # An unopened port should time out quickly
    assert wait_for_port("127.0.0.1", 29999, timeout=0.1) is False


def test_get_node_height(tmp_path: Path):
    assert _get_node_height(tmp_path, "V0") == 0

    bad_file = tmp_path / "V1.json"
    bad_file.write_text("not json", encoding="utf-8")
    assert _get_node_height(tmp_path, "V1") == 0

    v2_file = tmp_path / "V2.json"
    v2_file.write_text(json.dumps({"finalized_height": 7}), encoding="utf-8")
    assert _get_node_height(tmp_path, "V2") == 7

    v3_file = tmp_path / "V3.json"
    v3_file.write_text(json.dumps({"block_hashes": ["h1", "h2", "h3"]}), encoding="utf-8")
    assert _get_node_height(tmp_path, "V3") == 3

    v4_file = tmp_path / "V4.json"
    v4_file.write_text(json.dumps({"blocks": [{"height": 1}, {"height": 2}]}), encoding="utf-8")
    assert _get_node_height(tmp_path, "V4") == 2


def test_launch_local_file_not_found(tmp_path: Path):
    with pytest.raises(FileNotFoundError, match="scenario file not found"):
        launch_local("nonexistent_scenario.json")

    scenario_file = tmp_path / "scenario.json"
    scenario_file.write_text("{}", encoding="utf-8")
    with pytest.raises(FileNotFoundError, match="genesis file not found"):
        launch_local(scenario_file, genesis_path="nonexistent_genesis.json")


def test_launch_local_mocked(tmp_path: Path):
    scenario_path = ROOT / "config/t1_normal.json"
    genesis_path = ROOT / "config/genesis.json"

    mock_proc = MagicMock()
    mock_proc.poll.return_value = 0

    with (
        patch("subprocess.Popen", return_value=mock_proc) as mock_popen,
        patch(
            "l1sim.launch_local.check_safety",
            return_value={"pass": True, "safety_ok": True},
        ) as mock_check,
    ):
        verdict = launch_local(
            scenario_path=scenario_path,
            genesis_path=genesis_path,
            base_port=20000,
            timeout=5.0,
            results_root=str(tmp_path),
            quiet=True,
        )

        assert verdict["pass"] is True
        assert mock_popen.call_count == 10
        assert mock_check.call_count == 1


def test_launch_local_mocked_no_check(tmp_path: Path):
    scenario_path = ROOT / "config/t1_normal.json"
    genesis_path = ROOT / "config/genesis.json"

    mock_proc = MagicMock()
    mock_proc.poll.return_value = 0

    with patch("subprocess.Popen", return_value=mock_proc):
        verdict = launch_local(
            scenario_path=scenario_path,
            genesis_path=genesis_path,
            base_port=20000,
            timeout=5.0,
            results_root=str(tmp_path),
            run_check=False,
            quiet=True,
        )

        assert verdict["pass"] is True
        assert verdict["status"] == "completed"


def test_launch_local_actions_kill_and_restart(tmp_path: Path):
    scenario_path = ROOT / "config/d3_restart.json"
    genesis_path = ROOT / "config/genesis.json"

    def make_mock(*args, **kwargs):
        p = MagicMock()
        p.poll.return_value = 0
        return p

    with (
        patch("subprocess.Popen", side_effect=make_mock) as mock_popen,
        patch("l1sim.launch_local._get_node_height", return_value=6),
        patch("l1sim.launch_local.check_safety", return_value={"pass": True, "safety_ok": True}),
    ):
        verdict = launch_local(
            scenario_path=scenario_path,
            genesis_path=genesis_path,
            base_port=20000,
            timeout=5.0,
            results_root=str(tmp_path),
            quiet=True,
        )

        assert verdict["pass"] is True
        # 10 initial + 1 restart of V8 = 11 calls to Popen
        assert mock_popen.call_count == 11


def test_main_cli_missing_args():
    with pytest.raises(SystemExit) as excinfo:
        main([])
    assert excinfo.value.code == 2


def test_main_cli_file_not_found():
    code = main(["--scenario", "nonexistent_file_xyz.json"])
    assert code == 1


def test_main_cli_success(tmp_path: Path):
    mock_verdict = {"scenario": "t1_normal", "pass": True}
    with patch("l1sim.launch_local.launch_local", return_value=mock_verdict):
        code = main(["--scenario", str(ROOT / "config/t1_normal.json"), "--quiet"])
        assert code == 0


def test_main_cli_failure(tmp_path: Path):
    mock_verdict = {"scenario": "t1_normal", "pass": False}
    with patch("l1sim.launch_local.launch_local", return_value=mock_verdict):
        code = main(["--scenario", str(ROOT / "config/t1_normal.json"), "--quiet"])
        assert code == 1
