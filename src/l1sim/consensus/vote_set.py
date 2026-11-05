"""Vote storage, deduplication, equivocation detection and quorum counting.

The ``VoteSet`` is the only place where votes are counted. The engine asks it
questions such as "is there a polka for block B in round r?" and never keeps
its own tallies, so every counting rule (rule 7, equivocation, quorum size)
lives in this file.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from l1sim.crypto import verify
from l1sim.models import CommitCertificate, Domain, Phase, Vote

if TYPE_CHECKING:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

ADDED = "added"
DUPLICATE = "duplicate"
EQUIVOCATION = "equivocation"
INVALID = "invalid"

_Slot = tuple[int, int, Phase]


def fault_tolerance(n: int) -> int:
    """Return f, the largest number of Byzantine validators that n validators can tolerate.

    BFT consensus is safe as long as fewer than one third of the validators are
    Byzantine, i.e. ``n >= 3f + 1``. Solving for the largest integer f gives
    ``f = (n - 1) // 3``.

    Args:
        n: Size of the validator set. Must be at least 1.

    Returns:
        The fault tolerance f.

    Raises:
        ValueError: If ``n < 1``; an empty validator set cannot reach any decision.

    Examples:
        >>> fault_tolerance(4), fault_tolerance(8), fault_tolerance(10)
        (1, 2, 3)
    """
    if type(n) is not int or n < 1:
        raise ValueError("validator set must contain at least one validator")
    return (n - 1) // 3


def quorum_size(n: int) -> int:
    """Return the number of votes needed for a quorum among n validators.

    The quorum is ``n - f`` with ``f = (n - 1) // 3`` (see :func:`fault_tolerance`).

    Why it equals 2f + 1 when n = 3f + 1:
        ``n - f = (3f + 1) - f = 2f + 1``. This is the textbook quorum that the
        Concept Guide uses (n = 4 -> 3, n = 7 -> 5, n = 10 -> 7).

    Why it is still safe for other n:
        Safety needs any two quorums to share at least ``f + 1`` validators, so
        that at least one honest validator is in both and, because an honest
        validator never votes for two blocks in the same round, two different
        blocks can never both reach a quorum. Two quorums of size q overlap in at
        least ``2q - n`` validators. With ``q = n - f`` the overlap is
        ``n - 2f``, and because ``n >= 3f + 1`` this is ``>= f + 1``. Liveness
        also holds: the ``n - f`` honest validators alone can form a quorum.

        Using the formula ``2f + 1`` literally would be unsafe when n is not of
        the form 3f + 1. For n = 8 (the old ``num_validators`` in
        ``config/default.json``) it gives 5, and two quorums of 5 out of 8 share
        only 2 validators, which could both be Byzantine (f = 2). ``n - f``
        gives 6, with an overlap of 4 >= f + 1 = 3.

    Args:
        n: Size of the validator set. Must be at least 1.

    Returns:
        The quorum size.

    Raises:
        ValueError: If ``n < 1``.

    Examples:
        >>> quorum_size(4), quorum_size(8), quorum_size(10)
        (3, 6, 7)
    """
    return n - fault_tolerance(n)


class VoteSet:
    """Stores validated votes for every round and counts them per (height, round, phase).

    Votes are kept in slots keyed by ``(height, round, phase)``; inside a slot
    each validator is counted at most once per value (rule 7). Votes of every round of the
    current height are kept: a round change never clears anything, because a
    late PRECOMMIT from an older round can still complete a commit for that
    round. Only :meth:`prune_below` removes votes, and the engine calls it when
    it moves to a new height.

    The set also accepts votes for future heights and rounds, which is how the
    engine buffers votes that arrive before the node itself gets there.

    Every vote is checked before it is stored:

    1. the voter must be in the validator set,
    2. the signature must verify with the voter's public key under
       ``Domain.VOTE`` (so a TX-domain signature is rejected),
    3. a second, different vote of the same voter in the same slot is an
       equivocation: recorded as evidence, and counted only for its own value.

    There is no cap on how many values one voter may sign in a slot: a cap lets
    a Byzantine voter send junk values first so that its vote for the committed
    block is the one dropped, which is the stall this counting fixes. The cost
    is that a flooding voter makes every node store its votes; flooding is not
    one of the lab's Byzantine behaviours (``docs/BYZANTINE_SCHEMA.md``).

    Why an equivocator's second vote still counts: a node that received the
    Byzantine "other" vote first would otherwise never count that validator for
    the block the rest of the network commits, and could wait forever (seen in
    T7: one node stuck while the others moved on). Counting it is safe: two
    values can both reach a quorum q only if at least ``2q - n = n - 2f >= f + 1``
    validators voted for both, so at least one honest validator would have
    equivocated, which never happens. Tendermint's vote set does the same.

    Attributes:
        quorum: Number of matching votes needed for a quorum, from
            :func:`quorum_size` applied to the validator set size.
        f: Fault tolerance, from :func:`fault_tolerance`. ``f + 1`` voters
            always include at least one honest validator (round-skip).
        equivocations: Evidence of every equivocation detected, as
            ``(first_vote, conflicting_vote)`` pairs in detection order. Each
            vote counts only for its own value. The list survives
            :meth:`prune_below` so that it can be written to the results file
            (test T7).

    Example:
        >>> votes = VoteSet(validator_keys)            # 4 validators -> quorum 3
        >>> votes.add(signed_prevote_from_v0)
        'added'
        >>> votes.add(signed_prevote_from_v0)           # gossip delivered it twice
        'duplicate'
        >>> votes.count(1, 0, Phase.PREVOTE, block_hash)
        1
    """

    def __init__(self, validator_keys: Mapping[str, Ed25519PublicKey]) -> None:
        """Create an empty vote set for a fixed validator set.

        Args:
            validator_keys: Map from validator id (for example ``"V0"``) to that
                validator's Ed25519 public key. The map is copied, so later
                changes by the caller do not affect this vote set. The validator
                set is fixed for a whole run (it comes from the genesis file).

        Raises:
            ValueError: If ``validator_keys`` is empty.
        """
        self._keys: dict[str, Ed25519PublicKey] = dict(validator_keys)
        self.quorum: int = quorum_size(len(self._keys))
        self.f: int = fault_tolerance(len(self._keys))
        self._slots: dict[_Slot, dict[str, Vote]] = {}
        # Later votes of an equivocator, per slot and voter, keyed by block hash (or None).
        self._conflicting: dict[_Slot, dict[str, dict[str | None, Vote]]] = {}
        self.equivocations: list[tuple[Vote, Vote]] = []

    def add(self, vote: Vote) -> str:
        """Validate a vote and store it if it is the voter's first vote in its slot.

        This implements rule 7 (each validator is counted at most once per
        value in a ``(height, round, phase)``) and the equivocation handling:
        record evidence, and count each vote only for its own value (see the
        class docstring for why that is safe).

        Checks happen in this order, matching the Concept Guide flowchart:

        1. unknown voter -> ``"invalid"``;
        2. bad signature, wrong domain or malformed signature -> ``"invalid"``;
        3. the voter already has a vote in the slot:

           - same ``block_hash`` -> ``"duplicate"`` (ignored, counted once).
             Ed25519 signatures are deterministic, so a re-delivered vote is
             byte-for-byte identical;
           - different ``block_hash`` (a block vs. another block, or a block vs.
             NIL), seen for the first time -> ``"equivocation"``. The first vote
             stays the voter's vote for :meth:`total` and :meth:`voters`; the
             new one is kept too and counts for its own value only; the pair is
             appended to :attr:`equivocations`;
           - that same conflicting vote again -> ``"duplicate"``;

        4. otherwise -> ``"added"``.

        Args:
            vote: A vote decoded from the network or created by this node.
                Format checks (hash format, phase, non-negative height) are
                already guaranteed by :class:`l1sim.models.Vote`.

        Returns:
            One of ``"added"``, ``"duplicate"``, ``"equivocation"``, ``"invalid"``.
            Only ``"added"`` and ``"equivocation"`` change a count, and only
            those are worth forwarding: each stored vote is then gossiped
            exactly once.

        Raises:
            Nothing for bad network input; bad votes return ``"invalid"``.
        """
        key = self._keys.get(vote.voter)
        if key is None or not verify(key, Domain.VOTE, vote, vote.signature):
            return INVALID

        slot = (vote.height, vote.round, vote.phase)
        by_voter = self._slots.setdefault(slot, {})
        existing = by_voter.get(vote.voter)

        if existing is None:
            by_voter[vote.voter] = vote
            return ADDED

        if existing.block_hash == vote.block_hash:
            return DUPLICATE

        later = self._conflicting.setdefault(slot, {}).setdefault(vote.voter, {})
        if vote.block_hash in later:
            return DUPLICATE  # the same conflicting vote again
        later[vote.block_hash] = vote
        self.equivocations.append((existing, vote))
        return EQUIVOCATION

    def count(self, height: int, round: int, phase: Phase, block_hash: str | None) -> int:
        """Return how many distinct validators voted for ``block_hash`` in one slot.

        Args:
            height: Block height of the slot.
            round: Round number of the slot.
            phase: ``Phase.PREVOTE`` or ``Phase.PRECOMMIT``.
            block_hash: The block hash to count, or ``None`` to count NIL votes.
                NIL is its own value: NIL votes never count towards a block.

        Returns:
            The number of distinct voters with a vote for that value, an
            equivocator's second vote included. ``0`` for a slot that has no votes.
        """
        return len(self.votes_for(height, round, phase, block_hash))

    def total(self, height: int, round: int, phase: Phase) -> int:
        """Return how many distinct validators voted in a slot, whatever they voted for.

        Tendermint calls 2f+1 such votes "a quorum of anything". It is not used
        to decide, only to know that most validators have reached a step.

        Args:
            height: Block height of the slot.
            round: Round number of the slot.
            phase: ``Phase.PREVOTE`` or ``Phase.PRECOMMIT``.

        Returns:
            The number of voters in the slot, NIL votes included.
        """
        return len(self._slots.get((height, round, phase), {}))

    def has_quorum(self, height: int, round: int, phase: Phase) -> str | None:
        """Return the block hash that reached a quorum in a slot, if any.

        A PREVOTE quorum for a block is a "polka"; a PRECOMMIT quorum for a
        block means the block can be finalized. With at most f equivocators, at
        most one value can reach a quorum in a slot (see the class docstring), so
        the answer is unique.

        NIL is handled separately by :meth:`has_nil_quorum`; this method never
        reports a NIL quorum. ``None`` therefore always means "no block has a
        quorum here".

        Args:
            height: Block height of the slot.
            round: Round number of the slot.
            phase: ``Phase.PREVOTE`` or ``Phase.PRECOMMIT``.

        Returns:
            The 64-character block hash with at least :attr:`quorum` votes, or
            ``None``.
        """
        slot = (height, round, phase)
        hashes = {vote.block_hash for vote in self._slots.get(slot, {}).values()}
        for later in self._conflicting.get(slot, {}).values():
            hashes.update(later)
        for block_hash in sorted(h for h in hashes if h):
            if self.count(height, round, phase, block_hash) >= self.quorum:
                return block_hash
        return None

    def has_nil_quorum(self, height: int, round: int, phase: Phase) -> bool:
        """Return whether NIL votes alone reached a quorum in a slot.

        Example: a quorum of NIL PREVOTEs (a "NIL polka") tells the engine that
        no block can get a polka in this round, so it may precommit NIL without
        waiting for its prevote timeout.

        Args:
            height: Block height of the slot.
            round: Round number of the slot.
            phase: ``Phase.PREVOTE`` or ``Phase.PRECOMMIT``.

        Returns:
            ``True`` if at least :attr:`quorum` validators voted NIL.
        """
        return self.count(height, round, phase, None) >= self.quorum

    def voters(self, height: int, round: int) -> set[str]:
        """Return the validators that have a counted vote of either phase in a round.

        Used for round-skip: f + 1 distinct voters in a higher round prove that
        at least one honest validator is already there. An equivocator counts
        once: this is a set of voter ids, whatever they voted for.

        Args:
            height: Block height.
            round: Round number at that height.

        Returns:
            The set of voter ids, PREVOTE and PRECOMMIT together.
        """
        voters: set[str] = set()
        for phase in Phase:
            voters.update(self._slots.get((height, round, phase), {}))
        return voters

    def rounds(self, height: int) -> list[int]:
        """Return every round of ``height`` that has at least one stored vote.

        The engine uses this to look for a PRECOMMIT quorum in *any* round of the
        current height, because a node may finalize a block from an older round
        after it has already moved on to a newer one.

        Args:
            height: The block height to inspect.

        Returns:
            Sorted list of round numbers (sorted for deterministic iteration).
        """
        return sorted({round for h, round, _ in self._slots if h == height})

    def votes_for(
        self, height: int, round: int, phase: Phase, block_hash: str | None
    ) -> tuple[Vote, ...]:
        """Return the counted votes for ``block_hash`` in one slot, sorted by voter id.

        The engine rebroadcasts the PREVOTEs of a polka with this (polka
        evidence), and :meth:`certificate` builds on it. Sorting by voter makes
        the result, and so what is sent, the same on every run.

        Args:
            height: Block height.
            round: Round number at that height.
            phase: PREVOTE or PRECOMMIT.
            block_hash: The block hash, or ``None`` for NIL votes.

        Returns:
            The signed votes, at most one per voter, possibly empty. An
            equivocator's vote is included for the value it is for, whether it
            arrived first or second.
        """
        slot = (height, round, phase)
        chosen = {
            voter: vote
            for voter, vote in self._slots.get(slot, {}).items()
            if vote.block_hash == block_hash
        }
        for voter, later in self._conflicting.get(slot, {}).items():
            if block_hash in later:
                chosen.setdefault(voter, later[block_hash])
        return tuple(chosen[voter] for voter in sorted(chosen))

    def certificate(self, height: int, round: int, block_hash: str) -> CommitCertificate:
        """Build the commit certificate proving that ``block_hash`` was finalized.

        The certificate contains every counted PRECOMMIT for ``block_hash`` in
        the given round, sorted by voter id so that the same set of votes always
        produces the same certificate bytes. It is stored by the engine for every
        finalized block as evidence of finality.

        Args:
            height: Height of the finalized block.
            round: Round in which the PRECOMMIT quorum formed. All precommits of
                a certificate come from one round (enforced by
                :class:`l1sim.models.CommitCertificate`).
            block_hash: Hash of the finalized block. NIL cannot be certified.

        Returns:
            A :class:`l1sim.models.CommitCertificate` with at least
            :attr:`quorum` signed precommits.

        Raises:
            ValueError: If fewer than :attr:`quorum` validators precommitted
                ``block_hash`` in that round.
        """
        precommits = self.votes_for(height, round, Phase.PRECOMMIT, block_hash)
        if len(precommits) < self.quorum:
            raise ValueError(
                f"only {len(precommits)} precommits for this block, quorum is {self.quorum}"
            )
        return CommitCertificate(height, block_hash, precommits)

    def prune_below(self, height: int) -> None:
        """Forget every vote whose height is lower than ``height``.

        Called by the engine right after it finalizes a block and moves to
        ``height``. Votes of the new height and of future heights are kept, so
        votes that arrived early are not lost. Equivocation evidence is kept.

        Args:
            height: The new current height. Votes with ``vote.height < height``
                are removed.
        """
        for slot in [slot for slot in self._slots if slot[0] < height]:
            del self._slots[slot]
            self._conflicting.pop(slot, None)
