"""Test-only harness: n consensus engines on an in-memory network driven by a fake clock.

This module lives under ``tests/`` on purpose. It is not a run mode of the
simulator and is never imported by ``src/``. It replaces the pieces that the
real node gets from other team members (gossip, TCP, ``loop.call_later``) with
tiny deterministic stand-ins, so that the engine's behaviour can be checked
without any network or ``asyncio`` code:

- :class:`FakeClock` is a priority queue of callbacks ordered by
  ``(time, sequence number)``. Nothing depends on real time, so every run of a
  test produces exactly the same sequence of events.
- :class:`Network` creates one :class:`ConsensusEngine` per validator, wires
  ``broadcast`` to "deliver to every other engine after ``latency_ms``" and
  wires ``schedule_timeout`` / ``cancel_timeout`` to the fake clock. A ``drop``
  rule lets a test remove chosen messages (a silent proposer, a node that
  misses prevotes, ...).

Every delivered message goes through ``decode(encode(message))`` so the tests
also prove that everything the engine sends survives the canonical wire format.
"""

from __future__ import annotations

import heapq
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from l1sim.consensus.engine import ConsensusEngine, Step, TimeoutConfig
from l1sim.crypto import KeyPair, keypair_from_seed, sign
from l1sim.encoding import block_hash, decode, encode
from l1sim.execution import Mempool, State, apply_block, compute_tx_root
from l1sim.models import (
    Block,
    BlockHeader,
    CommitCertificate,
    Domain,
    Kind,
    Message,
    Phase,
    Tx,
    Vote,
)

TIMEOUTS = TimeoutConfig(propose_ms=1000, prevote_ms=1000, precommit_ms=1000, delta_ms=300)
"""Timeouts used by the harness unless a test passes its own."""

DropRule = Callable[[str, str, Message], bool]
"""``drop(src, dst, message) -> bool``: return ``True`` to lose that one delivery."""


@dataclass(order=True)
class _Scheduled:
    """One pending callback in the :class:`FakeClock` queue.

    Ordering uses ``(time, seq)`` only, so two callbacks scheduled for the same
    time always run in the order they were scheduled. That tie-break is what
    makes the harness deterministic.

    Attributes:
        time: Fake time in milliseconds at which the callback runs.
        seq: Global scheduling counter used as a tie-break.
        action: The callback; excluded from comparisons.
        cancelled: Set by :meth:`FakeClock.cancel`; cancelled entries are skipped.
    """

    time: int
    seq: int
    action: Callable[[], None] = field(compare=False)
    cancelled: bool = field(default=False, compare=False)


class FakeClock:
    """Deterministic replacement for the event loop's timer (``loop.call_later``).

    Time only moves when :meth:`run` pops the next callback, and it jumps
    straight to that callback's time. There is no sleeping, so a test that
    simulates many seconds of consensus runs in milliseconds.

    Attributes:
        now: Current fake time in milliseconds (starts at 0).
    """

    def __init__(self) -> None:
        """Create a clock at time 0 with an empty queue."""
        self.now = 0
        self._queue: list[_Scheduled] = []
        self._seq = 0

    def call_later(self, delay_ms: int, action: Callable[[], None]) -> _Scheduled:
        """Schedule ``action`` to run ``delay_ms`` after the current fake time.

        Args:
            delay_ms: Non-negative delay in milliseconds.
            action: Callback taking no arguments.

        Returns:
            A handle that can be passed to :meth:`cancel`.
        """
        self._seq += 1
        entry = _Scheduled(self.now + delay_ms, self._seq, action)
        heapq.heappush(self._queue, entry)
        return entry

    def cancel(self, handle: _Scheduled) -> None:
        """Cancel a scheduled callback. Cancelling one that already ran is harmless.

        Args:
            handle: The value returned by :meth:`call_later`.
        """
        handle.cancelled = True

    def run(self, until: Callable[[], bool], max_ms: int = 60_000) -> bool:
        """Run callbacks in time order until ``until()`` is true.

        Args:
            until: Condition checked before every callback.
            max_ms: Safety limit on fake time; the run stops if the next callback
                is later than this.

        Returns:
            ``True`` if ``until()`` became true, ``False`` if the queue emptied or
            the time limit was reached first.
        """
        while not until():
            if not self._queue or self._queue[0].time > max_ms:
                return False
            entry = heapq.heappop(self._queue)
            if entry.cancelled:
                continue
            self.now = entry.time
            entry.action()
        return True


class Network:
    """n validators ``V0 .. V{n-1}``, each running a :class:`ConsensusEngine`, fully connected.

    Every engine gets its own :class:`State` and :class:`Mempool` (filled with
    the same ``txs``), keys from ``keypair_from_seed(seed, id)``, and callbacks
    that point back into this object. Messages broadcast by one engine are
    delivered to every other engine after ``latency_ms`` unless ``drop`` says
    otherwise.

    Attributes:
        clock: The shared :class:`FakeClock`.
        ids: Sorted validator ids.
        keys: ``id -> KeyPair``.
        engines: ``id -> ConsensusEngine``.
        finalized: ``id -> [(block, certificate), ...]`` in finalization order,
            recorded through ``on_finalize``.
        events: ``id -> [(event, fields), ...]`` recorded through ``logger``.
        sent: ``[(src, message), ...]`` for every message any engine broadcast,
            including ones that were later dropped.
        scheduled: ``id -> [(delay_ms, height, round, step), ...]`` for every
            ``schedule_timeout`` call.

    Example:
        >>> net = Network(4, txs=sample_txs())
        >>> net.start()
        >>> net.run_until_height(3)
        True
        >>> len({tuple(net.hashes(node)[:3]) for node in net.ids})
        1
    """

    def __init__(
        self,
        n: int,
        *,
        txs: Iterable[Tx] = (),
        timeouts: TimeoutConfig = TIMEOUTS,
        latency_ms: int = 10,
        drop: DropRule | None = None,
        max_txs_per_block: int = 10,
        seed: int = 7,
    ) -> None:
        """Build the network; engines are created but not started.

        Args:
            n: Number of validators.
            txs: Transactions put in every node's mempool before starting.
            timeouts: Step timeouts given to every engine.
            latency_ms: Delivery delay of every message. Keep it well below the
                timeouts for "synchronous" tests.
            drop: Optional :data:`DropRule` deciding which deliveries are lost.
            max_txs_per_block: Passed to every engine.
            seed: Seed for ``keypair_from_seed``.
        """
        self.clock = FakeClock()
        self.ids = sorted(f"V{i}" for i in range(n))
        self.keys: dict[str, KeyPair] = {node: keypair_from_seed(seed, node) for node in self.ids}
        self._latency = latency_ms
        self._drop = drop
        self.finalized: dict[str, list[tuple[Block, CommitCertificate]]] = {}
        self.events: dict[str, list[tuple[str, dict[str, Any]]]] = {}
        self.sent: list[tuple[str, Message]] = []
        self.scheduled: dict[str, list[tuple[int, int, int, Step]]] = {}
        self.engines: dict[str, ConsensusEngine] = {}

        validator_keys = {node: pair.public_key for node, pair in self.keys.items()}
        tx_list = list(txs)
        for node in self.ids:
            mempool = Mempool()
            for tx in tx_list:
                mempool.add(tx)
            self.finalized[node] = []
            self.events[node] = []
            self.scheduled[node] = []
            self.engines[node] = ConsensusEngine(
                node_id=node,
                keypair=self.keys[node],
                validator_keys=validator_keys,
                timeouts=timeouts,
                state=State(),
                mempool=mempool,
                max_txs_per_block=max_txs_per_block,
                broadcast=self._broadcaster(node),
                schedule_timeout=self._scheduler(node),
                cancel_timeout=self.clock.cancel,
                on_finalize=self._finalizer(node),
                logger=self._logger(node),
            )

    # ------------------------------------------------------------- callbacks

    def _broadcaster(self, src: str) -> Callable[[Message], None]:
        """Return the ``broadcast`` callback for engine ``src``.

        The callback records the message in :attr:`sent` and schedules one
        delivery per other validator, skipping deliveries that ``drop`` rejects.
        It never delivers to ``src`` itself, matching the engine's contract.

        Args:
            src: Id of the sending engine.

        Returns:
            ``broadcast(message)``.
        """

        def broadcast(message: Message) -> None:
            self.sent.append((src, message))
            for dst in self.ids:
                if dst == src or (self._drop is not None and self._drop(src, dst, message)):
                    continue
                self.clock.call_later(self._latency, self._deliverer(dst, message))

        return broadcast

    def _deliverer(self, dst: str, message: Message) -> Callable[[], None]:
        """Return a callback that hands ``message`` to engine ``dst``.

        The message is round-tripped through ``encode``/``decode`` first, like a
        real network would. ``BODY`` goes to ``on_proposal``, ``VOTE`` to
        ``on_vote``.

        Args:
            dst: Id of the receiving engine.
            message: The message to deliver.

        Returns:
            A callback taking no arguments.
        """

        def deliver() -> None:
            self.deliver(dst, decode(encode(message)))

        return deliver

    def _scheduler(self, node: str) -> Callable[[int, int, int, Step], Any]:
        """Return the ``schedule_timeout`` callback for engine ``node``.

        Args:
            node: Id of the engine.

        Returns:
            ``schedule_timeout(delay_ms, height, round, step) -> handle``; the
            call is also recorded in :attr:`scheduled`.
        """

        def schedule(delay_ms: int, height: int, round: int, step: Step) -> Any:
            self.scheduled[node].append((delay_ms, height, round, step))
            engine = self.engines[node]
            return self.clock.call_later(delay_ms, lambda: engine.on_timeout(height, round, step))

        return schedule

    def _finalizer(self, node: str) -> Callable[[Block, CommitCertificate], None]:
        """Return the ``on_finalize`` callback that records into :attr:`finalized`.

        Args:
            node: Id of the engine.

        Returns:
            ``on_finalize(block, certificate)``.
        """

        def on_finalize(block: Block, certificate: CommitCertificate) -> None:
            self.finalized[node].append((block, certificate))

        return on_finalize

    def _logger(self, node: str) -> Callable[..., None]:
        """Return the ``logger`` callback that records into :attr:`events`.

        Args:
            node: Id of the engine.

        Returns:
            ``logger(event, **fields)``.
        """

        def log(event: str, **fields: Any) -> None:
            self.events[node].append((event, fields))

        return log

    # --------------------------------------------------------------- driving

    def start(self, only: Sequence[str] | None = None) -> None:
        """Start engines in sorted id order.

        Args:
            only: Ids to start; ``None`` starts all. Engines that are not started
                still receive messages, which lets a test drive a single engine
                by hand.
        """
        for node in self.ids if only is None else sorted(only):
            self.engines[node].start()

    def deliver(self, dst: str, message: Message) -> None:
        """Hand ``message`` to engine ``dst`` immediately (no latency, no drop rule).

        Args:
            dst: Id of the receiving engine.
            message: A ``BODY`` or ``VOTE`` message.

        Raises:
            ValueError: For any other message kind.
        """
        engine = self.engines[dst]
        if message.kind is Kind.BODY:
            engine.on_proposal(message.payload)
        elif message.kind is Kind.VOTE:
            engine.on_vote(message.payload)
        else:
            raise ValueError(f"harness cannot deliver {message.kind.value}")

    def run_until_height(
        self, height: int, nodes: Sequence[str] | None = None, max_ms: int = 60_000
    ) -> bool:
        """Run the clock until every listed engine has finalized ``height``.

        Args:
            height: Target height.
            nodes: Ids to wait for; ``None`` means all.
            max_ms: Fake-time limit.

        Returns:
            ``True`` if the target was reached before the limit.
        """
        targets = self.ids if nodes is None else list(nodes)
        return self.clock.run(
            lambda: all(len(self.finalized[node]) >= height for node in targets), max_ms
        )

    # ------------------------------------------------------------- inspecting

    def hashes(self, node: str) -> list[str]:
        """Return the block hashes ``node`` finalized, in height order.

        Args:
            node: Id of the engine.

        Returns:
            List of block hashes.
        """
        return [block_hash(block.header) for block, _ in self.finalized[node]]

    def votes_sent(self, node: str) -> list[Vote]:
        """Return every vote ``node`` broadcast, in order.

        Args:
            node: Id of the engine.

        Returns:
            List of votes signed by ``node``.
        """
        return [
            message.payload
            for src, message in self.sent
            if src == node and message.kind is Kind.VOTE
        ]

    def event_names(self, node: str) -> list[str]:
        """Return the event types logged by ``node``, in order.

        Args:
            node: Id of the engine.

        Returns:
            List of event names.
        """
        return [event for event, _ in self.events[node]]

    def make_vote(
        self, voter: str, phase: Phase, height: int, round: int, block_hash: str | None
    ) -> Vote:
        """Create a vote signed by ``voter`` (for hand-driven tests).

        Args:
            voter: Validator id whose key signs the vote.
            phase: ``Phase.PREVOTE`` or ``Phase.PRECOMMIT``.
            height: Vote height.
            round: Vote round.
            block_hash: Block hash, or ``None`` for NIL.

        Returns:
            A vote with a valid ``Domain.VOTE`` signature.
        """
        vote = Vote(phase, height, round, block_hash, voter)
        return replace(vote, signature=sign(self.keys[voter].private_key, Domain.VOTE, vote))

    def make_block(
        self,
        proposer: str,
        height: int,
        round: int,
        txs: Sequence[Tx] = (),
        *,
        parent_hash: str,
        state: State | None = None,
        state_hash: str | None = None,
        signer: str | None = None,
    ) -> Block:
        """Create a signed proposal by hand, optionally broken on purpose.

        Args:
            proposer: Value of ``header.proposer``.
            height: Block height.
            round: Block round.
            txs: Transactions in the block.
            parent_hash: Hash of the parent block.
            state: State before the block, used to compute the honest
                ``state_hash``; defaults to an empty :class:`State`.
            state_hash: Override for ``header.state_hash`` (to build an invalid
                block).
            signer: Id whose key signs the header; defaults to ``proposer``.
                A different signer builds a forged proposal.

        Returns:
            The block.
        """
        before = State() if state is None else state
        header = BlockHeader(
            height=height,
            round=round,
            parent_hash=parent_hash,
            tx_root=compute_tx_root(list(txs)),
            state_hash=state_hash or apply_block(before, list(txs)).state_hash(),
            proposer=proposer,
        )
        key = self.keys[signer or proposer].private_key
        return Block(replace(header, signature=sign(key, Domain.HEADER, header)), tuple(txs))


def sample_txs() -> list[Tx]:
    """Return a small deterministic set of unsigned transactions for the mempools.

    Unsigned is fine here because the harness engines run without
    ``user_keys`` (transaction signatures are node.py's job before the mempool).

    Returns:
        Three transactions from Alice (nonces 0–2) and two from Bob (nonces 0–1).
    """
    return [Tx("Alice", "Alice/note", f"a{nonce}", nonce) for nonce in range(3)] + [
        Tx("Bob", "Bob/note", f"b{nonce}", nonce) for nonce in range(2)
    ]
