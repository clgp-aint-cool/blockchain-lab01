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
    each validator has at most one vote (rule 7). Votes of every round of the
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
    3. the voter must not already have a different vote in the same slot.

    Attributes:
        quorum: Number of matching votes needed for a quorum, from
            :func:`quorum_size` applied to the validator set size.
        equivocations: Evidence of every equivocation detected, as
            ``(first_vote, conflicting_vote)`` pairs in detection order. The
            first vote is the one that is counted; the conflicting one is never
            counted. The list survives :meth:`prune_below` so that it can be
            written to the results file (test T7).

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
        self._slots: dict[_Slot, dict[str, Vote]] = {}
        self.equivocations: list[tuple[Vote, Vote]] = []
        self._evidence: set[tuple[str, int, int, Phase, str | None]] = set()

    def add(self, vote: Vote) -> str:
        """Validate a vote and store it if it is the voter's first vote in its slot.

        This implements rule 7 (each validator is counted at most once per
        ``(height, round, phase)``) and the equivocation handling from the
        Concept Guide: keep the first vote, do not count the second, record
        evidence.

        Checks happen in this order, matching the Concept Guide flowchart:

        1. unknown voter -> ``"invalid"``;
        2. bad signature, wrong domain or malformed signature -> ``"invalid"``;
        3. the voter already has a vote in the slot:

           - same ``block_hash`` -> ``"duplicate"`` (ignored, counted once).
             Ed25519 signatures are deterministic, so a re-delivered vote is
             byte-for-byte identical;
           - different ``block_hash`` (a block vs. another block, or a block vs.
             NIL) -> ``"equivocation"``. The stored vote is kept, the new one is
             not counted, and the pair is appended to :attr:`equivocations`
             once. Receiving the same conflicting vote again returns
             ``"equivocation"`` again but does not add a second evidence entry;

        4. otherwise -> ``"added"``.

        Args:
            vote: A vote decoded from the network or created by this node.
                Format checks (hash format, phase, non-negative height) are
                already guaranteed by :class:`l1sim.models.Vote`.

        Returns:
            One of ``"added"``, ``"duplicate"``, ``"equivocation"``, ``"invalid"``.
            Only ``"added"`` changes any count.

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

        evidence_key = (vote.voter, vote.height, vote.round, vote.phase, vote.block_hash)
        if evidence_key not in self._evidence:
            self._evidence.add(evidence_key)
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
            The number of counted votes (duplicates and equivocations excluded).
            ``0`` for a slot that has no votes.
        """
        votes = self._slots.get((height, round, phase), {})
        return sum(1 for vote in votes.values() if vote.block_hash == block_hash)

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
        block means the block can be finalized. Because each validator counts at
        most once per slot and ``quorum > n / 2``, at most one value can reach a
        quorum in a slot, so the answer is unique.

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
        votes = self._slots.get((height, round, phase), {})
        hashes = sorted({vote.block_hash for vote in votes.values() if vote.block_hash})
        for block_hash in hashes:
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

    def certificate(self, height: int, round: int, block_hash: str) -> CommitCertificate:
        """Build the commit certificate proving that ``block_hash`` was finalized.

        The certificate contains every counted PRECOMMIT for ``block_hash`` in
        the given round, sorted by voter id so that the same set of votes always
        produces the same certificate bytes. It is stored by the engine for every
        finalized block and will be sent with blocks during block sync.

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
        votes = self._slots.get((height, round, Phase.PRECOMMIT), {})
        precommits = tuple(
            votes[voter] for voter in sorted(votes) if votes[voter].block_hash == block_hash
        )
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
