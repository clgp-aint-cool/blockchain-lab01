# Còn thiếu gì để bỏ devnet và test đầy đủ T1–T8, D1–D3

Ngày 2026-11-03. Đọc cùng [DEVNET_TO_PROJECT.md](DEVNET_TO_PROJECT.md) (cách chuyển từng phần devnet) và [TEAM_PLAN.md](TEAM_PLAN.md) (mục Testing plan, Phân công).

## Hiện trạng

`main` (tại `fad9f21`) đã có:

| Phần | Role |
|---|---|
| models, encoding, crypto, config, logger, client | A |
| execution: state, mempool, validation | B |
| engine, vote_set, timeout | D |

Còn lại:

- Network của C đang ở PR #16, chờ review lại.
- `faults.py`, `peer_limits.py` của B nằm ở branch `feat/network-faults`, **chưa có PR**.
- **Chưa có**: `node.py`, `run_node.py`, `launch_local.py`, `check_safety.py`, `check_reproducible.py`, `byzantine.py`, `block_sync`, `tests/run_tests.py`, `tests/requirements.txt`, `scripts/package.sh`, scenario ngoài `t1_normal.json`.

Theo timeline của TEAM_PLAN, nộp bài tối **05/11**.

## Giai đoạn 1: bỏ devnet (T1 chạy bằng nhiều process thật)

| # | Việc | Role | Ghi chú |
|---|---|---|---|
| 1 | Merge PR #16 (network) | **C** | Đang chờ review lại |
| 2 | Mở PR cho `feat/network-faults` (`FaultInjector`, `PeerLimits`), rebase lên `main` sau khi #16 merge | **B** | Branch này cũng tạo `network/__init__.py`; khi rebase giữ nội dung export của B |
| 3 | Thêm khối `peers` (host:port của từng validator) vào config; `load_config` đọc khối này | **A** | `StaticPeers` cần biết nối tới đâu; hiện config chưa có |
| 4 | **`node.py`**: chép `DevNode` từ `devnet/node.py`, rồi sửa:<br>• truyền `event_logger.event` cho cả engine lẫn network<br>• `Faults` → `FaultInjector`<br>• chờ `warmup_ms` rồi mới chạy engine<br>• tự dừng ở `max_height`<br>• ghi `ResultWriter` khi dừng | **A** | **Đường găng**: mọi việc phía sau đều chờ việc này |
| 5 | `run_node.py --id V0 --scenario …` (1 node / 1 terminal) | **A** | |
| 6 | `client.py --connect host:port`: gửi tx vào mạng thật | **A** | Hiện CLI chỉ in tx ra stdout |
| 7 | `check_safety.py`: đọc `logs/results/<scenario>/`, so chéo các node | **B** | Thay cho `verdict.json` của devnet |
| 8 | `launch_local.py`: bật N process, gửi tx, chờ, gọi `check_safety` | **B** | Thay cho `devnet/net.py` |
| 9 | Gắn `FaultInjector` vào `gossip.sender` | **C** + B | Cùng chỗ `Faults` của devnet đang gắn |
| 10 | Đối chiếu: chạy `t1_normal` bằng bản thật, rồi `diff -r logs/results` với bản devnet | **cả nhóm** | `diff` rỗng thì xoá `devnet/` và `tests/integration/test_devnet.py`, chuyển các test hay sang `tests/e2e/` |

## Giai đoạn 2: test đầy đủ T1–T8, D1–D3

| Test | Còn thiếu | Role |
|---|---|---|
| **T1** normal | Giai đoạn 1 + `tests/e2e/test_t1_normal.py` | A (owner), B (`launch_local`) |
| **T2** dup/reorder | Scenario `t2_dup_reorder.json` (`dup_rate`), `FaultInjector` đã gắn, test e2e. Bằng chứng: số `gossip_duplicate` và mỗi certificate có n − f voter **khác nhau**. Results phải có số duplicate (theo TEAM_PLAN) | **C** (owner), B (scenario, faults) |
| **T3** chữ ký sai | `byzantine.py` gửi được vote sai chữ ký, vote ký sai domain, HELLO mạo danh. `node.py` đếm các reason `bad_signature` / `wrong_domain` / `unknown_peer` vào results. Network đã từ chối HELLO sai sẵn | **A** (owner), D (`byzantine.py`) |
| **T4** replay tx | Scenario `t4_replay_tx.json` + test e2e. Logic đã có (mempool, nonce) | **B** |
| **T5** drop trước GST | **Locking** + **block sync** (D), fault theo `gst_height` (B), scenario t5 | **B** (owner), **D** |
| **T6** proposer im lặng | `byzantine.py` chế độ `silent`, scenario t6. Timeout sang round mới đã chạy được | **D** |
| **T7** f node equivocate | `byzantine.py` (vote 2 block, propose 2 block) + **locking**. Thiếu locking thì có thể mất safety | **D** |
| **T8** determinism | `check_reproducible.py`; sửa `scripts/check_determinism.sh`, vì nó đang gọi `l1sim.run` (module không tồn tại) | **B** |
| **D1** discovery | **Multicast discovery** (`discovery.py`): đề yêu cầu không node nào biết trước địa chỉ của node khác. `StaticPeers` đọc địa chỉ từ config nên **không đạt D1** | **C** |
| **D2** neighbor chết | Kill theo height (B). Với full mesh tĩnh thì có `peer_removed`, nhưng **không có peer để thay** cho tới khi node đó sống lại. Để assert "`peer_removed` rồi `peer_added`" cần discovery chọn neighbor khác (`target_degree`) | **C** (owner), B (kill theo height) |
| **D3** restart | **Block sync** (network đã hỗ trợ `GET_BLOCKS`/`BLOCKS`) + `verify_certificate`; restart theo height (B) | **D** (owner), B |

Việc thêm của **D** để liveness vững:
- round-skip: thấy f+1 vote ở round cao hơn thì nhảy lên;
- timeout tăng dần;
- chốt ai dừng engine ở `max_height`: `node.py` dừng engine, hay engine tự dừng.

## Unit test tối thiểu (TEAM_PLAN) còn thiếu

| Phần | Role |
|---|---|
| discovery: ANNOUNCE giả bị từ chối, chọn neighbor tất định, thay peer chết | **C** |
| faults (cùng seed cho cùng quyết định), peer_limits | **B** (có trên branch, cần merge) |
| engine: đã lock B thì không prevote C | **D** |
| block_sync: certificate thiếu chữ ký hoặc có chữ ký giả bị từ chối | **D** |

## Phần nộp bài

| Việc | Role |
|---|---|
| `tests/run_tests.py` + `tests/requirements.txt`, để người chấm chạy được mà không cần Makefile | **A** |
| README mục reproduce, chỉ dùng những gì có trong zip | **A** |
| `scripts/package.sh` tạo zip đúng 6 mục | **A** |
| Lần chạy chính thức, lưu `logs/` và `logs/results/` | cả nhóm |
| Chạy T1/T7 qua ZeroTier | **A** |
| REPORT.pdf: mỗi người viết phần mình, A ghép | cả nhóm |

## Theo role

**A**:
1. Khối `peers` trong config.
2. `node.py` (đường găng).
3. `run_node.py`.
4. `client --connect`.
5. Owner T1 và T3.
6. `run_tests.py`, `requirements.txt`, `package.sh`, README reproduce.
7. ZeroTier.

**B**:
1. PR `feat/network-faults`.
2. `check_safety.py`, `launch_local.py`.
3. `check_reproducible.py`, sửa `check_determinism.sh`.
4. Scenario t2..t8, d1..d3; kill/restart theo height.
5. Owner T4, T5, T8.

**C**:
1. Merge PR #16.
2. Gắn `FaultInjector`, owner T2.
3. `discovery.py` (multicast) cho D1, D2. `scripts/mcast_test.py` đã đo được multicast chạy trên localhost và ZeroTier.
4. Report phần Network / discovery / gossip. Ghi giới hạn HELLO replay (eclipse) và cách sửa bằng challenge–response.

**D**:
1. Locking (rule 4–5).
2. Block sync + `verify_certificate`.
3. Round-skip, timeout tăng dần.
4. `byzantine.py`.
5. Owner T6, T7, D3.

## Rủi ro

1. **`node.py` (A)**: T1 bằng process thật, mọi test e2e và T8 đều chờ nó.
2. **Locking + block sync (D)**: thiếu thì T5, T7, D3 không đạt, và T7 có thể mất safety.
3. **Discovery (C)**: thiếu thì D1, D2 không đúng đề. Nếu không kịp, ghi rõ trong report là dùng static peers.
