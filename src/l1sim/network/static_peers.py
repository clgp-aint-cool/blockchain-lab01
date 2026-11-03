"""StaticPeers: nối full mesh tới các peer có sẵn trong config.

Config `network.peers` = {node_id: "host:port"}, gồm cả chính node này. Thứ tự là
`sorted(peers)` (so chuỗi, nên "V10" < "V2"), không phụ thuộc thứ tự trong dict, nên mọi
node thấy cùng một thứ tự. Node đứng sau chủ động nối tới node đứng trước (v5 nối v0..v4,
v6..v9 nối tới v5). Một cặp node vì vậy chỉ có một bên quay số.

Kết nối đi vào chỉ được nhận từ node đứng sau (inbound filter của transport). Kẻ phát
lại HELLO của node đứng trước sẽ bị từ chối, nên không đá được kết nối thật ra; còn nếu
nó giả node đứng sau, node thật thấy mất kết nối và quay số lại, đá kẻ giả ra.

Mỗi `retry` giây, vòng lặp nối lại peer đứng trước mà chưa có kết nối. Thứ tự bật
node không quan trọng, node chết rồi bật lại cũng được nối lại.

Log: peer_added khi có peer mới (cả đi ra lẫn đi vào), peer_removed khi mất peer.
"""

from __future__ import annotations

import asyncio
import contextlib

from l1sim.network.events import emit
from l1sim.network.tcp_transport import TcpTransport


class StaticPeers:
    def __init__(
        self,
        node_id: str,
        peers: dict[str, str],
        transport: TcpTransport,
        logger=None,
        retry: float = 1.0,
    ) -> None:
        if node_id not in peers:
            raise ValueError(f"{node_id} không có trong network.peers")
        self.node_id = node_id
        self.transport = transport
        self.logger = logger
        self.retry = retry
        order = sorted(peers)
        me = order.index(node_id)
        self._dial = {p: peers[p] for p in order[:me]}  # peer mình phải nối
        self._accept = frozenset(order[me + 1 :])  # peer được nối tới mình
        self._known: set[str] = set()  # peer đã log peer_added
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self.transport.add_disconnect_handler(self._on_lost)
        self.transport.set_inbound_filter(self._accept.__contains__)
        self._task = asyncio.create_task(self._loop(), name=f"peers:{self.node_id}")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _loop(self) -> None:
        while True:
            connected = set(self.transport.peers())
            missing = [p for p in self._dial if p not in connected]
            await asyncio.gather(*(self._connect(p) for p in missing))
            self._sync_added()
            await asyncio.sleep(self.retry)

    async def _connect(self, peer: str) -> None:
        got = await self.transport.connect(self._dial[peer])
        if got is not None and got != peer:
            # Config ghi sai: ở địa chỉ của `peer` lại là node khác.
            self._event("wrong_peer_at_addr", expected=peer, got=got)
            self.transport.close(got)

    def _sync_added(self) -> None:
        # Kết nối đi vào do peer kia quay số, nên dò bằng cách so với danh sách đã biết.
        for p in self.transport.peers():
            if p not in self._known:
                self._known.add(p)
                self._event("peer_added", peer=p)

    def _on_lost(self, peer: str) -> None:
        self._known.discard(peer)
        self._event("peer_removed", peer=peer)

    def _event(self, event: str, **fields) -> None:
        emit(self.logger, self.node_id, event, **fields)
