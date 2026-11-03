import asyncio

import pytest

from l1sim.encoding import decode, encode
from l1sim.models import (
    GENESIS_HASH,
    Block,
    BlockHeader,
    BodyRequest,
    Kind,
    Message,
    Phase,
    Tx,
    Vote,
)
from l1sim.network.hello import make_hello
from tests.unit.p2p_cluster import Cluster


def _tx(value: str, origin: str = "v0") -> Message:
    return Message(Kind.TX, origin, 0, Tx("alice", "alice/k", value, 0))


def _vote(height: int = 1) -> Message:
    return Message(Kind.VOTE, "v0", height, Vote(Phase.PREVOTE, height, 0, None, "v0"))


def _header(height: int = 1, proposer: str = "v0") -> Message:
    header = BlockHeader(height, 0, GENESIS_HASH, "1" * 64, "2" * 64, proposer)
    return Message(Kind.HEADER, "v0", height, header)


def _record(c: Cluster, kind: Kind, accept: bool = True) -> dict[str, list]:
    """Gắn handler cho mọi node, trả dict node → danh sách (msg, from_peer)."""
    got: dict[str, list] = {v: [] for v in c.nodes}
    for v, node in c.nodes.items():

        def handler(msg, peer, v=v):
            got[v].append((msg, peer))
            return accept

        node.gossip.on(kind, handler)
    return got


def _cut(c: Cluster, a: str, b: str) -> None:
    """Giả lập cạnh a→b bị drop: sender của a bỏ mọi message tới b (chỗ B gắn FaultInjector)."""
    node = c.nodes[a]
    real = node.gossip.sender

    async def sender(peer, data):
        if peer != b:
            await real(peer, data)

    node.gossip.sender = sender


def test_wire_format_is_team_encoding():
    msg = _tx("1")
    assert decode(encode(msg)) == msg
    with pytest.raises(ValueError):
        decode(b'{"kind":"NOPE","origin":"v0","height":0,"payload":{}}')
    with pytest.raises(ValueError):
        decode(b"garbage")


def test_broadcast_reaches_every_node_once():
    async def main():
        c = Cluster(10)
        await c.start()
        got = _record(c, Kind.TX)
        await c.nodes["v0"].gossip.broadcast(_tx("1"))
        await c.until(lambda: all(len(got[v]) == 1 for v in got if v != "v0"))
        await asyncio.sleep(0.2)  # chờ các bản chuyển tiếp tới hết
        dups = len(c.log.names("gossip_duplicate"))
        await c.stop()
        return got, dups

    got, dups = asyncio.run(main())
    assert got["v0"] == []  # node phát không tự gọi handler của mình
    # Mỗi node nhận đúng 1 lần; bản tới trước có thể là bản chuyển tiếp nên không kiểm from_peer.
    assert all([m for m, _ in got[v]] == [_tx("1")] for v in got if v != "v0")
    assert dups > 0  # full mesh: các bản chuyển tiếp đều bị dedup


def test_same_bytes_twice_is_ignored():
    async def main():
        c = Cluster(2)
        await c.start()
        got = _record(c, Kind.VOTE)
        data = encode(_vote())
        await c.nodes["v0"].transport.send("v1", data)
        await c.nodes["v0"].transport.send("v1", data)
        await c.until(lambda: len(c.log.names("gossip_duplicate", node="v1")) == 1)
        await c.stop()
        return got["v1"]

    assert asyncio.run(main()) == [(_vote(), "v0")]


def test_non_canonical_bytes_rejected():
    async def main():
        c = Cluster(2)
        await c.start()
        got = _record(c, Kind.TX)
        await c.nodes["v0"].transport.send("v1", encode(_tx("1")) + b" ")
        await c.until(lambda: len(c.log.names("gossip_malformed", node="v1")) == 1)
        await c.stop()
        return got["v1"]

    assert asyncio.run(main()) == []


def test_hello_after_handshake_is_not_gossiped():
    async def main():
        c = Cluster(2)
        await c.start()
        data = make_hello("v0", c.sks["v0"], c.addrs["v0"])
        await c.nodes["v0"].transport.send("v1", data)
        await c.until(lambda: len(c.log.names("gossip_unexpected_kind", node="v1")) == 1)
        await c.stop()

    asyncio.run(main())


def test_forwarding_routes_around_dropped_link():
    async def main():
        c = Cluster(6)
        await c.start()
        _cut(c, "v0", "v5")
        got = _record(c, Kind.HEADER)
        await c.nodes["v0"].gossip.broadcast(_header())
        await c.until(lambda: len(got["v5"]) == 1)
        await c.stop()
        return got["v5"]

    msg, via = asyncio.run(main())[0]
    assert msg == _header() and via != "v0"  # tới v5 qua một node khác


def test_rejected_message_is_not_forwarded():
    async def main():
        c = Cluster(6)
        await c.start()
        _cut(c, "v0", "v5")
        got = _record(c, Kind.HEADER, accept=False)
        await c.nodes["v0"].gossip.broadcast(_header(proposer="bad"))
        await c.until(lambda: all(len(got[v]) == 1 for v in ["v1", "v2", "v3", "v4"]))
        await asyncio.sleep(0.2)
        await c.stop()
        return got["v5"]

    assert asyncio.run(main()) == []


def test_send_to_reaches_only_target():
    req = Message(Kind.BODY_REQUEST, "v0", 0, BodyRequest("ab" * 32))

    async def main():
        c = Cluster(4)
        await c.start()
        got = _record(c, Kind.BODY_REQUEST)
        await c.nodes["v0"].gossip.send_to("v2", req)
        await c.until(lambda: len(got["v2"]) == 1)
        await asyncio.sleep(0.1)
        await c.stop()
        return got

    got = asyncio.run(main())
    assert got["v2"] == [(req, "v0")]
    assert got["v1"] == got["v3"] == []


def test_wrong_kind_for_api_rejected():
    body = Message(Kind.BODY, "v0", 1, Block(_header().payload, ()))

    async def main():
        c = Cluster(2)
        await c.start()
        g = c.nodes["v0"].gossip
        errors = []
        for call in (g.broadcast(body), g.send_to("v1", _tx("1"))):
            try:
                await call
            except ValueError:
                errors.append(True)
        await c.stop()
        return errors

    assert asyncio.run(main()) == [True, True]
    with pytest.raises(ValueError):
        Cluster(1).nodes["v0"].gossip.on(Kind.HELLO, lambda m, p: True)


def test_handler_error_does_not_break_gossip():
    async def main():
        c = Cluster(2)
        await c.start()
        got = []

        def handler(msg, peer):
            if msg.payload.value == "boom":
                raise RuntimeError("bug ở tầng trên")
            got.append(msg.payload.value)
            return True

        c.nodes["v1"].gossip.on(Kind.TX, handler)
        await c.nodes["v0"].gossip.broadcast(_tx("boom"))
        await c.nodes["v0"].gossip.broadcast(_tx("ok"))
        await c.until(lambda: got == ["ok"])
        await c.stop()

    asyncio.run(main())


def test_nowait_api_for_engine_callback():
    # ConsensusEngine gọi broadcast(message) -> None, không await được.
    async def main():
        c = Cluster(3)
        await c.start()
        got = _record(c, Kind.VOTE)
        g = c.nodes["v0"].gossip
        g.broadcast_nowait(_vote())
        req = Message(Kind.BODY_REQUEST, "v0", 0, BodyRequest("cd" * 32))
        reqs = _record(c, Kind.BODY_REQUEST)
        g.send_to_nowait("v2", req)
        await c.until(lambda: len(got["v1"]) == len(got["v2"]) == 1 and len(reqs["v2"]) == 1)
        errors = 0
        for bad in (lambda: g.broadcast_nowait(req), lambda: g.send_to_nowait("v1", _vote())):
            try:
                bad()
            except ValueError:
                errors += 1
        await c.stop()
        return got["v0"], errors

    own, errors = asyncio.run(main())
    assert own == [] and errors == 2  # không tự gọi handler của mình; sai kind ném lỗi ngay
