import pytest

from l1sim.encoding import (
    EncodingError,
    block_hash,
    decode,
    domain_prefix,
    encode,
    message_id,
    sha256_hex,
)
from l1sim.models import (
    CHAIN_ID,
    GENESIS_HASH,
    Block,
    BlockHeader,
    BodyRequest,
    Hello,
    Kind,
    Message,
    Phase,
    Tx,
    Vote,
)

HASH = "a" * 64


def _block(height: int) -> Block:
    return Block(
        BlockHeader(height, 0, GENESIS_HASH, HASH, HASH, "V0"),
        (Tx("Alice", "Alice/name", "Ada", 0),),
    )


def test_encoding_sorts_mapping_keys_and_round_trips_message():
    assert encode({"b": 2, "a": 1}) == encode({"a": 1, "b": 2})

    message = Message(
        kind="TX",
        origin="V0",
        height=1,
        payload=Tx("Alice", "Alice/name", "Ada", 0),
    )
    assert decode(encode(message)) == message


def test_encoding_round_trips_typed_block_message():
    message = Message(
        kind=Kind.BODY,
        origin="V0",
        height=2,
        payload=_block(2),
    )
    assert decode(encode(message)) == message


def test_encoding_rejects_noncanonical_values():
    with pytest.raises(EncodingError):
        encode({"value": 1.5})
    with pytest.raises(EncodingError):
        encode({1: "not a string key"})
    with pytest.raises(ValueError):
        decode(b"not-json")
    valid_message = encode(Message(Kind.TX, "V0", 1, Tx("Alice", "Alice/name", "Ada", 0)))
    with pytest.raises(ValueError, match="non-canonical"):
        decode(valid_message.replace(b',"origin"', b', "origin"', 1))
    duplicate_height = valid_message.replace(b',"kind"', b',"height":1,"kind"', 1)
    with pytest.raises(ValueError, match="non-canonical"):
        decode(duplicate_height)


def test_identifiers_are_sha256_and_block_hash_ignores_signature():
    header = BlockHeader(1, 0, GENESIS_HASH, HASH, HASH, "V0")
    signed_header = BlockHeader(1, 0, GENESIS_HASH, HASH, HASH, "V0", "ab" * 64)
    message = Message(Kind.VOTE, "V0", 1, Vote(Phase.PREVOTE, 1, 0, None, "V0"))

    assert sha256_hex(b"data") == "3a6eb0790f39ac87c94f3856b2dd2c5d110e6811602261a9a923d3bb23adc8b7"
    assert message_id(message) == message_id(message)
    assert message_id(message) != message_id(
        Message(Kind.VOTE, "V0", 2, Vote(Phase.PREVOTE, 2, 0, None, "V0"))
    )
    assert block_hash(header) == block_hash(signed_header)
    assert domain_prefix("TX") == b"TX:lab01"


def test_all_supported_message_kinds_round_trip():
    block = _block(2)
    block_hash_value = block_hash(block.header)
    vote = Vote(Phase.PRECOMMIT, 2, 0, block_hash_value, "V0")
    payloads = {
        Kind.TX: Tx("Alice", "Alice/name", "Ada", 0),
        Kind.HEADER: block.header,
        Kind.BODY_REQUEST: BodyRequest(block_hash_value),
        Kind.BODY: block,
        Kind.VOTE: vote,
        Kind.HELLO: Hello("V0", "127.0.0.1:9000", CHAIN_ID, 1),
    }

    for kind, payload in payloads.items():
        height = payload.finalized_height if kind is Kind.HELLO else 2
        message = Message(kind, "V0", height, payload)
        assert decode(encode(message)) == message


def test_message_rejects_mismatched_payload_and_hello_identity():
    with pytest.raises(ValueError, match="payload does not match"):
        Message(Kind.BODY_REQUEST, "V0", 2, _block(2))
    with pytest.raises(ValueError, match="HELLO node_id"):
        Message(Kind.HELLO, "V1", 0, Hello("V0", "127.0.0.1:9000", CHAIN_ID, 0))


def test_transaction_enforces_sender_key_ownership():
    with pytest.raises(ValueError, match="start with the sender"):
        Tx("Alice", "Bob/name", "Ada", 0)


@pytest.mark.parametrize(
    ("kind", "payload"),
    [
        (Kind.HEADER, _block(2).header),
        (Kind.BODY, _block(2)),
        (Kind.VOTE, Vote(Phase.PREVOTE, 2, 0, None, "V0")),
        (Kind.HELLO, Hello("V0", "127.0.0.1:9000", CHAIN_ID, 2)),
    ],
)
def test_message_height_must_match_height_bearing_payload(kind, payload):
    with pytest.raises(ValueError, match="message height"):
        Message(kind, "V0", 3, payload)
