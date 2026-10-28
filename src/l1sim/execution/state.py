"""State representation and deterministic state transitions."""

from __future__ import annotations

from dataclasses import dataclass, field

from l1sim.encoding import hash_with_domain
from l1sim.models import Tx


@dataclass
class State:
    """Shared application state storing key-value records and sender nonces."""

    data: dict[str, str] = field(default_factory=dict)
    nonces: dict[str, int] = field(default_factory=dict)

    def copy(self) -> State:
        """Return an independent copy of this State."""
        return State(
            data=dict(self.data),
            nonces=dict(self.nonces),
        )

    def get(self, key: str, default: str | None = None) -> str | None:
        """Retrieve a value by key."""
        return self.data.get(key, default)

    def get_nonce(self, sender: str) -> int:
        """Return the next expected nonce for the given sender (defaults to 0)."""
        return self.nonces.get(sender, 0)

    def state_hash(self) -> str:
        """Compute the deterministic commitment hash representing current state."""
        payload = {
            "data": self.data,
            "nonces": self.nonces,
        }
        return hash_with_domain("STATE", payload)


def apply_tx(state: State, tx: Tx) -> State:
    """Apply a single transaction to a copy of the state and return the new State."""
    new_state = state.copy()
    new_state.data[tx.key] = tx.value
    new_state.nonces[tx.sender] = new_state.get_nonce(tx.sender) + 1
    return new_state


def apply_block(state: State, txs: tuple[Tx, ...] | list[Tx]) -> State:
    """Apply all transactions in a block sequentially to a copy of the state."""
    new_state = state.copy()
    for tx in txs:
        new_state.data[tx.key] = tx.value
        new_state.nonces[tx.sender] = new_state.get_nonce(tx.sender) + 1
    return new_state
