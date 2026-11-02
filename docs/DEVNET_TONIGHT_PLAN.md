# Kế hoạch tối nay: ghép code thành mạng chạy được và xem nó trên web

## Kết quả (31/10/2026): đã làm xong

```bash
PYTHONPATH=src:. python -m devnet.run --nodes 4 --port 8080   # mở http://localhost:8080
PYTHONPATH=src:. python -m pytest tests/integration           # I1/I2
```

| File mới | Việc |
|---|---|
| `devnet/node.py` | `DevNode`: ghép transport, gossip, `ConsensusEngine`, `Mempool`, `State`; header trước body; xin lại body khi bị mất |
| `devnet/net.py` | `Devnet`: chạy N node, tắt/bật node, sinh tx đã ký, kiểm safety trên chain của mọi node |
| `devnet/run.py` | HTTP server (chỉ thư viện chuẩn) + API ở mục 3.2 |
| `devnet/dashboard.html` | giao diện ở mục 3.1 |
| `tests/integration/test_devnet.py` | 3 test: 4 node cùng hash và state_hash, có 10 tx; tx replay bị từ chối; tắt 1 node vẫn finalize |

Không sửa file nào của A/B/C/D. `pytest` toàn bộ: **136 passed**; chạy 5 lần đều pass. `ruff check` sạch.

### Kết quả từng kịch bản

| # | Kịch bản | Kết quả |
|---|---|---|
| 1–2 | 4 node, gửi tx | ✅ cả 4 node cùng block hash và state_hash ở mọi height; tx vào block |
| 3 | Replay tx | ✅ bị từ chối, `tx_rejected reason=bad_nonce` |
| 4 | Tắt v1 | ✅ chain chạy tiếp; chỉ height mà v1 làm proposer (5, 9, …) phải sang round 1 |
| 5 | Tắt thêm v3 (còn 2 < quorum 3) | ✅ chain dừng, SAFETY vẫn OK |
| 6 | Bật lại v1, v3 | ❌ **cả mạng đứng hẳn.** v1, v3 bật lại ở height 1 (không có block sync), v0, v2 ở height 13 thiếu phiếu |
| 7 | drop 0.1 | ✅ 25 block / 25 s, toàn round 0, đủ tx |
| 7 | drop 0.3 | ⚠️ chậm: 4 block / 25 s. 1 node mất precommit của h4 nên kẹt lại ở h4; 3 node còn lại vừa đủ quorum và lệch round |
| 8 | 10 node, tắt v7, v8, v9 | ✅ chạy tiếp, đủ 30 tx. Chậm ở h7 vì round 0, 1, 2 đều trúng proposer đã tắt |

Không lần nào thấy vi phạm safety.

### Phát hiện cần báo nhóm

1. **D (D3, rất cần):** chưa có block sync nên node tụt height không bao giờ đuổi kịp. Sau một sự cố mất quorum, cả mạng không tự hồi phục (kịch bản 6). Khi drop cao, chỉ 1 node lỡ precommit là bị bỏ lại (kịch bản 7).
2. **D:** engine chưa có cơ chế nhảy round (ví dụ thấy f+1 vote của round cao hơn thì nhảy lên). Khi drop, các node lệch round và mất thêm vài round mới gặp lại nhau.
3. **A (node.py):** `devnet/node.py` có thể dùng làm bản mẫu. Hai chỗ cần làm giống: (a) proposer phát HEADER thay vì BODY; (b) **BODY_REQUEST phải tự xin lại**, vì đây là message 1-1, mất là mất hẳn. Bản đầu không xin lại nên khi có drop, round thất bại nhiều hơn hẳn.
4. **A + D:** nếu proposer giữ header lại (như `--block-ms` ở đây) thì mọi timeout phải dài hơn khoảng giữ đó. Nếu không, proposer hết giờ prevote trước khi node khác kịp vote, và mạng 3/4 node thất bại ở mọi height của proposer đó. Bản đầu của `run.py` đã dính lỗi này, giờ đã sửa.

### Giới hạn của bản ghép

- Node bật lại chạy lại từ genesis (không lưu xuống đĩa, không có block sync).
- Fault chỉ là drop/delay ngẫu nhiên đơn giản, chưa phải `FaultInjector` của B.
- Dashboard đọc trạng thái trong cùng process. Muốn xem nhiều process hoặc nhiều máy thì cần đọc từ file log JSONL (mục 6).

Mục tiêu: trong 1 buổi tối, chạy được **N validator thật** (TCP thật, chữ ký thật, engine thật của D, execution thật của B) và mở trình duyệt để **nhìn mạng L1 hoạt động**: node nào ở height/round/step nào, block nào đã finalize, các node có cùng hash không, tx đi vào chain ra sao, tắt 1 node thì chuyện gì xảy ra.

Không chờ `node.py`, `config.py`, `logger.py`, `launch_local.py` của A/B. Tối nay viết một **bản ghép tạm** đặt riêng trong `devnet/`, không đụng vào file của ai. Khi A viết `node.py` thật, phần ghép này chính là bản mẫu để A lấy lại.

## 1. Những gì đã có và khớp nhau (đã kiểm khi đọc code)

| Mảnh | API dùng tối nay |
|---|---|
| Key | `crypto.keypair_from_seed(42, "vN")` → `KeyPair(private_key, public_key)` |
| Mạng (C) | `TcpTransport`, `StaticPeers`, `handshake_fn(...)`, `Gossip.on(kind, handler)`, `broadcast_nowait`, `send_to_nowait`, `gossip.sender` |
| Consensus (D) | `ConsensusEngine(node_id, keypair, validator_keys, timeouts, state, mempool, max_txs_per_block, broadcast, schedule_timeout, cancel_timeout, on_finalize, logger, user_keys)`, rồi `start()`, `on_proposal(block)`, `on_vote(vote)`, `on_timeout(h, r, step)` |
| Execution (B) | `State()`, `Mempool()`, `mempool.add(tx)`, `validate_tx(state, tx, user_keys)` |
| Encoding (A) | `block_hash(header)`, `Message(kind, origin, height, payload)` |

Engine không dùng asyncio hay đồng hồ, nên bản ghép chỉ cần nối 4 callback. Đây là lý do có thể làm xong trong 1 tối.

## 2. Bản ghép: `devnet/node.py` (~150 dòng)

Một `DevNode` gồm transport, static peers, gossip, engine, mempool và state.

### 2.1 Nối callback của engine

| Callback của engine | Nối vào |
|---|---|
| `broadcast(msg)` | `self._out(msg)`. Hàm này chặn BODY lại để làm header-before-body (xem 2.2), còn lại gọi `gossip.broadcast_nowait(msg)` |
| `schedule_timeout(ms, h, r, step)` | `loop.call_later(ms / 1000, engine.on_timeout, h, r, step)`, trả về handle |
| `cancel_timeout(handle)` | `handle.cancel()` (huỷ handle đã chạy rồi cũng không sao) |
| `on_finalize(block, cert)` | ghi vào `self.chain[height] = block_hash`, đẩy event cho dashboard |
| `logger(event, **fields)` | `Recorder`: thêm `seq`, `t_ms`, lưu vào ring buffer cho dashboard và ghi `logs/devnet/vN.jsonl`. Network cũng dùng chung logger này, vì `events.emit` đã nhận được logger dạng hàm |

### 2.2 Header trước body (giải quyết TODO ở `engine.py:675`)

```
Proposer                         Node khác
engine._broadcast(BODY) ──► _out(): lưu bodies[hash] = block
                           gossip.broadcast_nowait(HEADER)  ──flood──► on HEADER:
                                                                         đã có body? → bỏ qua
                                                                         chưa có  → send_to_nowait(header.proposer, BODY_REQUEST(hash))
                                                                         return True (để gossip chuyển tiếp header)
on BODY_REQUEST ◄──────────────────────────────────────────────────────
  có body → send_to_nowait(peer, BODY(block))  ──direct──► on BODY:
                                                             block_hash(block.header) == hash đã xin? 
                                                             → bodies[hash] = block; engine.on_proposal(block)
```

- Xin body từ `header.proposer`, vì full mesh nên luôn có kết nối thẳng tới proposer. Không xin từ peer vừa chuyển tiếp, vì peer đó có thể cũng chưa có body.
- Node nào đã có body cũng trả lời BODY_REQUEST được. Sau này block sync dùng lại đường này.

### 2.3 Handler gossip

| Kind | Handler | Trả về (có chuyển tiếp không) |
|---|---|---|
| `TX` | `validate_tx(engine.state, tx, user_keys)` → `mempool.add(tx)` | `True` nếu vừa thêm |
| `HEADER` | như 2.2 | `True` |
| `VOTE` | `engine.on_vote(vote)` | `True` (dedup do gossip lo; VoteSet tự bỏ vote sai chữ ký) |
| `BODY_REQUEST`, `BODY` | như 2.2 | `False` (DIRECT, không chuyển tiếp) |

### 2.4 Lúc nào gọi `engine.start()`

Đợi tới khi `len(transport.peers) >= n - 1` hoặc hết 3 s, rồi mới `start()`. Nếu start sớm, proposer của (h=1, r=0) phát block khi chưa có ai nghe. Chain vẫn chạy tiếp ở round 1 nhưng mất vài giây vô ích.

### 2.5 Fault giả (thay cho `faults.py` của B tối nay)

Bọc `gossip.sender` bằng một hàm có `drop_rate` và `delay_ms`, dùng `random.Random(seed)`. Hàm này cũng **đếm số message trên từng cạnh** để dashboard vẽ lưu lượng. Khi B xong `FaultInjector` thì thay vào đúng chỗ này.

## 3. Cách xem mạng hoạt động: dashboard web

Chọn **1 process, 1 event loop**: `devnet/run.py` chạy N `DevNode` trên `127.0.0.1` cổng ngẫu nhiên, kèm một HTTP server nhỏ viết bằng `asyncio.start_server` (chỉ thư viện chuẩn, **không cần cài thêm gì**), cùng chạy trên loop đó. Dashboard đọc thẳng trạng thái thật của các engine, không có dữ liệu giả.

```bash
PYTHONPATH=src python -m devnet.run --nodes 4 --port 8080
# mở http://localhost:8080
```

### 3.1 Giao diện (1 trang HTML + JS thuần, poll mỗi 500 ms)

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ L1 devnet   n=4  f=1  quorum=3   height 7   ✅ SAFETY OK   [Gửi 5 tx] drop[0.0▾]│
├───────────────────────────────┬──────────────────────────────────────────────┤
│        (v0) h7 r0 PREVOTE     │ Chain       v0      v1      v2      v3       │
│       ╱   ╲                   │  h1       3fa9c1  3fa9c1  3fa9c1  3fa9c1      │
│   (v3)─────(v1)  ← dày = nhiều│  h2       b81e02  b81e02  b81e02  b81e02      │
│       ╲   ╱       message     │  ...      (cùng hash = cùng màu)             │
│        (v2) ✖ đã tắt          │  h7       ·       ·       ·       ✖          │
│  màu node = step; ★ proposer  │                                              │
│  [Tắt v2] [Bật lại v2]        │ Mempool: v0 3 · v1 3 · v2 – · v3 3            │
├───────────────────────────────┴──────────────────────────────────────────────┤
│ Event feed  [lọc node ▾] [lọc event ▾]                                         │
│ 1203 v1 h7 r0 prevote block=3fa9c1                                            │
│ 1204 v3 h7 r0 handshake_failed reason=unknown_peer                            │
└──────────────────────────────────────────────────────────────────────────────┘
```

| Vùng | Hiện gì | Lấy từ |
|---|---|---|
| Thanh trên | n, f, quorum, height cao nhất, **SAFETY**: đỏ nếu 2 node có hash khác nhau ở cùng height | `engine.finalized` của mọi node |
| Vòng node (SVG) | màu theo `step`, ★ là proposer hiện tại, xám là node đã tắt; cạnh = kết nối đang mở, độ dày = số message trong 2 s gần nhất | `engine.height/round/step`, `transport.peers`, bộ đếm ở 2.5 |
| Bảng chain | hàng = height, cột = node, ô = 6 ký tự đầu của block hash, tô màu theo hash | `engine.finalized` |
| Event feed | event mới nhất của engine và network, lọc được | `Recorder` |
| Nút bấm | gửi tx, tắt/bật node, chỉnh drop rate | `POST /api/...` |

### 3.2 API của server

| Route | Việc |
|---|---|
| `GET /` | trả `devnet/dashboard.html` |
| `GET /api/state` | ảnh chụp: mỗi node `{height, round, step, proposer, peers, mempool, alive}`, `chain[h][node]`, `safety_ok`, lưu lượng từng cạnh |
| `GET /api/events?after=<seq>` | event mới từ `seq` trở đi |
| `POST /api/tx?n=5` | ký 5 tx bằng key user `u0..u4` (`keypair_from_seed(42, "uN")`), gửi vào gossip qua 1 node |
| `POST /api/node/<id>/stop`, `/start` | `stop()` / tạo lại `DevNode` cùng port |
| `POST /api/fault?drop=0.2&delay=50` | đổi tham số fault ở 2.5 |

## 4. Kịch bản thử tối nay (bấm trên dashboard)

| # | Làm | Phải thấy | Tương ứng test |
|---|---|---|---|
| 1 | Chạy 4 node, không làm gì | height tăng đều (block rỗng), cả bảng chain cùng màu | T1 (rút gọn) |
| 2 | Bấm "Gửi 5 tx" vài lần | mempool tăng rồi về 0, `txs` trong event `finalize` > 0, `state_hash` giống nhau | T1 |
| 3 | Gửi lại đúng tx cũ (nút "replay") | bị từ chối ở `validate_tx`, không vào block | T4 |
| 4 | Tắt v2 (n=4, f=1) | 3 node còn lại vẫn finalize; khi tới lượt v2 làm proposer thì round tăng lên 1 rồi mới finalize | T6, D2 |
| 5 | Tắt thêm v3 (còn 2 < quorum 3) | height **dừng**, nhưng SAFETY vẫn OK: mất liveness, không mất safety | minh hoạ BFT |
| 6 | Bật lại v2, v3 | `peer_added`, chain chạy tiếp; v2 mới bật thì lệch height vì chưa có block sync (giới hạn đã biết, chờ D) | D2, D3 (một phần) |
| 7 | Kéo drop lên 0.3 | thấy nhiều round > 0, chain chậm lại nhưng vẫn đúng | T5 (không có locking nên chỉ xem, chưa phải bằng chứng safety) |
| 8 | Chạy 10 node | như #1–#2 với n=10, f=3 | T1 thật |

## 5. Thứ tự làm tối nay (ước lượng)

| Bước | Việc | Thời gian | Xong khi |
|---|---|---|---|
| 1 | `devnet/node.py`: `DevNode`, nối callback, header-before-body | 1 h | — |
| 2 | `tests/integration/test_devnet.py`: 4 `DevNode` tới height 3 cùng hash, có tx | 30 ph | **I1 + I2 của TEST_PLAN pass**. Đây là mốc quan trọng nhất |
| 3 | `devnet/run.py` + HTTP server + `/api/state` | 45 ph | `curl /api/state` ra JSON đúng |
| 4 | `devnet/dashboard.html`: vòng node, bảng chain, event feed | 1 h | xem được kịch bản #1–#2 |
| 5 | Nút tắt/bật node, gửi tx, drop | 30 ph | kịch bản #3–#8 |

Nếu thiếu thời gian, dừng sau bước 2 vẫn có giá trị: lần đầu cả 4 phần A/B/C/D chạy chung và có test tự động.

## 6. Rủi ro và giới hạn biết trước

- **Không có locking (D chưa làm):** khi có drop, về lý thuyết có thể mất safety. Dashboard sẽ báo đỏ nếu chuyện đó xảy ra. Đó là phát hiện tốt, không phải lỗi của dashboard.
- **Không có block sync:** node bật lại không đuổi kịp height. Bảng chain sẽ thấy rõ.
- **1 process:** fault và timing không giống 10 process thật, nhưng code mạng vẫn là TCP thật. Khi B có `launch_local.py`, cho mỗi `run_node` ghi `logs/devnet/vN.jsonl`, rồi để dashboard đọc file thay vì đọc object, là xem được cả mạng nhiều process hoặc nhiều máy qua ZeroTier mà không phải đổi giao diện.
- **Không đụng file của người khác:** mọi thứ nằm trong `devnet/` và `tests/integration/`. `config/default.json` (chain_id sai) không dùng; `run.py` tự tạo config trong code.
- `devnet/` không nằm trong package `l1sim`, nên không ảnh hưởng `pip install` hay coverage của mọi người.

## Thêm: người dùng tự gửi tx

- Dashboard có khung **"Người dùng gửi tx"**: chọn user `u0..u4`, nhập key/value, chọn node nhận, nonce (để trống = tự động), có thể ký bằng key user khác để thử chữ ký giả. Khung **"State của node"** hiện key → value và nonce on-chain của node đang chọn.
- API:
  - `POST /api/tx/custom?user=&key=&value=&via=&nonce=&sign_as=` trả `{accepted, reason}`. `reason` có thể là `bad_signature`, `not_owner`, `bad_nonce`.
  - `GET /api/kv?node=vN` trả state của node đó.
- `not_owner` bị chặn ngay khi tạo `Tx` (model của A kiểm `key` phải bắt đầu bằng `sender/`), nên tx đó không bao giờ đi vào mạng.
- Test: `tests/integration/test_devnet.py::test_user_custom_tx_lands_in_state`.
- Chỉ sửa `devnet/` và test, không đụng file của A/B/D.
