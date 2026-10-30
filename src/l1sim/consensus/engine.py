"""Tendermint-style consensus engine: rounds, steps, timeouts and proposer rotation.

The engine never touches the network, the clock or ``asyncio``. Everything that
leaves the engine goes through callbacks injected by ``node.py`` (or by the
test harness), which keeps the state machine deterministic and unit-testable.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum


class Step(str, Enum):
    """The three steps of a round, in the order a round goes through them.

    ``PROPOSE``: waiting for the round's proposer to send a block.
    ``PREVOTE``: this node has prevoted and waits for a PREVOTE quorum (polka).
    ``PRECOMMIT``: this node has precommitted and waits for a PRECOMMIT quorum.

    The value is also what the engine passes to ``schedule_timeout`` and
    receives back in ``on_timeout``, so a timeout knows which step it ends.
    """

    PROPOSE = "PROPOSE"
    PREVOTE = "PREVOTE"
    PRECOMMIT = "PRECOMMIT"


@dataclass(frozen=True)
class TimeoutConfig:
    """Timeout durations for each step, growing linearly with the round number.

    A step in round r waits ``base + r * delta_ms`` milliseconds, where base is
    ``propose_ms``, ``prevote_ms`` or ``precommit_ms``. Growing timeouts are what
    gives liveness under partial synchrony: before GST messages may be slower
    than any fixed timeout, but after GST message delay is bounded, so some
    round eventually has timeouts long enough for every honest message to
    arrive (Concept Guide, "Timeout"; test T6).

    The values always come from the scenario config (see :meth:`from_config`);
    the engine has no built-in defaults.

    Attributes:
        propose_ms: Base time to wait for the proposal before prevoting NIL.
        prevote_ms: Base time to wait for a polka before precommitting NIL.
        precommit_ms: Base time to wait for a commit before starting the next
            round.
        delta_ms: Extra time added to every step for each round after round 0.
    """

    propose_ms: int
    prevote_ms: int
    precommit_ms: int
    delta_ms: int

    def __post_init__(self) -> None:
        """Reject values that would make timeouts negative or non-integer.

        Raises:
            ValueError: If any field is not an ``int`` (``bool`` is refused too)
                or is negative. Floats are refused because they are not allowed
                in anything that may end up in canonical encoding or logs.
        """
        for name in ("propose_ms", "prevote_ms", "precommit_ms", "delta_ms"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")

    @classmethod
    def from_config(cls, consensus: Mapping[str, object]) -> TimeoutConfig:
        """Build the timeouts from the ``"consensus"`` block of a scenario config.

        Expected keys (all in milliseconds):
        ``timeout_propose_ms``, ``timeout_prevote_ms``, ``timeout_precommit_ms``
        and ``timeout_delta_ms``. Extra keys are ignored.

        Args:
            consensus: The ``"consensus"`` mapping, for example
                ``json.load(f)["consensus"]``.

        Returns:
            A validated :class:`TimeoutConfig`.

        Raises:
            KeyError: If a key is missing. There is deliberately no default, so a
                config that forgets ``timeout_delta_ms`` fails loudly instead of
                silently running with fixed timeouts.
            ValueError: If a value is not a non-negative integer.

        Example:
            >>> TimeoutConfig.from_config({"timeout_propose_ms": 1000,
            ...     "timeout_prevote_ms": 1000, "timeout_precommit_ms": 1000,
            ...     "timeout_delta_ms": 300}).duration(Step.PROPOSE, 2)
            1600
        """
        return cls(
            propose_ms=consensus["timeout_propose_ms"],
            prevote_ms=consensus["timeout_prevote_ms"],
            precommit_ms=consensus["timeout_precommit_ms"],
            delta_ms=consensus["timeout_delta_ms"],
        )

    def duration(self, step: Step, round: int) -> int:
        """Return how long ``step`` may last in ``round``, in milliseconds.

        Args:
            step: The step being timed.
            round: The round number (0 for the first round of a height).

        Returns:
            ``base(step) + round * delta_ms``.

        Raises:
            ValueError: If ``round`` is negative.
        """
        if type(round) is not int or round < 0:
            raise ValueError("round must be a non-negative integer")
        base = {
            Step.PROPOSE: self.propose_ms,
            Step.PREVOTE: self.prevote_ms,
            Step.PRECOMMIT: self.precommit_ms,
        }[Step(step)]
        return base + round * self.delta_ms


def proposer_for(validators: Sequence[str] | Mapping[str, object], height: int, round: int) -> str:
    """Return the validator allowed to propose a block at ``(height, round)``.

    The rule is ``sorted(validators)[(height + round) % n]``. Every node computes
    it locally from the fixed validator set, so all honest nodes agree on the
    proposer without exchanging messages. Sorting makes the result independent
    of the order in which validators appear in the config. Adding the round
    rotates the proposer when a round fails, so a silent proposer only costs one
    round (test T6).

    Args:
        validators: Validator ids, either as a sequence or as the keys of a
            mapping such as ``validator_keys``.
        height: Block height.
        round: Round number at that height.

    Returns:
        The id of the proposer.

    Raises:
        ValueError: If the validator set is empty.

    Example:
        >>> proposer_for(["V2", "V0", "V1", "V3"], height=1, round=0)
        'V1'
        >>> proposer_for(["V2", "V0", "V1", "V3"], height=1, round=1)
        'V2'
    """
    ordered = sorted(validators)
    if not ordered:
        raise ValueError("validator set must not be empty")
    return ordered[(height + round) % len(ordered)]
