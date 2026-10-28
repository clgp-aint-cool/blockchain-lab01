"""Validation rules for transactions and blocks (Rule 3)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from l1sim.crypto import verify
from l1sim.encoding import hash_with_domain
from l1sim.execution.state import State, apply_tx
from l1sim.models import Block, Domain, Tx

if TYPE_CHECKING:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


def compute_tx_root(txs: tuple[Tx, ...] | list[Tx]) -> str:
    """Compute the deterministic commitment hash for an ordered sequence of transactions."""
    return hash_with_domain("TXS", list(txs))


def validate_tx(
    state: State,
    tx: Tx,
    user_keys: dict[str, Ed25519PublicKey] | None = None,
) -> tuple[bool, str]:
    """Validate a transaction against current state.

    Checks are performed in order: signature → ownership → nonce (per CONCEPT_GUIDE).

    Args:
        state: The current application state providing nonce baseline.
        tx: The transaction to validate.
        user_keys: Map of sender id to Ed25519PublicKey used to verify the signature.
            When None, signature verification is skipped — the caller (e.g. node.py)
            is responsible for verifying the signature before calling this function
            or passing the block to validate_block.

    Returns (True, "") if valid, or (False, reason) where reason is one of:
    - "bad_signature": signature is invalid or sender has no known key
    - "not_owner": key does not belong to sender (must start with f"{sender}/")
    - "bad_nonce": tx.nonce does not match expected nonce
    """
    # 1. Signature check (per CONCEPT_GUIDE order: sig -> ownership -> nonce)
    if user_keys is not None:
        if tx.sender not in user_keys:
            return False, "bad_signature"
        pk = user_keys[tx.sender]
        if not verify(pk, Domain.TX, tx, tx.signature):
            for other_domain in (Domain.HEADER, Domain.VOTE, Domain.HELLO):
                if verify(pk, other_domain, tx, tx.signature):
                    return False, "wrong_domain"
            return False, "bad_signature"

    # 2. Ownership check
    if not tx.key.startswith(f"{tx.sender}/"):
        return False, "not_owner"

    # 3. Nonce check
    expected_nonce = state.get_nonce(tx.sender)
    if tx.nonce != expected_nonce:
        return False, "bad_nonce"

    return True, ""


def validate_block(
    block: Block,
    parent_hash: str,
    state: State,
    expected_proposer: str,
    validator_keys: dict[str, Ed25519PublicKey] | None = None,
    user_keys: dict[str, Ed25519PublicKey] | None = None,
) -> tuple[bool, str]:
    """Validate a proposed block following Consensus Rule 3.

    Rule 3: Accepts and prevotes only for a block with a valid parent,
    valid transactions, correct state hash, correct proposer, and valid signature.

    Returns (True, "") if valid, or (False, reason) where reason is:
    - "bad_proposer": proposer does not match expected proposer
    - "bad_parent": parent_hash does not match expected parent hash
    - "bad_signature": header signature is invalid
    - "bad_tx_root": tx_root does not match hash of transactions
    - "bad_state_hash": state_hash does not match post-execution state hash
    - tx failure reasons: "not_owner", "bad_nonce", "bad_signature"
    """
    header = block.header

    if header.proposer != expected_proposer:
        return False, "bad_proposer"

    if header.parent_hash != parent_hash:
        return False, "bad_parent"

    if validator_keys is not None:
        if header.proposer not in validator_keys:
            return False, "bad_signature"
        proposer_pk = validator_keys[header.proposer]
        if not verify(proposer_pk, Domain.HEADER, header, header.signature):
            for other_domain in (Domain.TX, Domain.VOTE, Domain.HELLO):
                if verify(proposer_pk, other_domain, header, header.signature):
                    return False, "wrong_domain"
            return False, "bad_signature"

    if header.tx_root != compute_tx_root(block.txs):
        return False, "bad_tx_root"

    # Sequentially validate and apply transactions on a copy of state
    temp_state = state.copy()
    for tx in block.txs:
        ok, reason = validate_tx(temp_state, tx, user_keys)
        if not ok:
            return False, reason
        temp_state = apply_tx(temp_state, tx)

    if header.state_hash != temp_state.state_hash():
        return False, "bad_state_hash"

    return True, ""
