"""Inbound rate limiting and peer blocking."""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from typing import Any

_DEFAULT_RATE = 1000  # max messages per second per peer (inbound)
_DEFAULT_BLOCK = 10.0  # seconds to block an overactive peer
_DEFAULT_OUTRATE = 1000  # max messages per second per peer (outbound)


class PeerLimits:
    """Inbound rate-limit and block/unblock, plus outbound rate-limit.

    Inbound:
        on_receive(peer_id) -> bool
            True  = process the message normally
            False = drop (peer is blocked or just got blocked)

    Outbound:
        allow_send(peer_id) -> bool
            True  = OK to send
            False = rate-limit reached; caller should queue or drop

    All state is in-memory; cleared on restart.
    """

    def __init__(
        self,
        rate_limit: int = _DEFAULT_RATE,
        block_duration: float = _DEFAULT_BLOCK,
        out_rate_limit: int = _DEFAULT_OUTRATE,
        logger: Callable[..., Any] | None = None,
        time_provider: Callable[[], float] | None = None,
    ) -> None:
        if type(rate_limit) is not int or rate_limit < 1:
            raise ValueError("rate_limit must be a positive integer")
        if (
            type(block_duration) not in (int, float)
            or isinstance(block_duration, bool)
            or block_duration <= 0
        ):
            raise ValueError("block_duration must be a positive number")
        if type(out_rate_limit) is not int or out_rate_limit < 1:
            raise ValueError("out_rate_limit must be a positive integer")
        if logger is not None and not callable(logger):
            raise TypeError("logger must be callable")
        if time_provider is not None and not callable(time_provider):
            raise TypeError("time_provider must be callable")

        self._rate = rate_limit
        self._block_dur = float(block_duration)
        self._out_rate = out_rate_limit
        self._logger = logger
        self._now = time_provider or time.monotonic

        # peer_id -> deque of timestamps (inbound)
        self._in_times: dict[str, deque[float]] = {}
        # peer_id -> timestamp when blocked (inbound)
        self._blocked: dict[str, float] = {}
        # peer_id -> deque of timestamps (outbound)
        self._out_times: dict[str, deque[float]] = {}

    @property
    def rate_limit(self) -> int:
        return self._rate

    @property
    def block_duration(self) -> float:
        return self._block_dur

    @property
    def out_rate_limit(self) -> int:
        return self._out_rate

    def on_receive(self, peer_id: str) -> bool:
        """Call for every inbound message from peer_id.

        Returns True if the message should be processed, False if dropped.
        """
        if not isinstance(peer_id, str) or not peer_id:
            raise TypeError("peer_id must be a non-empty string")

        now = self._now()

        # Check / expire block
        if peer_id in self._blocked:
            if now - self._blocked[peer_id] < self._block_dur:
                return False  # still blocked
            # Expired -> unblock
            del self._blocked[peer_id]
            if self._logger:
                self._logger("unblock", peer=peer_id)

        # Inbound sliding window
        window = self._in_times.setdefault(peer_id, deque())
        cutoff = now - 1.0
        while window and window[0] < cutoff:
            window.popleft()
        window.append(now)

        if len(window) > self._rate:
            self._blocked[peer_id] = now
            window.clear()
            if self._logger:
                self._logger("block", peer=peer_id)
            return False

        return True

    def allow_send(self, peer_id: str) -> bool:
        """Call before sending a message to peer_id.

        Returns True if within rate limit, False if outbound rate exceeded.
        """
        if not isinstance(peer_id, str) or not peer_id:
            raise TypeError("peer_id must be a non-empty string")

        now = self._now()
        window = self._out_times.setdefault(peer_id, deque())
        cutoff = now - 1.0
        while window and window[0] < cutoff:
            window.popleft()

        if len(window) >= self._out_rate:
            return False

        window.append(now)
        return True

    def is_blocked(self, peer_id: str) -> bool:
        """Check if peer is currently blocked (read-only query)."""
        if not isinstance(peer_id, str) or not peer_id:
            raise TypeError("peer_id must be a non-empty string")

        if peer_id not in self._blocked:
            return False

        now = self._now()
        return (now - self._blocked[peer_id]) < self._block_dur

    def unblock(self, peer_id: str) -> None:
        """Explicitly unblock a peer."""
        if not isinstance(peer_id, str) or not peer_id:
            raise TypeError("peer_id must be a non-empty string")

        if peer_id in self._blocked:
            del self._blocked[peer_id]
            if self._logger:
                self._logger("unblock", peer=peer_id)
