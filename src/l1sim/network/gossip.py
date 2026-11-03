"""Gossip: lan message tới mọi node qua transport.

Hai loại message:
- FLOOD (TX, HEADER, VOTE): gửi cho mọi peer; node nhận chuyển tiếp tiếp nếu handler
  chấp nhận. Dedup theo `encoding.message_id(msg)`, nên mỗi node xử lý mỗi message
  đúng 1 lần dù nhận nhiều bản (bản trùng log `gossip_duplicate`; tên `duplicate_ignored`
  dành cho engine khi VoteSet gặp vote đã đếm).
  Message được đánh dấu đã thấy trước khi gọi handler, nên message bị từ chối sẽ không
  bao giờ được xét lại dù bản khác tới sau. Vì vậy handler chỉ được trả False cho
  message sai vĩnh viễn (chữ ký sai, height cũ, nonce cũ), không cho lý do tạm thời.
- DIRECT (BODY_REQUEST, BODY, GET_BLOCKS, BLOCKS): gửi 1-1, không dedup, không chuyển tiếp.
HELLO chỉ dùng trong handshake (hello.py), không đi qua gossip.

HEADER trước BODY: node nhận HEADER hợp lệ mà chưa có body thì handler của tầng trên
gọi send_to(from_peer, Message(BODY_REQUEST, ...)). Gossip chỉ chuyển message, không biết
logic này.

Mọi lần gửi đi qua `self.sender(peer, bytes)`, mặc định là transport.send. Role B thay
bằng FaultInjector để làm trễ, nhân đôi, đảo thứ tự hoặc drop.

Message trên dây = `encoding.encode(Message)` của A (canonical JSON). Bytes không
canonical hoặc không hợp lệ bị `encoding.decode` từ chối.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable

from l1sim.encoding import decode, encode, message_id
from l1sim.models import Kind, Message
from l1sim.network.events import emit
from l1sim.network.tcp_transport import TcpTransport

FLOOD = frozenset({Kind.TX, Kind.HEADER, Kind.VOTE})
DIRECT = frozenset({Kind.BODY_REQUEST, Kind.BODY, Kind.GET_BLOCKS, Kind.BLOCKS})

# handler(msg, from_peer) -> True nếu hợp lệ, False chỉ khi sai vĩnh viễn (xem docstring).
# Có thể là hàm thường hoặc async.
Handler = Callable[[Message, str], "bool | Awaitable[bool]"]
SenderFn = Callable[[str, bytes], Awaitable[None]]

_log = logging.getLogger(__name__)


class Gossip:
    def __init__(self, node_id: str, transport: TcpTransport, logger=None) -> None:
        self.node_id = node_id
        self.transport = transport
        self.logger = logger
        self.sender: SenderFn = transport.send
        self._handlers: dict[Kind, Handler] = {}
        self._seen: set[str] = set()  # message_id của FLOOD đã gặp
        self._tasks: set[asyncio.Task] = set()
        transport.set_receiver(self._on_bytes)

    def on(self, kind: Kind | str, handler: Handler) -> None:
        kind = Kind(kind)
        if kind not in FLOOD and kind not in DIRECT:
            raise ValueError(f"kind không hợp lệ: {kind.value}")
        self._handlers[kind] = handler

    async def broadcast(self, msg: Message) -> str:
        """Gửi message FLOOD cho mọi peer. Trả message_id."""
        mid, data = self._prepare_broadcast(msg)
        await self._send_all(data, exclude=None)
        return mid

    async def send_to(self, peer: str, msg: Message) -> None:
        """Gửi message DIRECT cho 1 peer."""
        await self._send_one(peer, self._prepare_send(peer, msg))

    def broadcast_nowait(self, msg: Message) -> str:
        """Như broadcast nhưng không cần await, hợp với callback `broadcast(message) -> None`
        của ConsensusEngine. Kiểm kind và đánh dấu seen ngay; việc gửi chạy trong task.
        Phải gọi khi event loop đang chạy."""
        mid, data = self._prepare_broadcast(msg)
        self._spawn(self._send_all(data, exclude=None))
        return mid

    def send_to_nowait(self, peer: str, msg: Message) -> None:
        """Như send_to nhưng không cần await."""
        self._spawn(self._send_one(peer, self._prepare_send(peer, msg)))

    async def stop(self) -> None:
        for t in list(self._tasks):
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    # ---------- nội bộ ----------

    def _prepare_broadcast(self, msg: Message) -> tuple[str, bytes]:
        if msg.kind not in FLOOD:
            raise ValueError(f"{msg.kind.value} không phải message FLOOD, dùng send_to")
        mid = message_id(msg)
        self._seen.add(mid)  # bản quay về từ peer sẽ bị bỏ qua
        self._event("gossip_broadcast", kind=msg.kind.value, mid=mid[:16])
        return mid, encode(msg)

    def _prepare_send(self, peer: str, msg: Message) -> bytes:
        if msg.kind not in DIRECT:
            raise ValueError(f"{msg.kind.value} không phải message DIRECT, dùng broadcast")
        self._event("gossip_send", kind=msg.kind.value, peer=peer)
        return encode(msg)

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _on_bytes(self, peer: str, data: bytes) -> None:
        # Receiver của transport là hàm thường; xử lý trong task để handler được phép async.
        self._spawn(self._handle(peer, data))

    async def _handle(self, peer: str, data: bytes) -> None:
        try:
            msg = decode(data)
        except ValueError as e:
            self._event("gossip_malformed", peer=peer, reason=str(e))
            return
        kind = msg.kind.value
        if msg.kind not in FLOOD and msg.kind not in DIRECT:
            self._event("gossip_unexpected_kind", kind=kind, peer=peer)
            return

        mid = message_id(msg)
        if msg.kind in FLOOD:
            # Kiểm và đánh dấu trước lệnh await đầu tiên, nên 2 bản đến sát nhau không lọt.
            if mid in self._seen:
                self._event("gossip_duplicate", kind=kind, peer=peer, mid=mid[:16])
                return
            self._seen.add(mid)

        handler = self._handlers.get(msg.kind)
        if handler is None:
            self._event("gossip_no_handler", kind=kind, peer=peer)
            return
        try:
            ok = handler(msg, peer)
            if inspect.isawaitable(ok):
                ok = await ok
        except Exception:  # lỗi tầng trên không được làm chết gossip
            _log.exception("handler %s lỗi với message từ %s", kind, peer)
            self._event("gossip_handler_error", kind=kind, peer=peer)
            ok = False
        self._event("gossip_recv", kind=kind, peer=peer, mid=mid[:16], accepted=ok is True)

        # Chỉ chuyển tiếp message được chấp nhận: HEADER sai không lan tiếp.
        # Gửi lại đúng bytes đã nhận; decode() bảo đảm chúng là canonical.
        if msg.kind in FLOOD and ok is True:
            await self._send_all(data, exclude=peer)

    async def _send_all(self, data: bytes, exclude: str | None) -> None:
        peers = [p for p in self.transport.peers() if p != exclude]
        await asyncio.gather(*(self._send_one(p, data) for p in peers))

    async def _send_one(self, peer: str, data: bytes) -> None:
        try:
            await self.sender(peer, data)
        except Exception:  # sender do B gắn vào có thể lỗi; không làm hỏng lần gửi khác
            _log.exception("sender lỗi khi gửi tới %s", peer)
            self._event("gossip_send_error", peer=peer)

    def _event(self, event: str, **fields) -> None:
        emit(self.logger, self.node_id, event, **fields)
