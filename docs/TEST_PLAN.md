# Kế hoạch test toàn bộ ứng dụng và những gì còn thiếu

Cập nhật: 31/10/2026, trên `main` = `d8bffd8` cộng phần network (Role C) chưa commit.
Mốc từ TEAM_PLAN: **01/11 T1 pass**, **04/11 mọi test pass**.

## 1. Hiện trạng

| Phần | Owner | Có code | Unit test |
|---|---|---|---|
| `models`, `encoding`, `crypto` | A | ✅ | ✅ `test_crypto`, `test_encoding` |
| `execution/` (state, mempool, validation) | B | ✅ | ✅ `test_execution` (26 test) |
| `consensus/vote_set.py` | D | ✅ | ✅ `test_vote_set` |
| `consensus/engine.py` | D | ⚠️ chưa có locking (rule 4–5, "PR 2") | ✅ `test_engine`, chạy trên mạng giả trong bộ nhớ |
| `network/` (transport, hello, static_peers, gossip, events) | C | ✅ | ✅ 5 file test, chạy TCP thật trong 1 process |

`PYTHONPATH=src pytest` cho **133 passed**, coverage **93%**.

**Chưa có gì chạy được từ đầu tới cuối.** Mỗi module chỉ được test riêng. Chưa có `node.py` để ghép, chưa có process nào chạy thật. Vì vậy chưa test e2e nào (T1–T8, D1–D3) chạy được, và `tests/e2e/` đang trống.

## 2. Còn thiếu gì

### 2.1 Code (theo `TEAM_PLAN.md` mục Cấu trúc thư mục)

| Thiếu | Owner | Chặn test nào |
|---|---|---|
| `config.py` + `config/genesis.json` + scenario `config/t*.json`, `d*.json` | A | tất cả e2e |
| `logger.py`: event log JSONL và results writer | A | tất cả e2e, T8 |
| **`node.py`**: ghép transport, gossip, engine, mempool, state, route message | A | **tất cả e2e: đây là đường găng** |
| `run_node.py` (chạy 1 node / 1 terminal), `client.py` (gửi tx của scenario) | A | T1, T4, demo |
| `tests/run_tests.py`, `tests/requirements.txt` | A | người chấm chạy test |
| `network/faults.py`: FaultInjector gắn vào `gossip.sender` | B | T2, T5 |
| `network/peer_limits.py`: rate limit, block/unblock | B | log `block`/`unblock` |
| `launch_local.py`: bật N process theo scenario, kill/restart theo height | B | tất cả e2e, D2, D3 |
| `tests/check_safety.py`, `tests/check_reproducible.py` | B | assert của mọi e2e, T8 |
| Locking rule 4–5 trong `engine.py` | D | T5, T7 (safety khi có fault hoặc Byzantine) |
| `consensus/byzantine.py`: silent, equivocate | D | T3, T6, T7 |
| `consensus/block_sync.py` + `verify_certificate` | D | D3, T5 (node tụt lại) |
| Multicast discovery (tuỳ chọn) | C | D1 theo định nghĩa gốc |

### 2.2 Chỗ lệch giữa các phần đã có (phải sửa trước khi chạy e2e)

| # | Vấn đề | Ở đâu | Ai sửa |
|---|---|---|---|
| 1 | `config/default.json` ghi `chain_id: "lab01-devnet"`, `num_validators: 8`, nhưng `models.CHAIN_ID = "lab01"` (Hello từ chối chain khác) và kế hoạch dùng 10 node | `config/default.json` | A |
| 2 | Config `network` chưa có khối `peers` mà `StaticPeers` cần; dùng `gst_ms` trong khi TEAM_PLAN chốt GST theo **height** (`gst_height`) | `config/default.json` | A + B |
| 3 | Engine broadcast `Kind.BODY` (`engine.py:675`, có TODO); gossip chỉ cho broadcast TX/HEADER/VOTE | `node.py` phải tách HEADER → BODY_REQUEST → BODY | A |
| 4 | Engine và gossip cùng log `duplicate_ignored` | chốt cách đếm cho T2 | C + D |
| 5 | `scripts/check_determinism.sh` gọi `python -m l1sim.run`, module này không tồn tại | script | B |
| 6 | `make lint` local fail vì `UP038` ở `encoding.py:56,62`, `models.py:69` (ruff 0.12.1). CI có thể dùng ruff khác phiên bản. Nên ghim phiên bản ruff | `pyproject.toml` | A |
| 7 | Gói chưa cài thì `pytest` báo `No module named 'l1sim'`; cần `make install` hoặc `PYTHONPATH=src` | README | A |
| 8 | Interface Gossip trong TEAM_PLAN chưa cập nhật theo code (`on(kind, handler)`, `broadcast_nowait`, `StaticPeers` thay `Discovery`) | `TEAM_PLAN.md` | C |

### 2.3 Unit test tối thiểu trong TEAM_PLAN: đã có / còn thiếu

| Mục | Trạng thái |
|---|---|
| crypto: sửa field, sai domain, vote height khác | ✅ |
| encoding: thứ tự key, round-trip | ✅ |
| tcp_transport: 2 frame dính nhau | ✅ |
| HELLO sai chữ ký / id lạ | ✅ (`test_hello`) |
| ANNOUNCE, chọn neighbor | ➖ không áp dụng cho bản LITE (full mesh); chỉ cần nếu làm multicast |
| gossip: nhận từ 2 peer thì handler chạy 1 lần | ✅. **Thiếu:** assert "không gửi ngược lại người gửi" (C) |
| faults: cùng input cho cùng quyết định, không có delay trong vùng cấm | ❌ chưa có `faults.py` (B) |
| peer_limits: vượt hạn mức thì block, hết hạn thì unblock | ❌ (B) |
| state, validation, mempool | ✅ |
| vote_set: dedup, equivocation, quorum 2f+1 | ✅ |
| engine: đã lock B thì không prevote C | ❌ chưa có locking (D) |
| engine: không ký 2 prevote cùng (h, r) | ✅ |
| block_sync: certificate thiếu hoặc giả bị từ chối | ❌ (D) |

## 3. Kế hoạch test theo tầng

Thứ tự từ nhanh và rẻ tới chậm và đắt. Tầng dưới phải xanh rồi mới lên tầng trên.

```
L0  lint            ruff check + ruff format --check                  mỗi commit (CI)
L1  unit            từng module, không I/O ngoài localhost            mỗi commit (CI), < 5 s
L2  integration     N Node đầy đủ trong 1 process, TCP thật           mỗi commit (CI), < 30 s
L3  e2e             10 process qua launch_local, scenario T1–T8, D1–D3  trước merge / hằng ngày
L4  reproducibility T8: mỗi scenario chạy 2 lần, so logs/results/     trước nộp
L5  nhiều máy       ZeroTier laptop + homelab, chạy tay               demo 03–04/11
```

### L0 – Lint

- `make lint`. Trước tiên sửa mục 2.2 #6 (ghim ruff, hoặc sửa 3 dòng `UP038` của A).

### L1 – Unit (đã có 133, cần bổ sung)

| Thêm test | Owner |
|---|---|
| `test_faults.py`: quyết định là hàm thuần của `(seed, src, dst, message_id)`; gọi theo thứ tự khác vẫn ra cùng kết quả; delay chỉ nằm ở 2 cực; sau `gst_height` không drop | B |
| `test_peer_limits.py` | B |
| Locking trong `test_engine.py`: lock B ở round r, round r+1 có proposal C thì prevote NIL; có polka mới hơn thì unlock | D |
| `test_block_sync.py`: certificate thiếu 1 chữ ký, chữ ký giả, height lệch | D |
| `test_config.py`: genesis đọc ra đúng validator và public key; scenario thiếu trường thì báo lỗi | A |
| `test_logger.py`: results ghi bằng `encode()`, không có timestamp; 2 lần ghi cùng dữ liệu ra cùng bytes | A |
| `test_gossip.py`: forward không gửi lại cho peer vừa gửi tới | C |

### L2 – Integration trong 1 process (mới, nên có trước e2e)

Mục đích là bắt lỗi ghép nối trong vài giây, không phải chờ 10 process. Dựng N `Node` (`node.py`) trên `127.0.0.1:0` giống `tests/unit/p2p_cluster.py`, nhưng dùng engine thật thay vì handler giả.

| ID | Kịch bản | Assert | Owner |
|---|---|---|---|
| I1 | 4 node, không fault, 5 tx, tới height 3 | mọi node cùng `block_hash` ở mỗi height, cùng `state_hash` | A + C |
| I2 | Header trước body: chặn BODY ở mọi node trừ proposer | node khác gửi `BODY_REQUEST` và nhận BODY qua `send_to`; không node nào nhận BODY qua broadcast | A + C |
| I3 | `gossip.sender` = FaultInjector, `dup_rate=1.0` | `duplicate_ignored > 0`, VoteSet không đếm trùng, vẫn finalize | B + C |
| I4 | FaultInjector drop cạnh v0→v3 | v3 vẫn nhận vote/header qua chuyển tiếp, vẫn finalize | B + C |
| I5 | Tắt 1 node giữa chừng (n=4, f=1) | 3 node còn lại vẫn finalize; log `peer_removed` | C + D |
| I6 | Node lạ / key sai cố nối vào | `handshake_failed reason=unknown_peer / bad_signature`, không ảnh hưởng chain | C |

Đặt trong `tests/integration/`, chạy trong CI. Mỗi test < 5 s, dùng `consensus.timeout_*` nhỏ (~100 ms).

### L3 – E2E (10 process, `pytest -m e2e`)

Mọi test đều có dạng: `launch_local.py config/<scenario>.json`, chờ tới height mục tiêu hoặc hết thời gian, rồi đọc `logs/results/<scenario>/*.json`. Assert chung cho **mọi** scenario: `check_safety()` (mỗi height đúng 1 block hash trên mọi node).

| Test | Scenario | Assert riêng | Bị chặn bởi | Owner |
|---|---|---|---|---|
| T1 | 10 node, ~20 tx, height 5, không fault | cùng hash từng height, cùng `state_hash` cuối | node.py, config, logger, launch_local, client | A |
| T2 | `dup_rate=0.3`, delay hai cực | không vote nào bị đếm 2 lần; results có `duplicate_ignored` | + faults.py, chốt #4 | C |
| T3 | node gửi tx/vote chữ ký sai, vote ký domain TX, HELLO mạo danh | đếm đúng `bad_signature` / `wrong_domain` / `unknown_peer` | + byzantine.py | A |
| T4 | gửi lại cùng tx ở nhiều height | tx chỉ có 1 lần trong chain | + client.py | B |
| T5 | `drop_rate=0.4` khi `height < gst_height` | safety luôn đúng; sau GST chain tiếp tục | + faults.py, **locking**, block_sync | B |
| T6 | proposer của (h=2, r=0) im lặng | h=2 finalize ở round 1 | + byzantine.py (silent) | D |
| T7 | f=3 node equivocate | không finalize xung đột; results có danh sách equivocation | + byzantine.py, **locking** | D |
| D1 | bật node theo thứ tự xáo trộn | mọi node đủ 9 peer (full mesh từ config), T1 vẫn pass | node.py; **chốt định nghĩa D1** | C |
| D2 | kill 1 peer của v0 sau height 2 | v0 log `peer_removed` rồi `peer_added` khi bật lại, chain tiếp tục | + launch_local kill/restart theo height | C |
| D3 | kill v8 ở height 3, bật lại ở height 6 | v8 block sync tới height hiện tại, cùng chain | + block_sync, `on_hello` → `peer_heights` | D |

### L4 – Reproducibility (T8)

- `check_reproducible.py`: chạy **mọi** scenario 2 lần, `diff -r logs/results/` phải byte-identical.
- Những gì **không** được đưa vào results: timestamp, port thật, số `duplicate_ignored` của gossip (phụ thuộc thời điểm), thứ tự `conn_open`.
- Những gì **được** đưa vào: block hash từng height, `state_hash`, danh sách peers đã sort, số reject theo reason, danh sách equivocation đã sort.
- Riêng network, cần kiểm: chạy D2 2 lần, `peers` cuối trong results giống nhau.

### L5 – Nhiều máy qua ZeroTier (chạy tay)

- Chạy `scripts/mcast_test.py` để kiểm kết nối, mở firewall dải TCP port.
- Config `network.peers` dùng IP ZeroTier, `--listen 0.0.0.0:<port>`.
- Chạy T1 và T7 chia 5 node laptop / 5 node homelab. So results với lần chạy một máy, chúng phải giống nhau.
- Thử laptop dùng 5G hotspot.

## 4. Thứ tự làm (đường găng)

```
31/10  A: config.py + genesis + logger.py ──┐
       B: faults.py + launch_local.py       │
       D: locking (PR 2)                    │
       C: test "không gửi ngược", chốt #4/#8│
                                            ▼
01/11  A + C: node.py → I1, I2 (L2) → T1 (L3)     ← gate "T1 pass"
02/11  B + C: I3, I4 → T2, T4, T5 ; check_safety
       D: byzantine.py → T6, T7
03/11  D: block_sync → D3 ; C: D1, D2 ; B: check_reproducible → T8
       A: ZeroTier T1, T7 (L5)
04/11  run_tests.py cho người chấm; mọi test xanh; T8 trên mọi scenario   ← gate
```

**Việc C có thể làm ngay, không phải chờ ai:**

1. Thêm test "forward không gửi ngược lại người gửi" vào `test_gossip.py`.
2. Viết sẵn I6 (node lạ nối vào) bằng `p2p_cluster.py`.
3. Viết sẵn khung test D1/D2 trong `tests/e2e/`, đánh dấu `skip` cho tới khi có `launch_local.py`.
4. Đề xuất với A khối config:
   ```json
   "network": {"peers": {"v0": "127.0.0.1:9000", "...": "..."}, "retry_interval": 1.0}
   ```
5. Commit phần network để A bắt đầu `node.py`. Đây là việc chặn đường găng nhiều nhất.

## 5. Lệnh kiểm tra

```bash
make install            # hoặc: export PYTHONPATH=src
make lint               # L0
pytest tests/unit       # L1
pytest tests/integration   # L2 (khi có)
pytest -m e2e           # L3 (khi có)
python tests/check_reproducible.py   # L4 (khi có)
```
