"""Safety and state consistency verification for L1 simulation results."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Collection, Mapping
from pathlib import Path
from typing import Any

from l1sim.logger import ResultsCollector, ResultWriter


def check_safety_for_nodes(
    nodes_data: Mapping[str, Mapping[str, Any]],
    *,
    scenario_name: str = "unknown",
    max_height: int | None = None,
    num_validators: int | None = None,
    byzantine_nodes: Collection[str] = (),
) -> dict[str, Any]:
    """Verify safety and state consistency across an in-memory dictionary of node results.

    Args:
        nodes_data: mapping of node_id -> node result dictionary (as written by ResultWriter).
        scenario_name: name of the scenario.
        max_height: target height required to pass liveness.
        num_validators: total validator count in the network (n). Defaults to len(nodes_data).
        byzantine_nodes: optional set of node IDs to exclude from consistency checks.

    Returns:
        dict containing verdict fields:
          - scenario: str
          - n: int (total validator count)
          - f: int (fault tolerance, (n - 1) // 3)
          - quorum_count: int (n - f)
          - quorum_ok: bool
          - common_height: int
          - max_height: int
          - reached_max_height: bool
          - safety_violations: list of dicts with conflicting hashes
          - same_state_at_common_height: bool
          - state_ok: bool
          - total_txs: int
          - pass: bool
    """
    # Keep total validator count n from num_validators or total nodes_data
    # so f and quorum reflect the whole network size.
    n = num_validators if num_validators is not None else len(nodes_data)
    f = (n - 1) // 3 if n >= 4 else 0
    quorum_count = n - f if n >= 4 else n

    # Safety and state consistency are only checked across honest nodes
    if byzantine_nodes:
        honest_nodes_data = {v: d for v, d in nodes_data.items() if v not in byzantine_nodes}
    else:
        honest_nodes_data = dict(nodes_data)

    if not honest_nodes_data:
        return {
            "scenario": scenario_name,
            "n": n,
            "f": f,
            "quorum_count": quorum_count,
            "quorum_ok": False,
            "common_height": 0,
            "max_height": max_height or 0,
            "reached_max_height": False,
            "safety_violations": [],
            "safety_ok": False,
            "same_state_at_common_height": False,
            "state_ok": False,
            "total_txs": 0,
            "pass": False,
        }

    # Parse per-node history: height -> {"hash": str, "state_hash": str, "txs": int, "round": int}
    node_history: dict[str, dict[int, dict[str, Any]]] = {}
    node_final_height: dict[str, int] = {}
    node_final_state_hash: dict[str, str] = {}

    for node_id, data in honest_nodes_data.items():
        hist: dict[int, dict[str, Any]] = {}
        blocks = data.get("blocks", [])
        if isinstance(blocks, list):
            for b in blocks:
                if isinstance(b, dict) and "height" in b and "hash" in b:
                    hist[int(b["height"])] = b
        elif isinstance(blocks, dict):
            for h_str, b in blocks.items():
                if isinstance(b, dict) and "hash" in b:
                    hist[int(h_str)] = b

        block_hashes = data.get("block_hashes")
        if isinstance(block_hashes, list) and not hist:
            for idx, h_val in enumerate(block_hashes, start=1):
                hist[idx] = {"height": idx, "hash": str(h_val)}

        node_history[node_id] = hist
        fin_h = data.get("finalized_height")
        if fin_h is None:
            fin_h = max(hist.keys(), default=0)
        node_final_height[node_id] = int(fin_h)
        node_final_state_hash[node_id] = str(data.get("state_hash", ""))

    all_heights = sorted({h for hist in node_history.values() for h in hist})

    # 1. Check Safety: at each height, every node that finalized a block must agree on hash
    safety_violations: list[dict[str, Any]] = []
    for h in all_heights:
        hashes_at_h: dict[str, str] = {
            v: node_history[v][h]["hash"] for v in node_history if h in node_history[v]
        }
        if len(set(hashes_at_h.values())) > 1:
            safety_violations.append({"height": h, "hashes": hashes_at_h})

    # 2. Common height: highest height reached by at least n - f nodes (BFT quorum)
    sorted_heights = sorted(node_final_height.values(), reverse=True)
    has_quorum = bool(quorum_count > 0 and len(sorted_heights) >= quorum_count)
    if has_quorum:
        common_height = sorted_heights[quorum_count - 1]
    else:
        common_height = 0

    # 3. Check execution state consistency:
    # (a) For each node with finalized_height > 0, verify actual execution state_hash
    # against the declared block state_hash at its own finalized height.
    execution_state_ok = True
    for v, fin_h in node_final_height.items():
        if fin_h > 0 and fin_h in node_history[v]:
            block_sh = node_history[v][fin_h].get("state_hash")
            node_sh = node_final_state_hash.get(v)
            if block_sh and node_sh and block_sh != node_sh:
                execution_state_ok = False
                break

    # (b) State consistency at common_height across nodes reaching this height
    same_state = True
    if common_height > 0:
        state_hashes = set()
        nodes_at_common = [v for v in honest_nodes_data if node_final_height[v] >= common_height]
        for v in nodes_at_common:
            sh = node_history[v].get(common_height, {}).get("state_hash")
            if not sh:
                sh = node_final_state_hash.get(v, "")
            if sh:
                state_hashes.add(sh)
            # If the node stopped at common_height, its final state_hash must also match
            if node_final_height[v] == common_height and node_final_state_hash.get(v):
                state_hashes.add(node_final_state_hash[v])
        same_state = (
            len(state_hashes) == 1 and bool(next(iter(state_hashes))) and execution_state_ok
        )
    else:
        initial_hashes = {h for h in node_final_state_hash.values() if h}
        same_state = (len(initial_hashes) <= 1) and execution_state_ok

    # 4. Total transactions finalized across common chain
    total_txs = 0
    if common_height > 0 and node_history:
        first_node = next(iter(node_history))
        for h in range(1, common_height + 1):
            if h in node_history[first_node]:
                total_txs += int(node_history[first_node][h].get("txs", 0))

    reached_max = bool(has_quorum and max_height and common_height >= max_height)
    verdict_pass = bool(
        has_quorum and not safety_violations and same_state and (not max_height or reached_max)
    )

    return {
        "scenario": scenario_name,
        "n": n,
        "f": f,
        "quorum_count": quorum_count,
        "quorum_ok": has_quorum,
        "common_height": common_height,
        "max_height": max_height or 0,
        "reached_max_height": reached_max,
        "safety_violations": safety_violations,
        "safety_ok": not safety_violations,
        "same_state_at_common_height": same_state,
        "state_ok": same_state,
        "total_txs": total_txs,
        "pass": verdict_pass,
    }


def check_safety(
    scenario_name: str,
    results_root: str | Path = "logs/results",
    *,
    max_height: int | None = None,
    num_validators: int | None = None,
    byzantine_nodes: Collection[str] = (),
    write_verdict: bool = False,
) -> dict[str, Any]:
    """Read node result files under results_root/scenario_name and perform safety check.

    Args:
        scenario_name: directory name of the scenario.
        results_root: parent directory containing scenario result folders.
        max_height: optional target max height to check liveness against.
        num_validators: optional total validator count (n) in scenario.
        byzantine_nodes: optional set of node IDs to exclude (honest nodes only).
        write_verdict: if True, writes verdict.json into the results directory.

    Returns:
        verdict dict.
    """
    collector = ResultsCollector(scenario_name, results_root=results_root)
    node_ids = collector.node_ids()
    if not node_ids:
        raise FileNotFoundError(f"no node result files (*.json) found in {collector.path}")

    nodes_data = collector.load_nodes(node_ids)

    # If max_height is not passed, check if existing verdict.json has it
    if max_height is None:
        verdict_path = collector.path / "verdict.json"
        if verdict_path.is_file():
            try:
                with verdict_path.open("r", encoding="utf-8") as f:
                    old_v = json.load(f)
                    if isinstance(old_v, dict) and "max_height" in old_v:
                        max_height = int(old_v["max_height"])
            except Exception:
                pass

    if not byzantine_nodes or num_validators is None or max_height is None:
        sc_file = Path("config") / f"{scenario_name}.json"
        if sc_file.is_file():
            try:
                sc_raw = json.loads(sc_file.read_text(encoding="utf-8"))
                if num_validators is None and "num_validators" in sc_raw:
                    num_validators = int(sc_raw["num_validators"])
                if max_height is None and "max_height" in sc_raw:
                    max_height = int(sc_raw["max_height"])
                if not byzantine_nodes:
                    byz_raw = sc_raw.get("byzantine", [])
                    byz_set = set()
                    for item in byz_raw:
                        if isinstance(item, str):
                            byz_set.add(item)
                        elif hasattr(item, "node"):
                            byz_set.add(item.node)
                        elif isinstance(item, dict) and "node" in item:
                            byz_set.add(item["node"])
                    byzantine_nodes = sorted(byz_set)
            except Exception:
                pass

    verdict = check_safety_for_nodes(
        nodes_data,
        scenario_name=scenario_name,
        max_height=max_height,
        num_validators=num_validators,
        byzantine_nodes=byzantine_nodes,
    )

    if write_verdict:
        writer = ResultWriter(scenario_name, results_root=results_root)
        writer.write_verdict(verdict)

    return verdict


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for check_safety."""
    parser = argparse.ArgumentParser(
        description="Verify safety and state consistency from simulation results."
    )
    parser.add_argument(
        "target",
        nargs="?",
        default=None,
        help="Path to scenario result dir (e.g. logs/results/t1_normal) or scenario name.",
    )
    parser.add_argument(
        "--scenario",
        default=None,
        help="Scenario name (if target is not supplied).",
    )
    parser.add_argument(
        "--results-root",
        default="logs/results",
        help="Root directory for results (default: logs/results).",
    )
    parser.add_argument(
        "--max-height",
        type=int,
        default=None,
        help="Expected max height.",
    )
    parser.add_argument(
        "--num-validators",
        type=int,
        default=None,
        help="Total validator count (n) in scenario.",
    )
    parser.add_argument(
        "--write-verdict",
        action="store_true",
        help="Write verdict.json into the result directory.",
    )

    args = parser.parse_args(argv)

    if args.target:
        p = Path(args.target)
        if p.is_dir() and p.name:
            scenario_name = p.name
            results_root = str(p.parent)
        else:
            scenario_name = args.target
            results_root = args.results_root
    elif args.scenario:
        scenario_name = args.scenario
        results_root = args.results_root
    else:
        parser.print_help(sys.stderr)
        return 2

    try:
        verdict = check_safety(
            scenario_name,
            results_root=results_root,
            max_height=args.max_height,
            num_validators=args.num_validators,
            write_verdict=args.write_verdict,
        )
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    print(json.dumps(verdict, indent=2))
    if verdict["pass"]:
        print("SAFETY CHECK PASSED")
        return 0
    else:
        print("SAFETY CHECK FAILED", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
