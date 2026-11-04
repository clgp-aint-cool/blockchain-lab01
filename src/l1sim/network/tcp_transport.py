"""TCP transport giữa các node: framing, kết nối, gửi và nhận bytes.

Transport chỉ chuyển bytes. Nó không biết message là vote hay block, cũng không
tự kiểm chữ ký: việc xác thực peer nằm trong hàm handshake (HELLO) truyền vào.

Mỗi frame trên dây = 4 bytes độ dài (big-endian, unsigned) + nội dung.

Quy ước của bản tối giản:
- Ai nối tới ai là việc của tầng trên (StaticPeers: node đứng sau trong `sorted(peers)`
  nối tới node đứng trước), sao cho một cặp node không bao giờ cùng mở kết nối tới nhau.
  Nếu vẫn có kết nối mới tới peer đã nối (peer khởi động lại), kết nối mới thay kết nối
  cũ. Tầng trên có thể đặt inbound filter để chỉ nhận kết nối đi vào từ peer được phép
  quay số tới mình; filter chạy trước bước thay kết nối (xem hello.py về HELLO replay).
- Mỗi peer chỉ có 1 coroutine: vòng đọc. Gửi là một lệnh `writer.write()`; lệnh này
  chạy đồng bộ, đưa nguyên cả frame vào buffer, nên các frame không bao giờ xen lẫn.
- Mỗi disconnect handler được gọi đúng 1 lần khi mất kết nối; không gọi khi kết nối bị
  thay bằng kết nối mới, hay khi `stop()`. Reconnect là việc của tầng trên. Có thể đăng
  ký nhiều handler theo dõi kết nối, gọi theo thứ tự đăng ký.
"""

from __future__ import annotations

import asyncio
import logging
import struct
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from l1sim.encoding import MAX_MESSAGE_BYTES
from l1sim.network.events import emit

MAX_FRAME = MAX_MESSAGE_BYTES  # 1 frame = 1 message; lớn hơn thì decode() cũng từ chối
HEADER = struct.Struct(">I")
CONNECT_TIMEOUT = 2.0
HANDSHAKE_TIMEOUT = 2.0
MAX_BUFFER = 4 * 1024 * 1024  # peer đọc chậm tới mức buffer gửi vượt ngưỡng này thì bỏ message


@dataclass(frozen=True)
class ClientSession:
    """An inbound TX session, deliberately outside the validator peer table."""

    first_frame: bytes


HandshakeFn = Callable[
    [asyncio.StreamReader, asyncio.StreamWriter, bool], Awaitable["str | ClientSession | None"]
]
ClientHandler = Callable[[asyncio.StreamReader, asyncio.StreamWriter, bytes], Awaitable[None]]
ReceiverFn = Callable[[str, bytes], None]
DisconnectFn = Callable[[str], None]
InboundFilterFn = Callable[[str], bool]

_log = logging.getLogger(__name__)


class FrameTooLarge(Exception):
    """Độ dài frame vượt MAX_FRAME."""


def parse_addr(addr: str) -> tuple[str, int]:
    """'10.147.17.20:9003' → ('10.147.17.20', 9003)."""
    host, sep, port = addr.rpartition(":")
    if not sep or not host:
        raise ValueError(f"địa chỉ không hợp lệ: {addr!r}, cần dạng host:port")
    return host, int(port)


def frame(data: bytes) -> bytes:
    """4 bytes độ dài + data."""
    if len(data) > MAX_FRAME:
        raise FrameTooLarge(len(data))
    return HEADER.pack(len(data)) + data


async def write_frame(writer: asyncio.StreamWriter, data: bytes) -> None:
    """Ghi 1 frame rồi chờ gửi đi. Dùng trong handshake."""
    writer.write(frame(data))
    await writer.drain()


async def read_frame(reader: asyncio.StreamReader) -> bytes:
    """Đọc 1 frame. Ném FrameTooLarge nếu độ dài > MAX_FRAME, IncompleteReadError nếu peer đóng."""
    (length,) = HEADER.unpack(await reader.readexactly(HEADER.size))
    if length > MAX_FRAME:
        raise FrameTooLarge(length)
    return await reader.readexactly(length)


class TcpTransport:
    def __init__(self, node_id: str, listen_addr: str, logger=None) -> None:
        self.node_id = node_id
        self.listen_addr = listen_addr
        self.logger = logger
        self.port: int | None = None  # port thật sau start(), hữu ích khi bind port 0

        self._server: asyncio.base_events.Server | None = None
        self._writers: dict[str, asyncio.StreamWriter] = {}  # peer_id → writer
        self._tasks: dict[str, asyncio.Task] = {}  # peer_id → vòng đọc
        self._handshake: HandshakeFn | None = None
        self._receiver: ReceiverFn | None = None
        self._on_disconnect: list[DisconnectFn] = []
        self._inbound_filter: InboundFilterFn | None = None
        self._client_handler: ClientHandler | None = None
        self._client_tasks: set[asyncio.Task] = set()
        self._client_writers: set[asyncio.StreamWriter] = set()
        self._stopping = False

    # ---------- cấu hình ----------

    def set_handshake(self, fn: HandshakeFn) -> None:
        """fn(reader, writer, outbound) -> peer_id nếu HELLO hợp lệ, None nếu không."""
        self._handshake = fn

    def set_receiver(self, callback: ReceiverFn) -> None:
        """callback(peer_id, data) cho mỗi frame nhận được sau handshake."""
        self._receiver = callback

    def set_client_handler(self, callback: ClientHandler) -> None:
        """Serve inbound TX sessions without registering or replacing validator peers."""
        self._client_handler = callback

    def add_disconnect_handler(self, callback: DisconnectFn) -> None:
        """Thêm callback(peer_id), gọi khi mất kết nối tới peer. Không đè handler đã có."""
        self._on_disconnect.append(callback)

    def set_inbound_filter(self, fn: InboundFilterFn) -> None:
        """fn(peer_id) -> False thì từ chối kết nối đi vào từ peer đó (sau HELLO hợp lệ)."""
        self._inbound_filter = fn

    # ---------- API ----------

    async def start(self) -> None:
        if self._handshake is None:
            raise RuntimeError("gọi set_handshake() trước start()")
        host, port = parse_addr(self.listen_addr)
        self._server = await asyncio.start_server(self._on_inbound, host, port, reuse_address=True)
        self.port = self._server.sockets[0].getsockname()[1]
        self._event("listen", addr=f"{host}:{self.port}")

    async def connect(self, addr: str) -> str | None:
        """Mở kết nối tới addr và handshake. Trả peer_id nếu thành công, None nếu không."""
        if self._handshake is None:
            raise RuntimeError("gọi set_handshake() trước connect()")
        host, port = parse_addr(addr)
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), CONNECT_TIMEOUT
            )
        except (OSError, asyncio.TimeoutError) as e:
            self._event("connect_failed", addr=addr, reason=type(e).__name__)
            return None
        return await self._attach(reader, writer, outbound=True)

    async def send(self, peer_id: str, data: bytes) -> None:
        """Đưa frame vào buffer gửi của peer; không chờ gửi xong."""
        writer = self._writers.get(peer_id)
        if writer is None or writer.is_closing():
            self._event("send_no_peer", peer=peer_id)
            return
        if writer.transport.get_write_buffer_size() > MAX_BUFFER:
            # Peer đọc quá chậm. Bỏ message thay vì để RAM phình; consensus chịu được mất message.
            self._event("send_buffer_full", peer=peer_id)
            return
        try:
            writer.write(frame(data))
        except FrameTooLarge:
            self._event("send_too_large", peer=peer_id, size=len(data))

    def close(self, peer_id: str) -> None:
        if peer_id in self._writers:
            self._drop(peer_id, "closed_by_local")

    def peers(self) -> list[str]:
        return sorted(self._writers)

    async def stop(self) -> None:
        self._stopping = True
        if self._server is not None:
            self._server.close()
        writers = list(self._writers.values())
        tasks = list(self._tasks.values())
        client_tasks = list(self._client_tasks)
        client_writers = list(self._client_writers)
        for task in client_tasks:
            task.cancel()
        for writer in client_writers:
            writer.close()
        for peer_id in list(self._writers):
            self._drop(peer_id, "stop", notify=False)
        await asyncio.gather(*tasks, *client_tasks, return_exceptions=True)
        for writer in writers + client_writers:
            try:
                await writer.wait_closed()
            except (OSError, ConnectionError):
                pass
        self._client_tasks.clear()
        self._client_writers.clear()
        # Python >= 3.12.1: wait_closed() chờ mọi kết nối đi vào đóng hẳn,
        # nên phải đóng kết nối ở trên trước, không thì treo mãi.
        if self._server is not None:
            await self._server.wait_closed()

    # ---------- nội bộ ----------

    async def _on_inbound(self, reader, writer) -> None:
        await self._attach(reader, writer, outbound=False)

    async def _attach(self, reader, writer, outbound: bool) -> str | None:
        """Handshake rồi gắn kết nối vào peer_id. Dùng chung cho kết nối đi ra và đi vào."""
        addr = _peername(writer)
        reason = "rejected"
        try:
            peer_id = await asyncio.wait_for(
                self._handshake(reader, writer, outbound), HANDSHAKE_TIMEOUT
            )
        except asyncio.TimeoutError:
            peer_id, reason = None, "handshake_timeout"
        except Exception as e:  # HELLO hỏng, peer đóng giữa chừng, ...
            peer_id, reason = None, getattr(e, "reason", type(e).__name__)
        if isinstance(peer_id, ClientSession):
            if outbound or self._stopping or self._client_handler is None:
                writer.close()
                return None
            self._client_writers.add(writer)
            task = asyncio.create_task(self._serve_client(reader, writer, peer_id.first_frame))
            self._client_tasks.add(task)
            task.add_done_callback(self._client_tasks.discard)
            return None
        if peer_id == self.node_id:
            peer_id, reason = None, "self_connect"
        elif peer_id is not None and not outbound and self._inbound_filter is not None:
            if not self._inbound_filter(peer_id):
                # Phải chặn trước bước thay kết nối, nếu không HELLO bị replay sẽ đá kết
                # nối thật của peer này ra.
                peer_id, reason = None, "unexpected_inbound"
        if peer_id is None or self._stopping:
            self._event("handshake_failed", addr=addr, outbound=outbound, reason=reason)
            writer.close()
            return None

        if peer_id in self._writers:  # peer khởi động lại → kết nối mới thay kết nối cũ
            self._drop(peer_id, "replaced", notify=False)
        self._writers[peer_id] = writer
        self._tasks[peer_id] = asyncio.create_task(
            self._read_loop(peer_id, reader, writer), name=f"read:{peer_id}"
        )
        self._event("conn_open", peer=peer_id, outbound=outbound, addr=addr)
        return peer_id

    async def _serve_client(self, reader, writer, first_frame: bytes) -> None:
        self._event("client_open", addr=_peername(writer))
        try:
            await self._client_handler(reader, writer, first_frame)
        except asyncio.IncompleteReadError as exc:
            if exc.partial:
                self._event("client_error", reason="incomplete_frame")
        except (OSError, asyncio.TimeoutError, ValueError, FrameTooLarge) as exc:
            self._event("client_error", reason=type(exc).__name__)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
            finally:
                self._client_writers.discard(writer)
                self._event("client_closed")

    async def _read_loop(self, peer_id: str, reader, writer) -> None:
        reason = "eof"
        try:
            while True:
                data = await read_frame(reader)
                if self._receiver is None:
                    continue
                try:
                    self._receiver(peer_id, data)
                except Exception:  # lỗi ở tầng trên không được làm rớt kết nối
                    _log.exception("receiver lỗi khi xử lý frame từ %s", peer_id)
                    self._event("receiver_error", peer=peer_id)
        except asyncio.IncompleteReadError:
            reason = "eof"
        except FrameTooLarge as e:
            reason = f"frame_too_large:{e.args[0]}"
        except (ConnectionError, OSError) as e:
            reason = type(e).__name__
        # Chỉ drop nếu đây vẫn là kết nối hiện tại của peer (chưa bị thay hay đóng).
        if self._writers.get(peer_id) is writer:
            self._drop(peer_id, reason)

    def _drop(self, peer_id: str, reason: str, notify: bool = True) -> None:
        writer = self._writers.pop(peer_id)
        task = self._tasks.pop(peer_id, None)
        if task is not None and task is not asyncio.current_task():
            task.cancel()
        writer.close()
        self._event("conn_closed", peer=peer_id, reason=reason)
        if not notify or self._stopping:
            return
        for handler in list(self._on_disconnect):
            try:
                handler(peer_id)
            except Exception:  # 1 handler lỗi không được chặn các handler sau
                _log.exception("disconnect handler lỗi với %s", peer_id)

    def _event(self, event: str, **fields) -> None:
        emit(self.logger, self.node_id, event, **fields)


def _peername(writer: asyncio.StreamWriter) -> str:
    peer = writer.get_extra_info("peername")
    return f"{peer[0]}:{peer[1]}" if peer else "?"
