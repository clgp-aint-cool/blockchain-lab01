"""Exercise authenticated TCP and Gossip with ten independent validator processes."""

import json
import socket
import subprocess
import sys
import time
from contextlib import ExitStack
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]


def available_base_port():
    for base in range(19000, 20000, 10):
        try:
            with ExitStack() as stack:
                for port in range(base, base + 10):
                    sock = stack.enter_context(socket.socket())
                    sock.bind(("127.0.0.1", port))
            return base
        except OSError:
            continue
    raise RuntimeError("no free range of ten local ports")


@pytest.mark.e2e
def test_ten_processes_finalize_and_repeat_identically(tmp_path):
    previous = None
    for run in range(2):
        workdir = tmp_path / str(run)
        workdir.mkdir()
        base = available_base_port()
        scenario_path = ROOT / "config/t1_normal.json"
        if run == 1:
            scenario = json.loads(scenario_path.read_text())
            scenario["network"]["peers"] = {
                f"V{index}": f"127.0.0.1:{base + 9 - index}" for index in range(10)
            }
            scenario_path = workdir / "scenario.json"
            scenario_path.write_text(json.dumps(scenario), encoding="utf-8")
        processes = []
        try:
            for index in range(10):
                processes.append(
                    subprocess.Popen(
                        [
                            sys.executable,
                            "-m",
                            "l1sim.run_node",
                            "--id",
                            f"V{index}",
                            "--scenario",
                            str(scenario_path),
                            "--genesis",
                            str(ROOT / "config/genesis.json"),
                            "--base-port",
                            str(base),
                            "--timeout",
                            "15",
                        ]
                        + (["--no-auto-txs"] if run == 1 else []),
                        cwd=workdir,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                    )
                )
            if run == 1:
                deadline = time.monotonic() + 15
                for address in scenario["network"]["peers"].values():
                    host, port = address.rsplit(":", 1)
                    while True:
                        try:
                            with socket.create_connection((host, int(port)), timeout=0.5):
                                break
                        except OSError:
                            if time.monotonic() >= deadline:
                                raise AssertionError(
                                    "node did not open its client ingress"
                                ) from None
                            time.sleep(0.05)
                client = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "l1sim.client",
                        "--connect",
                        scenario["network"]["peers"]["V0"],
                        "--scenario",
                        str(scenario_path),
                        "--genesis",
                        str(ROOT / "config/genesis.json"),
                    ],
                    cwd=workdir,
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
                assert client.returncode == 0, client.stdout + client.stderr
                acknowledgements = [json.loads(line) for line in client.stdout.splitlines()]
                assert len(acknowledgements) == 20
                assert all(result["accepted"] for result in acknowledgements)
            for process in processes:
                stdout, stderr = process.communicate(timeout=25)
                assert process.returncode == 0, stdout + stderr
                assert "finalized height 5" in stdout
                assert not stderr
            current = {}
            results = []
            for index in range(10):
                path = workdir / f"logs/results/t1_normal/V{index}.json"
                current[index] = path.read_bytes()
                result = json.loads(current[index])
                assert result.pop("node_id") == f"V{index}"
                assert result["finalized_height"] == 5
                assert len(result["block_hashes"]) == 5
                results.append(result)
                events_path = workdir / f"logs/events/t1_normal/V{index}.jsonl"
                events = [json.loads(line) for line in events_path.read_text().splitlines()]
                assert sum(event["event"] == "conn_open" for event in events) == 9
                assert not any(event["event"] == "gossip_handler_error" for event in events)
                assert any(
                    event["event"] == "gossip_recv" and event["accepted"] for event in events
                )
                commits = [event for event in events if event["event"] == "finalize"]
                assert len(commits) == 5
                assert sum(event["txs"] for event in commits) == 20
            assert all(result == results[0] for result in results)
            if previous is not None:
                assert current == previous
            previous = current
        finally:
            for process in processes:
                if process.poll() is None:
                    process.terminate()
            for process in processes:
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
