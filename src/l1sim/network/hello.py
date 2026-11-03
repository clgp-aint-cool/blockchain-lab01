"""HELLO: hai node chứng minh danh tính ngay sau khi mở kết nối TCP.

Mỗi bên gửi 1 frame = `encoding.encode(Message(kind=HELLO, payload=Hello(...)))`, tức
cùng định dạng canonical với mọi message khác. `Hello.signature` được ký bằng
`crypto.sign(sk, Domain.HELLO, hello)` (domain `HELLO:lab01`), nên chữ ký HELLO không bao
giờ dùng lại được làm chữ ký vote hay block.

HELLO mang thêm `advertise_addr` và `finalized_height`; tầng trên nhận chúng qua
`on_hello` (dùng cho block sync).

Bên nhận kiểm theo thứ tự, lỗi đầu tiên gặp phải là reason:
malformed → wrong_chain → unknown_peer → self_connect → bad_signature.

Giới hạn: HELLO không có nonce (không challenge–response), nên ai bắt được HELLO của v1
(vd v1 gửi nó khi kẻ đó nối tới v1) có thể phát lại mãi để giả làm v1 ở tầng kết nối.
- Safety không bị ảnh hưởng: mọi vote/block bên trong vẫn có chữ ký riêng.
- Liveness thì có: transport cho kết nối mới thay kết nối cũ, nên replay có thể đá kết
  nối thật của v1 ra, và làm vậy với mọi peer của một node là cô lập node đó (eclipse).
  StaticPeers giảm rủi ro này: chỉ nhận kết nối đi vào từ node đứng sau, nên chỉ giả
  được node đứng sau, và node thật sẽ quay số lại đá kẻ giả ra. Kẻ tấn công vẫn có thể
  làm kết nối chập chờn. Cách sửa triệt để: mỗi bên gửi nonce ngẫu nhiên trước, HELLO
  ký nonce của bên kia (cần đổi `Hello` trong models.py).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import replace

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from l1sim.crypto import sign, verify
from l1sim.encoding import decode, encode
from l1sim.models import CHAIN_ID, Domain, Hello, Kind, Message
from l1sim.network.tcp_transport import HandshakeFn, read_frame, write_frame

HelloFn = Callable[[Hello], None]
ValidatorKeys = Mapping[str, "bytes | Ed25519PublicKey"]


class HelloError(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason  # transport ghi reason này vào log handshake_failed


def make_hello(
    node_id: str, sk: Ed25519PrivateKey, advertise_addr: str, finalized_height: int = 0
) -> bytes:
    hello = Hello(node_id, advertise_addr, CHAIN_ID, finalized_height)
    hello = replace(hello, signature=sign(sk, Domain.HELLO, hello))
    return encode(Message(Kind.HELLO, node_id, finalized_height, hello))


def _chain_of(data: bytes) -> object:
    """chain_id thô trong frame, để phân biệt wrong_chain với malformed."""
    try:
        return json.loads(data)["payload"]["chain_id"]
    except (ValueError, KeyError, TypeError):
        return None


def check_hello(data: bytes, validators: ValidatorKeys, me: str) -> Hello:
    """Trả Hello của peer nếu hợp lệ, nếu không ném HelloError(reason).

    validators: node_id → public key Ed25519, dạng 32 bytes thô (`KeyPair.public_bytes`)
    hoặc `Ed25519PublicKey` (cùng map `validator_keys` mà ConsensusEngine/VoteSet dùng).
    """
    try:
        msg = decode(data)
    except (ValueError, TypeError):
        chain = _chain_of(data)
        if isinstance(chain, str) and chain != CHAIN_ID:
            raise HelloError("wrong_chain") from None
        raise HelloError("malformed") from None
    if msg.kind is not Kind.HELLO:
        raise HelloError("malformed")
    hello: Hello = msg.payload
    if hello.node_id not in validators:
        raise HelloError("unknown_peer")
    if hello.node_id == me:
        raise HelloError("self_connect")
    pk = validators[hello.node_id]
    if not isinstance(pk, Ed25519PublicKey):
        try:
            pk = Ed25519PublicKey.from_public_bytes(pk)
        except (ValueError, TypeError):
            raise HelloError("bad_signature") from None
    if not verify(pk, Domain.HELLO, hello, hello.signature):
        raise HelloError("bad_signature")
    return hello


def handshake_fn(
    node_id: str,
    sk: Ed25519PrivateKey,
    validators: ValidatorKeys,
    advertise_addr: Callable[[], str],
    finalized_height: Callable[[], int] = lambda: 0,
    on_hello: HelloFn | None = None,
) -> HandshakeFn:
    """Hàm đưa vào transport.set_handshake(): gửi HELLO của mình, kiểm HELLO của peer.

    advertise_addr và finalized_height là hàm vì chúng đổi theo thời gian (port thật chỉ
    có sau start(), height tăng dần). on_hello(hello) được gọi khi HELLO của peer hợp lệ.
    """

    async def hs(reader, writer, outbound: bool) -> str:
        await write_frame(writer, make_hello(node_id, sk, advertise_addr(), finalized_height()))
        hello = check_hello(await read_frame(reader), validators, node_id)
        if on_hello is not None:
            on_hello(hello)
        return hello.node_id

    return hs
