"""DevNode: ghép network (C) + ConsensusEngine (D) + execution (B) thành 1 validator chạy thật.

Bản ghép tạm để test và demo, chờ `l1sim/node.py` của A. Không sửa module nào của nhóm.

- Callback của engine: broadcast → gossip, timeout → loop.call_later, logger → NodeLog
  (Recorder cho dashboard + `EventLogger` JSONL của nhóm nếu chạy theo scenario).
- Header trước body (TODO ở engine.py `_propose`): engine phát BODY, `_out` giữ body lại và
  chỉ flood HEADER. Node nhận HEADER xin body thẳng từ proposer (BODY_REQUEST), proposer
  trả BODY riêng cho node đó, rồi node mới gọi `engine.on_proposal(block)`.
- `block_ms`: proposer giữ HEADER thêm `block_ms` trước khi phát, để chain chạy đủ chậm
  cho mắt người theo dõi trên dashboard. Test đặt 0.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections import Counter, deque
from collections.abc import Mapping
from dataclasses import dataclass, field

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from l1sim.consensus.engine import ConsensusEngine, TimeoutConfig, proposer_for
from l1sim.crypto import KeyPair, keypair_from_seed, verify
from l1sim.encoding import EncodingError, block_hash
from l1sim.execution import Mempool, State, validate_tx
from l1sim.logger import EventLogger
from l1sim.models import Block, BlockHeader, BodyRequest, Domain, Kind, Message, Tx
from l1sim.network.gossip import Gossip
from l1sim.network.hello import handshake_fn
from l1sim.network.static_peers import StaticPeers
from l1sim.network.tcp_transport import TcpTransport

# Event quá dày (mỗi message 1 dòng) không đưa vào feed và file log, chỉ đếm.
# gossip_duplicate: full mesh 10 node thì mỗi message flood tới 9 lần, 8 lần là trùng.
# (duplicate_ignored của engine, vote đã đếm, vẫn được ghi.)
NOISY = frozenset({"gossip_broadcast", "gossip_send", "gossip_recv", "gossip_duplicate"})
BODY_RETRY_S = 0.3
BODY_MAX_TRIES = 10


@dataclass
class Keys:
    """Key của validator và user, sinh từ seed nên lần chạy nào cũng giống nhau."""

    validators: dict[str, KeyPair]
    users: dict[str, KeyPair]

    @classmethod
    def make(cls, validators: int | list[str], users: int | list[str] = 5, seed: int = 42) -> Keys:
        """Key sinh bằng `keypair_from_seed(seed, id)`, giống genesis.json và TxClient.

        validators: số node (id V0..V{n-1}, như genesis) hoặc danh sách id.
        users: số user (u0..) hoặc danh sách tên (vd sender trong scenario).
        """
        vids = [f"V{i}" for i in range(validators)] if isinstance(validators, int) else validators
        uids = [f"u{i}" for i in range(users)] if isinstance(users, int) else users
        return cls(
            {v: keypair_from_seed(seed, v) for v in vids},
            {u: keypair_from_seed(seed, u) for u in uids},
        )

    @property
    def validator_pks(self) -> dict[str, Ed25519PublicKey]:
        return {v: kp.public_key for v, kp in self.validators.items()}

    @property
    def user_pks(self) -> dict[str, Ed25519PublicKey]:
        return {u: kp.public_key for u, kp in self.users.items()}


class Recorder:
    """Logger dùng chung cho engine và network: `recorder(event, **fields)`."""

    def __init__(self, maxlen: int = 5000) -> None:
        self.events: deque[dict] = deque(maxlen=maxlen)
        self.counts: Counter[str] = Counter()
        self.seq = 0
        self._t0 = time.monotonic()

    def __call__(self, event: str, **fields) -> None:
        self.counts[event] += 1
        if event in NOISY:
            return
        self.seq += 1
        t_ms = int((time.monotonic() - self._t0) * 1000)
        self.events.append({"seq": self.seq, "t_ms": t_ms, "event": event, **fields})

    def since(self, seq: int, limit: int = 500) -> list[dict]:
        out = [e for e in self.events if e["seq"] > seq]
        return out[-limit:]


class NodeLog:
    """Logger của 1 node, đưa cho engine và network.

    Engine và network cùng gọi `logger(event, **fields)` và không gửi `node`. Ở đây tự gắn
    id của node rồi ghi vào Recorder (dashboard) và, nếu có, EventLogger
    (logs/events/<scenario>/<node>.jsonl, logger chính thức của nhóm). `node` đến từ
    caller (vd `body_unrequested` trong file này) bị bỏ, vì id lấy từ chính node.
    """

    def __init__(self, node_id: str, recorder: Recorder, file: EventLogger | None = None) -> None:
        self.node_id = node_id
        self.recorder = recorder
        self.file = file

    def __call__(self, event: str, **fields) -> None:
        fields.pop("node", None)
        self.recorder(event, node=self.node_id, **fields)
        if self.file is None or event in NOISY:
            return
        height = fields.pop("height", 0)
        try:
            self.file.event(event, height=height, **fields)
        except (ValueError, TypeError, EncodingError) as e:
            self.recorder("log_write_error", node=self.node_id, for_event=event, error=str(e))


@dataclass
class Faults:
    """Fault giả cho gossip.sender (thay cho FaultInjector của B), kèm đếm lưu lượng."""

    drop_rate: float = 0.0
    delay_ms: int = 0  # trễ ngẫu nhiên trong [min_delay_ms, delay_ms]
    seed: int = 42
    min_delay_ms: int = 0
    sent: Counter = field(default_factory=Counter)  # (src, dst) -> số message
    dropped: int = 0

    def __post_init__(self) -> None:
        self._rng = random.Random(self.seed)

    def sender(self, src: str, transport: TcpTransport):
        async def send(peer: str, data: bytes) -> None:
            self.sent[(src, peer)] += 1
            if self.drop_rate and self._rng.random() < self.drop_rate:
                self.dropped += 1
                return
            if self.delay_ms:
                lo = min(self.min_delay_ms, self.delay_ms)
                await asyncio.sleep(self._rng.uniform(lo, self.delay_ms) / 1000)
            await transport.send(peer, data)

        return send


@dataclass(frozen=True)
class NodeConfig:
    timeouts: TimeoutConfig
    max_txs_per_block: int = 50
    block_ms: int = 0
    retry: float = 0.2
    mesh_wait: float = 3.0


class DevNode:
    def __init__(
        self,
        node_id: str,
        keys: Keys,
        cfg: NodeConfig,
        recorder: Recorder,
        faults: Faults,
        listen: str = "127.0.0.1:0",
        on_finalize=None,
        event_log: EventLogger | None = None,
    ) -> None:
        self.id = node_id
        self.keys = keys
        self.cfg = cfg
        self.log = NodeLog(node_id, recorder, event_log)
        self.faults = faults
        self._on_finalize_cb = on_finalize
        self._validators = sorted(keys.validators)
        self._vpks = keys.validator_pks
        self._upks = keys.user_pks

        self.transport = TcpTransport(node_id, listen, logger=self.log)
        self.transport.set_handshake(
            handshake_fn(
                node_id,
                keys.validators[node_id].private_key,
                self._vpks,
                advertise_addr=lambda: f"127.0.0.1:{self.transport.port}",
                finalized_height=lambda: self.engine.height - 1 if self.engine else 0,
            )
        )
        self.gossip = Gossip(node_id, self.transport, logger=self.log)
        self.gossip.sender = faults.sender(node_id, self.transport)
        for kind, fn in (
            (Kind.TX, self._on_tx),
            (Kind.HEADER, self._on_header),
            (Kind.VOTE, self._on_vote),
            (Kind.BODY_REQUEST, self._on_body_request),
            (Kind.BODY, self._on_body),
        ):
            self.gossip.on(kind, fn)

        self.mempool = Mempool()
        self.engine: ConsensusEngine | None = None
        self.peers: StaticPeers | None = None
        self.bodies: dict[str, Block] = {}  # block_hash -> block (của mình hoặc đã nhận)
        self.requested: set[str] = set()  # block_hash đã gửi BODY_REQUEST
        self.alive = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._boot: asyncio.Task | None = None
        self._pending: set[asyncio.TimerHandle] = set()
        # Mở thì engine được chạy. Scenario đóng lại để gửi tx trước height 1 (xem Devnet.start).
        self.go = asyncio.Event()

    # ------------------------------------------------------------ vòng đời

    async def listen(self) -> str:
        """Bước 1: mở cổng. Trả địa chỉ thật để đưa vào bảng peers."""
        await self.transport.start()
        return f"127.0.0.1:{self.transport.port}"

    async def start(self, addrs: Mapping[str, str]) -> None:
        """Bước 2: nối peer, đợi đủ mesh (hoặc hết mesh_wait) rồi chạy engine."""
        self._loop = asyncio.get_running_loop()
        self.peers = StaticPeers(self.id, dict(addrs), self.transport, self.log, self.cfg.retry)
        await self.peers.start()
        self.engine = ConsensusEngine(
            node_id=self.id,
            keypair=self.keys.validators[self.id],
            validator_keys=self._vpks,
            timeouts=self.cfg.timeouts,
            state=State(),
            mempool=self.mempool,
            max_txs_per_block=self.cfg.max_txs_per_block,
            broadcast=self._out,
            schedule_timeout=self._schedule,
            cancel_timeout=self._cancel,
            on_finalize=self._finalized,
            logger=self.log,
            user_keys=self._upks,
        )
        self.alive = True
        self._boot = asyncio.create_task(self._boot_engine())

    async def _boot_engine(self) -> None:
        deadline = time.monotonic() + self.cfg.mesh_wait
        while len(self.transport.peers()) < len(self._validators) - 1:
            if time.monotonic() > deadline:
                break
            await asyncio.sleep(0.05)
        await self.go.wait()
        self.log("engine_start", node=self.id, peers=len(self.transport.peers()))
        self.engine.start()

    async def stop(self) -> None:
        self.alive = False
        if self._boot is not None:
            self._boot.cancel()
        for h in list(self._pending):
            h.cancel()
        self._pending.clear()
        if self.peers is not None:
            await self.peers.stop()
        await self.gossip.stop()
        await self.transport.stop()
        self.log("node_stopped", node=self.id)

    # ------------------------------------------------------ callback engine

    def _out(self, msg: Message) -> None:
        if not self.alive:
            return
        if msg.kind is Kind.BODY:  # header trước body
            block: Block = msg.payload
            self.bodies[block_hash(block.header)] = block
            header_msg = Message(Kind.HEADER, self.id, msg.height, block.header)
            if self.cfg.block_ms:
                self._later(self.cfg.block_ms / 1000, self._flood, header_msg)
            else:
                self._flood(header_msg)
            return
        self._flood(msg)

    def _flood(self, msg: Message) -> None:
        if self.alive:
            self.gossip.broadcast_nowait(msg)

    def _later(self, delay: float, fn, *args) -> asyncio.TimerHandle:
        def run() -> None:
            self._pending.discard(handle)
            if self.alive:
                fn(*args)

        handle = self._loop.call_later(delay, run)
        self._pending.add(handle)
        return handle

    def _schedule(self, delay_ms: int, height: int, round: int, step):
        return self._later(delay_ms / 1000, self.engine.on_timeout, height, round, step)

    def _cancel(self, handle: asyncio.TimerHandle) -> None:
        handle.cancel()
        self._pending.discard(handle)

    def _finalized(self, block: Block, cert) -> None:
        h = block.header.height
        # Giữ body 20 height gần nhất để còn trả lời BODY_REQUEST của node chậm.
        self.bodies = {k: b for k, b in self.bodies.items() if b.header.height > h - 20}
        if self._on_finalize_cb is not None:
            self._on_finalize_cb(self, block, cert)

    # --------------------------------------------------------- handler mạng

    def _on_tx(self, msg: Message, peer: str) -> bool:
        return self._accept_tx(msg.payload, via=peer)

    def _accept_tx(self, tx: Tx, via: str) -> bool:
        ok, reason = validate_tx(self.engine.state, tx, self._upks)
        # Nonce lớn hơn nonce hiện tại vẫn nhận (tx nối tiếp của cùng user); nhỏ hơn là replay.
        if not ok and reason == "bad_nonce" and tx.nonce > self.engine.state.get_nonce(tx.sender):
            ok = True
        if not ok:
            self.log(
                "tx_rejected",
                node=self.id,
                reason=reason,
                sender=tx.sender,
                nonce=tx.nonce,
                via=via,
            )
            return False
        return self.mempool.add(tx)

    def submit_tx(self, tx: Tx) -> bool:
        """Client gửi tx vào node này: nhận vào mempool rồi flood cho mạng."""
        if not self.alive or self.engine is None:
            return False
        if not self._accept_tx(tx, via="client"):
            return False
        self.gossip.broadcast_nowait(Message(Kind.TX, self.id, self.engine.height, tx))
        return True

    def _on_vote(self, msg: Message, peer: str) -> bool:
        self.engine.on_vote(msg.payload)
        return True

    def _on_header(self, msg: Message, peer: str) -> bool:
        header: BlockHeader = msg.payload
        pk = self._vpks.get(header.proposer)
        if pk is None or not verify(pk, Domain.HEADER, header, header.signature):
            self.log("header_rejected", node=self.id, peer=peer, proposer=header.proposer)
            return False
        if header.height < self.engine.height:
            return False  # height đã xong, không lan tiếp
        digest = block_hash(header)
        if digest not in self.bodies and digest not in self.requested:
            self.requested.add(digest)
            self._request_body(header, digest, attempt=0)
        return True

    def _request_body(self, header: BlockHeader, digest: str, attempt: int) -> None:
        """Xin body; chưa có sau BODY_RETRY_S thì xin lại, lần lượt proposer rồi các peer khác.

        BODY_REQUEST/BODY là message 1-1, mất là mất hẳn (khác HEADER/VOTE được flood nhiều
        đường), nên phải tự xin lại. Node nào đã nhận body cũng trả lời được.
        """
        if digest not in self.requested or header.height < self.engine.height:
            return
        peers = self.transport.peers()
        if not peers or attempt >= BODY_MAX_TRIES:
            return
        # Lần đầu xin thẳng proposer (full mesh); sau đó xoay vòng qua các peer.
        order = [header.proposer] + sorted(p for p in peers if p != header.proposer)
        order = [p for p in order if p in peers]
        target = order[attempt % len(order)]
        self.gossip.send_to_nowait(
            target, Message(Kind.BODY_REQUEST, self.id, header.height, BodyRequest(digest))
        )
        self._later(BODY_RETRY_S, self._request_body, header, digest, attempt + 1)

    def _on_body_request(self, msg: Message, peer: str) -> bool:
        block = self.bodies.get(msg.payload.block_hash)
        if block is not None:
            self.gossip.send_to_nowait(
                peer, Message(Kind.BODY, self.id, block.header.height, block)
            )
        return False

    def _on_body(self, msg: Message, peer: str) -> bool:
        block: Block = msg.payload
        digest = block_hash(block.header)
        if digest not in self.requested:
            if digest not in self.bodies:  # body đã có: chỉ là trả lời trễ của lần xin lại
                self.log("body_unrequested", node=self.id, peer=peer)
            return False
        self.requested.discard(digest)
        self.bodies[digest] = block
        self.engine.on_proposal(block)
        return False

    # ------------------------------------------------------------ dashboard

    def snapshot(self) -> dict:
        e = self.engine
        if e is None:
            return {"id": self.id, "alive": self.alive}
        return {
            "id": self.id,
            "alive": self.alive,
            "height": e.height,
            "round": e.round,
            "step": e.step.value,
            "proposer": proposer_for(self._validators, e.height, e.round),
            "peers": sorted(self.transport.peers()) if self.alive else [],
            "mempool": len(self.mempool),
            "state_hash": e.state.state_hash(),
            "locked_round": e.locked_round,
        }
