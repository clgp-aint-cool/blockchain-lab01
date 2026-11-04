"""Exercise the real NodeNetwork sender binding and its cancellation lifecycle."""

import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from l1sim.client import build_scenario_transactions
from l1sim.config import load_config
from l1sim.encoding import encode
from l1sim.models import Kind, Message
from l1sim.node import Node, NodeNetwork
from tests.unit.test_node import ROOT


@pytest.mark.parametrize(
    ("drop_rate", "duplicate_rate", "gst_height", "copies"),
    [(1.0, 0.0, 10, 0), (0.0, 1.0, 0, 2), (1.0, 0.0, 0, 1)],
)
def test_gossip_sender_applies_real_fault_decisions(drop_rate, duplicate_rate, gst_height, copies):
    async def exercise():
        genesis, scenario = load_config(
            ROOT / "config/t1_normal.json", ROOT / "config/genesis.json"
        )
        scenario = replace(
            scenario,
            network=replace(
                scenario.network,
                drop_rate=drop_rate,
                duplicate_rate=duplicate_rate,
                gst_height=gst_height,
            ),
        )
        peers = {v: f"127.0.0.1:{9000 + i}" for i, v in enumerate(genesis.validators)}
        node = Node(
            "V0", genesis, scenario, peer_addresses=peers, logger=lambda *args, **kwargs: None
        )
        network = NodeNetwork(node)
        network.transport.send = AsyncMock()
        message = Message(Kind.TX, "V0", 1, build_scenario_transactions(scenario)[0])
        data = encode(message)
        try:
            assert network.gossip.sender == network._send_with_faults
            await network.gossip.sender("V1", data)
            assert network.transport.send.await_count == copies
            for call in network.transport.send.await_args_list:
                assert call.args == ("V1", data)
        finally:
            node.stop()
            await network.close()

    asyncio.run(exercise())


def test_gossip_stop_cancels_delayed_copies(monkeypatch):
    async def exercise():
        genesis, scenario = load_config(
            ROOT / "config/t1_normal.json", ROOT / "config/genesis.json"
        )
        peers = {v: f"127.0.0.1:{9000 + i}" for i, v in enumerate(genesis.validators)}
        node = Node(
            "V0", genesis, scenario, peer_addresses=peers, logger=lambda *args, **kwargs: None
        )
        network = NodeNetwork(node)
        network.transport.send = AsyncMock()
        monkeypatch.setattr(network.fault_injector, "decide", lambda *args: [60_000, 60_000])
        monkeypatch.setattr(network.transport, "peers", lambda: ["V1"])
        message = Message(Kind.TX, "V0", 1, build_scenario_transactions(scenario)[0])
        try:
            network.gossip.broadcast_nowait(message)
            await asyncio.sleep(0.01)
            assert network.gossip._tasks
            await asyncio.wait_for(network.gossip.stop(), timeout=1)
            assert not network.gossip._tasks
            network.transport.send.assert_not_awaited()
        finally:
            node.stop()
            await network.close()

    asyncio.run(exercise())
