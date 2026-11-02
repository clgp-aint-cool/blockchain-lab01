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
