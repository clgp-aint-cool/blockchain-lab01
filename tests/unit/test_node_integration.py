import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from l1sim.client import build_scenario_transactions
from l1sim.config import load_config
from l1sim.crypto import keypair_from_seed, sign
from l1sim.encoding import block_hash, decode, encode
from l1sim.logger import EventLogger, ResultWriter
from l1sim.models import BodyRequest, Domain, Kind, Message, Phase, Tx, Vote
from l1sim.node import Node, NodeNetwork
from l1sim.run_node import run_node
from tests.unit.test_node import ROOT


@pytest.fixture
def configs():
    return load_config(ROOT / "config/t1_normal.json", ROOT / "config/genesis.json")


def make_node(tmp_path, configs, **kwargs):
    genesis, scenario = configs
    return Node(
        "V0",
        genesis,
        scenario,
        logger=EventLogger("V0", scenario.name, tmp_path / "events"),
        result_writer=ResultWriter(scenario.name, tmp_path / "results"),
        **kwargs,
    )


def test_pending_nonces_duplicates_and_bad_signatures(tmp_path, configs):
    sent = []
    node = make_node(tmp_path, configs, broadcast=sent.append)
    txs = build_scenario_transactions(configs[1])
    # A future nonce can arrive before nonce 0 and must remain queued.
    for tx in reversed(txs):
        assert node.submit_transaction(tx)
    assert len(node.mempool) == 20
    assert len(sent) == 20
    assert not node.submit_transaction(txs[0])
    assert not node.submit_transaction(replace(txs[0], signature="00" * 64))
    assert len(sent) == 20
    node.state.nonces["Alice"] = 1
    node.mempool.purge_stale(node.state)
    assert not node.submit_transaction(txs[0])
    node.stop()
    assert not node.submit_transaction(txs[1])


def test_stop_cancels_every_timer_and_late_callbacks(tmp_path, configs):
    async def exercise():
        node = make_node(tmp_path, configs)
        fired = []
        node.engine.on_timeout = lambda *args: fired.append(args)
        for _ in range(3):
            node.schedule_timeout(1, 1, 0, node.engine.step)
        handles = tuple(node._timeout_handles)
        node.stop()
        node.stop()
        await asyncio.sleep(0.01)
        assert all(handle.cancelled() for handle in handles)
        assert not node._timeout_handles
        assert not fired
        with pytest.raises(RuntimeError):
            node.start()

    asyncio.run(exercise())


def test_nodes_finalize_same_transactions_through_header_body_protocol(tmp_path, configs):
    async def exercise():
        genesis, scenario = configs
        loop = asyncio.get_running_loop()
        nodes = {}
        messages = []

        def send(peer, message):
            messages.append(message)
            loop.call_soon(nodes[peer].receive_message, decode(encode(message)))

        def broadcast(sender, message):
            for peer in genesis.validators:
                if peer != sender:
                    send(peer, message)

        for validator in genesis.validators:
            nodes[validator] = Node(
                validator,
                genesis,
                scenario,
                logger=EventLogger(validator, scenario.name, tmp_path / "events"),
                result_writer=ResultWriter(scenario.name, tmp_path / "results"),
                broadcast=lambda message, sender=validator: broadcast(sender, message),
                send_to=send,
            )
        try:
            for node in nodes.values():
                for tx in build_scenario_transactions(scenario):
                    node.submit_transaction(tx)
            for node in nodes.values():
                node.start()
            await asyncio.wait_for(
                asyncio.gather(*(node.completed.wait() for node in nodes.values())), timeout=5
            )
            assert {message.kind for message in messages} >= {
                Kind.TX,
                Kind.HEADER,
                Kind.BODY_REQUEST,
                Kind.BODY,
                Kind.VOTE,
            }
            assert not any(message.height > scenario.max_height for message in messages)
            results = []
            for node in nodes.values():
                assert len(node.finalized) == scenario.max_height
                assert node.engine.height == scenario.max_height
                assert not node._timeout_handles
                assert node.state.nonces == {"Alice": 7, "Bob": 7, "Charlie": 6}
                assert len(node.mempool) == 0
                assert sum(len(block.txs) for block, _ in node.finalized.values()) == 20
                events = [json.loads(line) for line in node.logger.path.read_text().splitlines()]
                assert sum(event["event"] == "finalize" for event in events) == 5
                assert not any(event["height"] > scenario.max_height for event in events)
                # Late inputs and explicit start calls cannot restart a completed engine.
                event_count = len(events)
                node.engine.start_round(1)
                node.engine.start()
                node.engine.on_timeout(node.engine.height, node.engine.round, node.engine.step)
                node.engine.on_proposal(node.finalized[scenario.max_height][0])
                node.engine.on_vote(node.finalized[scenario.max_height][1].precommits[0])
                assert len(node.logger.path.read_text().splitlines()) == event_count
                result = json.loads(node.write_result().read_bytes())
                result.pop("node_id")
                results.append(result)
            assert all(result == results[0] for result in results)
        finally:
            for node in nodes.values():
                node.stop()

    asyncio.run(exercise())


def test_body_request_is_targeted_and_forged_header_is_ignored(tmp_path, configs):
    async def exercise():
        node = make_node(tmp_path, configs)
        proposer = Node(
            "V1",
            *configs,
            logger=lambda *args, **kwargs: None,
            result_writer=ResultWriter(configs[1].name, tmp_path / "results"),
        )
        sent = []
        proposer.broadcast_handler = sent.append
        replies = []
        proposer.send_handler = lambda peer, message: replies.append((peer, message))
        requests = []
        node.send_handler = lambda peer, message: requests.append((peer, message))
        try:
            proposer.start()
            header = next(message for message in sent if message.kind is Kind.HEADER)
            forged = replace(header, payload=replace(header.payload, signature="00" * 64))
            node.receive_message(forged)
            assert not requests
            node.receive_message(header)
            peer, request = requests.pop()
            assert peer == "V1"
            assert request.payload == BodyRequest(block_hash(header.payload))
            proposer.receive_message(request)
            assert replies[0][0] == "V0"
            assert replies[0][1].kind is Kind.BODY
        finally:
            node.stop()
            proposer.stop()

    asyncio.run(exercise())


def test_cli_timeout_is_failure_and_resources_are_closed(tmp_path, configs, monkeypatch, capsys):
    adapters = []

    class NoPeers(NodeNetwork):
        async def start(self):
            adapters.append(self)

    monkeypatch.chdir(tmp_path)
    status = asyncio.run(
        run_node(
            "V0",
            str(ROOT / "config/t1_normal.json"),
            str(ROOT / "config/genesis.json"),
            timeout_s=0.01,
            network_factory=NoPeers,
        )
    )
    assert status == 1
    assert "timed out" in capsys.readouterr().err
    assert not adapters[0].gossip._tasks
    assert not adapters[0].transport.peers()
    assert not adapters[0].node._timeout_handles
    result = json.loads((tmp_path / "logs/results/t1_normal/V0.json").read_bytes())
    assert result["finalized_height"] == 0


def test_transport_closes_on_cancellation(tmp_path, configs, monkeypatch):
    class WaitingAdapter(NodeNetwork):
        async def start(self):
            self.started.set()

        async def close(self):
            self.closed = True

        def __init__(self, node):
            super().__init__(node)
            self.started = asyncio.Event()
            self.closed = False

    async def exercise():
        adapters = []

        def factory(node):
            adapter = WaitingAdapter(node)
            adapters.append(adapter)
            return adapter

        task = asyncio.create_task(
            run_node(
                "V0",
                str(ROOT / "config/t1_normal.json"),
                str(ROOT / "config/genesis.json"),
                network_factory=factory,
            )
        )
        await asyncio.sleep(0)
        await adapters[0].started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert adapters[0].closed
        assert adapters[0].node._stopped

    monkeypatch.chdir(tmp_path)
    asyncio.run(exercise())


def test_gossip_relays_transactions_and_bodies_across_multiple_hops(tmp_path, configs):
    from l1sim.network.gossip import DIRECT, FLOOD, Gossip

    async def exercise():
        genesis, scenario = configs
        loop = asyncio.get_running_loop()
        transports = {}
        nodes = {}
        gossips = {}

        class RingTransport:
            def __init__(self, node_id, neighbors):
                self.node_id = node_id
                self.neighbors = neighbors

            def set_receiver(self, receiver):
                self.receiver = receiver

            def peers(self):
                return self.neighbors

            async def send(self, peer, data):
                loop.call_soon(transports[peer].receiver, self.node_id, data)

        for index, validator in enumerate(genesis.validators):
            node = Node(
                validator,
                genesis,
                scenario,
                logger=EventLogger(validator, scenario.name, tmp_path / "events"),
                result_writer=ResultWriter(scenario.name, tmp_path / "results"),
            )
            nodes[validator] = node
            neighbors = [genesis.validators[(index - 1) % 10], genesis.validators[(index + 1) % 10]]
            transport = RingTransport(validator, neighbors)
            transports[validator] = transport
            gossip = Gossip(validator, transport, logger=node._event_adapter)
            gossips[validator] = gossip
            for kind in FLOOD | DIRECT:
                gossip.on(kind, node.receive_message)
            node.broadcast_handler = gossip.broadcast_nowait
            node.send_handler = gossip.send_to_nowait

        try:
            for tx in build_scenario_transactions(scenario):
                assert nodes["V0"].submit_transaction(tx)

            async def all_received():
                while not all(len(node.mempool) == 20 for node in nodes.values()):
                    await asyncio.sleep(0.01)

            await asyncio.wait_for(all_received(), timeout=3)
            for node in nodes.values():
                node.start()
            await asyncio.wait_for(
                asyncio.gather(*(node.completed.wait() for node in nodes.values())), timeout=5
            )
            assert len({node.state.state_hash() for node in nodes.values()}) == 1
            for node in nodes.values():
                assert node.state.nonces == {"Alice": 7, "Bob": 7, "Charlie": 6}
                assert not node._body_waiters
                events = [json.loads(line) for line in node.logger.path.read_text().splitlines()]
                assert not any(event["event"] == "gossip_handler_error" for event in events)
                assert sum(event["event"] == "conn_open" for event in events) == 0
        finally:
            for node in nodes.values():
                node.stop()
            await asyncio.gather(*(gossip.stop() for gossip in gossips.values()))

    asyncio.run(exercise())


def test_receive_message_forwards_only_what_the_node_keeps(tmp_path, configs):
    genesis, scenario = configs
    node = make_node(tmp_path, configs)
    tx = build_scenario_transactions(scenario)[0]
    unsigned = Tx(tx.sender, tx.key, "conflicting value", tx.nonce)
    user = keypair_from_seed(scenario.seed, tx.sender)
    conflicting = replace(unsigned, signature=sign(user.private_key, Domain.TX, unsigned))

    assert node.receive_message(Message(Kind.TX, "V1", 0, tx), "V1") is True
    # Same (sender, nonce), different tx: the mempool keeps the first, gossip must not flood it.
    assert node.receive_message(Message(Kind.TX, "V1", 0, conflicting), "V1") is False
    assert len(node.mempool) == 1

    validator = keypair_from_seed(scenario.seed, "V1")
    vote = Vote(Phase.PREVOTE, 1, 0, None, "V1")
    signed = replace(vote, signature=sign(validator.private_key, Domain.VOTE, vote))
    tx_domain = replace(vote, signature=sign(validator.private_key, Domain.TX, vote))

    assert node.receive_message(Message(Kind.VOTE, "V1", 1, signed), "V1") is True
    assert node.receive_message(Message(Kind.VOTE, "V1", 1, signed), "V1") is False
    assert node.receive_message(Message(Kind.VOTE, "V1", 1, tx_domain), "V1") is False
    records = [json.loads(line) for line in node.logger.path.read_text().splitlines()]
    rejected = [record for record in records if record["event"] == "vote_rejected"]
    assert [record["reason"] for record in rejected] == ["wrong_domain"]


def test_old_headers_and_bodies_are_pruned(tmp_path, configs):
    node = make_node(tmp_path, configs)
    node._headers = {"old": SimpleNamespace(height=1), "new": SimpleNamespace(height=25)}
    node._blocks = {
        "old": SimpleNamespace(header=SimpleNamespace(height=1)),
        "new": SimpleNamespace(header=SimpleNamespace(height=25)),
    }
    node._body_waiters = {"old": {"V1"}, "new": {"V2"}}

    node._prune_bodies(25)

    assert set(node._headers) == set(node._blocks) == set(node._body_waiters) == {"new"}
