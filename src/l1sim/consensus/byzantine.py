"""Byzantine behaviours for the scenarios of T3, T6 and T7.

The scenario format is described in ``docs/BYZANTINE_SCHEMA.md``. A Byzantine
node runs the normal, honest :class:`~l1sim.consensus.engine.ConsensusEngine`;
only what it *sends* is changed. :meth:`Byzantine.outgoing` sits between the
engine's ``broadcast`` callback and the network and turns one outgoing
proposal or vote into zero, one or two ``(delay_ms, message)`` pairs.

Nothing here is random: every faulty message is derived from the honest one
(a flipped hex digit, a re-signature, an empty block), so a scenario with
Byzantine nodes still gives the same results on every run (T8).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace

from l1sim.consensus.vote_set import fault_tolerance
from l1sim.crypto import KeyPair, sign
from l1sim.encoding import block_hash, hash_with_domain
from l1sim.execution import State, apply_block, compute_tx_root
from l1sim.models import Block, BlockHeader, Domain, Kind, Message

BEHAVIORS = (
    "silent",
    "equivocate",
    "bad_signature",
    "wrong_domain",
    "invalid_proposal",
    "delay",
)
MESSAGES = ("proposal", "prevote", "precommit")
WRONG_DOMAINS = {"TX": Domain.TX, "HEADER": Domain.HEADER, "HELLO": Domain.HELLO}

_FIELDS = {
    "node",
    "behavior",
    "messages",
    "heights",
    "from_height",
    "to_height",
    "rounds",
    "domain",
    "delay_ms",
}

Outgoing = list[tuple[int, Message]]


@dataclass(frozen=True)
class ByzantineRule:
    """One validated entry of the scenario's ``byzantine`` list.

    Attributes:
        node: The misbehaving validator.
        behavior: One of :data:`BEHAVIORS`.
        messages: Message kinds the rule applies to, from :data:`MESSAGES`.
        heights: Heights it applies to; ``None`` means every height.
        rounds: Rounds it applies to; ``None`` means every round.
        domain: Signing domain used by ``wrong_domain``.
        delay_ms: Delay used by ``delay``.
    """

    node: str
    behavior: str
    messages: frozenset[str]
    heights: frozenset[int] | None = None
    rounds: frozenset[int] | None = None
    domain: Domain = Domain.TX
    delay_ms: int = 0

    def matches(self, kind: str, height: int, round: int) -> bool:
        """Return whether the rule applies to an outgoing message.

        Args:
            kind: ``"proposal"``, ``"prevote"`` or ``"precommit"``.
            height: Height of the message.
            round: Round of the message (for a proposal, the header's round).

        Returns:
            ``True`` if kind, height and round all match.
        """
        return (
            kind in self.messages
            and (self.heights is None or height in self.heights)
            and (self.rounds is None or round in self.rounds)
        )


def parse_byzantine(
    raw: object, validators: Sequence[str], max_height: int
) -> tuple[ByzantineRule, ...]:
    """Validate the scenario's ``byzantine`` list and build the rules.

    Checks (``docs/BYZANTINE_SCHEMA.md``, "Kiểm khi đọc config"): every entry
    is an object with known fields; ``node`` is a validator; at most f
    distinct nodes; ``behavior`` and ``messages`` are known values; heights
    lie in ``1..max_height`` and ``heights`` is not mixed with
    ``from_height``/``to_height``; ``domain`` only with ``wrong_domain`` and
    never ``VOTE``; ``invalid_proposal`` only for proposals; ``delay_ms``
    required with ``delay`` and only there. The old form ``["V2"]`` is
    rejected because it does not say what the node does.

    Args:
        raw: The ``byzantine`` value from the scenario JSON.
        validators: The scenario's validator ids.
        max_height: The scenario's ``max_height``.

    Returns:
        The rules, in the order of the scenario.

    Raises:
        ValueError: With the index of the bad entry, if any check fails.
    """
    if not isinstance(raw, list):
        raise ValueError("byzantine must be a list of rule objects")
    rules = tuple(
        _parse_rule(entry, index, validators, max_height) for index, entry in enumerate(raw)
    )
    nodes = {rule.node for rule in rules}
    f = fault_tolerance(len(validators))
    if len(nodes) > f:
        raise ValueError(f"byzantine has {len(nodes)} distinct nodes; at most f = {f} are allowed")
    return rules


def _parse_rule(
    entry: object, index: int, validators: Sequence[str], max_height: int
) -> ByzantineRule:
    """Validate one ``byzantine`` entry; see :func:`parse_byzantine`."""
    where = f"byzantine[{index}]"
    if not isinstance(entry, dict):
        raise ValueError(
            f"{where} must be an object such as {{'node': 'V2', 'behavior': 'silent'}}"
        )
    unknown = sorted(set(entry) - _FIELDS)
    if unknown:
        raise ValueError(f"{where} has unknown fields: {', '.join(unknown)}")

    node = entry.get("node")
    if node not in validators:
        raise ValueError(f"{where}.node must be a validator id")
    behavior = entry.get("behavior")
    if behavior not in BEHAVIORS:
        raise ValueError(f"{where}.behavior must be one of {', '.join(BEHAVIORS)}")

    default = ["proposal"] if behavior == "invalid_proposal" else list(MESSAGES)
    messages = entry.get("messages", default)
    if (
        not isinstance(messages, list)
        or not messages
        # Strings first: set() raises TypeError on a list or dict element.
        or not all(isinstance(kind, str) for kind in messages)
        or len(set(messages)) != len(messages)
        or any(kind not in MESSAGES for kind in messages)
    ):
        raise ValueError(f"{where}.messages must be distinct values of {', '.join(MESSAGES)}")
    if behavior == "invalid_proposal" and messages != ["proposal"]:
        raise ValueError(f"{where}: invalid_proposal only applies to proposal")

    heights = _parse_heights(entry, where, max_height)
    rounds = entry.get("rounds")
    if rounds is not None:
        if not isinstance(rounds, list) or not rounds or not all(_is_int(r, 0) for r in rounds):
            raise ValueError(f"{where}.rounds must be a non-empty list of rounds >= 0")
        rounds = frozenset(rounds)

    domain = Domain.TX
    if "domain" in entry:
        if behavior != "wrong_domain":
            raise ValueError(f"{where}.domain is only used with wrong_domain")
        if not isinstance(entry["domain"], str) or entry["domain"] not in WRONG_DOMAINS:
            raise ValueError(f"{where}.domain must be one of {', '.join(WRONG_DOMAINS)}")
        domain = WRONG_DOMAINS[entry["domain"]]

    delay_ms = entry.get("delay_ms", 0)
    if behavior == "delay":
        if not _is_int(delay_ms, 1):
            raise ValueError(f"{where}.delay_ms must be a positive integer")
    elif "delay_ms" in entry:
        raise ValueError(f"{where}.delay_ms is only used with delay")

    return ByzantineRule(
        node=node,
        behavior=behavior,
        messages=frozenset(messages),
        heights=heights,
        rounds=rounds,
        domain=domain,
        delay_ms=delay_ms,
    )


def _parse_heights(entry: dict, where: str, max_height: int) -> frozenset[int] | None:
    """Return the heights of a rule from ``heights`` or ``from_height``/``to_height``."""
    has_range = "from_height" in entry or "to_height" in entry
    if "heights" in entry:
        if has_range:
            raise ValueError(f"{where}: use heights or from_height/to_height, not both")
        heights = entry["heights"]
        if not isinstance(heights, list) or not heights:
            raise ValueError(f"{where}.heights must be a non-empty list")
        if not all(_is_int(h, 1) and h <= max_height for h in heights):
            raise ValueError(f"{where}.heights must lie in 1..{max_height}")
        return frozenset(heights)
    if not has_range:
        return None
    low = entry.get("from_height", 1)
    high = entry.get("to_height", max_height)
    if not (_is_int(low, 1) and _is_int(high, 1) and low <= high <= max_height):
        raise ValueError(f"{where}: need 1 <= from_height <= to_height <= {max_height}")
    return frozenset(range(low, high + 1))


def _is_int(value: object, minimum: int) -> bool:
    """``bool`` is refused on purpose: ``True`` must not pass as height 1."""
    return type(value) is int and value >= minimum


class Byzantine:
    """Misbehaviour of one node, applied to the messages its engine sends.

    Only the rules whose ``node`` is this node are kept. For each outgoing
    message the first matching rule, in scenario order, decides what happens;
    a message that matches no rule is sent unchanged, so outside its rules the
    node behaves honestly. Every applied rule is logged as ``byzantine_action``
    so the report can show the attack really happened.

    Example:
        >>> byz = Byzantine("V2", parse_byzantine(raw, validators, 5), keypair, logger)
        >>> for delay_ms, message in byz.outgoing(message, engine.state, engine.proposals):
        ...     send_later(delay_ms, message)
    """

    def __init__(
        self,
        node_id: str,
        rules: Sequence[ByzantineRule],
        keypair: KeyPair,
        logger: Callable[..., None] | None = None,
        conflict_delay_ms: int = 0,
    ) -> None:
        """Keep the rules for ``node_id`` and the key used to re-sign messages.

        Args:
            node_id: This node's id.
            rules: Rules from :func:`parse_byzantine`; rules of other nodes are
                ignored, so the whole scenario list can be passed.
            keypair: This node's key pair (a Byzantine node signs with its own
                key; it cannot forge other validators' signatures).
            logger: ``logger(event, **fields)``, same convention as the engine.
            conflict_delay_ms: Delay of the second block of an equivocating
                proposal. On a real network, which of two proposals a node
                receives first depends on gossip timing, so prevotes could split
                differently on every run. ``node.py`` passes 2x the longest step
                timeout ("delay hai cực") so every honest node sees the real
                block first and results stay reproducible (T8). Conflicting
                votes need no delay: the counted vote is always the first one
                on the connection, and the second is never forwarded.
        """
        self.node_id = node_id
        self.rules = tuple(rule for rule in rules if rule.node == node_id)
        self._keypair = keypair
        self._logger = logger or (lambda event, **fields: None)
        self._conflict_delay_ms = conflict_delay_ms

    def outgoing(
        self, message: Message, state: State, proposals: Mapping[tuple[int, int], Block]
    ) -> Outgoing:
        """Turn one message the engine sends into what the network actually gets.

        Args:
            message: A message passed to the engine's ``broadcast``. Proposals
                (``BODY``) and votes are affected; anything else is returned
                unchanged.
            state: The engine's state, i.e. the parent state of the block being
                proposed (``engine.state``). Used to build the empty block of an
                equivocating proposal.
            proposals: The engine's proposals by ``(height, round)``
                (``engine.proposals``). Used to pick the second block of an
                equivocating vote.

        Returns:
            ``(delay_ms, message)`` pairs to send, in order; ``delay_ms == 0``
            means send now. Empty for ``silent``, two pairs for ``equivocate``.
        """
        kind, height, round = _describe(message)
        rule = next((r for r in self.rules if kind and r.matches(kind, height, round)), None)
        if rule is None:
            return [(0, message)]
        self._logger(
            "byzantine_action", height=height, round=round, behavior=rule.behavior, kind=kind
        )

        if rule.behavior == "silent":
            return []
        if rule.behavior == "delay":
            return [(rule.delay_ms, message)]
        if rule.behavior == "bad_signature":
            return [(0, self._resign(message, None))]
        if rule.behavior == "wrong_domain":
            return [(0, self._resign(message, rule.domain))]
        if rule.behavior == "invalid_proposal":
            return [(0, self._bad_state_hash(message))]
        delay = self._conflict_delay_ms if message.kind is Kind.BODY else 0
        return [(0, message), (delay, self._conflicting(message, state, proposals))]

    # ------------------------------------------------------------- builders

    def _resign(self, message: Message, domain: Domain | None) -> Message:
        """Return the message with its signature broken (``None``) or made under ``domain``."""
        payload = message.payload
        signed = payload.header if message.kind is Kind.BODY else payload
        signature = (
            _flip_last_hex(signed.signature)
            if domain is None
            else sign(self._keypair.private_key, domain, signed)
        )
        if message.kind is Kind.BODY:
            payload = replace(payload, header=replace(signed, signature=signature))
        else:
            payload = replace(signed, signature=signature)
        return replace(message, payload=payload)

    def _bad_state_hash(self, message: Message) -> Message:
        """Return the proposal with a wrong ``state_hash`` and a valid header signature."""
        header = replace(
            message.payload.header, state_hash=_flip_last_hex(message.payload.header.state_hash)
        )
        return replace(message, payload=replace(message.payload, header=self._sign_header(header)))

    def _conflicting(
        self, message: Message, state: State, proposals: Mapping[tuple[int, int], Block]
    ) -> Message:
        """Return a second, conflicting, correctly signed version of a proposal or vote.

        Proposal: the same height and round with no transactions; if the real
        block is already empty, the same block with a wrong ``state_hash``.
        Vote: NIL if the real vote is for a block; otherwise the round's
        proposal, or a made-up hash when there is none.
        """
        if message.kind is Kind.BODY:
            header = message.payload.header
            if message.payload.txs:
                header = replace(
                    header,
                    tx_root=compute_tx_root([]),
                    state_hash=apply_block(state, []).state_hash(),
                )
            else:
                header = replace(header, state_hash=_flip_last_hex(header.state_hash))
            return replace(message, payload=Block(self._sign_header(header), ()))

        vote = message.payload
        if vote.block_hash is not None:
            other = None
        else:
            proposal = proposals.get((vote.height, vote.round))
            other = (
                block_hash(proposal.header)
                if proposal is not None
                else hash_with_domain("BYZANTINE", vote)
            )
        second = replace(vote, block_hash=other, signature="")
        second = replace(second, signature=sign(self._keypair.private_key, Domain.VOTE, second))
        return replace(message, payload=second)

    def _sign_header(self, header: BlockHeader) -> BlockHeader:
        """Sign ``header`` with this node's key under ``Domain.HEADER``."""
        unsigned = replace(header, signature="")
        return replace(unsigned, signature=sign(self._keypair.private_key, Domain.HEADER, unsigned))


def _describe(message: Message) -> tuple[str | None, int, int]:
    """Return ``(kind, height, round)`` for a proposal or vote, ``(None, 0, 0)`` otherwise.

    Another validator's vote that this node only rebroadcasts (polka evidence)
    is not this node's own message, so no rule applies to it.
    """
    if message.kind is Kind.BODY:
        return "proposal", message.payload.header.height, message.payload.header.round
    if message.kind is Kind.VOTE and message.payload.voter == message.origin:
        vote = message.payload
        return vote.phase.value.lower(), vote.height, vote.round
    return None, 0, 0


def _flip_last_hex(value: str) -> str:
    """Flip the lowest bit of the last hex digit: still well-formed, never equal."""
    return value[:-1] + format(int(value[-1], 16) ^ 1, "x")
