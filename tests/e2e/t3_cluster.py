"""Ten real TCP validator processes with barriers around T3 fault delivery."""

import asyncio
import json
import multiprocessing
import time
import traceback
from dataclasses import replace
from pathlib import Path
from queue import Empty

from l1sim.client import build_scenario_transactions
from l1sim.config import load_config
from l1sim.crypto import sign, verify
from l1sim.encoding import decode
from l1sim.logger import EventLogger, ResultWriter
from l1sim.models import Domain, Kind, Message, Phase, Vote
from l1sim.node import Node, NodeNetwork
from tests.e2e.test_local_nodes import available_base_port

ROOT = Path(__file__).parents[2]


def _validator(node_id, peers, workdir, inject, start, stop, statuses):
    async def exercise():
        genesis, scenario = load_config(
            ROOT / "config/t3_bad_sig.json", ROOT / "config/genesis.json"
        )
        node = Node(
            node_id,
            genesis,
            scenario,
            listen_addr=peers[node_id],
            peer_addresses=peers,
            logger=EventLogger(node_id, scenario.name, Path(workdir) / "logs/events"),
            result_writer=ResultWriter(scenario.name, Path(workdir) / "logs/results"),
        )
        network = NodeNetwork(node)
        forwarded_invalid = []
        send_with_faults = network.gossip.sender

        async def observed_send(peer, data):
            message = decode(data)
            if message.kind is Kind.VOTE:
                vote = message.payload
                if vote.voter != node_id and not verify(
                    node.validator_keys[vote.voter], Domain.VOTE, vote, vote.signature
                ):
                    forwarded_invalid.append((peer, vote.voter, vote.phase.value))
            await send_with_faults(peer, data)

        network.gossip.sender = observed_send

        async def wait_for_flag(flag):
            while not flag.is_set():
                await asyncio.sleep(0.01)

        try:
            await network.start()
            await asyncio.wait_for(network.wait_ready(), timeout=20)
            network.activate()
            statuses.put((node_id, "ready"))
            await wait_for_flag(inject)
            if node.byzantine is not None:
                vote = Vote(Phase.PREVOTE, 1, 0, None, node_id)
                vote = replace(vote, signature=sign(node.keypair.private_key, Domain.VOTE, vote))
                # Use the production rule application and gossip path. No consensus
                # starts until every receiver has classified these invalid votes.
                node._broadcast(Message(Kind.VOTE, node_id, 1, vote))

            expected = {("V5", "bad_signature"), ("V6", "wrong_domain")}
            expected = {item for item in expected if item[0] != node_id}
            while True:
                events = [
                    json.loads(line)
                    for line in node.logger.path.read_text(encoding="utf-8").splitlines()
                ]
                rejected = {
                    (event["voter"], event["reason"])
                    for event in events
                    if event["event"] == "vote_rejected"
                }
                if expected <= rejected:
                    break
                await asyncio.sleep(0.01)
            assert node.engine.height == 1
            assert node.engine.votes.total(1, 0, Phase.PREVOTE) == 0
            received_votes = [
                event
                for event in events
                if event["event"] == "gossip_recv" and event.get("kind") == "VOTE"
            ]
            assert len(received_votes) >= len(expected)
            assert all(event["accepted"] is False for event in received_votes)
            statuses.put((node_id, "checked"))
            await wait_for_flag(start)
            for tx in build_scenario_transactions(scenario):
                node.submit_transaction(tx, broadcast=False)
            node.start()
            await asyncio.wait_for(node.completed.wait(), timeout=30)
            node.write_result()
            statuses.put((node_id, "done"))
            # Keep body-serving connections alive until every process has committed.
            await wait_for_flag(stop)
        finally:
            node.stop()
            await network.close()
            assert not forwarded_invalid, f"Invalid vote was forwarded: {forwarded_invalid}"

    try:
        asyncio.run(exercise())
    except Exception:
        statuses.put((node_id, "error", traceback.format_exc()))
        raise


def run_t3(workdir):
    scenario = json.loads((ROOT / "config/t3_bad_sig.json").read_text(encoding="utf-8"))
    base = available_base_port()
    peers = {node: f"127.0.0.1:{base + i}" for i, node in enumerate(scenario["validators"])}
    context = multiprocessing.get_context("spawn")
    inject, start, stop = (context.Event() for _ in range(3))
    statuses = context.Queue()
    processes = []

    def await_stage(stage):
        deadline = time.monotonic() + 40
        received = set()
        while received != set(peers):
            try:
                status = statuses.get(timeout=max(0.01, deadline - time.monotonic()))
            except Empty:
                raise AssertionError(f"T3 {stage}: missing {set(peers) - received}") from None
            assert status[1] == stage, f"T3 process failure: {status}"
            assert status[0] not in received
            received.add(status[0])

    try:
        for node in peers:
            process = context.Process(
                target=_validator, args=(node, peers, str(workdir), inject, start, stop, statuses)
            )
            process.start()
            processes.append(process)
        await_stage("ready")
        inject.set()
        await_stage("checked")
        start.set()
        await_stage("done")
        stop.set()
        for process in processes:
            process.join(timeout=10)
            assert process.exitcode == 0, f"T3 worker exited with {process.exitcode}"
    finally:
        stop.set()
        for process in processes:
            if process.is_alive():
                process.terminate()
            process.join(timeout=5)
        statuses.close()
        statuses.join_thread()

    results, events = {}, {}
    for node in peers:
        results[node] = (workdir / f"logs/results/{scenario['name']}/{node}.json").read_bytes()
        events[node] = [
            json.loads(line)
            for line in (workdir / f"logs/events/{scenario['name']}/{node}.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
    return scenario, results, events, {rule["node"] for rule in scenario["byzantine"]}
