"""Dựng N node TCP thật trong 1 process cho test network (không phải file test)."""

from __future__ import annotations

import asyncio

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from l1sim.crypto import keypair_from_seed
from l1sim.models import Hello
from l1sim.network.gossip import Gossip
from l1sim.network.hello import handshake_fn
from l1sim.network.static_peers import StaticPeers
from l1sim.network.tcp_transport import TcpTransport

SEED = 42
RETRY = 0.05


class EventLog:
    """Logger giả dùng chung cho cả cluster. Network không truyền `node`, nên mỗi node
    nhận một logger riêng `for_node(id)` tự gắn `node`, giống EventLogger thật."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def __call__(self, name: str, **fields) -> None:
        self.events.append((name, fields))

    def names(self, name: str, node: str | None = None) -> list[dict]:
        return [f for n, f in self.events if n == name and (node is None or f["node"] == node)]

    def for_node(self, node_id: str):
        return lambda name, **fields: self(name, node=node_id, **fields)


def make_keys(n: int) -> tuple[dict[str, Ed25519PrivateKey], dict[str, bytes]]:
    kps = {f"v{i}": keypair_from_seed(SEED, f"v{i}") for i in range(n)}
    sks = {v: kp.private_key for v, kp in kps.items()}
    pks = {v: kp.public_bytes for v, kp in kps.items()}
    return sks, pks


class Node:
    def __init__(self, node_id, sk, pks, log, listen="127.0.0.1:0") -> None:
        self.id = node_id
        log = log.for_node(node_id)
        self.transport = TcpTransport(node_id, listen, logger=log)
        self.hellos: dict[str, Hello] = {}  # HELLO mới nhất của từng peer
        self.transport.set_handshake(
            handshake_fn(
                node_id,
                sk,
                pks,
                advertise_addr=lambda: f"127.0.0.1:{self.transport.port}",
                on_hello=lambda h: self.hellos.__setitem__(h.node_id, h),
            )
        )
        self.gossip = Gossip(node_id, self.transport, logger=log)
        self.peers: StaticPeers | None = None

    async def stop(self) -> None:
        if self.peers is not None:
            await self.peers.stop()
        await self.gossip.stop()
        await self.transport.stop()


class Cluster:
    def __init__(self, n: int) -> None:
        self.log = EventLog()
        self.sks, self.pks = make_keys(n)
        self.nodes = {v: Node(v, self.sks[v], self.pks, self.log) for v in self.sks}
        self.addrs: dict[str, str] = {}

    async def start(self) -> None:
        for node in self.nodes.values():
            await node.transport.start()
            self.addrs[node.id] = f"127.0.0.1:{node.transport.port}"
        for node in self.nodes.values():
            await self.start_peers(node)
        await self.until(self.full_mesh)
        # peer_added của kết nối đi vào được log ở tick sau, chờ log bắt kịp.
        n = len(self.nodes)
        await self.until(lambda: len(self.log.names("peer_added")) == n * (n - 1))

    async def start_peers(self, node: Node) -> None:
        node.peers = StaticPeers(
            node.id, self.addrs, node.transport, self.log.for_node(node.id), retry=RETRY
        )
        await node.peers.start()

    async def restart(self, node_id: str) -> Node:
        """Bật lại node trên đúng cổng cũ, như khi process chết rồi chạy lại."""
        node = Node(node_id, self.sks[node_id], self.pks, self.log, self.addrs[node_id])
        await node.transport.start()
        await self.start_peers(node)
        self.nodes[node_id] = node
        return node

    def full_mesh(self) -> bool:
        n = len(self.nodes)
        return all(len(x.transport.peers()) == n - 1 for x in self.nodes.values())

    async def stop(self) -> None:
        for node in self.nodes.values():
            await node.stop()

    @staticmethod
    async def until(cond, timeout: float = 5.0) -> None:
        async def poll():
            while not cond():
                await asyncio.sleep(0.01)

        await asyncio.wait_for(poll(), timeout)
