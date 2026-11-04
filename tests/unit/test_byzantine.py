import pytest

from l1sim.consensus.byzantine import Byzantine, parse_byzantine
from l1sim.consensus.engine import proposer_for
from l1sim.crypto import verify
from l1sim.encoding import block_hash
from l1sim.execution import State
from l1sim.models import GENESIS_HASH, Domain, Kind, Message, Phase
from tests.unit.consensus_harness import Network, sample_txs

IDS4 = ["V0", "V1", "V2", "V3"]
IDS10 = [f"V{i}" for i in range(10)]


# --------------------------------------------------------------------- parsing


def test_schema_examples_parse():
    rules = parse_byzantine(
        [
            {
                "node": "V2",
                "behavior": "silent",
                "messages": ["proposal"],
                "heights": [2],
                "rounds": [0],
            },
            {"node": "V7", "behavior": "equivocate"},
            {"node": "V5", "behavior": "wrong_domain", "domain": "HEADER", "from_height": 2},
        ],
        IDS10,
        5,
    )

    silent, equivocate, wrong = rules
    assert silent.messages == {"proposal"} and silent.heights == {2} and silent.rounds == {0}
    assert equivocate.messages == {"proposal", "prevote", "precommit"}
    assert equivocate.heights is None and equivocate.rounds is None
    assert wrong.domain is Domain.HEADER and wrong.heights == {2, 3, 4, 5}


def test_invalid_proposal_defaults_to_proposals_and_delay_keeps_its_delay():
    invalid, delay = parse_byzantine(
        [
            {"node": "V3", "behavior": "invalid_proposal"},
            {"node": "V3", "behavior": "delay", "delay_ms": 2500, "messages": ["prevote"]},
        ],
        IDS10,
        5,
    )

    assert invalid.messages == {"proposal"}
    assert delay.delay_ms == 2500


@pytest.mark.parametrize(
    "raw",
    [
        ["V2"],  # old form: says nothing about the behaviour
        {"node": "V2", "behavior": "silent"},  # not a list
        [{"node": "V2", "behavior": "silent", "colour": "red"}],
        [{"node": "V99", "behavior": "silent"}],
        [{"node": "V2", "behavior": "explode"}],
        [{"node": "V2", "behavior": "silent", "messages": []}],
        [{"node": "V2", "behavior": "silent", "messages": ["tx"]}],
        [{"node": "V2", "behavior": "silent", "messages": [{}]}],  # unhashable elements
        [{"node": "V2", "behavior": "silent", "messages": [["prevote"]]}],
        [{"node": "V2", "behavior": "silent", "heights": [0]}],
        [{"node": "V2", "behavior": "silent", "heights": [6]}],
        [{"node": "V2", "behavior": "silent", "heights": [True]}],
        [{"node": "V2", "behavior": "silent", "heights": [2], "from_height": 1}],
        [{"node": "V2", "behavior": "silent", "from_height": 4, "to_height": 2}],
        [{"node": "V2", "behavior": "silent", "rounds": [-1]}],
        [{"node": "V2", "behavior": "silent", "domain": "TX"}],
        [{"node": "V2", "behavior": "wrong_domain", "domain": "VOTE"}],
        [{"node": "V2", "behavior": "wrong_domain", "domain": []}],
        [{"node": "V2", "behavior": "invalid_proposal", "messages": ["prevote"]}],
        [{"node": "V2", "behavior": "delay"}],
        [{"node": "V2", "behavior": "delay", "delay_ms": 0}],
        [{"node": "V2", "behavior": "silent", "delay_ms": 100}],
    ],
)
def test_bad_rules_are_rejected(raw):
    with pytest.raises(ValueError):
        parse_byzantine(raw, IDS10, 5)


def test_at_most_f_distinct_nodes():
    two = [{"node": "V1", "behavior": "silent"}, {"node": "V2", "behavior": "silent"}]
    same_node_twice = [
        {"node": "V1", "behavior": "silent", "heights": [1]},
        {"node": "V1", "behavior": "equivocate", "heights": [2]},
    ]

    with pytest.raises(ValueError, match="at most f = 1"):
        parse_byzantine(two, IDS4, 5)
    assert len(parse_byzantine(same_node_twice, IDS4, 5)) == 2


# ------------------------------------------------------------------- outgoing


def _byzantine(net, node, raw):
    return Byzantine(node, parse_byzantine(raw, net.ids, 5), net.keys[node])


def _vote_message(net, voter, block_hash_value=None, phase=Phase.PREVOTE, height=1, round=0):
    vote = net.make_vote(voter, phase, height, round, block_hash_value)
    return Message(Kind.VOTE, voter, height, vote)


def _proposal_message(net, txs=()):
    proposer = proposer_for(net.ids, 1, 0)
    block = net.make_block(proposer, 1, 0, list(txs), parent_hash=GENESIS_HASH)
    return proposer, Message(Kind.BODY, proposer, 1, block)


def test_unmatched_messages_are_sent_unchanged():
    net = Network(4)
    byz = _byzantine(net, "V1", [{"node": "V1", "behavior": "silent", "heights": [2]}])
    message = _vote_message(net, "V1")

    assert byz.outgoing(message, State(), {}) == [(0, message)]


def test_silent_and_delay():
    net = Network(4)
    message = _vote_message(net, "V1")
    silent = _byzantine(net, "V1", [{"node": "V1", "behavior": "silent"}])
    delay = _byzantine(net, "V1", [{"node": "V1", "behavior": "delay", "delay_ms": 2500}])

    assert silent.outgoing(message, State(), {}) == []
    assert delay.outgoing(message, State(), {}) == [(2500, message)]


def test_bad_signature_and_wrong_domain_votes():
    net = Network(4)
    key = net.keys["V1"].public_key
    message = _vote_message(net, "V1")
    bad = _byzantine(net, "V1", [{"node": "V1", "behavior": "bad_signature"}])
    wrong = _byzantine(net, "V1", [{"node": "V1", "behavior": "wrong_domain"}])

    [(_, broken)] = bad.outgoing(message, State(), {})
    [(_, tx_signed)] = wrong.outgoing(message, State(), {})
    assert not verify(key, Domain.VOTE, broken.payload, broken.payload.signature)
    assert not verify(key, Domain.VOTE, tx_signed.payload, tx_signed.payload.signature)
    assert verify(key, Domain.TX, tx_signed.payload, tx_signed.payload.signature)


def test_invalid_proposal_is_signed_but_has_a_wrong_state_hash():
    net = Network(4)
    proposer, message = _proposal_message(net, sample_txs()[:1])
    byz = _byzantine(net, proposer, [{"node": proposer, "behavior": "invalid_proposal"}])

    [(_, invalid)] = byz.outgoing(message, State(), {})
    header = invalid.payload.header
    assert header.state_hash != message.payload.header.state_hash
    assert verify(net.keys[proposer].public_key, Domain.HEADER, header, header.signature)


def test_equivocating_proposal_adds_a_signed_empty_block():
    net = Network(4)
    proposer, message = _proposal_message(net, sample_txs()[:2])
    byz = _byzantine(net, proposer, [{"node": proposer, "behavior": "equivocate"}])

    [(_, first), (_, second)] = byz.outgoing(message, State(), {})
    assert first == message
    assert second.payload.txs == ()
    assert block_hash(second.payload.header) != block_hash(first.payload.header)
    header = second.payload.header
    assert verify(net.keys[proposer].public_key, Domain.HEADER, header, header.signature)


def test_equivocating_votes_conflict_and_are_signed():
    net = Network(4)
    proposer, proposal = _proposal_message(net)
    digest = block_hash(proposal.payload.header)
    proposals = {(1, 0): proposal.payload}
    byz = _byzantine(net, "V1", [{"node": "V1", "behavior": "equivocate"}])

    [(_, nil), (_, for_block)] = byz.outgoing(_vote_message(net, "V1"), State(), proposals)
    [(_, block_vote), (_, for_nil)] = byz.outgoing(
        _vote_message(net, "V1", digest), State(), proposals
    )
    assert (nil.payload.block_hash, for_block.payload.block_hash) == (None, digest)
    assert (block_vote.payload.block_hash, for_nil.payload.block_hash) == (digest, None)
    key = net.keys["V1"].public_key
    assert verify(key, Domain.VOTE, for_block.payload, for_block.payload.signature)


# ------------------------------------------------- in-process scenarios (engine)


def _honest(net):
    return [node for node in net.ids if node not in net.byzantine]


def _assert_honest_agree(net, height):
    assert net.run_until_height(height, nodes=_honest(net))
    chains = {tuple(net.hashes(node)[:height]) for node in _honest(net)}
    assert len(chains) == 1


def _commit_round(net, node, height):
    return net.finalized[node][height - 1][1].precommits[0].round


def test_t6_silent_proposer_costs_one_round():
    silent = proposer_for(IDS4, 2, 0)
    rule = {
        "node": silent,
        "behavior": "silent",
        "messages": ["proposal"],
        "heights": [2],
        "rounds": [0],
    }
    net = Network(4, txs=sample_txs(), byzantine=parse_byzantine([rule], IDS4, 3))
    net.start()

    _assert_honest_agree(net, 3)
    assert _commit_round(net, "V0", 2) == 1
    assert "byzantine_action" in net.event_names(silent)


def _t7_network():
    raw = [{"node": node, "behavior": "equivocate"} for node in ("V7", "V8", "V9")]
    net = Network(10, txs=sample_txs(), byzantine=parse_byzantine(raw, IDS10, 9))
    net.start()
    return net


def test_t7_f_equivocators_cannot_break_safety():
    net = _t7_network()

    # Heights 7, 8 and 9 have V7, V8 and V9 as round-0 proposers: proposals equivocate too.
    _assert_honest_agree(net, 9)
    for node in _honest(net):
        evidence = {pair[0].voter for pair in net.engines[node].votes.equivocations}
        assert evidence == {"V7", "V8", "V9"}, node
        assert "proposal_conflict" in net.event_names(node), node


def test_t7_is_reproducible():
    first, second = _t7_network(), _t7_network()
    assert first.run_until_height(9, nodes=_honest(first))
    assert second.run_until_height(9, nodes=_honest(second))

    assert first.events == second.events
    assert all(first.hashes(node) == second.hashes(node) for node in first.ids)


def test_invalid_proposal_is_rejected_and_the_next_round_finalizes():
    proposer = proposer_for(IDS4, 1, 0)
    rule = {"node": proposer, "behavior": "invalid_proposal", "heights": [1], "rounds": [0]}
    net = Network(4, txs=sample_txs(), byzantine=parse_byzantine([rule], IDS4, 3))
    net.start()

    _assert_honest_agree(net, 2)
    assert _commit_round(net, "V0", 1) == 1
    assert ("invalid_block", "bad_state_hash") in [
        (event, fields.get("reason")) for event, fields in net.events["V0"]
    ]


def test_proposal_delayed_past_the_timeout_acts_like_silence():
    proposer = proposer_for(IDS4, 1, 0)
    rule = {
        "node": proposer,
        "behavior": "delay",
        "delay_ms": 2500,  # >= 2x the 1000 ms propose timeout
        "messages": ["proposal"],
        "heights": [1],
        "rounds": [0],
    }
    net = Network(4, txs=sample_txs(), byzantine=parse_byzantine([rule], IDS4, 3))
    net.start()

    _assert_honest_agree(net, 2)
    assert _commit_round(net, "V0", 1) == 1


def test_t3_bad_and_wrong_domain_votes_are_rejected_with_a_reason():
    raw = [
        {"node": "V3", "behavior": "bad_signature", "messages": ["prevote"], "heights": [1]},
        {"node": "V3", "behavior": "wrong_domain", "messages": ["precommit"], "heights": [1]},
    ]
    net = Network(4, txs=sample_txs(), byzantine=parse_byzantine(raw, IDS4, 3))
    net.start()

    _assert_honest_agree(net, 2)
    reasons = {fields["reason"] for event, fields in net.events["V0"] if event == "vote_rejected"}
    assert reasons == {"bad_signature", "wrong_domain"}


def test_second_block_of_an_equivocating_proposal_can_be_delayed():
    net = Network(4)
    proposer, message = _proposal_message(net, sample_txs()[:2])
    vote = _vote_message(net, proposer)
    rules = parse_byzantine([{"node": proposer, "behavior": "equivocate"}], net.ids, 5)
    byz = Byzantine(proposer, rules, net.keys[proposer], conflict_delay_ms=2000)

    assert [delay for delay, _ in byz.outgoing(message, State(), {})] == [0, 2000]
    assert [delay for delay, _ in byz.outgoing(vote, State(), {})] == [0, 0]
