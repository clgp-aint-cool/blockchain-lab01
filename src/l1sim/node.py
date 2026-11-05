"""Integration layer joining configuration, execution, logging, and consensus."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from l1sim.config import GenesisConfig, ScenarioConfig
from l1sim.consensus.byzantine import Byzantine
from l1sim.consensus.engine import ConsensusEngine, Step, TimeoutConfig, proposer_for
from l1sim.crypto import keypair_from_seed, verify
from l1sim.encoding import block_hash, decode, encode, hash_with_domain
from l1sim.execution import Mempool, State, validate_pending_tx
from l1sim.execution.validation import compute_tx_root
from l1sim.logger import EventLogger, ResultWriter
from l1sim.models import (
    Block,
    BlockHeader,
    BodyRequest,
    CommitCertificate,
    Domain,
    Kind,
    Message,
    Tx,
)
from l1sim.network.faults import FaultInjector
from l1sim.network.gossip import DIRECT, FLOOD, Gossip
from l1sim.network.hello import handshake_fn
from l1sim.network.static_peers import StaticPeers
from l1sim.network.tcp_transport import TcpTransport, read_frame, write_frame

BODY_KEEP_HEIGHTS = 20  # finalized bodies kept to answer BODY_REQUEST from slower peers
BODY_RETRY_MS = 300  # then ask every other validator for a body the first peer did not send

BroadcastHandler = Callable[[Message], None]
SendHandler = Callable[[str, Message], None]


class Node:
    """One validator node wired to the consensus and execution modules."""

    def __init__(
        self,
        node_id: str,
        genesis: GenesisConfig,
        scenario: ScenarioConfig,
        listen_addr: str = "127.0.0.1:9000",
        advertise_addr: str | None = None,
        logger: Any = None,
        broadcast: BroadcastHandler | None = None,
        loop: asyncio.AbstractEventLoop | None = None,
        send_to: SendHandler | None = None,
        result_writer: ResultWriter | None = None,
        peer_addresses: dict[str, str] | None = None,
    ) -> None:
        if node_id not in genesis.validators:
            raise ValueError(f"{node_id!r} is not a validator")
        if node_id not in scenario.validators:
            raise ValueError(f"{node_id!r} is not in the scenario validator list")
        if (
            genesis.validators != scenario.validators
            or genesis.seed != scenario.seed
            or genesis.chain_id != scenario.chain_id
        ):
            raise ValueError("scenario must match the genesis validator set, seed and chain")

        self.node_id = node_id
        self.genesis = genesis
        self.scenario = scenario
        self.listen_addr = listen_addr
        self.advertise_addr = advertise_addr or listen_addr
        self.loop = loop
        self.peer_addresses = dict(
            peer_addresses if peer_addresses is not None else scenario.network.peers
        )
        self.state = State()
        self.mempool = Mempool()
        self.finalized: dict[int, tuple[Block, CommitCertificate]] = {}
        self._timeout_handles: list[Any] = []
        self._started = False
        self._stopped = False
        self.completed = asyncio.Event()
        self._blocks: dict[str, Block] = {}
        self._headers: dict[str, BlockHeader] = {}
        self._body_waiters: dict[str, set[str]] = {}

        self.keypair = keypair_from_seed(scenario.seed, node_id)
        self.validator_keys: dict[str, Ed25519PublicKey] = {
            node: Ed25519PublicKey.from_public_bytes(bytes.fromhex(genesis.public_keys[node]))
            for node in genesis.validators
        }
        if self.keypair.public_bytes.hex() != genesis.public_keys[node_id]:
            raise ValueError("node signing key does not match genesis")
        self.user_keys: dict[str, Ed25519PublicKey] = {
            raw_tx["sender"]: keypair_from_seed(scenario.seed, raw_tx["sender"]).public_key
            for raw_tx in scenario.transactions
        }

        self.logger = logger if logger is not None else EventLogger(node_id, scenario.name)
        self._event_adapter = self._make_event_adapter(self.logger)
        self.broadcast_handler = broadcast or (lambda message: None)
        self.send_handler = send_to or (lambda peer, message: None)
        self.result_writer = (
            result_writer if result_writer is not None else ResultWriter(scenario.name)
        )

        consensus = {
            "timeout_propose_ms": scenario.consensus.timeout_propose_ms,
            "timeout_prevote_ms": scenario.consensus.timeout_prevote_ms,
            "timeout_precommit_ms": scenario.consensus.timeout_precommit_ms,
            "timeout_delta_ms": scenario.consensus.timeout_delta_ms,
        }
        self.timeouts = TimeoutConfig.from_config(consensus)
        self.engine = ConsensusEngine(
            node_id=node_id,
            keypair=self.keypair,
            validator_keys=self.validator_keys,
            timeouts=self.timeouts,
            state=self.state,
            mempool=self.mempool,
            max_txs_per_block=scenario.max_txs_per_block,
            broadcast=self._broadcast,
            schedule_timeout=self.schedule_timeout,
            cancel_timeout=self.cancel_timeout,
            on_finalize=self.on_finalize,
            logger=self._event_adapter,
            user_keys=self.user_keys,
            start_height=1,
        )
        # Only a node named in the scenario's byzantine rules misbehaves; see _broadcast.
        rules = [rule for rule in scenario.byzantine if rule.node == node_id]
        longest_step = max(
            scenario.consensus.timeout_propose_ms,
            scenario.consensus.timeout_prevote_ms,
            scenario.consensus.timeout_precommit_ms,
        )
        self.byzantine = (
            Byzantine(
                node_id,
                rules,
                self.keypair,
                self._event_adapter,
                conflict_delay_ms=2 * longest_step,
            )
            if rules
            else None
        )

    @staticmethod
    def _make_event_adapter(logger: Any) -> Callable[..., None]:
        def event(event: str, **fields: Any) -> None:
            if hasattr(logger, "event") and callable(logger.event):
                height = fields.pop("height", 0)
                fields.pop("node", None)
                logger.event(event, height=height, **fields)
            else:
                logger(event, **fields)

        return event

    def start(self) -> None:
        """Start consensus after the network is ready."""
        if self._started or self._stopped:
            raise RuntimeError("a node can only be started once")
        self.loop = self.loop or asyncio.get_running_loop()
        self._started = True
        self.engine.start()

    def stop(self) -> None:
        """Cancel pending timeouts and release node resources."""
        self._stopped = True
        self.engine.stop()
        for handle in tuple(self._timeout_handles):
            self.cancel_timeout(handle)
        self._timeout_handles.clear()

    def _pending_transaction_reason(self, tx: Tx) -> str | None:
        """Check signatures and reject committed nonces, allowing future nonces."""
        if not isinstance(tx, Tx):
            raise TypeError("tx must be a Tx instance")
        if self._stopped:
            return "node_stopped"
        if self.completed.is_set():
            return "max_height_reached"
        # Future nonces may arrive out of order; Mempool.take selects only executable txs.
        valid, reason = validate_pending_tx(self.state, tx, self.user_keys)
        if not valid:
            self._event_adapter("tx_rejected", reason=reason, sender=tx.sender, nonce=tx.nonce)
            return reason
        return None

    def _validate_pending_transaction(self, tx: Tx) -> bool:
        return self._pending_transaction_reason(tx) is None

    def submit_transaction(self, tx: Tx, *, broadcast: bool = True) -> bool:
        """Accept a local transaction and flood it once through Gossip."""
        accepted, _ = self.submit_transaction_result(tx, broadcast=broadcast)
        return accepted

    def submit_transaction_result(self, tx: Tx, *, broadcast: bool = True) -> tuple[bool, str]:
        """Return mempool acceptance and a stable rejection reason for client ingress."""
        reason = self._pending_transaction_reason(tx)
        if reason is not None:
            return False, reason
        added = self.mempool.add(tx)
        if added and broadcast:
            self._broadcast(Message(Kind.TX, self.node_id, self.engine.height, tx))
        return added, "" if added else "duplicate"

    def _broadcast(self, message: Message) -> None:
        """Send what the engine broadcasts, after the Byzantine rules if this node has any.

        The rules run before the BODY -> HEADER split, so a second (equivocating)
        block is also stored and can answer BODY_REQUEST. Delayed copies are sent
        with ``loop.call_later``.
        """
        if self._stopped or self.completed.is_set():
            return
        if self.byzantine is None:
            self._send_out(message)
            return
        outgoing = self.byzantine.outgoing(message, self.engine.state, self.engine.proposals)
        for delay_ms, out in outgoing:
            if delay_ms:
                loop = self.loop or asyncio.get_running_loop()
                loop.call_later(delay_ms / 1000.0, self._send_out, out)
            else:
                self._send_out(out)

    def _send_out(self, message: Message) -> None:
        """Advertise proposal headers; send bodies only in response to requests."""
        if self._stopped or self.completed.is_set():
            return
        if message.kind is Kind.BODY:
            block = message.payload
            self._blocks[block_hash(block.header)] = block
            message = Message(Kind.HEADER, self.node_id, message.height, block.header)
        self.broadcast_handler(message)

    def _retry_body_request(self, request: Message, asked: str) -> None:
        """Ask every other validator for a body that ``asked`` has not delivered yet.

        Fault decisions are a pure function of (link, message), so asking the same
        peer again would meet the same fate; each other link is an independent
        chance. A peer that only has the header answers once the body reaches it.
        """
        digest = request.payload.block_hash
        if self._stopped or self.completed.is_set() or digest in self._blocks:
            return
        if digest not in self._headers:
            return
        for other in sorted(self.validator_keys):
            if other not in (self.node_id, asked):
                self.send_handler(other, request)

    @staticmethod
    def _signature_reason(public_key, domain: Domain, payload) -> str | None:
        if public_key is None:
            return "unknown_peer"
        if verify(public_key, domain, payload, payload.signature):
            return None
        if any(
            verify(public_key, other, payload, payload.signature)
            for other in Domain
            if other is not domain
        ):
            return "wrong_domain"
        return "bad_signature"

    def receive_message(self, message: Message, from_peer: str | None = None) -> bool:
        """Route decoded messages; return whether Gossip should forward FLOOD messages."""
        if not isinstance(message, Message):
            raise TypeError("message must be a Message instance")
        if self._stopped:
            return False
        peer = from_peer or message.origin
        if peer not in self.validator_keys or message.origin not in self.validator_keys:
            return False
        if message.kind is Kind.BODY_REQUEST:
            block = self._blocks.get(message.payload.block_hash)
            if block is not None:
                self.send_handler(
                    peer, Message(Kind.BODY, self.node_id, block.header.height, block)
                )
                return True
            if message.payload.block_hash in self._headers:
                self._body_waiters.setdefault(message.payload.block_hash, set()).add(peer)
                return True
            return False
        if self.completed.is_set():
            return False
        if message.kind is not Kind.TX and not 1 <= message.height <= self.scenario.max_height:
            return False

        if message.kind is Kind.HEADER:
            header = message.payload
            digest = block_hash(header)
            public_key = self.validator_keys.get(header.proposer)
            reason = self._signature_reason(public_key, Domain.HEADER, header)
            if header.height < self.engine.height:
                reason = "old_height"
            elif header.proposer != proposer_for(self.validator_keys, header.height, header.round):
                reason = reason or "bad_proposer"
            if reason:
                self._event_adapter(
                    "header_rejected",
                    msg_height=message.height,
                    proposer=header.proposer,
                    reason=reason,
                )
                return False
            if digest not in self._blocks and digest not in self._headers:
                self._headers[digest] = header
                request = Message(
                    Kind.BODY_REQUEST, self.node_id, message.height, BodyRequest(digest)
                )
                self.send_handler(peer, request)
                loop = self.loop or asyncio.get_running_loop()
                loop.call_later(BODY_RETRY_MS / 1000, self._retry_body_request, request, peer)
            return True

        if message.kind is Kind.TX:
            if not self._validate_pending_transaction(message.payload):
                return False
            # Gossip forwards the original message, preserving origin and message_id.
            # A different tx with an already-used (sender, nonce) is refused and not flooded.
            return self.mempool.add(message.payload)
        if message.kind is Kind.BODY:
            block = message.payload
            digest = block_hash(block.header)
            if (
                self._headers.get(digest) != block.header
                or compute_tx_root(block.txs) != block.header.tx_root
            ):
                self._event_adapter("body_rejected", msg_height=message.height, block_hash=digest)
                return False
            self._blocks[digest] = block
            for waiter in sorted(self._body_waiters.pop(digest, set())):
                self.send_handler(waiter, Message(Kind.BODY, self.node_id, message.height, block))
            self.engine.on_proposal(block)
            return True
        if message.kind is Kind.VOTE:
            # The engine verifies the vote once, logs vote_rejected with a reason, and
            # returns True only for a vote it newly counts (an equivocator's conflicting
            # vote included), so duplicates and rejected votes are not forwarded.
            return self.engine.on_vote(message.payload)

        self._event_adapter(
            "message_ignored",
            kind=message.kind.value,
            origin=message.origin,
            msg_height=message.height,
        )
        return False

    def schedule_timeout(
        self,
        delay_ms: int,
        height: int,
        round: int,
        step: Step,
    ) -> Any:
        """Schedule an engine timeout through asyncio."""
        if self._stopped or self.completed.is_set() or height > self.scenario.max_height:
            return None

        def fire() -> None:
            if handle in self._timeout_handles:
                self._timeout_handles.remove(handle)
            if not self._stopped and not self.completed.is_set():
                self.engine.on_timeout(height, round, step)

        loop = self.loop or asyncio.get_running_loop()
        handle = loop.call_later(delay_ms / 1000.0, fire)
        self._timeout_handles.append(handle)
        return handle

    def cancel_timeout(self, handle: Any) -> None:
        """Cancel an asyncio timeout handle."""
        if handle is not None and hasattr(handle, "cancel"):
            handle.cancel()
        if handle in self._timeout_handles:
            self._timeout_handles.remove(handle)

    def on_finalize(self, block: Block, certificate: CommitCertificate) -> None:
        """Record a finalized block and write the node result."""
        self.state = self.engine.state
        self.finalized[block.header.height] = (block, certificate)
        self.mempool.purge_stale(self.state)
        self._prune_bodies(block.header.height)
        self.write_result()
        if block.header.height >= self.scenario.max_height:
            self.completed.set()
            self.engine.stop()

    def _prune_bodies(self, finalized_height: int) -> None:
        """Forget headers and bodies older than the last BODY_KEEP_HEIGHTS heights.

        Recent bodies are kept so a slower peer can still get them by BODY_REQUEST.
        """
        floor = finalized_height - BODY_KEEP_HEIGHTS
        self._blocks = {d: b for d, b in self._blocks.items() if b.header.height > floor}
        self._headers = {d: h for d, h in self._headers.items() if h.height > floor}
        self._body_waiters = {d: w for d, w in self._body_waiters.items() if d in self._headers}

    def write_result(self) -> Path:
        """Write a canonical result summary for this node.

        ``blocks`` gives, per finalized height, the block hash, the round of the
        proposal (``round``) and of the commit (``commit_round``), the proposer,
        the post-state hash and the transactions: T6 checks that height 2 was
        finalized in round 1 by another proposer. ``equivocators`` lists the
        validators caught equivocating (T7). It holds voter ids, not every
        detected pair: whether a given conflicting vote arrives before or after
        this node finalized that height depends on timing, so the pairs differ
        between runs, while the set of culprits does not (T8). Each detection
        stays in the event log as ``equivocation_detected``.
        """
        finalized_heights = sorted(self.finalized)
        blocks = []
        for height in finalized_heights:
            block, certificate = self.finalized[height]
            blocks.append(
                {
                    "height": height,
                    "hash": certificate.block_hash,
                    "round": block.header.round,
                    "commit_round": certificate.precommits[0].round,
                    "proposer": block.header.proposer,
                    "state_hash": block.header.state_hash,
                    "txs": len(block.txs),
                    "tx_ids": [hash_with_domain("TX", tx) for tx in block.txs],
                }
            )
        equivocators = sorted({first.voter for first, _ in self.engine.votes.equivocations})
        return self.result_writer.write_node(
            self.node_id,
            {
                "node_id": self.node_id,
                "chain_id": self.scenario.chain_id,
                "finalized_height": max(finalized_heights, default=0),
                "state_hash": self.engine.state.state_hash(),
                "block_hashes": [block["hash"] for block in blocks],
                "blocks": blocks,
                "equivocators": equivocators,
            },
        )


class NodeNetwork:
    """Wire C's authenticated TCP, static peer manager, and Gossip to one node."""

    def __init__(self, node: Node) -> None:
        self.node = node
        self.active = asyncio.Event()
        self.transactions_ready = asyncio.Event()
        self.transport = TcpTransport(node.node_id, node.listen_addr, logger=node._event_adapter)
        self.transport.set_handshake(
            handshake_fn(
                node.node_id,
                node.keypair.private_key,
                node.validator_keys,
                advertise_addr=lambda: node.advertise_addr,
                finalized_height=lambda: max(node.finalized, default=0),
                allow_client_txs=True,
            )
        )
        self.transport.set_client_handler(self._serve_client)
        self.gossip = Gossip(node.node_id, self.transport, logger=node._event_adapter)
        self.fault_injector = FaultInjector(
            node.scenario.seed,
            node.scenario.network,
            logger=node._event_adapter,
            timeout_base_ms=min(
                node.scenario.consensus.timeout_propose_ms,
                node.scenario.consensus.timeout_prevote_ms,
                node.scenario.consensus.timeout_precommit_ms,
            ),
            timeout_delta_ms=node.scenario.consensus.timeout_delta_ms,
        )
        self.gossip.sender = self._send_with_faults
        self.peers = StaticPeers(
            node.node_id,
            node.peer_addresses,
            self.transport,
            logger=node._event_adapter,
            retry=0.1,
        )
        for kind in FLOOD | DIRECT:
            self.gossip.on(kind, self._receive)
        node.broadcast_handler = self.gossip.broadcast_nowait
        node.send_handler = self.gossip.send_to_nowait

    async def _send_with_faults(self, peer: str, data: bytes) -> None:
        """Apply deterministic faults to each outgoing link, preserving wire bytes.

        Copies wait independently so their delays are relative to the same send.
        Gossip owns the caller task and cancels pending copies during shutdown.
        """
        message = decode(data)
        delays = self.fault_injector.decide(self.node.node_id, peer, message)

        async def send_copy(delay_ms: int) -> None:
            await asyncio.sleep(delay_ms / 1000)
            await self.transport.send(peer, data)

        results = await asyncio.gather(
            *(send_copy(delay) for delay in delays), return_exceptions=True
        )
        for result in results:
            if isinstance(result, BaseException):
                raise result

    async def _receive(self, message: Message, from_peer: str) -> bool:
        # Gossip keeps the pending messages until consensus is explicitly activated.
        await (self.transactions_ready if message.kind is Kind.TX else self.active).wait()
        return self.node.receive_message(message, from_peer)

    async def _serve_client(self, reader, writer, first_frame: bytes) -> None:
        # Wait for gossip links before acknowledging an injected transaction.
        await asyncio.wait_for(self.transactions_ready.wait(), timeout=10)
        data = first_frame
        while True:
            message = decode(data)
            if message.kind is not Kind.TX:
                await write_frame(
                    writer, encode({"accepted": False, "reason": "expected_tx", "tx_id": ""})
                )
                return
            tx = message.payload
            accepted, reason = self.node.submit_transaction_result(tx)
            result = {"accepted": accepted, "reason": reason, "tx_id": hash_with_domain("TX", tx)}
            self.node._event_adapter("client_tx", height=self.node.engine.height, **result)
            await asyncio.wait_for(write_frame(writer, encode(result)), timeout=10)
            data = await asyncio.wait_for(read_frame(reader), timeout=10)

    async def start(self) -> None:
        # Install StaticPeers' inbound filter before accepting TCP connections.
        await self.peers.start()
        await self.transport.start()

    async def wait_ready(self) -> None:
        expected = set(self.node.peer_addresses) - {self.node.node_id}
        while not expected.issubset(self.transport.peers()):
            await asyncio.sleep(0.02)
        self.transactions_ready.set()

    def activate(self) -> None:
        self.active.set()

    async def close(self) -> None:
        try:
            await self.peers.stop()
        finally:
            try:
                await self.transport.stop()
            finally:
                await self.gossip.stop()
