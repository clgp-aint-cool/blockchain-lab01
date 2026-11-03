import asyncio
from pathlib import Path

import pytest

from l1sim.config import load_config
from l1sim.network.gossip import Gossip
from l1sim.network.static_peers import StaticPeers
from l1sim.network.tcp_transport import TcpTransport
from l1sim.node import Node, NodeNetwork

ROOT = Path(__file__).parents[2]


def test_node_network_uses_real_modules():
    genesis, scenario = load_config(ROOT / "config/t1_normal.json", ROOT / "config/genesis.json")
    peers = {validator: f"127.0.0.1:{9000 + i}" for i, validator in enumerate(genesis.validators)}
    node = Node("V0", genesis, scenario, peer_addresses=peers)
    network = NodeNetwork(node)
    assert isinstance(network.transport, TcpTransport)
    assert isinstance(network.gossip, Gossip)
    assert isinstance(network.peers, StaticPeers)
    assert node.genesis.validators == genesis.validators


def test_node_initializes_modules_and_accepts_transaction(tmp_path):
    genesis, scenario = load_config(
        ROOT / "config/t1_normal.json",
        ROOT / "config/genesis.json",
    )
    from l1sim.client import build_scenario_transactions
    from l1sim.logger import EventLogger

    node = Node(
        "V0",
        genesis,
        scenario,
        logger=EventLogger("V0", "t1_normal", tmp_path),
    )
    tx = build_scenario_transactions(scenario)[0]

    assert node.submit_transaction(tx)
    assert len(node.mempool) == 1


def test_node_rejects_non_validator():
    genesis, scenario = load_config(
        ROOT / "config/t1_normal.json",
        ROOT / "config/genesis.json",
    )

    with pytest.raises(ValueError, match="not a validator"):
        Node("V99", genesis, scenario)


def test_node_rejects_invalid_message_type():
    genesis, scenario = load_config(
        ROOT / "config/t1_normal.json",
        ROOT / "config/genesis.json",
    )
    node = Node("V0", genesis, scenario)

    with pytest.raises(TypeError, match="Message"):
        node.receive_message(object())


def test_node_engine_events_include_round(tmp_path):
    genesis, scenario = load_config(
        ROOT / "config/t1_normal.json",
        ROOT / "config/genesis.json",
    )
    from l1sim.logger import EventLogger

    logger = EventLogger("V0", "t1_normal", tmp_path)
    node = Node("V0", genesis, scenario, logger=logger)

    async def start():
        node.start()
        node.stop()

    asyncio.run(start())

    line = logger.path.read_text(encoding="utf-8").strip()
    assert '"event":"new_round"' in line
    assert '"round":0' in line
