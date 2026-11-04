"""Shared immutable data types for the lab blockchain."""

import re
from dataclasses import dataclass
from enum import Enum

CHAIN_ID = "lab01"
GENESIS_HASH = "0" * 64

HASH_RE = re.compile(r"[0-9a-f]{64}")
SIG_RE = re.compile(r"[0-9a-f]{128}")
_IDENTIFIER = re.compile(r"[A-Za-z0-9_.:-]{1,128}")


class Phase(str, Enum):
    PREVOTE = "PREVOTE"
    PRECOMMIT = "PRECOMMIT"


class Kind(str, Enum):
    TX = "TX"
    HEADER = "HEADER"
    BODY_REQUEST = "BODY_REQUEST"
    BODY = "BODY"
    VOTE = "VOTE"
    HELLO = "HELLO"


class Domain(str, Enum):
    TX = "TX"
    HEADER = "HEADER"
    VOTE = "VOTE"
    HELLO = "HELLO"


def _check_height(name: str, value: int) -> None:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")


def _check_identifier(name: str, value: str) -> None:
    if type(value) is not str or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{name} is not a valid identifier")


def _check_hash(name: str, value: str) -> None:
    if type(value) is not str or not HASH_RE.fullmatch(value):
        raise ValueError(f"{name} must be a 64-character lowercase hex hash")


def _check_signature(value: str) -> None:
    if type(value) is not str or (value and not SIG_RE.fullmatch(value)):
        raise ValueError("signature must be empty or 128 lowercase hex characters")


def _check_text(name: str, value: str, *, allow_empty: bool = True) -> None:
    if type(value) is not str or (not allow_empty and not value):
        raise ValueError(f"{name} must be {'a non-empty ' if not allow_empty else 'a '}string")

    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError(f"{name} is not valid Unicode") from None


def _tuple_of(name: str, values: tuple, item_type: type) -> tuple:
    if not isinstance(values, (list, tuple)) or any(
        not isinstance(value, item_type) for value in values
    ):
        raise ValueError(f"{name} must contain only {item_type.__name__} values")

    return tuple(values)


@dataclass(frozen=True, slots=True)
class Tx:
    sender: str
    key: str
    value: str
    nonce: int
    signature: str = ""

    def __post_init__(self) -> None:
        _check_identifier("sender", self.sender)
        _check_text("key", self.key, allow_empty=False)

        if not self.key.startswith(f"{self.sender}/"):
            raise ValueError("key must start with the sender followed by '/'")

        _check_text("value", self.value)
        _check_height("nonce", self.nonce)
        _check_signature(self.signature)


@dataclass(frozen=True, slots=True)
class BlockHeader:
    height: int
    round: int
    parent_hash: str
    tx_root: str
    state_hash: str
    proposer: str
    signature: str = ""

    def __post_init__(self) -> None:
        _check_height("height", self.height)
        _check_height("round", self.round)
        _check_hash("parent_hash", self.parent_hash)
        _check_hash("tx_root", self.tx_root)
        _check_hash("state_hash", self.state_hash)
        _check_identifier("proposer", self.proposer)
        _check_signature(self.signature)


@dataclass(frozen=True, slots=True)
class Block:
    header: BlockHeader
    txs: tuple[Tx, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.header, BlockHeader):
            raise ValueError("header must be a BlockHeader")

        object.__setattr__(self, "txs", _tuple_of("txs", self.txs, Tx))


@dataclass(frozen=True, slots=True)
class Vote:
    phase: Phase
    height: int
    round: int
    block_hash: str | None
    voter: str
    signature: str = ""

    def __post_init__(self) -> None:
        try:
            object.__setattr__(self, "phase", Phase(self.phase))
        except ValueError as exc:
            raise ValueError("phase must be PREVOTE or PRECOMMIT") from exc

        _check_height("height", self.height)
        _check_height("round", self.round)

        if self.block_hash is not None:
            _check_hash("block_hash", self.block_hash)

        _check_identifier("voter", self.voter)
        _check_signature(self.signature)


@dataclass(frozen=True, slots=True)
class CommitCertificate:
    height: int
    block_hash: str
    precommits: tuple[Vote, ...]

    def __post_init__(self) -> None:
        _check_height("height", self.height)
        _check_hash("block_hash", self.block_hash)
        votes = _tuple_of("precommits", self.precommits, Vote)

        if not votes:
            raise ValueError("certificate needs precommits")

        if any(
            vote.phase is not Phase.PRECOMMIT
            or vote.height != self.height
            or vote.block_hash != self.block_hash
            for vote in votes
        ):
            raise ValueError("certificate contains a mismatched precommit")

        if len({vote.voter for vote in votes}) != len(votes):
            raise ValueError("certificate contains duplicate voters")

        if len({vote.round for vote in votes}) != 1:
            raise ValueError("precommits must be from the same round")

        object.__setattr__(self, "precommits", votes)


@dataclass(frozen=True, slots=True)
class BodyRequest:
    block_hash: str

    def __post_init__(self) -> None:
        _check_hash("block_hash", self.block_hash)


@dataclass(frozen=True, slots=True)
class Hello:
    node_id: str
    advertise_addr: str
    chain_id: str
    finalized_height: int
    signature: str = ""

    def __post_init__(self) -> None:
        _check_identifier("node_id", self.node_id)
        _check_text("advertise_addr", self.advertise_addr, allow_empty=False)

        if self.chain_id != CHAIN_ID:
            raise ValueError(f"chain_id must be {CHAIN_ID!r}")

        _check_height("finalized_height", self.finalized_height)
        _check_signature(self.signature)


_PAYLOAD_TYPES = {
    Kind.TX: Tx,
    Kind.HEADER: BlockHeader,
    Kind.BODY_REQUEST: BodyRequest,
    Kind.BODY: Block,
    Kind.VOTE: Vote,
    Kind.HELLO: Hello,
}


@dataclass(frozen=True, slots=True)
class Message:
    kind: Kind
    origin: str
    height: int
    payload: Tx | BlockHeader | BodyRequest | Block | Vote | Hello

    def __post_init__(self) -> None:
        try:
            object.__setattr__(self, "kind", Kind(self.kind))
        except ValueError as exc:
            raise ValueError("unknown message kind") from exc

        _check_identifier("origin", self.origin)
        _check_height("height", self.height)
        if not isinstance(self.payload, _PAYLOAD_TYPES[self.kind]):
            raise ValueError(f"payload does not match message kind {self.kind.value}")

        if self.kind is Kind.HELLO and self.payload.node_id != self.origin:
            raise ValueError("HELLO node_id must match message origin")

        payload_height = None
        if self.kind is Kind.HEADER:
            payload_height = self.payload.height

        elif self.kind is Kind.VOTE:
            payload_height = self.payload.height

        elif self.kind is Kind.BODY:
            payload_height = self.payload.header.height

        elif self.kind is Kind.HELLO:
            payload_height = self.payload.finalized_height

        if payload_height is not None and self.height != payload_height:
            raise ValueError("message height does not match payload height")
