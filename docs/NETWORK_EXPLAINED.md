# Tầng network (Role C) hoạt động như thế nào

Tài liệu này giải thích 4 file trong `src/l1sim/network/`. Mỗi file được mô tả qua 3 câu hỏi: làm gì, làm thế nào, và giải quyết yêu cầu nào của Role C. Code ở đây là bản sau khi đã ghép với `models` / `encoding` / `crypto` của A (xem [NETWORK_INTEGRATION_CHANGES.md](NETWORK_INTEGRATION_CHANGES.md)). Bản tối giản đã chọn được mô tả ở [ROLE_C_P2P_LITE.md](ROLE_C_P2P_LITE.md).

## 1. Role C phải giải quyết gì

Đề Lab01 có 10 validator, mỗi validator là một process riêng, có thể chạy trên nhiều máy qua ZeroTier. Role C phải làm cho các node:

1. **Nói chuyện được với nhau qua TCP thật:** chia luồng byte thành từng message, mở và đóng kết nối, không để một peer xấu làm hỏng node.
2. **Biết chắc mình đang nói với ai:** một node không được mạo danh validator khác (T3: `unknown_peer`, `bad_signature`).
3. **Tìm và giữ kết nối tới các node khác:** bật theo thứ tự bất kỳ, node chết rồi bật lại vẫn nối lại được (D1, D2).
4. **Lan message tới mọi node:** mỗi node xử lý mỗi message đúng 1 lần dù nhận nhiều bản (T2), và message vẫn tới khi một đường bị drop (T5).
5. **Chừa chỗ cắm cho người khác:** B gắn FaultInjector vào đường gửi, D và A gắn handler cho consensus, mempool và block sync.

Bốn file chia nhau các việc trên:

| Yêu cầu | File |
|---|---|
| 1. Truyền byte qua TCP | `tcp_transport.py` |
| 2. Xác thực peer | `hello.py` |
| 3. Tìm và giữ kết nối | `static_peers.py` |
| 4. Lan message, dedup | `gossip.py` |
| 5. Chỗ cắm | `gossip.sender`, `gossip.on()`, `transport.set_*()` |

## 2. Các lớp ghép với nhau thế nào

```
 consensus / mempool / block_sync  (A, B, D)
        │ broadcast(_nowait)(msg) / send_to(_nowait)(peer, msg)   ▲ handler(msg, from_peer) -> bool
        ▼                                                  │
 ┌──────────── gossip.py ────────────┐  decode → dedup → gọi handler → chuyển tiếp
 │ sender(peer, bytes)               │  ← B thay bằng FaultInjector
 └──────────────┬────────────────────┘
                ▼
 ┌──────── tcp_transport.py ─────────┐  frame 4 byte độ dài, 1 vòng đọc mỗi peer
 │ set_handshake ◄── hello.py        │  HELLO ký Ed25519, domain HELLO:lab01
 │ connect()     ◄── static_peers.py │  id lớn nối id nhỏ, thử lại mỗi 1 s
 └───────────────────────────────────┘
```

Dựng một node (rút từ `tests/unit/p2p_cluster.py`):

```python
transport = TcpTransport("v3", "0.0.0.0:9003", logger)
transport.set_handshake(handshake_fn("v3", kp.private_key, validator_keys,  # bytes hoặc Ed25519PublicKey
                                     advertise_addr=lambda: "10.147.17.20:9003"))
gossip = Gossip("v3", transport, logger)
gossip.on(Kind.VOTE, engine_on_vote)          # handler của D
await transport.start()
await StaticPeers("v3", config["network"]["peers"], transport, logger).start()
await gossip.broadcast(Message(Kind.VOTE, "v3", h, vote))
# Engine của D gọi broadcast(message) -> None, nên truyền bản không await:
# ConsensusEngine(..., broadcast=gossip.broadcast_nowait, logger=logger, ...)
```

## 3. `tcp_transport.py`: truyền byte qua TCP

**Vấn đề:** TCP là một luồng byte, không có ranh giới message. Hai lần `write()` có thể tới nơi dính liền nhau, một lần `write()` cũng có thể bị cắt làm hai.

**Cách làm:**

- **Framing:** mỗi frame gồm 4 byte độ dài (big-endian) rồi tới nội dung. `read_frame()` đọc đúng 4 byte, sau đó đọc đúng `length` byte bằng `readexactly()`.
- **Giới hạn độ dài:** frame dài hơn `MAX_FRAME` (1 MB, bằng `encoding.MAX_MESSAGE_BYTES`) bị từ chối ngay. Peer xấu không thể khai độ dài 4 GB để làm cạn RAM.
- **Một peer, một kết nối, một coroutine đọc:**
  - `_writers[peer]` giữ writer, `_tasks[peer]` giữ vòng đọc.
  - Gửi là một lệnh `writer.write(frame)`. Lệnh này chạy đồng bộ trên một thread nên các frame không bao giờ xen lẫn, không cần hàng đợi gửi.
- **Handshake bắt buộc:** cả kết nối đi ra (`connect`) lẫn đi vào (`_on_inbound`) đều đi qua `_attach()`.
  - `_attach()` chạy hàm handshake có timeout 2 giây. Handshake trả `peer_id` thì kết nối mới được dùng.
  - Lỗi handshake được log `handshake_failed` kèm `reason` (lấy từ `HelloError.reason`).
  - Nếu handshake trả về chính `node_id` của mình, kết nối bị từ chối với reason `self_connect`.
- **Peer khởi động lại:** kết nối mới tới cùng `peer_id` thay kết nối cũ và không báo disconnect, vì peer vẫn còn đó.
- **Mất kết nối:** disconnect handler được gọi **đúng 1 lần**. Không gọi khi kết nối bị thay thế hay khi `stop()`.
- **Chống peer chậm:** nếu buffer gửi tới một peer vượt 4 MB thì bỏ message, log `send_buffer_full`. Consensus chịu được mất message, còn RAM phình thì không.
- **Cô lập lỗi:** receiver ném exception thì chỉ log `receiver_error`, kết nối vẫn sống.

**Giải quyết:** yêu cầu 1. Đây là nền cho mọi file còn lại. Transport chỉ chuyển byte, không biết vote hay block là gì, nên test được độc lập (12 test trong `test_tcp_transport.py`).

## 4. `hello.py`: xác thực peer

**Vấn đề:** ai cũng mở được TCP tới port của node rồi tự xưng là `v1`. Nếu tin vào lời tự xưng đó, kẻ tấn công sẽ chiếm được chỗ của v1 trong mesh.

**Cách làm:**

- Ngay sau khi mở kết nối, **cả hai bên** gửi một frame HELLO = `encoding.encode(Message(Kind.HELLO, node_id, finalized_height, Hello(...)))`.
- `Hello` gồm `node_id`, `advertise_addr`, `chain_id = "lab01"`, `finalized_height` và `signature`.
- `signature = crypto.sign(sk, Domain.HELLO, hello)`, tức Ed25519 ký lên `HELLO:lab01` + canonical JSON của các trường còn lại.
- Bên nhận kiểm theo thứ tự, lỗi đầu tiên gặp phải là reason:

| Bước | Reason | Bắt được |
|---|---|---|
| `encoding.decode` thành công và kind là HELLO | `malformed` | rác, JSON sai schema, bytes không canonical |
| `chain_id == "lab01"` | `wrong_chain` | node của mạng khác |
| `node_id` có trong validator set | `unknown_peer` | node lạ (T3) |
| `node_id != me` | `self_connect` | config ghi nhầm địa chỉ của chính mình |
| `crypto.verify(pk[node_id], HELLO, ...)` | `bad_signature` | mạo danh, chữ ký domain khác, sửa trường sau khi ký (T3) |

- **Domain separation:** chữ ký HELLO có prefix `HELLO:lab01`, chữ ký vote có prefix `VOTE:lab01`. Một chữ ký vote bắt được trên mạng không dùng lại làm HELLO được, và ngược lại. Có test riêng cho điều này.
- `finalized_height` và `advertise_addr` nằm trong phần được ký, nên peer không sửa được sau khi ký. `on_hello` đưa các giá trị này lên tầng trên để block sync biết peer nào đi trước (D3).

**Giới hạn đã chấp nhận:** HELLO không có nonce. Kẻ bắt được HELLO của v1 có thể phát lại để giả làm v1 ở tầng kết nối. Tuy vậy nó không ký được vote hay block thay v1, nên safety không bị ảnh hưởng.

**Giải quyết:** yêu cầu 2, phần mạo danh peer của T3.

## 5. `static_peers.py`: tìm và giữ kết nối

**Vấn đề:** node bật theo thứ tự bất kỳ, có thể chết rồi bật lại. Nếu cả hai bên cùng chủ động nối thì một cặp node sẽ có 2 kết nối, và phải có thêm logic gộp.

**Cách làm:**

- Config `network.peers = {node_id: "host:port"}` giống nhau ở mọi node, nên mọi node thấy cùng một thứ tự.
- **Quy tắc "đứng sau nối tới đứng trước":** `v5` chủ động nối `v0..v4`, còn `v6..v9` nối tới `v5`. Mỗi cặp chỉ có một bên quay số, nên không bao giờ có kết nối trùng.
- **Vòng lặp mỗi `retry` giây (mặc định 1 s):**
  - Peer nào node phải nối mà chưa có kết nối thì `transport.connect()`.
  - Peer bên kia chưa bật thì lần sau thử lại, nên thứ tự bật node không quan trọng.
- **Peer chết:** disconnect handler log `peer_removed`. Vòng lặp tự nối lại khi peer bật lên, và khi đó log `peer_added`.
- **Config sai:** nếu ở địa chỉ của `v3` lại là `v4` trả lời HELLO, kết nối bị đóng và log `wrong_peer_at_addr`.

**Kết quả:** với 10 node thì được full mesh 45 kết nối. Mỗi message đi 1 hop khi mạng tốt.

**Giải quyết:** yêu cầu 3, cùng D1 (bản tối giản: full mesh từ config) và D2 (`peer_removed` rồi `peer_added`).

**Đánh đổi:** số kết nối tăng theo n², không dùng cho mạng lớn được. Multicast discovery (bậc 5, tuỳ chọn) có thể thêm vào sau mà không sửa file này: chỉ cần thêm peer vào danh sách cần nối.

## 6. `gossip.py`: lan message và dedup

**Vấn đề:**

- Vote, tx, header phải tới mọi node.
- Fault injection của B có thể nhân đôi message (T2) hoặc drop một cạnh (T5).
- Chuyển tiếp mà không dedup thì message sẽ lặp vô hạn trong mesh.

**Cách làm:**

- **Hai loại message:**
  - `FLOOD = {TX, HEADER, VOTE}`: `broadcast(msg)` gửi cho mọi peer.
  - Mỗi hàm gửi có bản async (`broadcast`, `send_to`) và bản không await (`broadcast_nowait`, `send_to_nowait`) cho callback đồng bộ của `ConsensusEngine`. Bản nowait kiểm kind ngay rồi gửi trong task.
  - `DIRECT = {BODY_REQUEST, BODY, GET_BLOCKS, BLOCKS}`: `send_to(peer, msg)` gửi 1-1, không dedup, không chuyển tiếp.
  - `HELLO` chỉ dùng trong handshake. Nhận HELLO qua gossip thì bỏ và log `gossip_unexpected_kind`.
- **Luồng nhận** (`_handle`):
  1. `encoding.decode(data)`. Lỗi thì log `gossip_malformed` và dừng. Decode của A bắt buộc bytes phải canonical, nên cùng một message luôn có cùng bytes.
  2. FLOOD: tính `mid = encoding.message_id(msg)`. Đã thấy thì log `duplicate_ignored` và dừng; chưa thấy thì đánh dấu đã thấy. Bước kiểm và bước đánh dấu nằm trước lệnh `await` đầu tiên, nên 2 bản tới sát nhau không lọt được.
  3. Gọi `handler(msg, from_peer)`. Handler có thể là hàm thường hoặc async. Handler lỗi thì coi như trả `False`, và gossip vẫn chạy tiếp.
  4. **Chỉ chuyển tiếp khi handler trả `True`.** Node gửi lại đúng bytes đã nhận cho mọi peer trừ người gửi. Nhờ vậy HEADER sai hay vote chữ ký sai dừng lại ngay ở node đầu tiên.
- **Node phát** đánh dấu `mid` của mình là đã thấy, nên bản quay về bị bỏ và handler của chính nó không bị gọi.
- **HEADER trước BODY:** gossip không biết logic này.
  - Handler HEADER thấy chưa có body thì gọi `send_to(from_peer, Message(BODY_REQUEST, ...))`.
  - Node có body trả lại bằng `send_to(..., Message(BODY, ...))`.
- **Chỗ cắm của B:** mọi lần gửi đều qua `self.sender(peer, bytes)`, mặc định là `transport.send`. B thay `sender` bằng FaultInjector để delay, nhân đôi hoặc drop mà không phải sửa gossip.

**Giải quyết:**

- Yêu cầu 4.
- **T2:** bản trùng bị dedup, nên không vote nào bị đếm 2 lần, và có số `duplicate_ignored` để đưa vào results.
- **T5:** FaultInjector drop cạnh v0→v5 thì v5 vẫn nhận qua node khác, vì các node đó chuyển tiếp. Đã có test `test_forwarding_routes_around_dropped_link`.
- Yêu cầu 5: `sender` cho B; `on(kind, handler)` cho A, B, D.

## 7. Một vote đi từ v0 tới v9

1. Engine của v0 gọi `broadcast(Message(VOTE, "v0", 4, vote))`, tức `gossip.broadcast_nowait`. Gossip tính `mid`, đánh dấu đã thấy, rồi `encode` một lần.
2. Với mỗi peer v1..v9, gossip gọi `sender(peer, bytes)`. Sender là FaultInjector của B, có thể delay hoặc drop cạnh v0→v9.
3. Transport viết `[len][bytes]` vào socket.
4. v1 nhận frame, decode, thấy `mid` mới, gọi handler VOTE. D verify chữ ký, đưa vào VoteSet và trả `True`. v1 chuyển tiếp cho v2..v9.
5. v9 nhận bản v1 chuyển tới, xử lý một lần. Các bản sau từ v2, v3, ... bị `duplicate_ignored`.

## 8. Event log mà network ghi

Đề mục 6 yêu cầu log sự kiện mạng. Mỗi event có `node=...`:

| Event | File | Khi nào |
|---|---|---|
| `listen` | transport | bắt đầu nghe port |
| `conn_open` / `conn_closed` | transport | sau handshake / khi mất kết nối (kèm reason) |
| `handshake_failed` | transport | HELLO sai (reason của hello.py), timeout |
| `connect_failed` | transport | không mở được TCP |
| `send_no_peer`, `send_buffer_full`, `send_too_large` | transport | không gửi được |
| `peer_added` / `peer_removed` | static_peers | có peer mới / mất peer |
| `wrong_peer_at_addr` | static_peers | config ghi sai địa chỉ |
| `gossip_broadcast`, `gossip_send` | gossip | gửi FLOOD / DIRECT |
| `gossip_recv` | gossip | nhận message, kèm `accepted` |
| `duplicate_ignored` | gossip | bản trùng bị bỏ (T2) |
| `gossip_malformed`, `gossip_unexpected_kind`, `gossip_no_handler`, `gossip_handler_error` | gossip | message bị bỏ |

Cả 3 file ghi event qua `network/events.py: emit()`. `emit` nhận logger dạng object có `logger.event(name, **fields)` hoặc dạng hàm `logger(name, **fields)` như engine của D; không có logger thì ghi qua `logging.debug`. Engine cũng log `duplicate_ignored` cho vote trùng; bản của gossip phân biệt được nhờ có `peer` và `mid`.

## 9. Test chứng minh

`PYTHONPATH=src pytest` → 133 test pass (cả test của execution và consensus). Các test network chạy node TCP thật trong 1 process, port 0:

| File | Chứng minh |
|---|---|
| `test_tcp_transport.py` (12) | framing, frame dính nhau / quá lớn / bị cắt, 100 message đúng thứ tự, disconnect gọi 1 lần, kết nối mới thay kết nối cũ, handshake bị từ chối, port chết, receiver lỗi không làm rớt kết nối |
| `test_hello.py` | mỗi reason có test riêng; chữ ký domain VOTE, HELLO không ký, sửa `finalized_height` sau khi ký đều bị từ chối; cluster 3 node nhận đúng `advertise_addr` |
| `test_static_peers.py` | 10 node thành full mesh; thứ tự bật không quan trọng; node chết thì `peer_removed`, bật lại thì nối lại; node không có trong config bị từ chối |
| `test_gossip.py` | mỗi node nhận đúng 1 lần; bản trùng bị bỏ; bytes không canonical bị bỏ; message bị handler từ chối không lan tiếp; cạnh bị drop thì đi đường vòng; `send_to` chỉ tới đúng 1 node; handler lỗi không làm chết gossip |

## 10. Còn thiếu / phụ thuộc người khác

- **A:** khối `network.peers` trong config, `node.py` dựng các lớp như mục 2. Engine hiện phát `Kind.BODY` qua `broadcast` (`consensus/engine.py:675`, có TODO). `node.py` phải đổi thành HEADER → BODY_REQUEST → BODY, vì gossip từ chối broadcast BODY.
- **B:** FaultInjector gắn vào `gossip.sender` với signature `(peer, bytes) -> Awaitable`.
- **D:** handler trả `bool`; dùng `send_to` cho BODY_REQUEST, BODY, GET_BLOCKS, BLOCKS; dùng `on_hello` để lấy `finalized_height` cho block sync. Chốt cách đếm `duplicate_ignored` cho T2, vì engine và gossip cùng log tên này.
- **Cả nhóm:** chốt D1 = "full mesh từ config", multicast là điểm cộng. Cập nhật interface Gossip trong TEAM_PLAN cho khớp code.
- **E2E T2, D1, D2** chỉ chạy được khi đã có `node.py`.
