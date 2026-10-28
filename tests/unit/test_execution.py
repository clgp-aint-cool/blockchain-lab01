"""Unit tests for the execution layer: State, validation, and Mempool."""

import pytest

from l1sim.crypto import keypair_from_seed, sign
from l1sim.execution import (
    Mempool,
    State,
    apply_block,
    apply_tx,
    compute_tx_root,
    validate_block,
    validate_tx,
)
from l1sim.models import GENESIS_HASH, Block, BlockHeader, Domain, Tx


@pytest.fixture
def keys():
    seed = 12345
    return {
        "Alice": keypair_from_seed(seed, "Alice"),
        "Bob": keypair_from_seed(seed, "Bob"),
        "Charlie": keypair_from_seed(seed, "Charlie"),
        "V0": keypair_from_seed(seed, "V0"),
        "V1": keypair_from_seed(seed, "V1"),
    }


@pytest.fixture
def user_keys(keys):
    return {
        "Alice": keys["Alice"].public_key,
        "Bob": keys["Bob"].public_key,
        "Charlie": keys["Charlie"].public_key,
    }


@pytest.fixture
def validator_keys(keys):
    return {
        "V0": keys["V0"].public_key,
        "V1": keys["V1"].public_key,
    }


def make_signed_tx(keys, sender: str, key: str, value: str, nonce: int) -> Tx:
    unsigned = Tx(sender=sender, key=key, value=value, nonce=nonce)
    signature = sign(keys[sender].private_key, Domain.TX, unsigned)
    return Tx(sender=sender, key=key, value=value, nonce=nonce, signature=signature)


def make_signed_block(
    keys,
    height: int,
    round: int,
    parent_hash: str,
    proposer: str,
    txs: tuple[Tx, ...],
    pre_state: State,
) -> Block:
    post_state = apply_block(pre_state, txs)
    tx_root = compute_tx_root(txs)
    state_hash = post_state.state_hash()

    unsigned_header = BlockHeader(
        height=height,
        round=round,
        parent_hash=parent_hash,
        tx_root=tx_root,
        state_hash=state_hash,
        proposer=proposer,
    )
    sig = sign(keys[proposer].private_key, Domain.HEADER, unsigned_header)
    signed_header = BlockHeader(
        height=height,
        round=round,
        parent_hash=parent_hash,
        tx_root=tx_root,
        state_hash=state_hash,
        proposer=proposer,
        signature=sig,
    )
    return Block(header=signed_header, txs=txs)


# ==============================================================================
# State tests
# ==============================================================================


def test_state_basic_and_copy():
    s1 = State()
    assert s1.data == {}
    assert s1.nonces == {}
    assert s1.get_nonce("Alice") == 0
    assert s1.get("Alice/foo") is None

    s2 = s1.copy()
    s2.data["Alice/foo"] = "bar"
    s2.nonces["Alice"] = 1

    # s1 remains unchanged (independent)
    assert s1.data == {}
    assert s1.nonces == {}
    assert s2.get("Alice/foo") == "bar"
    assert s2.get_nonce("Alice") == 1


def test_state_hash_determinism():
    s1 = State(data={"a": "1", "b": "2"}, nonces={"Alice": 1})
    s2 = State(data={"b": "2", "a": "1"}, nonces={"Alice": 1})
    s3 = State(data={"a": "1", "b": "3"}, nonces={"Alice": 1})

    assert s1.state_hash() == s2.state_hash()
    assert s1.state_hash() != s3.state_hash()
    assert len(s1.state_hash()) == 64


def test_apply_tx_and_block():
    tx1 = Tx("Alice", "Alice/msg", "hello", 0)
    tx2 = Tx("Bob", "Bob/msg", "world", 0)
    tx3 = Tx("Alice", "Alice/msg", "hi again", 1)

    s0 = State()
    s1 = apply_tx(s0, tx1)

    assert s0.data == {}  # immutability
    assert s1.get("Alice/msg") == "hello"
    assert s1.get_nonce("Alice") == 1

    s_block = apply_block(s0, (tx1, tx2, tx3))
    assert s_block.get("Alice/msg") == "hi again"
    assert s_block.get("Bob/msg") == "world"
    assert s_block.get_nonce("Alice") == 2
    assert s_block.get_nonce("Bob") == 1


# ==============================================================================
# Validation tests
# ==============================================================================


def test_validate_tx_success(keys, user_keys):
    state = State()
    tx = make_signed_tx(keys, "Alice", "Alice/msg", "val1", 0)

    ok, reason = validate_tx(state, tx, user_keys)
    assert ok is True
    assert reason == ""


def test_validate_tx_bad_nonce(keys, user_keys):
    state = State()
    # Expected nonce is 0, but sending nonce=1 -- signature must pass first
    tx = make_signed_tx(keys, "Alice", "Alice/msg", "val1", 1)

    ok, reason = validate_tx(state, tx, user_keys)
    assert ok is False
    assert reason == "bad_nonce"


def test_validate_tx_ownership_enforced_at_model_construction():
    """models.py prevents an invalid owner at construction time."""
    with pytest.raises(ValueError, match="key must start with the sender"):
        Tx("Alice", "Bob/msg", "val1", 0)


def test_validate_tx_bad_signature_checked_before_nonce(keys, user_keys):
    """Signature is verified before nonce when user_keys is provided.

    Per CONCEPT_GUIDE: order is signature -> ownership -> nonce.
    Here the tx has a valid owner but bad signature AND wrong nonce;
    the result must be bad_signature (checked first), not bad_nonce.
    """
    state = State()
    # Nonce is wrong (1 instead of 0) AND signature is corrupt
    corrupt_sig = "0" * 128
    bad_tx = Tx("Alice", "Alice/msg", "val1", 1, signature=corrupt_sig)

    ok, reason = validate_tx(state, bad_tx, user_keys)
    assert ok is False
    assert reason == "bad_signature"  # Signature checked before nonce


def test_validate_tx_bad_signature(keys, user_keys):
    state = State()
    # Corrupt signature
    corrupt_sig = "0" * 128
    bad_tx = Tx("Alice", "Alice/msg", "val1", 0, signature=corrupt_sig)

    ok, reason = validate_tx(state, bad_tx, user_keys)
    assert ok is False
    assert reason == "bad_signature"


def test_validate_tx_wrong_domain(keys, user_keys):
    state = State()
    unsigned = Tx("Alice", "Alice/msg", "val1", 0)
    # Signed with Domain.HEADER instead of Domain.TX
    wrong_sig = sign(keys["Alice"].private_key, Domain.HEADER, unsigned)
    bad_tx = Tx("Alice", "Alice/msg", "val1", 0, signature=wrong_sig)

    ok, reason = validate_tx(state, bad_tx, user_keys)
    assert ok is False
    assert reason == "bad_signature"


def test_validate_tx_unknown_sender(keys, user_keys):
    state = State()
    # Unknown sender not in user_keys
    stranger = keypair_from_seed(999, "Stranger")
    unsigned = Tx("Stranger", "Stranger/msg", "hi", 0)
    sig = sign(stranger.private_key, Domain.TX, unsigned)
    bad_tx = Tx("Stranger", "Stranger/msg", "hi", 0, signature=sig)

    ok, reason = validate_tx(state, bad_tx, user_keys)
    assert ok is False
    assert reason == "bad_signature"


def test_validate_block_success(keys, validator_keys, user_keys):
    state = State()
    tx1 = make_signed_tx(keys, "Alice", "Alice/msg", "val1", 0)
    tx2 = make_signed_tx(keys, "Bob", "Bob/msg", "val2", 0)

    block = make_signed_block(
        keys,
        height=1,
        round=0,
        parent_hash=GENESIS_HASH,
        proposer="V0",
        txs=(tx1, tx2),
        pre_state=state,
    )

    ok, reason = validate_block(
        block=block,
        parent_hash=GENESIS_HASH,
        state=state,
        expected_proposer="V0",
        validator_keys=validator_keys,
        user_keys=user_keys,
    )
    assert ok is True
    assert reason == ""


def test_validate_block_bad_proposer(keys, validator_keys, user_keys):
    state = State()
    block = make_signed_block(
        keys,
        height=1,
        round=0,
        parent_hash=GENESIS_HASH,
        proposer="V0",
        txs=(),
        pre_state=state,
    )

    ok, reason = validate_block(
        block=block,
        parent_hash=GENESIS_HASH,
        state=state,
        expected_proposer="V1",  # Expected V1, but header has V0
        validator_keys=validator_keys,
        user_keys=user_keys,
    )
    assert ok is False
    assert reason == "bad_proposer"


def test_validate_block_bad_parent(keys, validator_keys, user_keys):
    state = State()
    block = make_signed_block(
        keys,
        height=1,
        round=0,
        parent_hash=GENESIS_HASH,
        proposer="V0",
        txs=(),
        pre_state=state,
    )

    wrong_parent = "f" * 64
    ok, reason = validate_block(
        block=block,
        parent_hash=wrong_parent,
        state=state,
        expected_proposer="V0",
        validator_keys=validator_keys,
        user_keys=user_keys,
    )
    assert ok is False
    assert reason == "bad_parent"


def test_validate_block_bad_signature(keys, validator_keys, user_keys):
    state = State()
    block = make_signed_block(
        keys,
        height=1,
        round=0,
        parent_hash=GENESIS_HASH,
        proposer="V0",
        txs=(),
        pre_state=state,
    )

    # Tamper header signature
    corrupt_sig = "1" * 128
    bad_header = BlockHeader(
        height=block.header.height,
        round=block.header.round,
        parent_hash=block.header.parent_hash,
        tx_root=block.header.tx_root,
        state_hash=block.header.state_hash,
        proposer=block.header.proposer,
        signature=corrupt_sig,
    )
    bad_block = Block(header=bad_header, txs=block.txs)

    ok, reason = validate_block(
        block=bad_block,
        parent_hash=GENESIS_HASH,
        state=state,
        expected_proposer="V0",
        validator_keys=validator_keys,
        user_keys=user_keys,
    )
    assert ok is False
    assert reason == "bad_signature"


def test_validate_block_bad_tx_root(keys, validator_keys, user_keys):
    state = State()
    tx1 = make_signed_tx(keys, "Alice", "Alice/msg", "val1", 0)
    block = make_signed_block(
        keys,
        height=1,
        round=0,
        parent_hash=GENESIS_HASH,
        proposer="V0",
        txs=(tx1,),
        pre_state=state,
    )

    # Change tx in body without changing tx_root in header
    tx2 = make_signed_tx(keys, "Bob", "Bob/msg", "val2", 0)
    tampered_block = Block(header=block.header, txs=(tx2,))

    ok, reason = validate_block(
        block=tampered_block,
        parent_hash=GENESIS_HASH,
        state=state,
        expected_proposer="V0",
        validator_keys=validator_keys,
        user_keys=user_keys,
    )
    assert ok is False
    assert reason == "bad_tx_root"


def test_validate_block_bad_state_hash(keys, validator_keys, user_keys):
    state = State()
    tx1 = make_signed_tx(keys, "Alice", "Alice/msg", "val1", 0)
    tx_root = compute_tx_root((tx1,))
    fake_state_hash = "c" * 64

    header = BlockHeader(
        height=1,
        round=0,
        parent_hash=GENESIS_HASH,
        tx_root=tx_root,
        state_hash=fake_state_hash,
        proposer="V0",
    )
    sig = sign(keys["V0"].private_key, Domain.HEADER, header)
    signed_header = BlockHeader(
        height=1,
        round=0,
        parent_hash=GENESIS_HASH,
        tx_root=tx_root,
        state_hash=fake_state_hash,
        proposer="V0",
        signature=sig,
    )
    block = Block(header=signed_header, txs=(tx1,))

    ok, reason = validate_block(
        block=block,
        parent_hash=GENESIS_HASH,
        state=state,
        expected_proposer="V0",
        validator_keys=validator_keys,
        user_keys=user_keys,
    )
    assert ok is False
    assert reason == "bad_state_hash"


# ==============================================================================
# Mempool tests
# ==============================================================================


def test_mempool_add_and_dedup():
    pool = Mempool()
    tx1 = Tx("Alice", "Alice/msg", "v1", 0)
    tx1_dup = Tx("Alice", "Alice/msg", "v1_different_val", 0)

    assert pool.add(tx1) is True
    assert len(pool) == 1
    assert tx1 in pool

    # Duplicate (sender, nonce) rejected
    assert pool.add(tx1_dup) is False
    assert len(pool) == 1


def test_mempool_take_deterministic_sorting():
    pool = Mempool()
    # Add txs in jumbled order
    tx_bob_1 = Tx("Bob", "Bob/msg", "b1", 1)
    tx_alice_1 = Tx("Alice", "Alice/msg", "a1", 1)
    tx_bob_0 = Tx("Bob", "Bob/msg", "b0", 0)
    tx_alice_0 = Tx("Alice", "Alice/msg", "a0", 0)

    for tx in [tx_bob_1, tx_alice_1, tx_bob_0, tx_alice_0]:
        pool.add(tx)

    state = State()
    # take should return sorted: Alice 0, Alice 1, Bob 0, Bob 1
    selected = pool.take(state, max_n=10)
    assert len(selected) == 4
    assert [tx.sender for tx in selected] == ["Alice", "Alice", "Bob", "Bob"]
    assert [tx.nonce for tx in selected] == [0, 1, 0, 1]


def test_mempool_take_respects_max_n():
    pool = Mempool()
    pool.add(Tx("Alice", "Alice/msg", "a0", 0))
    pool.add(Tx("Bob", "Bob/msg", "b0", 0))
    pool.add(Tx("Charlie", "Charlie/msg", "c0", 0))

    state = State()
    selected = pool.take(state, max_n=2)
    assert len(selected) == 2
    assert [tx.sender for tx in selected] == ["Alice", "Bob"]


def test_mempool_remove_committed_and_purge():
    pool = Mempool()
    tx_a0 = Tx("Alice", "Alice/msg", "a0", 0)
    tx_a1 = Tx("Alice", "Alice/msg", "a1", 1)
    tx_b0 = Tx("Bob", "Bob/msg", "b0", 0)

    pool.add(tx_a0)
    pool.add(tx_a1)
    pool.add(tx_b0)

    assert len(pool) == 3

    # Commit a0 and b0
    pool.remove_committed([tx_a0, tx_b0])
    assert len(pool) == 1
    assert tx_a1 in pool
    assert tx_a0 not in pool

    # Advance state to nonce 2 for Alice, purge stale
    state = State(nonces={"Alice": 2})
    pool.purge_stale(state)
    assert len(pool) == 0
