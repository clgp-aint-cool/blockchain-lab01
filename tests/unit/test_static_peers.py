import asyncio

from l1sim.network.static_peers import StaticPeers
from l1sim.network.tcp_transport import TcpTransport
from tests.unit.p2p_cluster import Cluster


def test_ten_nodes_form_full_mesh():
    async def main():
        c = Cluster(10)
        await c.start()  # chờ tới khi mọi node có 9 peer và log đủ 90 peer_added
        outbound = [f for f in c.log.names("conn_open") if f["outbound"]]
        await c.stop()
        return outbound

    outbound = asyncio.run(main())
    # 45 kết nối, mỗi kết nối do node đứng sau quay số tới node đứng trước.
    assert len(outbound) == 45
    assert all(f["node"] > f["peer"] for f in outbound)


def test_start_order_does_not_matter():
    async def main():
        c = Cluster(4)
        for node in c.nodes.values():  # biết port trước, nhưng chưa ai nghe
            await node.transport.start()
            c.addrs[node.id] = f"127.0.0.1:{node.transport.port}"
        # v3 bắt đầu quay số trước, v0 bật StaticPeers sau cùng: vòng thử lại lo phần còn lại.
        for v in ["v3", "v2", "v1", "v0"]:
            await c.start_peers(c.nodes[v])
            await asyncio.sleep(0.02)
        await c.until(c.full_mesh)
        await c.stop()

    asyncio.run(main())


def test_dead_node_removed_then_reconnected():
    async def main():
        c = Cluster(5)
        await c.start()
        await c.nodes["v2"].stop()
        others = [v for v in c.nodes if v != "v2"]
        await c.until(lambda: len(c.log.names("peer_removed")) == 4)
        removed = sorted(f["node"] for f in c.log.names("peer_removed"))
        assert all(c.nodes[v].transport.peers() == sorted(set(others) - {v}) for v in others)

        added_before = len(c.log.names("peer_added"))
        await c.restart("v2")
        await c.until(c.full_mesh)
        # v2 thấy 4 peer, mỗi node còn lại thấy lại v2.
        await c.until(lambda: len(c.log.names("peer_added")) == added_before + 8)
        await c.stop()
        return removed

    assert asyncio.run(main()) == ["v0", "v1", "v3", "v4"]


def test_node_must_be_in_config():
    t = TcpTransport("v9", "127.0.0.1:0")
    try:
        StaticPeers("v9", {"v0": "127.0.0.1:1"}, t)
    except ValueError:
        return
    raise AssertionError("phải báo lỗi khi node không có trong config")


def test_dial_order_is_sorted_not_dict_order():
    t = TcpTransport("v2", "127.0.0.1:0")
    sp = StaticPeers("v2", {"v3": "h:3", "v1": "h:1", "v2": "h:2", "v0": "h:0"}, t)
    assert sorted(sp._dial) == ["v0", "v1"]
    assert sp._accept == {"v3"}


def test_replayed_hello_cannot_cut_a_real_peer():
    """Kẻ tấn công phát lại HELLO của v0 (node đứng trước) vào v2 qua kết nối đi vào:
    v2 phải từ chối, giữ nguyên kết nối thật tới v0 (review PR #16)."""
    from l1sim.network.hello import make_hello
    from l1sim.network.tcp_transport import read_frame, write_frame

    async def main():
        c = Cluster(3)
        await c.start()
        v2 = c.nodes["v2"].transport
        real = v2._writers["v0"]
        replay = make_hello("v0", c.sks["v0"], c.addrs["v0"])  # như HELLO v0 từng gửi đi

        reader, writer = await asyncio.open_connection("127.0.0.1", v2.port)
        await write_frame(writer, replay)
        await read_frame(reader)  # HELLO của v2
        await c.until(lambda: c.log.names("handshake_failed", node="v2"))
        still_real = v2._writers.get("v0") is real
        writer.close()
        await c.stop()
        return still_real, [f["reason"] for f in c.log.names("handshake_failed", node="v2")]

    still_real, reasons = asyncio.run(main())
    assert still_real
    assert reasons == ["unexpected_inbound"]
