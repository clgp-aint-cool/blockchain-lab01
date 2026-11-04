"""Canonical JSON encoding and SHA-256 identifiers."""

import hashlib
import json
import re
from dataclasses import fields, is_dataclass
from enum import Enum
from typing import Any

from l1sim.models import (
    CHAIN_ID,
    Block,
    BlockHeader,
    BodyRequest,
    Hello,
    Kind,
    Message,
    Tx,
    Vote,
)

MAX_MESSAGE_BYTES = 1_000_000
_MAX_DEPTH = 32
_DOMAIN = re.compile(r"[A-Z0-9_]{1,32}")


class EncodingError(ValueError):
    """Raised when a value cannot be encoded deterministically."""


def _json_value(value: Any, depth: int = 0) -> Any:
    if depth > _MAX_DEPTH:
        raise EncodingError("value is nested too deeply")

    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _json_value(getattr(value, field.name), depth + 1)
            for field in fields(value)
        }

    if isinstance(value, Enum):
        return _json_value(value.value, depth + 1)

    if isinstance(value, bytes):
        return value.hex()

    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise EncodingError("JSON object keys must be strings")
        return {key: _json_value(item, depth + 1) for key, item in value.items()}

    if isinstance(value, (list, tuple)):
        return [_json_value(item, depth + 1) for item in value]

    if isinstance(value, float):
        raise EncodingError("float values are not supported")

    if value is None or isinstance(value, (str, int, bool)):
        return value

    raise EncodingError(f"unsupported value: {type(value).__name__}")


def encode(value: Any) -> bytes:
    """Encode a value as sorted, compact UTF-8 JSON; bytes are represented as hex."""
    try:
        text = json.dumps(
            _json_value(value),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )

        return text.encode("utf-8")
    except (TypeError, ValueError, RecursionError, UnicodeEncodeError) as exc:
        if isinstance(exc, EncodingError):
            raise
        raise EncodingError(f"cannot encode value: {exc}") from exc


def _build(cls: type, value: Any) -> Any:
    if not isinstance(value, dict) or set(value) != {field.name for field in fields(cls)}:
        raise ValueError(f"invalid {cls.__name__} fields")

    return cls(**value)


def _block(value: Any) -> Block:
    if not isinstance(value, dict) or set(value) != {"header", "txs"}:
        raise ValueError("invalid Block fields")

    if not isinstance(value["txs"], list):
        raise ValueError("Block txs must be a list")

    return Block(
        _build(BlockHeader, value["header"]),
        tuple(_build(Tx, tx) for tx in value["txs"]),
    )


_PAYLOAD_TYPES = {
    Kind.TX: Tx,
    Kind.HEADER: BlockHeader,
    Kind.BODY_REQUEST: BodyRequest,
    Kind.VOTE: Vote,
    Kind.HELLO: Hello,
}


def _payload(kind: Kind, value: Any) -> Any:
    if kind is Kind.BODY:
        return _block(value)

    return _build(_PAYLOAD_TYPES[kind], value)


def decode(data: bytes) -> Message:
    """Decode and validate a canonical network message."""
    if not isinstance(data, bytes):
        raise TypeError("data must be bytes")

    if len(data) > MAX_MESSAGE_BYTES:
        raise ValueError("message too large")

    try:
        value = json.loads(data.decode("utf-8"))

        if not isinstance(value, dict) or set(value) != {"kind", "origin", "height", "payload"}:
            raise ValueError("invalid Message fields")

        kind = Kind(value["kind"])
        message = Message(
            kind,
            value["origin"],
            value["height"],
            _payload(kind, value["payload"]),
        )
    except (ValueError, TypeError, KeyError, RecursionError) as exc:
        raise ValueError(f"invalid message: {exc}") from exc

    if encode(message) != data:
        raise ValueError("non-canonical message encoding")

    return message


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def domain_prefix(label: str) -> bytes:
    """Return a chain-specific prefix for signatures and hashes."""
    if not isinstance(label, str) or not _DOMAIN.fullmatch(label):
        raise ValueError("invalid domain")
    return f"{label}:{CHAIN_ID}".encode("ascii")


def hash_with_domain(label: str, value: Any) -> str:
    return sha256_hex(domain_prefix(label) + encode(value))


def message_id(message: Message) -> str:
    return hash_with_domain("MSG", message)


def block_hash(header: BlockHeader) -> str:
    unsigned = {
        field.name: getattr(header, field.name)
        for field in fields(BlockHeader)
        if field.name != "signature"
    }

    return hash_with_domain("BLOCK", unsigned)
