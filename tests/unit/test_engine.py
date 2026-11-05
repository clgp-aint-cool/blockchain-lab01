import json
from dataclasses import replace
from pathlib import Path

import pytest

from l1sim.consensus.engine import ConsensusEngine, Step, TimeoutConfig, proposer_for
from l1sim.crypto import sign, verify
from l1sim.encoding import block_hash
from l1sim.execution import Mempool, State
from l1sim.logger import EventLogger
from l1sim.models import GENESIS_HASH, Domain, Kind, Message, Phase, Vote
from tests.unit.consensus_harness import TIMEOUTS, Network, sample_txs

CONFIG = Path(__file__).resolve().parents[2] / "config" / "default.json"
CONSENSUS = {
    "timeout_propose_ms": 1000,
    "timeout_prevote_ms": 800,
    "timeout_precommit_ms": 600,
    "timeout_delta_ms": 300,
}


def test_proposer_rotates_with_height_and_round_over_sorted_validators():
    validators = ["V3", "V1", "V0", "V2"]

    assert proposer_for(validators, 1, 0) == "V1"
    assert proposer_for(validators, 1, 1) == "V2"
    assert proposer_for(validators, 2, 0) == "V2"
    assert proposer_for(validators, 3, 1) == "V0"
    assert proposer_for({"V1": None, "V0": None}, 1, 0) == "V1"


def test_proposer_requires_validators():
    with pytest.raises(ValueError):
        proposer_for([], 1, 0)


def test_timeouts_grow_with_round():
    timeouts = TimeoutConfig.from_config(CONSENSUS)

    assert timeouts.duration(Step.PROPOSE, 0) == 1000
    assert timeouts.duration(Step.PREVOTE, 0) == 800
    assert timeouts.duration(Step.PRECOMMIT, 2) == 1200
    assert timeouts.duration(Step.PROPOSE, 1) > timeouts.duration(Step.PROPOSE, 0)


def test_timeout_config_has_no_defaults():
    missing = {key: value for key, value in CONSENSUS.items() if key != "timeout_delta_ms"}

    with pytest.raises(KeyError):
        TimeoutConfig.from_config(missing)


@pytest.mark.parametrize("bad", [-1, 1.5, True])
def test_timeout_config_rejects_bad_values(bad):
    with pytest.raises(ValueError):
        TimeoutConfig.from_config({**CONSENSUS, "timeout_delta_ms": bad})


def test_default_config_provides_every_timeout():
    consensus = json.loads(CONFIG.read_text())["consensus"]

    assert TimeoutConfig.from_config(consensus).delta_ms == 300


# --------------------------------------------------------------------------- engine


def _assert_no_double_sign(net):
    """Rule 1-2: every node signed at most one value per (height, round, phase)."""
    for node in net.ids:
        seen = {}
        for vote in net.votes_sent(node):
            slot = (vote.height, vote.round, vote.phase)
            assert seen.setdefault(slot, vote.block_hash) == vote.block_hash, (node, slot)


def _assert_certificates_valid(net, node):
    keys = {voter: pair.public_key for voter, pair in net.keys.items()}
    quorum = net.engines[node].votes.quorum
    for block, certificate in net.finalized[node]:
        assert certificate.block_hash == block_hash(block.header)
        assert certificate.height == block.header.height
        assert len(certificate.precommits) >= quorum
        for vote in certificate.precommits:
            assert verify(keys[vote.voter], Domain.VOTE, vote, vote.signature)


@pytest.mark.parametrize("n", [4, 10])
def test_happy_path_all_nodes_finalize_the_same_blocks(n):
    net = Network(n, txs=sample_txs())
    net.start()

    assert net.run_until_height(3)

    chains = {tuple(net.hashes(node)[:3]) for node in net.ids}
    assert len(chains) == 1
    states = {net.engines[node].state.state_hash() for node in net.ids}
    assert len(states) == 1
    first_block = net.finalized["V0"][0][0]
    assert len(first_block.txs) == len(sample_txs())
    assert first_block.header.proposer == proposer_for(net.ids, 1, 0)
    assert all(block.header.round == 0 for block, _ in net.finalized["V0"])
    for node in net.ids:
        _assert_certificates_valid(net, node)
        assert sorted(net.engines[node].finalized)[:3] == [1, 2, 3]
    _assert_no_double_sign(net)


def test_same_inputs_produce_the_same_event_log():
    runs = []
    for _ in range(2):
        net = Network(4, txs=sample_txs())
        net.start()
        net.run_until_height(3)
        runs.append((net.events, net.hashes("V0")))

    assert runs[0] == runs[1]


def test_silent_proposer_times_out_and_next_round_finalizes():
    silent = proposer_for([f"V{i}" for i in range(4)], 1, 0)

    def drop(src, dst, message):
        return src == silent and message.kind is Kind.BODY and message.height == 1

    net = Network(4, txs=sample_txs(), drop=drop)
    net.start()

    assert net.run_until_height(3)

    for node in net.ids:
        block, certificate = net.finalized[node][0]
        assert block.header.round == 1
        assert block.header.proposer == proposer_for(net.ids, 1, 1) != silent
        assert certificate.precommits[0].round == 1
    assert len({tuple(net.hashes(node)[:3]) for node in net.ids}) == 1

    honest = next(node for node in net.ids if node != silent)
    assert "timeout" in net.event_names(honest)
    assert any(
        (vote.height, vote.round, vote.phase, vote.block_hash) == (1, 0, Phase.PREVOTE, None)
        for vote in net.votes_sent(honest)
    )
    propose_timeouts = [
        delay
        for delay, height, round, step in net.scheduled[honest]
        if height == 1 and step is Step.PROPOSE
    ]
    assert propose_timeouts[:2] == [1000, 1300]
    _assert_no_double_sign(net)


def test_late_proposal_after_nil_prevote_does_not_cause_second_prevote():
    net = Network(4, drop=lambda src, dst, message: True)
    net.start(only=["V0"])
    engine = net.engines["V0"]
    proposer = proposer_for(net.ids, 1, 0)
    block = net.make_block(proposer, 1, 0, parent_hash=GENESIS_HASH)

    assert net.clock.run(lambda: engine.step is Step.PREVOTE)
    net.deliver("V0", Message(Kind.BODY, proposer, 1, block))

    prevotes = [vote for vote in net.votes_sent("V0") if vote.phase is Phase.PREVOTE]
    assert [(vote.round, vote.block_hash) for vote in prevotes] == [(0, None)]
    _assert_no_double_sign(net)


def test_conflicting_proposals_get_only_one_prevote():
    net = Network(4, drop=lambda src, dst, message: True)
    net.start(only=["V0"])
    proposer = proposer_for(net.ids, 1, 0)
    first = net.make_block(proposer, 1, 0, sample_txs()[:1], parent_hash=GENESIS_HASH)
    second = net.make_block(proposer, 1, 0, sample_txs()[:2], parent_hash=GENESIS_HASH)

    net.deliver("V0", Message(Kind.BODY, proposer, 1, first))
    net.deliver("V0", Message(Kind.BODY, proposer, 1, second))

    assert [vote.block_hash for vote in net.votes_sent("V0")] == [block_hash(first.header)]
    assert "proposal_conflict" in net.event_names("V0")


def test_forged_proposal_cannot_take_the_real_proposers_slot():
    net = Network(4, drop=lambda src, dst, message: True)
    net.start(only=["V0"])
    proposer = proposer_for(net.ids, 1, 0)
    forger = next(node for node in net.ids if node not in (proposer, "V0"))
    forged = net.make_block(proposer, 1, 0, parent_hash=GENESIS_HASH, signer=forger)
    real = net.make_block(proposer, 1, 0, sample_txs()[:1], parent_hash=GENESIS_HASH)

    net.deliver("V0", Message(Kind.BODY, proposer, 1, forged))
    net.deliver("V0", Message(Kind.BODY, proposer, 1, real))

    assert [vote.block_hash for vote in net.votes_sent("V0")] == [block_hash(real.header)]
    assert ("proposal_rejected", "bad_signature") in [
        (event, fields.get("reason")) for event, fields in net.events["V0"]
    ]


def test_invalid_block_gets_nil_prevote():
    net = Network(4, drop=lambda src, dst, message: True)
    net.start(only=["V0"])
    proposer = proposer_for(net.ids, 1, 0)
    bad = net.make_block(proposer, 1, 0, parent_hash=GENESIS_HASH, state_hash="f" * 64)

    net.deliver("V0", Message(Kind.BODY, proposer, 1, bad))

    assert [(vote.phase, vote.block_hash) for vote in net.votes_sent("V0")] == [
        (Phase.PREVOTE, None)
    ]
    assert ("invalid_block", "bad_state_hash") in [
        (event, fields.get("reason")) for event, fields in net.events["V0"]
    ]


def test_node_missing_prevotes_still_finalizes_from_precommits():
    def drop(src, dst, message):
        return dst == "V3" and message.kind is Kind.VOTE and message.payload.phase is Phase.PREVOTE

    net = Network(4, txs=sample_txs(), drop=drop)
    net.start()

    assert net.run_until_height(3)

    assert net.hashes("V3")[:3] == net.hashes("V0")[:3]
    block_precommits = [
        vote
        for vote in net.votes_sent("V3")
        if vote.phase is Phase.PRECOMMIT and vote.block_hash is not None
    ]
    assert block_precommits == []
    _assert_no_double_sign(net)


def test_precommit_follows_the_polka_not_the_own_prevote():
    net = Network(4, drop=lambda src, dst, message: True)
    net.start(only=["V0"])
    proposer = proposer_for(net.ids, 1, 0)
    bad = net.make_block(proposer, 1, 0, parent_hash=GENESIS_HASH, state_hash="f" * 64)
    good = net.make_block(proposer, 1, 0, parent_hash=GENESIS_HASH)

    net.deliver("V0", Message(Kind.BODY, proposer, 1, bad))
    for voter in ("V1", "V2", "V3"):
        vote = net.make_vote(voter, Phase.PREVOTE, 1, 0, block_hash(good.header))
        net.deliver("V0", Message(Kind.VOTE, voter, 1, vote))

    # V0 prevoted NIL and does not have a valid copy of the polka block, so it may not
    # precommit it; it waits for the prevote timeout and precommits NIL.
    assert net.clock.run(lambda: net.engines["V0"].step is Step.PRECOMMIT)
    assert [(vote.phase, vote.block_hash) for vote in net.votes_sent("V0")] == [
        (Phase.PREVOTE, None),
        (Phase.PRECOMMIT, None),
    ]


def test_nil_prevote_then_polka_for_a_known_valid_block_precommits_that_block():
    net = Network(4, drop=lambda src, dst, message: True)
    net.start(only=["V0"])
    engine = net.engines["V0"]
    proposer = proposer_for(net.ids, 1, 0)
    block = net.make_block(proposer, 1, 0, parent_hash=GENESIS_HASH)
    digest = block_hash(block.header)

    assert net.clock.run(lambda: engine.step is Step.PREVOTE)
    net.deliver("V0", Message(Kind.BODY, proposer, 1, block))
    for voter in ("V1", "V2", "V3"):
        vote = net.make_vote(voter, Phase.PREVOTE, 1, 0, digest)
        net.deliver("V0", Message(Kind.VOTE, voter, 1, vote))

    assert [(vote.phase, vote.block_hash) for vote in net.votes_sent("V0")] == [
        (Phase.PREVOTE, None),
        (Phase.PRECOMMIT, digest),
    ]


def test_commit_from_an_older_round_is_finalized_in_a_later_round():
    net = Network(4, drop=lambda src, dst, message: True)
    net.start(only=["V0"])
    engine = net.engines["V0"]
    proposer = proposer_for(net.ids, 1, 0)
    block = net.make_block(proposer, 1, 0, parent_hash=GENESIS_HASH)
    digest = block_hash(block.header)

    net.deliver("V0", Message(Kind.BODY, proposer, 1, block))
    assert net.clock.run(lambda: engine.round == 2)

    for voter in ("V1", "V2", "V3"):
        vote = net.make_vote(voter, Phase.PRECOMMIT, 1, 0, digest)
        net.deliver("V0", Message(Kind.VOTE, voter, 1, vote))

    assert net.hashes("V0") == [digest]
    assert net.finalized["V0"][0][1].precommits[0].round == 0
    assert engine.height == 2 and engine.round == 0


def test_future_height_messages_are_buffered_and_old_heights_ignored():
    reference = Network(4, txs=sample_txs())
    reference.start()
    assert reference.run_until_height(2)
    (block1, cert1), (block2, cert2) = reference.finalized["V0"][:2]

    net = Network(4, txs=sample_txs(), drop=lambda src, dst, message: True)
    net.start(only=["V0"])
    engine = net.engines["V0"]

    def send_height(block, certificate):
        net.deliver("V0", Message(Kind.BODY, block.header.proposer, block.header.height, block))
        for vote in certificate.precommits:
            net.deliver("V0", Message(Kind.VOTE, vote.voter, vote.height, vote))

    send_height(block2, cert2)
    assert engine.height == 1 and net.finalized["V0"] == []

    send_height(block1, cert1)
    assert net.hashes("V0") == reference.hashes("V0")[:2]
    assert engine.height == 3

    old = cert1.precommits[0]
    net.deliver("V0", Message(Kind.VOTE, old.voter, old.height, old))
    assert net.event_names("V0")[-1] == "ignored_old_height"


def test_engine_rejects_node_outside_validator_set():
    net = Network(4)

    with pytest.raises(ValueError):
        ConsensusEngine(
            node_id="V9",
            keypair=net.keys["V0"],
            validator_keys={node: pair.public_key for node, pair in net.keys.items()},
            timeouts=TIMEOUTS,
            state=State(),
            mempool=Mempool(),
            max_txs_per_block=1,
            broadcast=lambda message: None,
            schedule_timeout=lambda *args: None,
            cancel_timeout=lambda handle: None,
            on_finalize=lambda block, certificate: None,
            logger=lambda event, **fields: None,
        )


def test_engine_never_sends_node_to_the_logger():
    net = Network(4, txs=sample_txs())
    net.start()

    assert net.run_until_height(2)
    for node, events in net.events.items():
        assert events, node
        assert all("node" not in fields for _, fields in events)


def test_engine_logs_through_event_logger(tmp_path):
    net = Network(4)
    event_logger = EventLogger("V1", "engine_logger", tmp_path)
    engine = ConsensusEngine(
        node_id="V1",
        keypair=net.keys["V1"],
        validator_keys={node: pair.public_key for node, pair in net.keys.items()},
        timeouts=TIMEOUTS,
        state=State(),
        mempool=Mempool(),
        max_txs_per_block=1,
        broadcast=lambda message: None,
        schedule_timeout=lambda *args: None,
        cancel_timeout=lambda handle: None,
        on_finalize=lambda block, certificate: None,
        logger=event_logger.event,
    )

    engine.start()

    records = [json.loads(line) for line in event_logger.path.read_text().splitlines()]
    assert [record["event"] for record in records][:3] == ["new_round", "propose", "prevote"]
    assert all(record["node"] == "V1" for record in records)
    assert all(record["height"] == 1 and record["round"] == 0 for record in records)


def _rejection_reasons(net, node):
    return [fields["reason"] for event, fields in net.events[node] if event == "vote_rejected"]


def test_on_vote_returns_true_only_for_counted_votes():
    net = Network(4, drop=lambda src, dst, message: True)
    net.start(only=["V0"])
    engine = net.engines["V0"]
    vote = net.make_vote("V2", Phase.PREVOTE, 1, 0, None)
    conflicting = net.make_vote("V2", Phase.PREVOTE, 1, 0, "a" * 64)
    future = net.make_vote("V3", Phase.PRECOMMIT, 4, 2, None)

    assert engine.on_vote(vote) is True
    assert engine.on_vote(vote) is False  # duplicate
    # A conflicting vote counts for its own value, so it is forwarded once...
    assert engine.on_vote(conflicting) is True
    assert engine.on_vote(conflicting) is False  # ...and not again
    assert engine.on_vote(future) is True  # kept until the engine gets there
    assert {"duplicate_ignored", "equivocation_detected"} <= set(net.event_names("V0"))


def test_on_vote_rejects_invalid_votes_with_a_reason():
    net = Network(4, drop=lambda src, dst, message: True)
    net.start(only=["V0"])
    engine = net.engines["V0"]
    vote = Vote(Phase.PREVOTE, 1, 0, None, "V2")
    forged = replace(vote, signature=sign(net.keys["V3"].private_key, Domain.VOTE, vote))
    tx_domain = replace(vote, signature=sign(net.keys["V2"].private_key, Domain.TX, vote))
    stranger = Vote(Phase.PREVOTE, 1, 0, None, "V9")
    stranger = replace(stranger, signature=sign(net.keys["V0"].private_key, Domain.VOTE, stranger))

    assert engine.on_vote(forged) is False
    assert engine.on_vote(tx_domain) is False
    assert engine.on_vote(stranger) is False
    assert _rejection_reasons(net, "V0") == ["bad_signature", "wrong_domain", "unknown_voter"]


def test_on_proposal_returns_whether_the_block_was_stored():
    net = Network(4, drop=lambda src, dst, message: True)
    net.start(only=["V0"])
    engine = net.engines["V0"]
    proposer = proposer_for(net.ids, 1, 0)
    forger = next(node for node in net.ids if node not in (proposer, "V0"))
    forged = net.make_block(proposer, 1, 0, parent_hash=GENESIS_HASH, signer=forger)
    block = net.make_block(proposer, 1, 0, sample_txs()[:1], parent_hash=GENESIS_HASH)
    conflicting = net.make_block(proposer, 1, 0, sample_txs()[:2], parent_hash=GENESIS_HASH)
    wrong_proposer = net.make_block(forger, 1, 0, parent_hash=GENESIS_HASH)

    assert engine.on_proposal(forged) is False
    assert engine.on_proposal(wrong_proposer) is False
    assert engine.on_proposal(block) is True
    assert engine.on_proposal(block) is False  # same proposal again
    assert engine.on_proposal(conflicting) is False


def test_stop_ignores_every_input_and_cancels_the_pending_timeout():
    net = Network(4, drop=lambda src, dst, message: True)
    net.start(only=["V0"])
    engine = net.engines["V0"]
    proposer = proposer_for(net.ids, 1, 0)
    assert proposer != "V0"  # V0 waits in PROPOSE with a timeout pending
    pending = engine._timeout_handle
    before = (engine.height, engine.round, engine.step, len(net.sent), len(net.events["V0"]))

    engine.stop()

    assert pending.cancelled
    block = net.make_block(proposer, 1, 0, parent_hash=GENESIS_HASH)
    assert engine.on_proposal(block) is False
    assert engine.on_vote(net.make_vote("V2", Phase.PREVOTE, 1, 0, None)) is False
    engine.on_timeout(1, 0, Step.PROPOSE)
    engine.start_round(1)
    net.clock.run(lambda: False, max_ms=30_000)
    after = (engine.height, engine.round, engine.step, len(net.sent), len(net.events["V0"]))
    assert after == before


def test_stop_inside_on_finalize_keeps_the_finalized_height():
    net = Network(4, txs=sample_txs())
    engine = net.engines["V0"]
    record = engine._on_finalize

    def finalize_then_stop(block, certificate):
        record(block, certificate)
        engine.stop()

    engine._on_finalize = finalize_then_stop
    net.start()

    assert net.run_until_height(3, nodes=["V1", "V2", "V3"])
    ((block, _),) = net.finalized["V0"]
    assert engine.height == 1
    assert engine.parent_hash == block_hash(block.header)
    assert all(vote.height == 1 for vote in net.votes_sent("V0"))


def _lock_v0_on_round_0_block():
    """V0 alone (every delivery dropped) sees a polka for the round-0 proposal and locks it."""
    net = Network(4, drop=lambda src, dst, message: True)
    net.start(only=["V0"])
    engine = net.engines["V0"]
    proposer = proposer_for(net.ids, 1, 0)
    block = net.make_block(proposer, 1, 0, sample_txs()[:1], parent_hash=GENESIS_HASH)
    net.deliver("V0", Message(Kind.BODY, proposer, 1, block))
    for voter in ("V2", "V3"):
        vote = net.make_vote(voter, Phase.PREVOTE, 1, 0, block_hash(block.header))
        net.deliver("V0", Message(Kind.VOTE, voter, 1, vote))
    return net, engine, block


def _votes(net, node, phase, round):
    return [v.block_hash for v in net.votes_sent(node) if v.phase is phase and v.round == round]


def test_precommit_on_a_polka_locks_the_block():
    net, engine, block = _lock_v0_on_round_0_block()

    assert _votes(net, "V0", Phase.PRECOMMIT, 0) == [block_hash(block.header)]
    assert engine.locked_block == block and engine.locked_round == 0
    assert "lock" in net.event_names("V0")


def test_lock_survives_rounds_and_locked_block_is_prevoted():
    net, engine, block = _lock_v0_on_round_0_block()
    assert net.clock.run(lambda: engine.round == 1)
    proposer = proposer_for(net.ids, 1, 1)
    other = net.make_block(proposer, 1, 1, sample_txs()[:2], parent_hash=GENESIS_HASH)
    net.deliver("V0", Message(Kind.BODY, proposer, 1, other))

    assert _votes(net, "V0", Phase.PREVOTE, 1) == [block_hash(block.header)]
    assert engine.locked_block == block  # a timeout never releases the lock
    _assert_no_double_sign(net)


def test_locked_proposer_re_proposes_its_block():
    net, engine, block = _lock_v0_on_round_0_block()
    round = next(r for r in range(1, 8) if proposer_for(net.ids, 1, r) == "V0")

    assert net.clock.run(lambda: engine.round == round)
    proposals = [m.payload for src, m in net.sent if src == "V0" and m.kind is Kind.BODY]
    assert proposals == [block]  # the same block, original header and hash
    assert "repropose" in net.event_names("V0")
    assert _votes(net, "V0", Phase.PREVOTE, round) == [block_hash(block.header)]


def test_polka_in_the_current_round_moves_the_lock():
    net, engine, block = _lock_v0_on_round_0_block()
    assert net.clock.run(lambda: engine.round == 1 and engine.step is Step.PREVOTE)
    proposer = proposer_for(net.ids, 1, 1)
    other = net.make_block(proposer, 1, 1, sample_txs()[:2], parent_hash=GENESIS_HASH)
    net.deliver("V0", Message(Kind.BODY, proposer, 1, other))
    for voter in ("V1", "V2", "V3"):
        vote = net.make_vote(voter, Phase.PREVOTE, 1, 1, block_hash(other.header))
        net.deliver("V0", Message(Kind.VOTE, voter, 1, vote))

    assert _votes(net, "V0", Phase.PRECOMMIT, 1) == [block_hash(other.header)]
    assert engine.locked_block == other and engine.locked_round == 1


def test_late_polka_from_a_newer_round_unlocks_in_the_next_round():
    """Concept Guide, 'Ba mốc round': locked on B at round 0, a polka for C at round 1
    arrives after V0 precommitted NIL; in round 2 V0 may prevote C (1 > locked_round 0)."""
    net, engine, block = _lock_v0_on_round_0_block()
    assert net.clock.run(lambda: engine.round == 1 and engine.step is Step.PRECOMMIT)
    proposer = proposer_for(net.ids, 1, 1)
    other = net.make_block(proposer, 1, 1, sample_txs()[:2], parent_hash=GENESIS_HASH)
    net.deliver("V0", Message(Kind.BODY, proposer, 1, other))
    for voter in ("V1", "V2", "V3"):
        vote = net.make_vote(voter, Phase.PREVOTE, 1, 1, block_hash(other.header))
        net.deliver("V0", Message(Kind.VOTE, voter, 1, vote))
    assert engine.locked_block == block  # seeing the polka late does not move the lock

    assert net.clock.run(lambda: engine.round == 2)
    assert _votes(net, "V0", Phase.PREVOTE, 2) == [block_hash(other.header)]


def test_locking_keeps_safety_when_round_0_precommits_are_lost():
    """Concept Guide counter-example: V0 finalizes B in round 0, the others miss the
    precommits. Without locking they would finalize the round-1 proposal instead."""

    def drop(src, dst, message):
        vote = message.payload
        return (
            message.kind is Kind.VOTE
            and vote.phase is Phase.PRECOMMIT
            and vote.round == 0
            and dst != "V0"
        )

    net = Network(4, txs=sample_txs(), drop=drop)
    net.start()

    assert net.run_until_height(1)
    first = {net.hashes(node)[0] for node in net.ids}
    assert len(first) == 1, "two different blocks finalized at height 1"
    assert net.run_until_height(2)
    assert net.finalized["V0"][0][1].precommits[0].round == 0
    assert net.finalized["V1"][0][1].precommits[0].round >= 1
    _assert_no_double_sign(net)


def _partial_polka_network():
    """PR #22 review, P1: 10 honest nodes, only V0–V3 get the round-0 prevotes.

    V0–V3 see the polka for the round-0 block and lock it; every round-0
    precommit is lost. From round 1 on, everything is delivered.
    """
    lockers = {"V0", "V1", "V2", "V3"}
    net = None

    def drop(src, dst, message):
        if message.kind is not Kind.VOTE or net.engines[src].round != 0:
            return False
        return message.payload.phase is Phase.PRECOMMIT or dst not in lockers

    net = Network(10, txs=sample_txs(), drop=drop)
    return net, lockers


def test_polka_evidence_lets_nodes_that_missed_the_polka_converge():
    net, lockers = _partial_polka_network()
    net.start()

    assert net.run_until_height(1, max_ms=30_000)
    assert len({net.hashes(node)[0] for node in net.ids}) == 1
    block = net.finalized["V0"][0][0]
    assert block.header.round == 0  # the block V0–V3 locked, not a newer one
    assert all(net.finalized[node][0][1].precommits[0].round <= 2 for node in net.ids)
    assert all("polka_evidence" in net.event_names(node) for node in lockers)
    _assert_no_double_sign(net)


def test_without_polka_evidence_the_partial_polka_stalls(monkeypatch):
    """Control for the test above: the old behaviour never finalizes height 1."""
    monkeypatch.setattr(ConsensusEngine, "_send_polka_evidence", lambda self: None)
    net, _ = _partial_polka_network()
    net.start()

    assert not net.run_until_height(1, max_ms=30_000)


def test_round_skip_needs_f_plus_one_validators_in_the_higher_round():
    net = Network(4, drop=lambda src, dst, message: True)
    net.start(only=["V0"])
    engine = net.engines["V0"]

    one = net.make_vote("V1", Phase.PREVOTE, 1, 3, None)
    net.deliver("V0", Message(Kind.VOTE, "V1", 1, one))
    assert engine.round == 0  # f = 1 voter could be Byzantine

    two = net.make_vote("V2", Phase.PRECOMMIT, 1, 3, None)
    net.deliver("V0", Message(Kind.VOTE, "V2", 1, two))
    assert engine.round == 3
    assert ("round_skip", 3) in [(e, f.get("to_round")) for e, f in net.events["V0"]]


@pytest.mark.parametrize("junk", [1, 3])
def test_equivocators_votes_count_for_the_block_that_gets_committed(junk):
    """T7 stall: V7–V9 send V0 other precommits first, then their precommit for A.

    Only V1–V4 precommit A honestly, so V0 reaches 7 only if the equivocators'
    later votes count for A too. Before the fix they were dropped and V0 waited
    forever at height 1 while the other nodes moved on. ``junk=3`` is the PR #28
    review case: three different values first must not crowd out the vote for A.
    """
    net = Network(10, drop=lambda src, dst, message: True)
    net.start(only=["V0"])
    engine = net.engines["V0"]
    proposer = proposer_for(net.ids, 1, 0)
    block = net.make_block(proposer, 1, 0, sample_txs()[:1], parent_hash=GENESIS_HASH)
    other = net.make_block(proposer, 1, 0, sample_txs()[:2], parent_hash=GENESIS_HASH)
    net.deliver("V0", Message(Kind.BODY, proposer, 1, block))
    digest = block_hash(block.header)
    first_values = [block_hash(other.header), "c" * 64, "d" * 64][:junk]

    for voter in ("V7", "V8", "V9"):
        for value in first_values:
            fake = net.make_vote(voter, Phase.PRECOMMIT, 1, 0, value)
            net.deliver("V0", Message(Kind.VOTE, voter, 1, fake))
    for voter in ("V7", "V8", "V9", "V1", "V2", "V3", "V4"):
        vote = net.make_vote(voter, Phase.PRECOMMIT, 1, 0, digest)
        net.deliver("V0", Message(Kind.VOTE, voter, 1, vote))

    assert net.hashes("V0") == [digest]
    assert engine.height == 2
    certificate = net.finalized["V0"][0][1]
    assert sorted(vote.voter for vote in certificate.precommits) == [
        "V1",
        "V2",
        "V3",
        "V4",
        "V7",
        "V8",
        "V9",
    ]
    assert len(engine.votes.equivocations) == 3 * junk  # still detected and reported


def test_conflicting_proposal_that_gets_committed_is_still_finalized():
    """An equivocating proposer's second block reaches V0 first; the others commit the first one."""
    net = Network(4, drop=lambda src, dst, message: True)
    net.start(only=["V0"])
    engine = net.engines["V0"]
    proposer = proposer_for(net.ids, 1, 0)
    committed = net.make_block(proposer, 1, 0, sample_txs()[:1], parent_hash=GENESIS_HASH)
    other = net.make_block(proposer, 1, 0, sample_txs()[:2], parent_hash=GENESIS_HASH)

    net.deliver("V0", Message(Kind.BODY, proposer, 1, other))
    net.deliver("V0", Message(Kind.BODY, proposer, 1, committed))
    digest = block_hash(committed.header)
    for voter in ("V1", "V2", "V3"):
        vote = net.make_vote(voter, Phase.PRECOMMIT, 1, 0, digest)
        net.deliver("V0", Message(Kind.VOTE, voter, 1, vote))

    assert net.hashes("V0") == [digest]
    assert engine.height == 2
    assert "commit_waiting_for_block" not in net.event_names("V0")
