"""External client transactions through real TCP ingress, gossip, and consensus."""

import asyncio
import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from l1sim.client import build_scenario_transactions, make_signed_tx, send_transactions
from l1sim.config import load_config
from l1sim.crypto import keypair_from_seed, sign
from l1sim.encoding import encode
from l1sim.logger import ResultWriter
from l1sim.models import Domain, Kind, Message, Phase, Vote
from l1sim.network.hello import HelloError, make_hello
from l1sim.network.tcp_transport import read_frame, write_frame
from l1sim.node import Node, NodeNetwork
from tests.e2e.test_local_nodes import available_base_port

ROOT = Path(__file__).parents[2]
pytestmark = pytest.mark.e2e


def test_client_cli_injects_all_transactions_and_nodes_finalize(tmp_path):
    async def exercise():
        genesis, scenario = load_config(
            ROOT / "config/t1_normal.json", ROOT / "config/genesis.json"
        )
        base = available_base_port()
        peers = {v: f"127.0.0.1:{base + i}" for i, v in enumerate(genesis.validators)}
        events = []
        nodes = {
            v: Node(
                v,
                genesis,
                scenario,
                peers[v],
                peers[v],
                peer_addresses=peers,
                logger=lambda event, **fields: events.append((event, fields)),
                result_writer=ResultWriter(scenario.name, tmp_path),
            )
            for v in peers
        }
        networks = {v: NodeNetwork(node) for v, node in nodes.items()}
        client_process = None
        try:
            for network in networks.values():
                await network.start()
            await asyncio.wait_for(
                asyncio.gather(*(network.wait_ready() for network in networks.values())), timeout=5
            )
            txs = build_scenario_transactions(scenario)
            bad = replace(txs[0], signature="00" * 64)
            unknown = make_signed_tx(scenario.seed, "Unknown", "Unknown/note", "hello", 0)
            results = await send_transactions(peers["V0"], [bad, unknown], genesis)
            assert [result.reason for result in results] == ["bad_signature", "bad_signature"]
            assert not any(result.accepted for result in results)
            assert all(len(node.mempool) == 0 for node in nodes.values())
            client_process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "l1sim.client",
                "--connect",
                peers["V0"],
                "--scenario",
                str(ROOT / "config/t1_normal.json"),
                "--genesis",
                str(ROOT / "config/genesis.json"),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(client_process.communicate(), timeout=15)
            assert client_process.returncode == 0, stderr.decode()
            acknowledgements = [json.loads(line) for line in stdout.splitlines()]
            assert len(acknowledgements) == 20
            assert all(result["accepted"] and not result["reason"] for result in acknowledgements)

            async def all_received():
                while not all(len(node.mempool) == 20 for node in nodes.values()):
                    await asyncio.sleep(0.01)

            await asyncio.wait_for(all_received(), timeout=5)
            (duplicate,) = await send_transactions(peers["V0"], [txs[0]], genesis)
            assert not duplicate.accepted and duplicate.reason == "duplicate"

            # A client session must never gain the ability to inject consensus votes.
            reader, writer = await asyncio.open_connection("127.0.0.1", base)
            try:
                await read_frame(reader)  # server HELLO
                await write_frame(writer, encode(Message(Kind.TX, "Alice", 0, txs[0])))
                assert json.loads(await read_frame(reader))["reason"] == "duplicate"
                vote = Vote(Phase.PREVOTE, 1, 0, None, "V1")
                vote = replace(
                    vote,
                    signature=sign(
                        keypair_from_seed(scenario.seed, "V1").private_key, Domain.VOTE, vote
                    ),
                )
                await write_frame(writer, encode(Message(Kind.VOTE, "V1", 1, vote)))
                assert json.loads(await read_frame(reader))["reason"] == "expected_tx"
            finally:
                writer.close()
                await writer.wait_closed()
            assert all(len(network.transport.peers()) == 9 for network in networks.values())
            assert not any(name in ("prevote", "precommit") for name, _ in events)
            for v, node in nodes.items():
                node.start()
                networks[v].activate()
            await asyncio.wait_for(
                asyncio.gather(*(node.completed.wait() for node in nodes.values())), timeout=10
            )
            assert len({node.state.state_hash() for node in nodes.values()}) == 1
            for node in nodes.values():
                assert node.state.nonces == {"Alice": 7, "Bob": 7, "Charlie": 6}
                assert sum(len(block.txs) for block, _ in node.finalized.values()) == 20
            (committed,) = await send_transactions(peers["V0"], [txs[0]], genesis)
            assert not committed.accepted and committed.reason == "max_height_reached"
        finally:
            if client_process is not None and client_process.returncode is None:
                client_process.kill()
                await client_process.wait()
            for node in nodes.values():
                node.stop()
            await asyncio.gather(*(network.close() for network in networks.values()))
        assert all(not network.transport._client_tasks for network in networks.values())
        assert all(not network.transport._client_writers for network in networks.values())

    asyncio.run(exercise())


def test_client_rejects_forged_validator_before_sending_any_transaction():
    async def exercise():
        genesis, scenario = load_config(
            ROOT / "config/t1_normal.json", ROOT / "config/genesis.json"
        )
        received = []
        done = asyncio.Event()

        async def forged_server(reader, writer):
            try:
                await write_frame(
                    writer,
                    make_hello("V0", keypair_from_seed(999, "V0").private_key, "127.0.0.1:9000"),
                )
                data = await reader.read()
                received.append(data)
            finally:
                writer.close()
                await writer.wait_closed()
                done.set()

        server = await asyncio.start_server(forged_server, "127.0.0.1", 0)
        try:
            port = server.sockets[0].getsockname()[1]
            with pytest.raises(HelloError, match="bad_signature"):
                await send_transactions(
                    f"127.0.0.1:{port}", build_scenario_transactions(scenario), genesis
                )
            await asyncio.wait_for(done.wait(), timeout=2)
            assert received == [b""]
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(exercise())


@pytest.mark.parametrize("failure", ["timeout", "wrong_ack"])
def test_client_fails_when_server_does_not_acknowledge_transaction(failure):
    async def exercise():
        genesis, scenario = load_config(
            ROOT / "config/t1_normal.json", ROOT / "config/genesis.json"
        )
        finished = asyncio.Event()

        async def server_session(reader, writer):
            try:
                await write_frame(
                    writer,
                    make_hello(
                        "V0", keypair_from_seed(scenario.seed, "V0").private_key, "127.0.0.1:9000"
                    ),
                )
                await read_frame(reader)
                if failure == "wrong_ack":
                    await write_frame(
                        writer, encode({"accepted": True, "reason": "", "tx_id": "0" * 64})
                    )
                await reader.read()
            finally:
                writer.close()
                await writer.wait_closed()
                finished.set()

        server = await asyncio.start_server(server_session, "127.0.0.1", 0)
        try:
            port = server.sockets[0].getsockname()[1]
            expected = asyncio.TimeoutError if failure == "timeout" else ValueError
            with pytest.raises(expected):
                await send_transactions(
                    f"127.0.0.1:{port}",
                    build_scenario_transactions(scenario)[:1],
                    genesis,
                    timeout_s=0.1,
                )
            await asyncio.wait_for(finished.wait(), timeout=2)
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(exercise())
