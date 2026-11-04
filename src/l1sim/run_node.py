"""Command-line entry point for a validator using authenticated TCP and Gossip."""

from __future__ import annotations

import argparse
import asyncio
import math
import sys
from collections.abc import Callable

from l1sim.client import send_scenario
from l1sim.config import ConfigError, load_config
from l1sim.network.tcp_transport import parse_addr
from l1sim.node import Node, NodeNetwork


def parse_address(address: str) -> tuple[str, int]:
    """Validate a configured listen or advertise address."""
    host, port = parse_addr(address)
    if not 1 <= port <= 65535 or any(char.isspace() for char in host):
        raise ValueError("address must use host:port with port 1..65535")
    return host, port


async def run_node(
    node_id: str,
    scenario_path: str,
    genesis_path: str = "config/genesis.json",
    port: int | None = None,
    listen: str | None = None,
    advertise: str | None = None,
    timeout_s: float = 60.0,
    base_port: int = 9000,
    network_factory: Callable[[Node], NodeNetwork] = NodeNetwork,
    auto_submit: bool = True,
) -> int:
    """Run to max_height using C's network; report incomplete runs as failures."""
    node = None
    network = None
    try:
        genesis, scenario = load_config(scenario_path, genesis_path)
        if node_id not in genesis.validators:
            raise ValueError(f"{node_id!r} is not a validator")
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError("timeout must be finite and positive")
        peers = dict(scenario.network.peers) or {
            validator: f"127.0.0.1:{base_port + index}"
            for index, validator in enumerate(genesis.validators)
        }
        for address in peers.values():
            parse_address(address)
        expected_port = parse_address(peers[node_id])[1]
        resolved_port = port if port is not None else expected_port
        listen_addr = listen or f"0.0.0.0:{resolved_port}"
        advertise_addr = advertise or peers[node_id]
        if parse_address(advertise_addr) != parse_address(peers[node_id]):
            raise ValueError("advertise address must match network.peers for this validator")
        if parse_address(listen_addr)[1] != expected_port:
            raise ValueError(
                "listen port must match the peer address; configure network.peers "
                "or use the same --base-port on every local node"
            )
        node = Node(
            node_id,
            genesis,
            scenario,
            listen_addr,
            advertise_addr,
            loop=asyncio.get_running_loop(),
            peer_addresses=peers,
        )
        network = network_factory(node)
        await network.start()
        print(f"{node_id}: {listen_addr}; signed HELLO, StaticPeers, Gossip", flush=True)
        try:
            await asyncio.wait_for(network.wait_ready(), timeout=timeout_s)
        except asyncio.TimeoutError:
            print(f"{node_id}: timed out waiting for authenticated peers", file=sys.stderr)
            return 1
        # Every validator preloads the same deterministic scenario before proposing.
        if auto_submit:
            # All validators already seed these transactions locally; flooding them
            # from every node adds duplicate traffic ahead of the first proposal.
            send_scenario(scenario, lambda tx: node.submit_transaction(tx, broadcast=False))
        else:
            expected_txs = len({(tx["sender"], tx["nonce"]) for tx in scenario.transactions})

            async def wait_for_client_txs():
                while len(node.mempool) < expected_txs:
                    await asyncio.sleep(0.02)

            try:
                await asyncio.wait_for(wait_for_client_txs(), timeout=timeout_s)
            except asyncio.TimeoutError:
                print(f"{node_id}: timed out waiting for client transactions", file=sys.stderr)
                return 1
        await asyncio.sleep(scenario.warmup_ms / 1000)
        node.start()
        network.activate()
        try:
            await asyncio.wait_for(node.completed.wait(), timeout=timeout_s)
        except asyncio.TimeoutError:
            print(
                f"{node_id}: timed out before finalizing height {scenario.max_height}",
                file=sys.stderr,
            )
            return 1
        # Allow slower peers to receive final votes and request finalized bodies.
        await asyncio.sleep(scenario.consensus.timeout_precommit_ms / 1000)
        print(f"{node_id}: finalized height {scenario.max_height}", flush=True)
        return 0
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 1
    except (ValueError, OSError) as exc:
        print(f"node error: {exc}", file=sys.stderr)
        return 1
    finally:
        try:
            if node is not None:
                node.stop()
                node.write_result()
        finally:
            if network is not None:
                await network.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one validator with signed TCP and Gossip")
    parser.add_argument("--id", required=True, help="validator id, for example V0")
    parser.add_argument("--port", type=int, help="TCP port (default: configured peer port)")
    parser.add_argument(
        "--base-port",
        type=int,
        default=9000,
        help="first local peer port when network.peers is omitted",
    )
    parser.add_argument("--scenario", required=True, help="scenario JSON file")
    parser.add_argument("--genesis", default="config/genesis.json", help="genesis JSON file")
    parser.add_argument("--listen", help="listen address, for example 0.0.0.0:9000")
    parser.add_argument("--advertise", help="address matching this validator in network.peers")
    parser.add_argument(
        "--no-auto-txs",
        action="store_true",
        help="wait for the scenario transaction count from external clients",
    )
    parser.add_argument(
        "--timeout", type=float, default=60, help="startup/consensus deadline in seconds"
    )
    args = parser.parse_args(argv)
    try:
        return asyncio.run(
            run_node(
                node_id=args.id,
                scenario_path=args.scenario,
                genesis_path=args.genesis,
                port=args.port,
                listen=args.listen,
                advertise=args.advertise,
                timeout_s=args.timeout,
                base_port=args.base_port,
                auto_submit=not args.no_auto_txs,
            )
        )
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
