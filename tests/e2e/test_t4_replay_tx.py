"""End-to-end test for T4 (Replay Transaction Prevention)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from l1sim.launch_local import available_base_port, launch_local

ROOT = Path(__file__).parents[2]

pytestmark = pytest.mark.e2e


def test_t4_replay_tx(tmp_path: Path):
    """Verify that transactions replayed across nodes only finalize once.

    The scenario submits 20 transactions but only 11 have unique (sender, nonce)
    pairs.  The other 9 share a nonce with an earlier tx and must be rejected.
    Every node must commit exactly 11 distinct tx_ids with no duplicates.
    """
    base_port = available_base_port(count=10, start=19600, end=25000)
    results_root = tmp_path / "results"
    verdict = launch_local(
        scenario_path=ROOT / "config/t4_replay_tx.json",
        genesis_path=ROOT / "config/genesis.json",
        base_port=base_port,
        timeout=30.0,
        results_root=results_root,
        quiet=True,
    )

    assert verdict["pass"] is True
    assert verdict["safety_ok"] is True
    assert verdict["same_state_at_common_height"] is True
    assert verdict["common_height"] == 5
    assert verdict["reached_max_height"] is True
    assert verdict["safety_violations"] == []

    # T4-specific: each of the 9 replayed (sender, nonce) pairs must be rejected,
    # leaving exactly 11 unique transactions across all committed blocks.
    assert verdict["total_txs"] == 11, (
        f"Expected 11 unique txs (20 submitted, 9 replay-rejected), got {verdict['total_txs']}"
    )

    # Verify no tx_id appears more than once in any node's committed blocks.
    for result_file in sorted((results_root / "t4_replay_tx").glob("V*.json")):
        data = json.loads(result_file.read_text(encoding="utf-8"))
        all_tx_ids = [tx for block in data.get("blocks", []) for tx in block.get("tx_ids", [])]
        assert len(all_tx_ids) == len(set(all_tx_ids)), (
            f"{result_file.name}: duplicate tx_ids found, replay not prevented: {all_tx_ids}"
        )
