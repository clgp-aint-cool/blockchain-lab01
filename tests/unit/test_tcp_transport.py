import asyncio

import pytest

from l1sim.network.tcp_transport import (
    HEADER,
    MAX_FRAME,
    FrameTooLarge,
    TcpTransport,
    frame,
    read_frame,
    write_frame,
)


def _reader_with(data: bytes, eof: bool = True) -> asyncio.StreamReader:
    r = asyncio.StreamReader()
    r.feed_data(data)
    if eof:
        r.feed_eof()
    return r


def _id_handshake(my_id: str):
    """Handshake giả cho test: mỗi bên gửi id của mình, đọc id bên kia. Không có chữ ký."""

    async def hs(reader, writer, outbound):
        await write_frame(writer, my_id.encode())
        return (await read_frame(reader)).decode()

    return hs


async def _make(node_id: str, **kw) -> TcpTransport:
    t = TcpTransport(node_id, "127.0.0.1:0", **kw)
    t.set_handshake(_id_handshake(node_id))
    await t.start()
    return t


async def _until(cond, timeout: float = 2.0) -> None:
    async def poll():
        while not cond():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(poll(), timeout)


# ---------- framing ----------


@pytest.mark.parametrize("payload", [b"", b"hello", b"x" * MAX_FRAME])
def test_frame_round_trip(payload):
    async def main():
        return await read_frame(_reader_with(frame(payload)))

    assert asyncio.run(main()) == payload


def test_two_frames_glued_are_split():
    async def main():
        r = _reader_with(frame(b"vote") + frame(b"block"))
        return await read_frame(r), await read_frame(r)

    assert asyncio.run(main()) == (b"vote", b"block")


def test_oversize_frame_rejected():
    async def main():
        await read_frame(_reader_with(HEADER.pack(MAX_FRAME + 1)))

    with pytest.raises(FrameTooLarge):
        asyncio.run(main())
    with pytest.raises(FrameTooLarge):
        frame(b"x" * (MAX_FRAME + 1))


def test_peer_closes_mid_frame():
    async def main():
        await read_frame(_reader_with(HEADER.pack(10) + b"abc"))

    with pytest.raises(asyncio.IncompleteReadError):
        asyncio.run(main())


# ---------- transport ----------


def test_messages_arrive_in_order():
    async def main():
        a, b = await _make("a"), await _make("b")
        got = []
        b.set_receiver(lambda peer, data: got.append((peer, data)))

        assert await a.connect(f"127.0.0.1:{b.port}") == "b"
        for i in range(100):
            await a.send("b", str(i).encode())
        await _until(lambda: len(got) == 100)
        await a.stop()
        await b.stop()
        return got

    got = asyncio.run(main())
    assert got == [("a", str(i).encode()) for i in range(100)]


def test_disconnect_fires_once():
    async def main():
        a, b = await _make("a"), await _make("b")
        lost = []
        b.add_disconnect_handler(lost.append)

        await a.connect(f"127.0.0.1:{b.port}")
        await _until(lambda: b.peers() == ["a"])
        a.close("b")
        await _until(lambda: b.peers() == [])
        await asyncio.sleep(0.1)
        await a.stop()
        await b.stop()
        return lost

    assert asyncio.run(main()) == ["a"]


def test_reconnect_replaces_old_connection():
    async def main():
        a, b = await _make("a"), await _make("b")
        lost_a, lost_b, got = [], [], []
        a.add_disconnect_handler(lost_a.append)
        b.add_disconnect_handler(lost_b.append)
        b.set_receiver(lambda peer, data: got.append(data))

        await a.connect(f"127.0.0.1:{b.port}")
        await _until(lambda: b.peers() == ["a"])
        old = b._writers["a"]
        await a.connect(f"127.0.0.1:{b.port}")  # như khi a khởi động lại rồi nối lại
        await _until(lambda: b._writers.get("a") not in (None, old))
        await asyncio.sleep(0.1)

        await a.send("b", b"ping")
        await _until(lambda: got == [b"ping"])
        state = (a.peers(), b.peers(), lost_a, lost_b)
        await a.stop()
        await b.stop()
        return state

    # Vẫn đúng 1 kết nối mỗi bên, và thay kết nối không bị coi là mất peer.
    assert asyncio.run(main()) == (["b"], ["a"], [], [])


def test_rejected_handshake_and_self_connect():
    async def main():
        a = await _make("a")
        b = TcpTransport("b", "127.0.0.1:0")

        async def reject(reader, writer, outbound):
            return None

        b.set_handshake(reject)
        await b.start()
        r1 = await a.connect(f"127.0.0.1:{b.port}")
        r2 = await a.connect(f"127.0.0.1:{a.port}")  # tự nối vào chính mình
        state = (r1, r2, a.peers(), b.peers())
        await a.stop()
        await b.stop()
        return state

    assert asyncio.run(main()) == (None, None, [], [])


def test_connect_to_dead_port_returns_none():
    async def main():
        a = await _make("a")
        dead = await _make("x")
        port = dead.port
        await dead.stop()
        r = await a.connect(f"127.0.0.1:{port}")
        await a.stop()
        return r

    assert asyncio.run(main()) is None


def test_receiver_error_does_not_drop_connection():
    async def main():
        a, b = await _make("a"), await _make("b")
        got = []

        def recv(peer, data):
            if data == b"boom":
                raise ValueError("bad message")
            got.append(data)

        b.set_receiver(recv)
        await a.connect(f"127.0.0.1:{b.port}")
        await a.send("b", b"boom")
        await a.send("b", b"ok")
        await _until(lambda: got == [b"ok"])
        peers = b.peers()
        await a.stop()
        await b.stop()
        return peers

    assert asyncio.run(main()) == ["a"]


def test_all_disconnect_handlers_are_called():
    async def main():
        a, b = await _make("a"), await _make("b")
        first, second = [], []

        def broken(peer):
            raise RuntimeError("handler lỗi")

        b.add_disconnect_handler(first.append)
        b.add_disconnect_handler(broken)  # không được chặn handler sau nó
        b.add_disconnect_handler(second.append)

        await a.connect(f"127.0.0.1:{b.port}")
        await _until(lambda: b.peers() == ["a"])
        a.close("b")
        await _until(lambda: second == ["a"])
        await a.stop()
        await b.stop()
        return first, second

    assert asyncio.run(main()) == (["a"], ["a"])


def test_inbound_filter_rejects_before_replacing():
    async def main():
        a, b, c = await _make("a"), await _make("b"), await _make("c")
        b.set_inbound_filter(lambda peer: peer == "a")
        events = []
        b.logger = lambda name, **f: events.append((name, f))

        assert await a.connect(f"127.0.0.1:{b.port}") == "b"
        await _until(lambda: b.peers() == ["a"])
        old = b._writers["a"]
        await c.connect(f"127.0.0.1:{b.port}")  # c không được phép nối vào b
        await _until(lambda: any(n == "handshake_failed" for n, _ in events))
        # Filter chặn theo peer_id: kết nối sẵn có của "a" không bị đụng tới.
        peers, same = b.peers(), b._writers["a"] is old
        for t in (a, b, c):
            await t.stop()
        return peers, same, [f["reason"] for n, f in events if n == "handshake_failed"]

    peers, same, reasons = asyncio.run(main())
    assert peers == ["a"] and same
    assert reasons == ["unexpected_inbound"]
