# Role C bản tối giản: P2P bằng TCP thật, ít code nhất mà vẫn pass test

Bản này thay cho Bước 3–7 trong [ROLE_C_P2P.md](ROLE_C_P2P.md) khi cần làm nhanh. Vẫn dùng **TCP thật** (`tcp_transport.py` đã xong, bản tối giản), chỉ cắt những phần tốn công mà đề Lab01 không yêu cầu.

## 1. Cắt gì, giữ gì

| Thành phần | Kế hoạch cũ | Bản tối giản | Vì sao cắt được |
|---|---|---|---|
| Kết nối | mỗi node chọn 4 neighbor, tối đa 8 | **Full mesh**: nối với cả 9 node còn lại | 10 node chỉ có 45 kết nối, không đáng kể. Bỏ hẳn phần xếp hạng neighbor |
| Ai chủ động nối | cả hai bên, rồi gộp kết nối trùng | **Node id lớn nối tới node id nhỏ** (`v5` nối `v0..v4`) | Không bao giờ có 2 kết nối cùng lúc cho một cặp, nên transport bỏ được quy tắc gộp kết nối |
| Tìm peer | multicast ANNOUNCE có ký | **Danh sách peer trong config**. Multicast là bậc cuối, làm nếu còn thời gian | Đề không yêu cầu discovery tự động |
| Kết nối lại | bảo trì neighbor, chọn peer thay thế | **Một vòng lặp mỗi 1 giây**: peer nào trong config chưa nối thì nối lại | Full mesh thì không cần chọn ai thay |
| Transport | `send_queue` + `_write_loop` mỗi peer, quy tắc gộp kết nối, dataclass `Connection` | **1 coroutine đọc mỗi peer**, `send()` gọi thẳng `writer.write()`, kết nối mới thay kết nối cũ | Xem Bậc 0. ~325 → ~240 dòng, kể cả docstring |
| HELLO | ký, kiểm nhiều trường | Ký `HELLO:lab01\|node_id`, kiểm 4 điều | Giữ vì chỉ ~40 dòng và chặn được giả mạo |
| Gossip | flooding + dedup + HEADER/BODY + sync | Giữ nguyên ý, code tối giản | Đề bắt buộc HEADER trước BODY; T2, T5 cần dedup và chuyển tiếp |

**Không đổi:** framing, API của `TcpTransport`, log sự kiện mạng, dedup, chuyển tiếp. Fault injection và rate limit vẫn là của B, block sync là của D; C chỉ chừa chỗ cắm vào.

## 2. Kiến trúc sau khi tối giản

```
consensus / mempool / block_sync           (B, D)
        │  gossip.broadcast(kind, payload)       ▲ handler(kind, payload, from_peer)
        ▼                                         │
gossip.py      dedup sha256, chuyển tiếp, HEADER→BODY, gửi 1-1           [C]
        │  sender(peer, bytes)  ← mặc định transport.send, B bọc FaultInjector vào đây
        ▼
tcp_transport.py   ✅ đã xong (bản tối giản)                              [C]
        ▲  connect(addr), set_handshake(hello), add_disconnect_handler, set_inbound_filter
        │
static_peers.py    đọc config, nối peer có id nhỏ hơn, thử lại mỗi 1 s  [C]
hello.py           tạo / kiểm HELLO có ký                                [C]
```

File: `src/l1sim/network/{tcp_transport,hello,static_peers,gossip}.py`. Đổi tên `discover.py` thành `static_peers.py`. Nếu sau này làm multicast thì thêm `multicast.py`, không sửa file khác.

## 3. Các bậc làm việc

Mỗi bậc xong là chạy được. Ước lượng tính theo giờ làm thật.

### Bậc 0: `tcp_transport` bản tối giản ✅ (đã xong)

Nhờ quy tắc "id lớn nối id nhỏ", transport rút gọn được:

| Trước | Sau | Vì sao vẫn đúng |
|---|---|---|
| Mỗi peer có `send_queue` và coroutine `_write_loop` | `send()` gọi thẳng `writer.write(frame(data))` | `write()` chạy đồng bộ, đưa nguyên frame vào buffer một lần; asyncio chỉ có 1 thread nên frame không xen lẫn |
| Hàng đợi đầy (10 000) thì bỏ message | `transport.get_write_buffer_size() > MAX_BUFFER` (4 MB) thì bỏ | Cùng tác dụng chống peer chậm làm phình RAM |
| `_prefer_new`: giữ kết nối do id nhỏ hơn mở | Kết nối mới thay kết nối cũ, không báo disconnect | Không còn trường hợp hai bên cùng mở; kết nối trùng chỉ xảy ra khi peer khởi động lại |
| 2 task mỗi peer + `_log_task_error` | 1 task: vòng đọc | Không còn vòng ghi |
| dataclass `Connection` | `_writers: dict[peer, writer]`, `_tasks: dict[peer, task]` | Chỉ còn 2 thứ cần giữ |
| Timeout, kích thước hàng đợi truyền vào `__init__` | Hằng số `CONNECT_TIMEOUT`, `HANDSHAKE_TIMEOUT`, `MAX_BUFFER` | Ít tham số hơn |
| `_run_handshake` + `_register` | Gộp thành `_attach(reader, writer, outbound)` | Kết nối đi ra và đi vào dùng chung một đường |

**Giữ nguyên:** framing 4 byte và `MAX_FRAME`, handshake có timeout, disconnect handler gọi đúng 1 lần, lỗi trong receiver không làm rớt kết nối, API `start / connect / send / close / peers / stop` và 3 hàm `set_*`.

**Đánh đổi:** nếu sau này cho cả hai bên cùng chủ động nối (ví dụ khi làm multicast), phải giữ quy tắc "id lớn nối id nhỏ" cho mọi nguồn địa chỉ, nếu không sẽ cần thêm lại quy tắc gộp kết nối.

**Test** `tests/unit/test_tcp_transport.py`, 12 test:
- framing: vòng tròn, 2 frame dính nhau, frame quá lớn, peer đóng giữa chừng
- 100 message đến đúng thứ tự
- disconnect handler gọi đúng 1 lần
- nối lại thay kết nối cũ, không báo disconnect
- handshake bị từ chối, tự nối vào chính mình, nối tới port chết
- receiver lỗi không làm rớt kết nối

### Bậc 1: HELLO (~1,5 giờ) → gate 29/10 "2 node HELLO"

```python
# hello.py
def make_hello(node_id, sk) -> bytes            # encode({type, chain_id, node_id, sig})
def check_hello(data, validators, me) -> str    # trả node_id, hoặc raise HelloError(reason)
def handshake_fn(node_id, sk, validators):      # trả hàm đưa vào transport.set_handshake
    async def hs(reader, writer, outbound):
        await write_frame(writer, make_hello(node_id, sk))
        return check_hello(await read_frame(reader), validators, node_id)
    return hs
```

Kiểm theo thứ tự, mỗi lỗi một reason: `malformed` → `wrong_chain` → `unknown_peer` → `self_connect` → `bad_signature`.
Hàm ký và kiểm chữ ký lấy từ `crypto.py` của A, domain `HELLO:lab01`.

**Test** `tests/unit/test_hello.py`:
- HELLO đúng thì trả `node_id`.
- Mỗi reason có 1 test.
- Chữ ký có domain `VOTE:` thì bị từ chối.

### Bậc 2: StaticPeers (~1,5 giờ)

```python
class StaticPeers:
    def __init__(self, node_id, peers: dict[str, str], transport, logger, retry=1.0)
    async def start(self)      # task: mỗi `retry` giây, connect các peer id < mình mà chưa nối
    async def stop(self)
    # transport.add_disconnect_handler → log peer_removed; vòng lặp tự nối lại
    # transport.set_inbound_filter → chỉ nhận kết nối đi vào từ node đứng sau trong sorted(peers)
    # mỗi lần nối được → log peer_added
```

Config (chốt với A):
```json
"network": {
  "peers": {"v0": "127.0.0.1:9000", "v1": "127.0.0.1:9001", "...": "..."},
  "retry_interval": 1.0
}
```

**Test** `tests/unit/test_static_peers.py`, 10 transport trong 1 process, port 0:
- Sau 2 giây, mọi node có đủ 9 peer.
- Đóng 1 node → các node khác log `peer_removed`. Bật lại → `peer_added`.

### Bậc 3: Gossip (~3–4 giờ) → gate 01/11 "T1 pass"

```python
FLOOD = {"TX", "HEADER", "PREVOTE", "PRECOMMIT"}
DIRECT = {"BODY_REQUEST", "BODY", "GET_BLOCKS", "BLOCKS"}

class Gossip:
    def __init__(self, node_id, transport, logger)
    sender: Callable[[str, bytes], Awaitable]   # mặc định transport.send; B thay bằng FaultInjector
    def on(self, kind, handler)                 # handler(payload, from_peer) -> bool (True = hợp lệ)
    async def broadcast(self, kind, payload)    # gửi cho mọi peer, đánh dấu seen
    async def send_to(self, peer, kind, payload)# 1-1, không chuyển tiếp
    def _on_bytes(self, peer, data)             # receiver của transport
```

Luồng nhận, khoảng 25 dòng:
```
mid = sha256(data)
if mid in seen: log(gossip_duplicate); return
seen.add(mid)
kind, payload = decode(data)               # lỗi → log(malformed), return
ok = handler[kind](payload, peer)          # tầng trên kiểm chữ ký và hợp lệ
if kind in FLOOD and ok:
    for p in peers() - {peer}: sender(p, data)
```

- **Chỉ chuyển tiếp khi handler trả `True`.** HEADER chưa được chấp nhận thì không lan tiếp, đúng với ý "body chỉ gửi sau khi header được chấp nhận".
- **HEADER → BODY:** khi nhận HEADER hợp lệ mà chưa có body, handler của B/D gọi `send_to(from_peer, "BODY_REQUEST", hash)`. Node có body thì trả `send_to(..., "BODY", body)`. Gossip không cần biết logic này, chỉ chuyển message.
- Full mesh nên chuyển tiếp hầu như chỉ tạo bản trùng. Nhưng vẫn **phải giữ**: khi FaultInjector drop một cạnh, đường vòng qua node khác giúp message vẫn tới (T5).

**Test** `tests/unit/test_gossip.py`, 10 node TCP thật trong 1 process:
- `broadcast` từ v0 → mọi node gọi handler đúng 1 lần.
- Gửi lại cùng bytes → `gossip_duplicate`, handler không chạy lần 2.
- Handler trả `False` → không chuyển tiếp.
- Cắt kết nối v0–v5 → v5 vẫn nhận nhờ chuyển tiếp.
- `send_to` → chỉ đúng 1 node nhận.

### Bậc 4: Ráp với nhóm và test e2e (~3 giờ) → gate 04/11

| Test | Điều C phải đảm bảo | Cách kiểm |
|---|---|---|
| **T2** lặp, đảo thứ tự | gossip dedup; B bật `dup_rate`, delay khác nhau | `gossip_duplicate > 0` (gossip chặn bản trùng), mỗi `CommitCertificate` có n − f voter **khác nhau**, không vote nào bị đếm 2 lần, không 2 block cùng height |
| **T5** mất, trễ trước GST | chuyển tiếp tạo đường vòng; D có `GET_BLOCKS` | safety pass; sau GST chain tiếp tục |
| **T8** chạy lại cùng seed | ghi vào results chỉ những thứ tất định: `peers` sorted, số `gossip_duplicate` **không** ghi (phụ thuộc thời điểm) | `check_reproducible.py` của B |
| **D2** node chết | log `peer_removed`, nối lại khi node bật lại | chain tiếp tục tới height mục tiêu |
| **D1** discovery | bản tối giản: mọi node đủ 9 peer, graph liên thông | đổi định nghĩa D1 thành "kết nối đầy đủ từ config", **cần chốt với nhóm** |

Log sự kiện mạng, đề mục 6: `send`, `recv`, `gossip_duplicate`, `peer_added`, `peer_removed`, `handshake_failed`. Các sự kiện `drop`, `delay`, `block`, `unblock` do FaultInjector và PeerLimits của B ghi.

### Bậc 5, tuỳ chọn: multicast discovery (~3 giờ)

Chỉ làm khi bậc 1–4 đã pass. `multicast.py` gửi `ANNOUNCE{node_id, addr, sig}` mỗi 2 giây. Nghe thấy thì gọi `static_peers.add(node_id, addr)`. Quy tắc nối "id lớn nối id nhỏ" và vòng thử lại giữ nguyên, không sửa file khác. D1 dùng lại định nghĩa cũ: không ghi sẵn địa chỉ trong config.

## 4. Lịch

| Ngày | Việc | Gate |
|---|---|---|
| 27/10 | ✅ `tcp_transport` bản tối giản + 12 test | |
| 28/10 | Bậc 1 HELLO, chốt config `network.peers` với A | |
| 29/10 | Bậc 2 StaticPeers | **2 node HELLO, 10 node full mesh** |
| 30–31/10 | Bậc 3 Gossip, đưa API cho B và D | |
| 01/11 | Ráp với node.py của A | **T1 pass** |
| 02–04/11 | Bậc 4: T2, T5, D2, T8 cùng B và D | **mọi test pass** |
| 04–05/11 | Bậc 5 nếu còn thời gian; viết phần Network của report | |

## 5. Cần chốt với nhóm

- [ ] **A:** khối `network.peers` trong config; interface logger (`logger(name, **fields)`, node.py truyền `event_logger.event`); hàm ký và kiểm chữ ký trong `crypto.py`.
- [ ] **B:** FaultInjector gắn vào `gossip.sender`, cùng chữ ký hàm `(peer, bytes) -> Awaitable`.
- [ ] **D:** dùng `send_to` cho `BODY_REQUEST`/`BODY`/`GET_BLOCKS`/`BLOCKS`; handler trả `bool`.
- [ ] **Cả nhóm:** D1 đổi thành "full mesh từ config", multicast là điểm cộng.

## 6. Trong report nên viết vì sao chọn full mesh

- Với n = 10, full mesh có 45 kết nối, mỗi message đi tối đa 1 hop khi mạng tốt. Độ trễ thấp nhất và đơn giản nhất.
- Vẫn có gossip chuyển tiếp, nên khi một cạnh bị drop, message đi đường vòng qua node khác.
- Quy tắc "id lớn nối id nhỏ" loại bỏ hoàn toàn trường hợp hai bên cùng mở kết nối.
- Nhược điểm: số kết nối tăng theo n², nên không dùng được cho mạng lớn. Đó là lúc cần neighbor selection, phần đã thiết kế ở [ROLE_C_P2P.md](ROLE_C_P2P.md) mục Bước 5.
