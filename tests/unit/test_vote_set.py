from dataclasses import replace

import pytest

from l1sim.consensus.vote_set import VoteSet, fault_tolerance, quorum_size
from l1sim.crypto import keypair_from_seed, sign
from l1sim.models import Domain, Phase, Vote

HASH_A = "a" * 64
HASH_B = "b" * 64


def _keys(n):
    return {f"V{i}": keypair_from_seed(7, f"V{i}") for i in range(n)}


def _vote(keys, voter, phase=Phase.PREVOTE, height=1, round=0, block_hash=HASH_A):
    vote = Vote(phase, height, round, block_hash, voter)
    return replace(vote, signature=sign(keys[voter].private_key, Domain.VOTE, vote))


def _vote_set(keys):
    return VoteSet({voter: pair.public_key for voter, pair in keys.items()})


@pytest.mark.parametrize(
    ("n", "f", "quorum"),
    [(1, 0, 1), (4, 1, 3), (7, 2, 5), (8, 2, 6), (10, 3, 7)],
)
def test_quorum_is_n_minus_f(n, f, quorum):
    assert fault_tolerance(n) == f
    assert quorum_size(n) == quorum


def test_empty_validator_set_is_rejected():
    with pytest.raises(ValueError):
        VoteSet({})


def test_duplicate_vote_is_counted_once():
    keys = _keys(4)
    votes = _vote_set(keys)
    vote = _vote(keys, "V0")

    assert votes.add(vote) == "added"
    assert votes.add(vote) == "duplicate"
    assert votes.count(1, 0, Phase.PREVOTE, HASH_A) == 1


def test_equivocation_keeps_first_vote_and_records_evidence():
    keys = _keys(4)
    votes = _vote_set(keys)
    first = _vote(keys, "V1", block_hash=HASH_A)
    second = _vote(keys, "V1", block_hash=HASH_B)

    assert votes.add(first) == "added"
    assert votes.add(second) == "equivocation"
    assert votes.add(second) == "equivocation"

    assert votes.count(1, 0, Phase.PREVOTE, HASH_A) == 1
    assert votes.count(1, 0, Phase.PREVOTE, HASH_B) == 0
    assert votes.equivocations == [(first, second)]


def test_block_versus_nil_is_also_equivocation():
    keys = _keys(4)
    votes = _vote_set(keys)

    assert votes.add(_vote(keys, "V2", block_hash=None)) == "added"
    assert votes.add(_vote(keys, "V2", block_hash=HASH_A)) == "equivocation"
    assert votes.count(1, 0, Phase.PREVOTE, None) == 1


def test_same_voter_may_vote_in_each_phase_and_round():
    keys = _keys(4)
    votes = _vote_set(keys)

    assert votes.add(_vote(keys, "V0", Phase.PREVOTE, round=0)) == "added"
    assert votes.add(_vote(keys, "V0", Phase.PRECOMMIT, round=0)) == "added"
    assert votes.add(_vote(keys, "V0", Phase.PREVOTE, round=1, block_hash=HASH_B)) == "added"
    assert votes.equivocations == []


@pytest.mark.parametrize("n", [4, 10])
def test_quorum_reached_at_exactly_2f_plus_1(n):
    keys = _keys(n)
    votes = _vote_set(keys)
    f = (n - 1) // 3

    for i in range(2 * f):
        votes.add(_vote(keys, f"V{i}"))
    assert votes.has_quorum(1, 0, Phase.PREVOTE) is None

    votes.add(_vote(keys, f"V{2 * f}"))
    assert votes.has_quorum(1, 0, Phase.PREVOTE) == HASH_A


def test_nil_is_its_own_value():
    keys = _keys(4)
    votes = _vote_set(keys)
    for voter in ("V0", "V1"):
        votes.add(_vote(keys, voter, block_hash=None))
    votes.add(_vote(keys, "V2", block_hash=HASH_A))

    assert votes.total(1, 0, Phase.PREVOTE) == 3
    assert votes.has_quorum(1, 0, Phase.PREVOTE) is None
    assert not votes.has_nil_quorum(1, 0, Phase.PREVOTE)

    votes.add(_vote(keys, "V3", block_hash=None))
    assert votes.has_nil_quorum(1, 0, Phase.PREVOTE)
    assert votes.has_quorum(1, 0, Phase.PREVOTE) is None


def test_bad_signature_is_rejected():
    keys = _keys(4)
    votes = _vote_set(keys)
    vote = _vote(keys, "V0")

    assert votes.add(replace(vote, block_hash=HASH_B)) == "invalid"
    assert votes.add(replace(vote, signature="")) == "invalid"
    assert votes.count(1, 0, Phase.PREVOTE, HASH_B) == 0


def test_vote_signed_by_another_validator_is_rejected():
    keys = _keys(4)
    votes = _vote_set(keys)
    vote = Vote(Phase.PREVOTE, 1, 0, HASH_A, "V0")
    forged = replace(vote, signature=sign(keys["V1"].private_key, Domain.VOTE, vote))

    assert votes.add(forged) == "invalid"


def test_vote_signed_with_wrong_domain_is_rejected():
    keys = _keys(4)
    votes = _vote_set(keys)
    vote = Vote(Phase.PREVOTE, 1, 0, HASH_A, "V0")
    wrong = replace(vote, signature=sign(keys["V0"].private_key, Domain.TX, vote))

    assert votes.add(wrong) == "invalid"


def test_unknown_voter_is_rejected():
    keys = _keys(5)
    votes = VoteSet({f"V{i}": keys[f"V{i}"].public_key for i in range(4)})

    assert votes.add(_vote(keys, "V4")) == "invalid"


def test_certificate_contains_sorted_precommits_for_one_block():
    keys = _keys(4)
    votes = _vote_set(keys)
    for voter in ("V3", "V1", "V0"):
        votes.add(_vote(keys, voter, Phase.PRECOMMIT, height=2, round=1))
    votes.add(_vote(keys, "V2", Phase.PRECOMMIT, height=2, round=1, block_hash=None))

    certificate = votes.certificate(2, 1, HASH_A)

    assert certificate.height == 2
    assert certificate.block_hash == HASH_A
    assert [vote.voter for vote in certificate.precommits] == ["V0", "V1", "V3"]


def test_certificate_requires_quorum():
    keys = _keys(4)
    votes = _vote_set(keys)
    for voter in ("V0", "V1"):
        votes.add(_vote(keys, voter, Phase.PRECOMMIT))

    with pytest.raises(ValueError, match="quorum"):
        votes.certificate(1, 0, HASH_A)


def test_votes_of_older_round_are_still_counted():
    keys = _keys(4)
    votes = _vote_set(keys)
    for voter in ("V0", "V1"):
        votes.add(_vote(keys, voter, Phase.PRECOMMIT, round=0))
    for voter in ("V0", "V1", "V2"):
        votes.add(_vote(keys, voter, Phase.PREVOTE, round=1, block_hash=None))

    assert votes.add(_vote(keys, "V2", Phase.PRECOMMIT, round=0)) == "added"
    assert votes.has_quorum(1, 0, Phase.PRECOMMIT) == HASH_A
    assert votes.rounds(1) == [0, 1]


def test_prune_below_keeps_current_and_future_heights():
    keys = _keys(4)
    votes = _vote_set(keys)
    for height in (1, 2, 3):
        votes.add(_vote(keys, "V0", height=height))
    votes.add(_vote(keys, "V1", height=1, block_hash=HASH_A))
    votes.add(_vote(keys, "V1", height=1, block_hash=HASH_B))

    votes.prune_below(2)

    assert votes.rounds(1) == []
    assert votes.count(2, 0, Phase.PREVOTE, HASH_A) == 1
    assert votes.count(3, 0, Phase.PREVOTE, HASH_A) == 1
    assert len(votes.equivocations) == 1
