"""End-to-end test for T2 (Duplicate messages and reordering under packet duplication)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from l1sim.launch_local import available_base_port, launch_local

ROOT = Path(__file__).parents[2]

pytestmark = pytest.mark.e2e


def test_t2_dup_reorder(tmp_path: Path):
    """Verify safety and progress when the network duplicates 30% of messages.

    Duplicated gossip/consensus messages must not cause transactions to be
    committed more than once.  All 20 unique transactions must appear in
    committed blocks with no duplicates despite packet duplication.
    """
    base_port = available_base_port(count=10, start=19200, end=25000)
    results_root = tmp_path / "results"
    verdict = launch_local(
        scenario_path=ROOT / "config/t2_dup_reorder.json",
        genesis_path=ROOT / "config/genesis.json",
        base_port=base_port,
        timeout=90.0,
        results_root=results_root,
        quiet=True,
    )

    assert verdict["pass"] is True
    assert verdict["safety_ok"] is True
    assert verdict["same_state_at_common_height"] is True
    assert verdict["reached_max_height"] is True
    assert verdict["safety_violations"] == []

    # T2-specific: network duplicates must not produce duplicate committed txs.
    # All 20 submitted transactions have unique (sender, nonce) pairs, so all
    # 20 must be committed exactly once.
    assert verdict["total_txs"] == 20, (
        f"Expected 20 txs committed (all unique), got {verdict['total_txs']}"
    )

    # Verify no tx_id appears more than once in any node's committed blocks.
    for result_file in sorted((results_root / "t2_dup_reorder").glob("V*.json")):
        data = json.loads(result_file.read_text(encoding="utf-8"))
        all_tx_ids = [tx for block in data.get("blocks", []) for tx in block.get("tx_ids", [])]
        assert len(all_tx_ids) == len(set(all_tx_ids)), (
            f"{result_file.name}: duplicate tx_ids found despite dedup: {all_tx_ids}"
        )
