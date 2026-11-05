"""Unit tests for check_safety."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.check_safety import check_safety, check_safety_for_nodes, main


def test_check_safety_empty_nodes():
    res = check_safety_for_nodes({})
    assert res["pass"] is False
    assert res["n"] == 0
    assert res["common_height"] == 0


def test_check_safety_single_node_pass():
    node_data = {
        "V0": {
            "node": "V0",
            "finalized_height": 2,
            "blocks": [
                {"height": 1, "hash": "h1", "state_hash": "s1", "txs": 1},
                {"height": 2, "hash": "h2", "state_hash": "s2", "txs": 2},
            ],
            "state_hash": "s2",
        }
    }
    res = check_safety_for_nodes(node_data, scenario_name="test", max_height=2)
    assert res["pass"] is True
    assert res["safety_violations"] == []
    assert res["same_state_at_common_height"] is True
    assert res["common_height"] == 2
    assert res["total_txs"] == 3


def test_check_safety_detects_hash_conflict():
    node_data = {
        "V0": {
            "node": "V0",
            "finalized_height": 1,
            "blocks": [{"height": 1, "hash": "block_A", "state_hash": "sA", "txs": 1}],
            "state_hash": "sA",
        },
        "V1": {
            "node": "V1",
            "finalized_height": 1,
            "blocks": [{"height": 1, "hash": "block_B", "state_hash": "sB", "txs": 1}],
            "state_hash": "sB",
        },
    }
    res = check_safety_for_nodes(node_data, scenario_name="test_equivocation")
    assert res["pass"] is False
    assert len(res["safety_violations"]) == 1
    assert res["safety_violations"][0]["height"] == 1
    assert res["safety_violations"][0]["hashes"] == {"V0": "block_A", "V1": "block_B"}


def test_check_safety_detects_state_mismatch_at_common_height():
    node_data = {
        "V0": {
            "node": "V0",
            "finalized_height": 1,
            "blocks": [{"height": 1, "hash": "h1", "state_hash": "state_1", "txs": 1}],
            "state_hash": "state_1",
        },
        "V1": {
            "node": "V1",
            "finalized_height": 1,
            "blocks": [{"height": 1, "hash": "h1", "state_hash": "state_2", "txs": 1}],
            "state_hash": "state_2",
        },
    }
    res = check_safety_for_nodes(node_data, scenario_name="test_state_mismatch")
    assert res["pass"] is False
    assert res["safety_violations"] == []
    assert res["same_state_at_common_height"] is False


def test_check_safety_blocks_as_dict_format():
    node_data = {
        "V0": {
            "node": "V0",
            "finalized_height": 1,
            "blocks": {"1": {"hash": "h1", "state_hash": "s1", "txs": 2}},
            "state_hash": "s1",
        },
        "V1": {
            "node": "V1",
            "finalized_height": 1,
            "blocks": {"1": {"hash": "h1", "state_hash": "s1", "txs": 2}},
            "state_hash": "s1",
        },
    }
    res = check_safety_for_nodes(node_data, max_height=1)
    assert res["pass"] is True
    assert res["total_txs"] == 2


def test_check_safety_max_height_unreached():
    node_data = {
        "V0": {
            "node": "V0",
            "finalized_height": 2,
            "blocks": [
                {"height": 1, "hash": "h1", "state_hash": "s1", "txs": 1},
                {"height": 2, "hash": "h2", "state_hash": "s2", "txs": 1},
            ],
            "state_hash": "s2",
        }
    }
    res = check_safety_for_nodes(node_data, max_height=5)
    assert res["pass"] is False
    assert res["reached_max_height"] is False
    assert res["common_height"] == 2


def test_check_safety_height_zero():
    node_data = {
        "V0": {"node": "V0", "finalized_height": 0, "blocks": [], "state_hash": "init"},
        "V1": {"node": "V1", "finalized_height": 0, "blocks": [], "state_hash": "init"},
    }
    res = check_safety_for_nodes(node_data)
    assert res["pass"] is True
    assert res["common_height"] == 0
    assert res["same_state_at_common_height"] is True


def test_check_safety_fallback_final_state_and_height():
    node_data = {
        "V0": {
            "node": "V0",
            # No finalized_height explicit field -> falls back to max(hist.keys())
            "blocks": [{"height": 1, "hash": "h1", "state_hash": "s1", "txs": 1}],
            "state_hash": "s1",
        },
        "V1": {
            "node": "V1",
            "finalized_height": 1,
            # Block at common_height 1 is missing from blocks list -> falls back to final_state_hash
            "blocks": [],
            "state_hash": "s1",
        },
    }
    res = check_safety_for_nodes(node_data)
    assert res["pass"] is True
    assert res["common_height"] == 1
    assert res["same_state_at_common_height"] is True


def test_check_safety_from_directory(tmp_path: Path):
    scenario_dir = tmp_path / "sc_test"
    scenario_dir.mkdir()

    v0_data = {
        "node": "V0",
        "finalized_height": 1,
        "blocks": [{"height": 1, "hash": "h1", "state_hash": "s1", "txs": 3}],
        "state_hash": "s1",
    }
    (scenario_dir / "V0.json").write_text(json.dumps(v0_data), encoding="utf-8")
    (scenario_dir / "verdict.json").write_text(json.dumps({"max_height": 1}), encoding="utf-8")

    verdict = check_safety("sc_test", results_root=tmp_path, write_verdict=True)
    assert verdict["pass"] is True
    assert verdict["max_height"] == 1
    assert (scenario_dir / "verdict.json").is_file()


def test_check_safety_corrupt_verdict_json(tmp_path: Path):
    scenario_dir = tmp_path / "sc_corrupt"
    scenario_dir.mkdir()
    v0_data = {
        "node": "V0",
        "finalized_height": 1,
        "blocks": [{"height": 1, "hash": "h1", "state_hash": "s1", "txs": 1}],
        "state_hash": "s1",
    }
    (scenario_dir / "V0.json").write_text(json.dumps(v0_data), encoding="utf-8")
    (scenario_dir / "verdict.json").write_text("NOT_VALID_JSON{{{", encoding="utf-8")

    verdict = check_safety("sc_corrupt", results_root=tmp_path)
    assert verdict["pass"] is True


def test_check_safety_missing_dir(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        check_safety("non_existent", results_root=tmp_path)


def test_check_safety_cli_target_as_scenario_name(tmp_path: Path):
    scenario_dir = tmp_path / "t_cli_name"
    scenario_dir.mkdir()
    v0_data = {
        "node": "V0",
        "finalized_height": 1,
        "blocks": [{"height": 1, "hash": "h1", "state_hash": "s1", "txs": 1}],
        "state_hash": "s1",
    }
    (scenario_dir / "V0.json").write_text(json.dumps(v0_data), encoding="utf-8")

    code = main(["t_cli_name", "--results-root", str(tmp_path), "--max-height", "1"])
    assert code == 0


def test_check_safety_cli_target_as_dir_path(tmp_path: Path):
    scenario_dir = tmp_path / "t_cli_dir"
    scenario_dir.mkdir()
    v0_data = {
        "node": "V0",
        "finalized_height": 1,
        "blocks": [{"height": 1, "hash": "h1", "state_hash": "s1", "txs": 1}],
        "state_hash": "s1",
    }
    (scenario_dir / "V0.json").write_text(json.dumps(v0_data), encoding="utf-8")

    code = main([str(scenario_dir), "--max-height", "1"])
    assert code == 0


def test_check_safety_cli_failure(tmp_path: Path):
    scenario_dir = tmp_path / "t_cli_fail"
    scenario_dir.mkdir()
    v0_data = {
        "node": "V0",
        "finalized_height": 1,
        "blocks": [{"height": 1, "hash": "h1", "state_hash": "s1", "txs": 1}],
        "state_hash": "s1",
    }
    (scenario_dir / "V0.json").write_text(json.dumps(v0_data), encoding="utf-8")

    code = main(["--scenario", "t_cli_fail", "--results-root", str(tmp_path), "--max-height", "10"])
    assert code == 1


def test_check_safety_cli_error():
    assert main([]) == 2
    assert main(["non_existent_folder_xyz"]) == 2


def test_check_safety_node_py_format():
    node_data = {
        "V0": {
            "node_id": "V0",
            "chain_id": "lab01",
            "finalized_height": 2,
            "state_hash": "sh_common",
            "block_hashes": ["hash1", "hash2"],
        },
        "V1": {
            "node_id": "V1",
            "chain_id": "lab01",
            "finalized_height": 2,
            "state_hash": "sh_common",
            "block_hashes": ["hash1", "hash2"],
        },
    }
    res = check_safety_for_nodes(node_data, scenario_name="test_node_format", max_height=2)
    assert res["pass"] is True
    assert res["safety_violations"] == []
    assert res["same_state_at_common_height"] is True
    assert res["common_height"] == 2


def test_check_safety_bft_quorum_with_dead_node():
    # 10 validators (f=3): V0..V8 reach height 5, V9 died at height 2.
    # BFT quorum n - f = 7 nodes reached height 5 -> common_height is 5.
    node_data = {
        f"V{i}": {
            "node_id": f"V{i}",
            "finalized_height": 5,
            "state_hash": "state_h5",
            "block_hashes": [f"hash_{h}" for h in range(1, 6)],
        }
        for i in range(9)
    }
    node_data["V9"] = {
        "node_id": "V9",
        "finalized_height": 2,
        "state_hash": "state_h2",
        "block_hashes": ["hash_1", "hash_2"],
    }
    res = check_safety_for_nodes(node_data, scenario_name="test_d2_quorum", max_height=5)
    assert res["pass"] is True
    assert res["common_height"] == 5
    assert res["reached_max_height"] is True
    assert res["safety_ok"] is True
    assert res["same_state_at_common_height"] is True


def test_check_safety_ignores_byzantine_nodes():
    # V0..V6 are honest, V7 is Byzantine with conflicting state/block
    node_data = {
        f"V{i}": {
            "node_id": f"V{i}",
            "finalized_height": 5,
            "state_hash": "honest_state",
            "block_hashes": [f"hash_{h}" for h in range(1, 6)],
        }
        for i in range(7)
    }
    node_data["V7"] = {
        "node_id": "V7",
        "finalized_height": 5,
        "state_hash": "corrupt_state",
        "block_hashes": ["bad_hash_1"],
    }
    # Without excluding V7: fails due to state mismatch / safety violation
    res_fail = check_safety_for_nodes(node_data, scenario_name="test_byz", max_height=5)
    assert res_fail["pass"] is False

    # With excluding V7: passes and preserves total validator count n
    res_ok = check_safety_for_nodes(
        node_data, scenario_name="test_byz", max_height=5, byzantine_nodes=["V7"]
    )
    assert res_ok["pass"] is True
    assert res_ok["same_state_at_common_height"] is True
    assert res_ok["safety_ok"] is True
    assert res_ok["n"] == 8
    assert res_ok["f"] == 2

    # Explicit num_validators=10 (f=3, quorum=7)
    res_10 = check_safety_for_nodes(
        node_data,
        scenario_name="test_byz",
        max_height=5,
        num_validators=10,
        byzantine_nodes=["V7"],
    )
    assert res_10["pass"] is True
    assert res_10["n"] == 10
    assert res_10["f"] == 3


def test_check_safety_requires_quorum_for_liveness():
    """Verify quorum threshold (N=10, f=3 => quorum=7) for reached_max_height and pass."""
    # 1 of 10 nodes reaching height 5: must fail liveness
    nodes_1 = {
        "V0": {
            "node_id": "V0",
            "finalized_height": 5,
            "state_hash": "same_state",
            "block_hashes": [f"h{i}" for i in range(1, 6)],
        }
    }
    res_1 = check_safety_for_nodes(nodes_1, num_validators=10, max_height=5)
    assert res_1["common_height"] == 0
    assert res_1["reached_max_height"] is False
    assert res_1["quorum_ok"] is False
    assert res_1["pass"] is False

    # 6 of 10 nodes reaching height 5: still below quorum (6 < 7), must fail
    nodes_6 = {
        f"V{i}": {
            "node_id": f"V{i}",
            "finalized_height": 5,
            "state_hash": "same_state",
            "block_hashes": [f"h{h}" for h in range(1, 6)],
        }
        for i in range(6)
    }
    res_6 = check_safety_for_nodes(nodes_6, num_validators=10, max_height=5)
    assert res_6["common_height"] == 0
    assert res_6["reached_max_height"] is False
    assert res_6["quorum_ok"] is False
    assert res_6["pass"] is False

    # 7 of 10 nodes reaching height 5: exactly achieves quorum (7 >= 7), must pass
    nodes_7 = {
        f"V{i}": {
            "node_id": f"V{i}",
            "finalized_height": 5,
            "state_hash": "same_state",
            "block_hashes": [f"h{h}" for h in range(1, 6)],
        }
        for i in range(7)
    }
    res_7 = check_safety_for_nodes(nodes_7, num_validators=10, max_height=5)
    assert res_7["common_height"] == 5
    assert res_7["reached_max_height"] is True
    assert res_7["quorum_ok"] is True
    assert res_7["pass"] is True


def test_check_safety_detects_actual_execution_state_divergence():
    """Verify detection when actual node execution state diverges from declared block state."""
    nodes = {
        f"V{i}": {
            "finalized_height": 1,
            "state_hash": "ACTUAL_BAD_STATE" if i == 0 else "ACTUAL_GOOD_STATE",
            "blocks": [
                {
                    "height": 1,
                    "hash": "same-block",
                    "state_hash": "declared-state",
                    "txs": 1,
                }
            ],
        }
        for i in range(10)
    }
    verdict = check_safety_for_nodes(nodes, num_validators=10, max_height=1)
    assert verdict["state_ok"] is False
    assert verdict["same_state_at_common_height"] is False
    assert verdict["pass"] is False
