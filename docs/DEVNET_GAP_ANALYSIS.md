# Ghép PR #13, #15 với network (C) và còn thiếu gì để chạy hoàn chỉnh

Ngày 2026-11-02. Dựa trên `main` tại `fad9f21` (đã có PR #13 config/logger/client và PR #15 logger của engine), cộng phần network của C (chưa commit) và `devnet/`.

## 1. Kết quả demo hôm nay

Lần đầu **mọi mảnh của cả nhóm chạy chung trên scenario thật** `config/t1_normal.json` (10 node V0..V9, 20 tx, max_height 5):

```
genesis.json + t1_normal.json ──load_config (A)──► devnet/run.py
TxClient (A) ── ký 20 tx ──► DevNode.submit_tx ──flood TX (C)──► mempool (B) của 10 node
ConsensusEngine (D) ── HEADER flood / BODY_REQUEST / BODY (C) ──► validate_block + apply (B)
mỗi node ── EventLogger (A) ──► logs/events/t1_normal/V*.jsonl
kết thúc ── ResultWriter (A) ──► logs/results/t1_normal/V*.json + verdict.json
```

Lệnh:

```bash
python devnet/run.py --scenario config/t1_normal.json --headless   # chạy tới max_height rồi thoát
python devnet/run.py --scenario config/t1_normal.json              # có dashboard http://localhost:8080
```

| Kiểm tra | Kết quả |
|---|---|
| T1: 10 node, 20 tx, height 5 | ✅ 5 block, mỗi block đúng 4 tx (= `max_txs_per_block`), cả 10 node cùng hash từng height, cùng `state_hash` `6820bef7…` |
| State cuối | `Alice/account=hello-19`, `Bob/account=hello-20`, `Charlie/account=hello-18`: đúng tx nonce cuối của mỗi người |
| T8: chạy 2 lần | ✅ `logs/results/t1_normal/` **giống từng byte**; `logs/events/` khác (cổng TCP, thứ tự event), README cho phép |
| Tắt f = 3 node (dashboard) | ✅ 7 node còn lại vẫn finalize (h6 → h11), safety OK |
| Tắt node thứ 4 (còn 6 < quorum 7) | ✅ chain dừng ở h11, **không** finalize sai (safety OK) |
| Bật lại 1 node | ⚠️ node đó quay về h1 và không theo kịp: **chưa có block sync** |
| Tx sai (dashboard / curl) | ✅ `bad_signature`, `bad_nonce` bị từ chối; `not_owner` bị `Tx` chặn ngay khi tạo |
| Test tự động | ✅ `PYTHONPATH=src pytest`: **168 passed** (thêm `test_t1_normal_scenario_end_to_end`) |

## 2. Đã sửa gì để ghép được

### 2.1 Network (C), chỉ file của C

| File | Sửa | Vì sao |
|---|---|---|
| `network/events.py` | Logger kiểu object (có `.event`, như `EventLogger`) **không được truyền `node`** nữa | `EventLogger` tự ghi `node` và báo `ValueError` nếu caller truyền `node`. Cùng quy ước với engine sau PR #15. Logger kiểu hàm (dùng chung nhiều node) vẫn được truyền `node`. |
| `tests/unit/test_network_events.py` | Cập nhật kỳ vọng + thêm test ghi thật vào `EventLogger` | Bắt lỗi trên nếu có ai đổi lại |
| `tests/unit/p2p_cluster.py` | Logger giả dùng chung cả cluster đổi từ `.event()` sang `__call__` | Dùng chung nhiều node thì phải là kiểu hàm để nhận `node` |

Trước khi sửa, 166 test vẫn pass, vì **chưa test nào nối network với `EventLogger` thật**. Lỗi chỉ lộ khi ghép.

### 2.2 Devnet (`devnet/`, ngoài package `l1sim`)

- Id validator đổi `v0` → **`V0`** cho khớp `genesis.json`. Key vẫn là `keypair_from_seed(seed, id)`, nên **khớp public key trong genesis**.
- `--scenario`: đọc `load_config` (A), timeout bằng `TimeoutConfig.from_config` (D), `max_txs_per_block`, `drop_rate`, `min/max_delay_ms`, `warmup_ms` lấy từ scenario.
- Gửi tx bằng **`TxClient` của A** (`Devnet.submit_scenario`).
- `NodeLog`: mỗi node một logger, gắn `node` rồi ghi vào Recorder (dashboard) và `EventLogger` (JSONL). Engine không gửi `node` nữa (PR #15), nên phải có lớp này.
- Kết quả bằng `ResultWriter` của A: `V*.json` (các block, state, state_hash) và `verdict.json`.
- Thứ tự khởi động theo scenario: nối mesh → gửi tx → dùng `warmup_ms` cho tx lan tới mempool mọi node → **mới cho engine chạy**. Nếu không, 3 block đầu rỗng và proposer height 1 chỉ có 1 tx (đã gặp khi chạy thử).
- `gossip_duplicate` (trước đây gossip cũng gọi là `duplicate_ignored`) chỉ đếm, không ghi vào file và feed: 10 node full mesh thì mỗi message tới 9 lần, chiếm 829 trên 901 dòng log của V0.

## 3. Còn thiếu gì để chạy hoàn chỉnh (không cần devnet)

Devnet đang **đứng tạm** ở 3 chỗ. Khi có file thật của nhóm thì bỏ devnet.

| Devnet đang làm thay | File thật cần có | Owner |
|---|---|---|
| `devnet/node.py` (`DevNode`: ghép transport + gossip + engine + mempool, header trước body, xin lại body) | `l1sim/node.py`, `run_node.py` | A |
| `devnet/net.py` (N node trong 1 process, kill/restart) | `launch_local.py` (N process, kill/restart theo height) | B |
| `Faults` trong `devnet/node.py` | `network/faults.py` (`FaultInjector`), đã có ở branch `feat/network-faults`, chưa merge | B |
| `safety_violations`, `verdict.json` | `tests/check_safety.py`, `tests/check_reproducible.py` | B |

### 3.1 Theo từng người, xếp theo mức chặn

**A**
1. **`node.py`** là đường găng: chép logic từ `devnet/node.py`. Bắt buộc có:
   - HEADER flood, BODY_REQUEST thẳng proposer, BODY 1-1, **xin lại body** (BODY 1-1 mất là mất hẳn);
   - mỗi node một `EventLogger`;
   - **giữ engine tới khi tx của scenario đã vào mempool** (mục 2.2).
2. `run_node.py` (1 node / 1 terminal, đọc địa chỉ peer từ config) và `tests/run_tests.py`.
3. Config chưa có khối **`peers`** (địa chỉ host:port từng validator) mà `StaticPeers` cần để chạy nhiều process. Devnet tự cấp cổng nên chưa cần.
4. `EventLogger` dùng **bộ đếm** làm `time` (0, 1, 2…). Đề yêu cầu "timestamp". Cần chốt: giữ bộ đếm (tốt cho T8) hay `time_provider` mili-giây.

**B**
1. Merge `feat/network-faults` (`FaultInjector`, `PeerLimits`). ⚠️ **Sẽ xung đột với C**: branch này tạo `src/l1sim/network/__init__.py` (export `FaultInjector`, …), trong khi network của C cũng có `__init__.py` (rỗng) nhưng **chưa commit**. Ai merge sau thì giữ nội dung export của B.
2. `launch_local.py` + `check_safety.py` + `check_reproducible.py`.
3. `scripts/check_determinism.sh` gọi `python -m l1sim.run`, module này không tồn tại. Tạm thời có thể dùng `devnet/run.py --scenario … --headless` rồi `diff -r logs/results`.
4. Scenario còn lại: `t2…t8.json`, `d1…d3.json`. Hiện chỉ có `t1_normal.json`.

**C (bạn)**
1. **Commit + mở PR phần network**, trước khi B merge `feat/network-faults`, để giải quyết xung đột `__init__.py` sớm.
2. Gắn `FaultInjector` của B vào `gossip.sender` (cùng chỗ `Faults` của devnet đang gắn) và viết test T2 (dup/reorder) với faults thật.
3. ~~Chốt với D cách đếm `duplicate_ignored` cho T2~~: đã tách, gossip log `gossip_duplicate`, `duplicate_ignored` chỉ còn của engine (review PR #16).
4. (Tuỳ chọn) multicast discovery cho D1.

**D**
1. **Locking (rule 4–5)**: `locked_block` vẫn là placeholder. Không có locking thì T5 (drop trước GST) và T7 (equivocate) **có thể mất safety**. Devnet chưa thấy lỗi vì chưa có node gian lận.
2. **Block sync** (`GET_BLOCKS`/`BLOCKS` network đã hỗ trợ) + `verify_certificate`: thiếu cái này thì restart node là kẹt (mục 1), chặn D3 và T5.
3. **Round-skip**: node tụt round không tự nhảy lên khi thấy f+1 vote round cao hơn (đã gặp khi drop 0.3).
4. `consensus/byzantine.py`: silent (T6), equivocate (T7), chữ ký sai (T3).
5. Engine **chạy quá `max_height`**: devnet tự dừng khi đủ height. Cần chốt `node.py` dừng engine hay engine tự dừng.

### 3.2 Trạng thái T1–T8 nếu chạy bằng devnet hôm nay

| Test | Chạy được bằng devnet? | Còn thiếu |
|---|---|---|
| T1 normal | ✅ đủ (scenario thật, verdict pass) | chỉ cần node.py + launch_local để chạy nhiều process |
| T2 dup/reorder | ⚠️ có delay/drop, **chưa có duplicate** | `FaultInjector` (B), scenario t2, đếm `gossip_duplicate` + kiểm voter khác nhau trong certificate |
| T3 chữ ký sai | ⚠️ tx sai chữ ký được (dashboard); vote/HELLO sai thì chưa | byzantine.py (D) |
| T4 replay tx | ✅ nút Replay / test `test_replayed_tx_is_rejected` | scenario t4 |
| T5 drop trước GST | ⚠️ drop được, nhưng **không có locking và block sync** | D: locking + block sync; B: GST theo height |
| T6 proposer im lặng | ⚠️ thay bằng **tắt node** (crash) → round 1 ✅ | byzantine `silent` đúng nghĩa (D) |
| T7 f equivocate | ❌ | byzantine.py + locking (D) |
| T8 determinism | ✅ results giống từng byte qua 2 lần chạy | sửa `check_determinism.sh` (B) |

## 4. Thứ tự làm tiếp (đề xuất)

1. **C**: commit + PR network. **B**: rebase `feat/network-faults` lên đó, giữ `__init__.py` của B.
2. **A**: `node.py` từ `devnet/node.py` → **T1 chạy nhiều process** với `run_node.py`.
3. **D**: locking → block sync → round-skip (theo thứ tự này: locking là safety, hai cái sau là liveness).
4. **B**: `launch_local.py`, `check_safety.py`, scenario t2..t8, sửa `check_determinism.sh`.
5. **D**: byzantine.py → T3, T6, T7.
6. Chạy cả T1–T8 + D1–D3, so với devnet: cùng scenario thì `logs/results` phải ra giống nhau.

## 5. Lỗi nhỏ thấy khi ghép (không chặn)

- `ruff check .` fail 4 lỗi `UP038` ở `config.py:119`, `encoding.py:56,62`, `models.py:69` (ruff 0.12.1 local). Không phải file của C; nên ghim phiên bản ruff trong `pyproject.toml` cho khớp CI.
- `load_scenario`/`load_genesis` bắt buộc **n ≥ 8**. Devnet không dùng `--scenario` vẫn cho chạy 4 node để demo, không qua loader.
