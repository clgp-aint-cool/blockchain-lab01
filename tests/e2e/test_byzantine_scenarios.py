"""T6 and T7 with ten real validator processes and the Byzantine rules of the scenario."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tests.e2e.test_local_nodes import available_base_port

ROOT = Path(__file__).parents[2]


def run_scenario(name, workdir):
    """Run every validator of ``config/<name>.json``; return results and event logs per node."""
    scenario_path = ROOT / f"config/{name}.json"
    scenario = json.loads(scenario_path.read_text())
    base = available_base_port()
    processes = []
    try:
        for node in scenario["validators"]:
            processes.append(
                subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "l1sim.run_node",
                        "--id",
                        node,
                        "--scenario",
                        str(scenario_path),
                        "--genesis",
                        str(ROOT / "config/genesis.json"),
                        "--base-port",
                        str(base),
                        "--timeout",
                        "120",
                    ],
                    cwd=workdir,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            )
        for process in processes:
            stdout, stderr = process.communicate(timeout=150)
            assert process.returncode == 0, stdout + stderr
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)

    results, events = {}, {}
    for node in scenario["validators"]:
        results[node] = (workdir / f"logs/results/{name}/{node}.json").read_bytes()
        lines = (workdir / f"logs/events/{name}/{node}.jsonl").read_text().splitlines()
        events[node] = [json.loads(line) for line in lines]
    byzantine = {rule["node"] for rule in scenario["byzantine"]}
    return scenario, results, events, byzantine


def honest_results(results, byzantine):
    """Parsed results of the honest nodes, without ``node_id`` so they can be compared."""
    parsed = {}
    for node, data in results.items():
        if node not in byzantine:
            result = json.loads(data)
            assert result.pop("node_id") == node
            parsed[node] = result
    return parsed


@pytest.mark.e2e
def test_t6_silent_proposer_is_replaced_in_round_1(tmp_path):
    scenario, results, events, byzantine = run_scenario("t6_silent_proposer", tmp_path)
    honest = honest_results(results, byzantine)

    first = next(iter(honest.values()))
    assert all(result == first for result in honest.values())
    assert first["finalized_height"] == scenario["max_height"]
    height_2 = first["blocks"][1]
    assert height_2["height"] == 2
    assert (height_2["round"], height_2["commit_round"], height_2["proposer"]) == (1, 1, "V3")
    actions = [e for e in events["V2"] if e["event"] == "byzantine_action"]
    assert [(e["behavior"], e["kind"], e["height"], e["round"]) for e in actions] == [
        ("silent", "proposal", 2, 0)
    ]


@pytest.mark.e2e
def test_t7_equivocators_are_caught_and_results_reproduce(tmp_path):
    runs = []
    for run in range(2):
        workdir = tmp_path / str(run)
        workdir.mkdir()
        runs.append(run_scenario("t7_byzantine", workdir))

    scenario, results, events, byzantine = runs[0]
    assert byzantine == {"V7", "V8", "V9"}
    honest = honest_results(results, byzantine)
    first = next(iter(honest.values()))
    assert all(result == first for result in honest.values())
    assert first["finalized_height"] == scenario["max_height"] == 9
    assert first["equivocators"] == ["V7", "V8", "V9"]
    # V7, V8, V9 propose heights 7-9 in round 0, so proposals equivocate too. The second
    # block is sent 2x timeout later, so it reaches honest nodes after they moved on.
    for node, height in (("V7", 7), ("V8", 8), ("V9", 9)):
        actions = {
            (e["kind"], e["height"]) for e in events[node] if e["event"] == "byzantine_action"
        }
        assert ("proposal", height) in actions, node
    for block in first["blocks"]:
        assert block["commit_round"] == 0  # the delayed second block never splits a round

    assert runs[1][1] == results  # T8: every results file byte-identical across runs
