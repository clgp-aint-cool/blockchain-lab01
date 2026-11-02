import asyncio
import json
from dataclasses import replace

import pytest

from l1sim.crypto import keypair_from_seed, sign
from l1sim.encoding import encode
from l1sim.models import CHAIN_ID, Domain, GetBlocks, Hello, Kind, Message
from l1sim.network.hello import HelloError, check_hello, make_hello
from tests.unit.p2p_cluster import SEED, Cluster, make_keys

SKS, PKS = make_keys(3)
ADDR = "127.0.0.1:9001"


def _check(data: bytes, me: str = "v0") -> Hello:
    return check_hello(data, PKS, me)


def _reason(data: bytes, me: str = "v0") -> str:
    with pytest.raises(HelloError) as e:
        _check(data, me)
    return e.value.reason


def _signed(hello: Hello, sk, domain: Domain = Domain.HELLO) -> bytes:
    hello = replace(hello, signature=sign(sk, domain, hello))
    return encode(Message(Kind.HELLO, hello.node_id, hello.finalized_height, hello))


def test_valid_hello_returns_hello():
    hello = _check(make_hello("v1", SKS["v1"], ADDR, finalized_height=3))
    assert (hello.node_id, hello.advertise_addr, hello.finalized_height) == ("v1", ADDR, 3)


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"not json",
        b"[]",
        b'{"kind":"HELLO"}',
        # Message hợp lệ nhưng không phải HELLO
        encode(Message(Kind.GET_BLOCKS, "v1", 0, GetBlocks(0))),
        # HELLO đúng nhưng không canonical (có khoảng trắng)
        json.dumps(json.loads(make_hello("v1", SKS["v1"], ADDR)), indent=1).encode(),
    ],
)
def test_malformed(data):
    assert _reason(data) == "malformed"


def test_wrong_chain():
    raw = json.loads(make_hello("v1", SKS["v1"], ADDR))
    raw["payload"]["chain_id"] = "other-chain"
    data = json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()
    assert _reason(data) == "wrong_chain"


def test_unknown_peer():
    assert _reason(make_hello("v99", SKS["v1"], ADDR)) == "unknown_peer"


def test_self_connect():
    assert _reason(make_hello("v0", SKS["v0"], ADDR), me="v0") == "self_connect"


def test_signed_by_wrong_key():
    # v2 tự xưng là v1
    assert _reason(make_hello("v1", SKS["v2"], ADDR)) == "bad_signature"


def test_unsigned_hello_rejected():
    hello = Hello("v1", ADDR, CHAIN_ID, 0)
    assert _reason(encode(Message(Kind.HELLO, "v1", 0, hello))) == "bad_signature"


def test_signature_from_other_domain_rejected():
    # Chữ ký hợp lệ của v1 nhưng ký với domain VOTE không dùng làm HELLO được.
    hello = Hello("v1", ADDR, CHAIN_ID, 0)
    assert _reason(_signed(hello, SKS["v1"], Domain.VOTE)) == "bad_signature"


def test_tampered_height_rejected():
    # Sửa finalized_height sau khi ký thì chữ ký không còn khớp.
    hello = Hello("v1", ADDR, CHAIN_ID, 0)
    hello = replace(hello, signature=sign(SKS["v1"], Domain.HELLO, hello))
    forged = replace(hello, finalized_height=99)
    assert _reason(encode(Message(Kind.HELLO, "v1", 99, forged))) == "bad_signature"


def test_handshake_reports_peer_hello():
    async def main():
        c = Cluster(3)
        await c.start()
        hellos = {v: dict(n.hellos) for v, n in c.nodes.items()}
        addrs = dict(c.addrs)
        await c.stop()
        return hellos, addrs

    hellos, addrs = asyncio.run(main())
    for v, seen in hellos.items():
        assert sorted(seen) == sorted(p for p in addrs if p != v)
        assert all(h.advertise_addr == addrs[p] for p, h in seen.items())


def test_accepts_public_key_objects():
    # Cùng map validator_keys (node_id -> Ed25519PublicKey) mà ConsensusEngine dùng.
    keys = {v: keypair_from_seed(SEED, v).public_key for v in PKS}
    assert check_hello(make_hello("v1", SKS["v1"], ADDR), keys, "v0").node_id == "v1"
    with pytest.raises(HelloError) as e:
        check_hello(make_hello("v1", SKS["v2"], ADDR), keys, "v0")
    assert e.value.reason == "bad_signature"
