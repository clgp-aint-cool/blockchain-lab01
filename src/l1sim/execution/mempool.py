"""Transaction mempool with deduplication and deterministic sorting."""

from __future__ import annotations

from collections.abc import Iterable

from l1sim.execution.state import State, apply_tx
from l1sim.execution.validation import validate_tx
from l1sim.models import Tx


class Mempool:
    """Stores pending valid transactions awaiting block inclusion."""

    def __init__(self) -> None:
        # Map (sender, nonce) -> Tx to ensure at most one tx per (sender, nonce)
        self._txs: dict[tuple[str, int], Tx] = {}

    def add(self, tx: Tx) -> bool:
        """Add a transaction to the mempool.

        Note: Signature verification is NOT performed here. The caller (node.py)
        is responsible for verifying the signature via validate_tx(user_keys=...)
        before adding to the mempool. This design keeps the mempool dependency-free
        from cryptographic keys.

        Returns True if added, False if a transaction with the same (sender, nonce) exists.
        """
        key = (tx.sender, tx.nonce)
        if key in self._txs:
            return False
        self._txs[key] = tx
        return True

    def take(self, state: State, max_n: int) -> list[Tx]:
        """Select up to max_n valid transactions, sorted deterministically by (sender, nonce)."""
        if max_n <= 0:
            return []

        # Deterministic sorting by (sender, nonce)
        sorted_keys = sorted(self._txs.keys())
        selected: list[Tx] = []
        temp_state = state.copy()

        for key in sorted_keys:
            if len(selected) >= max_n:
                break
            tx = self._txs[key]
            ok, _ = validate_tx(temp_state, tx)
            if ok:
                selected.append(tx)
                temp_state = apply_tx(temp_state, tx)

        return selected

    def remove_committed(self, txs: Iterable[Tx]) -> None:
        """Remove committed transactions from the mempool."""
        for tx in txs:
            self._txs.pop((tx.sender, tx.nonce), None)

    def purge_stale(self, state: State) -> None:
        """Remove transactions with nonces strictly lower than current state nonces."""
        stale_keys = [
            (sender, nonce) for (sender, nonce) in self._txs if nonce < state.get_nonce(sender)
        ]
        for key in stale_keys:
            self._txs.pop(key, None)

    def __len__(self) -> int:
        return len(self._txs)

    def __contains__(self, tx: Tx) -> bool:
        return (tx.sender, tx.nonce) in self._txs
