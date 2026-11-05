"""Local multi-process launcher and orchestrator for L1 simulation scenarios."""

from __future__ import annotations

import argparse
import json
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from contextlib import ExitStack
from pathlib import Path
from typing import Any

from l1sim.config import ConfigError, load_config
from l1sim.verification.check_safety import check_safety


def available_base_port(count: int = 10, start: int = 19000, end: int = 25000) -> int:
    """Find a contiguous range of available local TCP ports."""
    for base in range(start, end, count):
        try:
            with ExitStack() as stack:
                for port in range(base, base + count):
                    sock = stack.enter_context(socket.socket(socket.AF_INET, socket.SOCK_STREAM))
                    sock.bind(("127.0.0.1", port))
            return base
        except OSError:
            continue
    raise RuntimeError(f"no free range of {count} local ports in {start}..{end}")


def wait_for_port(host: str, port: int, timeout: float = 15.0) -> bool:
    """Wait until a local port is accepting TCP connections."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.2):
                return True
        except OSError:
            time.sleep(0.05)
    return False


def launch_local(
    scenario_path: str | Path,
    genesis_path: str | Path = "config/genesis.json",
    base_port: int | None = None,
    timeout: float = 60.0,
    results_root: str | Path = "logs/results",
    no_auto_txs: bool = False,
    run_check: bool = True,
    write_verdict: bool = True,
    quiet: bool = False,
    cwd: str | Path | None = None,
) -> dict[str, Any]:
    """Launch N node processes for a scenario and verify results."""
    scenario_p = Path(scenario_path)
    genesis_p = Path(genesis_path)
    if not scenario_p.is_file():
        raise FileNotFoundError(f"scenario file not found: {scenario_p}")
    if not genesis_p.is_file():
        raise FileNotFoundError(f"genesis file not found: {genesis_p}")

    genesis, scenario = load_config(scenario_p, genesis_p)
    validators = list(genesis.validators)

    # Determine ports and handle potential custom base_port overriding scenario.peers
    scenario_to_use = scenario_p
    temp_scenario_file: Path | None = None

    if scenario.network.peers:
        if base_port is not None:
            # Caller wants a different base_port; create a temporary scenario with rewritten peers
            raw_scenario = json.loads(scenario_p.read_text(encoding="utf-8"))
            new_peers = {v: f"127.0.0.1:{base_port + i}" for i, v in enumerate(validators)}
            raw_scenario["network"]["peers"] = new_peers
            temp_dir = tempfile.mkdtemp(prefix="l1sim_")
            temp_scenario_file = Path(temp_dir) / scenario_p.name
            temp_scenario_file.write_text(json.dumps(raw_scenario), encoding="utf-8")
            scenario_to_use = temp_scenario_file
            resolved_peers = new_peers
        else:
            resolved_peers = dict(scenario.network.peers)
    else:
        actual_base = base_port if base_port is not None else 9000
        resolved_peers = {v: f"127.0.0.1:{actual_base + i}" for i, v in enumerate(validators)}

    working_dir = Path(cwd) if cwd else Path.cwd()
    res_root = (
        working_dir / results_root if not Path(results_root).is_absolute() else Path(results_root)
    )
    scenario_results_dir = res_root / scenario.name
    node_output_dir = working_dir / "logs/results" / scenario.name

    # Clear stale results before run
    if node_output_dir.is_dir():
        for f in node_output_dir.glob("*.json"):
            try:
                f.unlink()
            except OSError:
                pass
    if scenario_results_dir.is_dir():
        for f in scenario_results_dir.glob("*.json"):
            try:
                f.unlink()
            except OSError:
                pass

    processes: dict[str, subprocess.Popen] = {}
    node_logs_dir = working_dir / "logs/nodes" / scenario.name
    node_logs_dir.mkdir(parents=True, exist_ok=True)
    log_files: list[Any] = []

    def spawn_node(node_id: str) -> subprocess.Popen:
        cmd = [
            sys.executable,
            "-X",
            "utf8",
            "-m",
            "l1sim.run_node",
            "--id",
            node_id,
            "--scenario",
            str(scenario_to_use),
            "--genesis",
            str(genesis_p),
            "--timeout",
            str(timeout),
        ]
        if base_port is not None and not scenario.network.peers:
            cmd.extend(["--base-port", str(base_port)])
        if no_auto_txs:
            cmd.append("--no-auto-txs")

        if quiet:
            err_path = node_logs_dir / f"{node_id}.stderr.log"
            out_path = node_logs_dir / f"{node_id}.stdout.log"
            err_f = open(err_path, "w", encoding="utf-8")
            out_f = open(out_path, "w", encoding="utf-8")
            log_files.extend([err_f, out_f])
            stdout_dest = out_f
            stderr_dest = err_f
        else:
            stdout_dest = None
            stderr_dest = None

        return subprocess.Popen(
            cmd,
            cwd=str(working_dir),
            stdout=stdout_dest,
            stderr=stderr_dest,
            text=True,
        )

    try:
        # 1. Start all validator processes
        for node_id in validators:
            processes[node_id] = spawn_node(node_id)

        # 2. If transactions need to be injected via client
        if no_auto_txs:
            first_addr = resolved_peers[validators[0]]
            host, port_str = first_addr.split(":", 1)
            ready = wait_for_port(host, int(port_str), timeout=timeout)
            if not ready:
                raise TimeoutError(f"Node {validators[0]} did not open port {port_str} for client")

            client_cmd = [
                sys.executable,
                "-X",
                "utf8",
                "-m",
                "l1sim.client",
                "--connect",
                first_addr,
                "--scenario",
                str(scenario_to_use),
                "--genesis",
                str(genesis_p),
            ]
            client_res = subprocess.run(
                client_cmd,
                cwd=str(working_dir),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            if client_res.returncode != 0:
                raise RuntimeError(
                    f"Client injection failed ({client_res.returncode}):\n{client_res.stderr}"
                )

        # 3. Wait for all node processes to complete
        start_time = time.monotonic()
        deadline = start_time + timeout

        while time.monotonic() < deadline:
            if all(proc.poll() is not None for proc in processes.values()):
                break
            time.sleep(0.05)

        # Allow brief time for file buffers to flush
        time.sleep(0.1)

        # Check for timed-out processes before terminating
        timed_out_nodes = [node_id for node_id, proc in processes.items() if proc.poll() is None]

        # 4. Terminate any remaining processes
        for proc in processes.values():
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=1.5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=1.0)

        if timed_out_nodes:
            raise TimeoutError(
                f"scenario '{scenario.name}' timed out after {timeout}s: "
                f"nodes {timed_out_nodes} did not complete"
            )

        # Check for failed processes
        failed_nodes = {
            node_id: proc.poll()
            for node_id, proc in processes.items()
            if proc.poll() is not None and proc.poll() != 0
        }
        if failed_nodes:
            err_lines = []
            for node_id, code in failed_nodes.items():
                err_path = node_logs_dir / f"{node_id}.stderr.log"
                if err_path.is_file():
                    err = err_path.read_text(encoding="utf-8")
                else:
                    err = ""
                    proc = processes[node_id]
                    if proc.stderr:
                        try:
                            err = proc.stderr.read()
                        except Exception:
                            pass
                err_lines.append(f"Node {node_id} exited with code {code}:\n{err}")
            raise RuntimeError("\n".join(err_lines))

        # Copy results to custom results_root if different from default
        if res_root != working_dir / "logs/results" and node_output_dir.is_dir():
            scenario_results_dir.mkdir(parents=True, exist_ok=True)
            for f in node_output_dir.glob("*.json"):
                shutil.copy2(f, scenario_results_dir / f.name)

        # 5. Check Safety and State Consistency
        if run_check:
            byz = [
                r.node if hasattr(r, "node") else (r["node"] if isinstance(r, dict) else r)
                for r in getattr(scenario, "byzantine", ())
            ]
            verdict = check_safety(
                scenario.name,
                results_root=str(res_root),
                max_height=scenario.max_height,
                num_validators=scenario.num_validators,
                byzantine_nodes=byz,
                write_verdict=write_verdict,
            )
            return verdict
        else:
            return {"scenario": scenario.name, "status": "completed", "pass": True}

    finally:
        # Close any redirect log files
        for f in log_files:
            try:
                f.close()
            except Exception:
                pass

        # Clean up any orphan processes
        for proc in processes.values():
            if proc.poll() is None:
                try:
                    proc.kill()
                    proc.wait(timeout=0.5)
                except Exception:
                    pass

        # Clean up temporary scenario file if created
        if temp_scenario_file and temp_scenario_file.is_file():
            try:
                temp_scenario_file.unlink()
                temp_scenario_file.parent.rmdir()
            except OSError:
                pass


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for launch_local."""
    parser = argparse.ArgumentParser(
        description="Launch local validator processes for an L1 simulation scenario."
    )
    parser.add_argument(
        "--scenario",
        required=True,
        help="Path to scenario JSON file (e.g. config/t1_normal.json)",
    )
    parser.add_argument(
        "--genesis",
        default="config/genesis.json",
        help="Path to genesis JSON file (default: config/genesis.json)",
    )
    parser.add_argument(
        "--base-port",
        type=int,
        default=None,
        help="Starting local TCP port (default: 9000)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        help="Overall simulation deadline in seconds (default: 60.0)",
    )
    parser.add_argument(
        "--results-root",
        default="logs/results",
        help="Root directory for results (default: logs/results)",
    )
    parser.add_argument(
        "--no-auto-txs",
        action="store_true",
        help="Submit scenario transactions via client instead of auto-seeding",
    )
    parser.add_argument(
        "--no-check",
        action="store_true",
        help="Skip check_safety verification after processes complete",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress validator stdout/stderr output",
    )

    args = parser.parse_args(argv)

    try:
        verdict = launch_local(
            scenario_path=args.scenario,
            genesis_path=args.genesis,
            base_port=args.base_port,
            timeout=args.timeout,
            results_root=args.results_root,
            no_auto_txs=args.no_auto_txs,
            run_check=not args.no_check,
            quiet=args.quiet,
        )
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    except (FileNotFoundError, ValueError, TimeoutError, RuntimeError) as exc:
        print(f"Launch error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted by user", file=sys.stderr)
        return 130

    print(json.dumps(verdict, indent=2))
    if verdict.get("pass"):
        print("SIMULATION & SAFETY CHECK PASSED")
        return 0
    else:
        print("SIMULATION & SAFETY CHECK FAILED", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
