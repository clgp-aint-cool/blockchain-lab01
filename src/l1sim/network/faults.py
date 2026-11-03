"""Network fault injection – deterministic drop/delay/duplicate."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from l1sim.config import NetworkConfig
from l1sim.encoding import message_id
from l1sim.models import Message


@dataclass(frozen=True, slots=True)
class Outcome:
    """Result of a fault-injection decision for one message on one link."""

    fate: str  # "drop" | "send" | "duplicate"
    delays_ms: tuple[int, ...]  # empty if drop, length 1 if send, length 2 if duplicate


class FaultInjector:
    """Pure hash-based fault injection layer.

    Placed between Gossip/node and TcpTransport.send. For every message
    dispatched to a neighbor, call decide() to get back a list of delay values:
      []        -> drop the message entirely
      [d]       -> send one copy after d ms
      [d1, d2]  -> send two copies (duplicate), after d1 and d2 ms

    All decisions are pure functions of (seed, src, dst, message_id(msg))
    so the same message on the same link always receives the same fate,
    regardless of call order. This is mandatory for reproducibility (T8).
    """

    def __init__(
        self,
        seed: int,
        fault_cfg: NetworkConfig,
        logger: Callable[..., Any] | None = None,
        timeout_base_ms: int = 1000,
        timeout_delta_ms: int = 300,
        max_round: int = 10,
    ) -> None:
        if type(seed) is not int:
            raise TypeError("seed must be an int")
        if not isinstance(fault_cfg, NetworkConfig):
            raise TypeError("fault_cfg must be a NetworkConfig instance")
        if type(timeout_base_ms) is not int or timeout_base_ms <= 0:
            raise ValueError("timeout_base_ms must be a positive integer")
        if type(timeout_delta_ms) is not int or timeout_delta_ms < 0:
            raise ValueError("timeout_delta_ms must be a non-negative integer")
        if type(max_round) is not int or max_round < 0:
            raise ValueError("max_round must be a non-negative integer")
        if logger is not None and not callable(logger):
            raise TypeError("logger must be callable")

        self._seed = seed
        self._cfg = fault_cfg
        self._logger = logger
        self._timeout_base_ms = timeout_base_ms
        self._timeout_delta_ms = timeout_delta_ms
        self._max_round = max_round
        # Ensure long delay exceeds 2x timeout even in higher rounds (avoiding forbidden zone)
        self._long_ms = 2 * (timeout_base_ms + max_round * timeout_delta_ms) + timeout_base_ms // 2

    @property
    def seed(self) -> int:
        return self._seed

    @property
    def config(self) -> NetworkConfig:
        return self._cfg

    @property
    def timeout_base_ms(self) -> int:
        return self._timeout_base_ms

    @property
    def timeout_delta_ms(self) -> int:
        return self._timeout_delta_ms

    @property
    def max_round(self) -> int:
        return self._max_round

    @property
    def long_delay_ms(self) -> int:
        return self._long_ms

    def _fingerprint(self, src: str, dst: str, msg: Message) -> bytes:
        mid = message_id(msg)
        key = f"{self._seed}|{src}|{dst}|{mid}".encode()
        return hashlib.sha256(key).digest()

    def decide_outcome(self, src: str, dst: str, msg: Message) -> Outcome:
        """Compute the full Outcome (fate and delays) deterministically."""
        if not isinstance(src, str) or not src:
            raise TypeError("src must be a non-empty string")
        if not isinstance(dst, str) or not dst:
            raise TypeError("dst must be a non-empty string")
        if not isinstance(msg, Message):
            raise TypeError("msg must be a Message instance")

        h = self._fingerprint(src, dst, msg)
        u = int.from_bytes(h[:8], "big") / (2**64)

        cfg = self._cfg
        height = msg.height
        before_gst = height < cfg.gst_height

        # Determine fate:
        # Pre-GST: drops occur according to drop_rate.
        # Post-GST (height >= gst_height): drops NEVER occur (guarantees liveness after GST).
        if before_gst and u < cfg.drop_rate:
            if self._logger:
                self._logger(
                    "fault_drop",
                    src=src,
                    dst=dst,
                    kind=msg.kind.value,
                    height=msg.height,
                )
            return Outcome(fate="drop", delays_ms=())

        is_duplicate = cfg.drop_rate <= u < (cfg.drop_rate + cfg.duplicate_rate)

        # Two-pole delay computation:
        # Short delay: pseudo-randomly in [1, 10% timeout_base_ms] based on hash,
        # ensuring reordering (T2) while strictly staying <= 10% timeout (outside forbidden zone).
        max_short_ms = max(1, int(self._timeout_base_ms * 0.10))
        dv1 = int.from_bytes(h[8:16], "big") / (2**64)
        short_ms_1 = min(max_short_ms, 1 + int(dv1 * max_short_ms))

        # Long delay (pre-GST only): >= 2x max timeout to avoid forbidden zone across rounds.
        sel1 = int.from_bytes(h[24:28], "big") / (2**32)
        if before_gst and sel1 >= 0.5:
            d1 = self._long_ms
        else:
            d1 = short_ms_1

        if is_duplicate:
            dv2 = int.from_bytes(h[16:24], "big") / (2**64)
            short_ms_2 = min(max_short_ms, 1 + int(dv2 * max_short_ms))
            sel2 = int.from_bytes(h[28:32], "big") / (2**32)
            if before_gst and sel2 >= 0.5:
                d2 = self._long_ms
            else:
                d2 = short_ms_2

            if self._logger:
                self._logger(
                    "fault_duplicate",
                    src=src,
                    dst=dst,
                    kind=msg.kind.value,
                    height=msg.height,
                )
            return Outcome(fate="duplicate", delays_ms=(d1, d2))

        return Outcome(fate="send", delays_ms=(d1,))

    def decide(self, src: str, dst: str, msg: Message) -> list[int]:
        """Return delay_ms list for each copy to send.

        Pure function of (seed, src, dst, message_id(msg), height).
        Returns:
            []: drop message
            [d]: send 1 copy after d ms
            [d1, d2]: send 2 copies (duplicate) after d1 and d2 ms
        """
        outcome = self.decide_outcome(src, dst, msg)
        return list(outcome.delays_ms)
