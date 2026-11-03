# Bỏ devnet, chạy demo 100% bằng code của nhóm

Ngày 2026-11-02. Đọc cùng [DEVNET_GAP_ANALYSIS.md](DEVNET_GAP_ANALYSIS.md) (mục 3: ai còn thiếu gì).

## 1. Devnet đang "đứng thay" những gì

Devnet **không** có logic blockchain riêng. Network, consensus, execution, config, logger, client đều là code thật của nhóm. Devnet chỉ là phần **keo dán** và **công cụ chạy**:

| Devnet | Làm gì | Thay bằng (file của nhóm) | Owner |
|---|---|---|---|
| `devnet/node.py` · `DevNode` | ghép transport + gossip + engine + mempool + state; HEADER → BODY_REQUEST → BODY, xin lại body; giữ engine tới khi tx scenario vào mempool | `src/l1sim/node.py` | A |
| `devnet/node.py` · `NodeLog` | gắn `node` cho event, ghi `EventLogger`, bỏ event ồn | trong `node.py` (dùng thẳng `EventLogger`) | A |
| `devnet/node.py` · `Keys` | key validator/user từ seed | `crypto.keypair_from_seed` + `genesis.json` (đã có) | A |
| `devnet/node.py` · `Faults` | drop/delay trên `gossip.sender` | `network/faults.py` · `FaultInjector` (branch `feat/network-faults`) | B, C gắn vào |
| `devnet/net.py` · `Devnet` | N node trong **1 process**, tự cấp cổng, kill/restart | `run_node.py` (1 process/node) + `launch_local.py` (bật N process) | A, B |
| `Devnet.run_scenario_start` | chờ mesh → gửi tx bằng `TxClient` → chờ `warmup_ms` → cho engine chạy | `client.py` (đã có) gọi từ `launch_local.py` hoặc node V0; mỗi `node.py` tự chờ `warmup_ms` | A, B |
| `Devnet.write_results` / verdict | `ResultWriter` mỗi node + so hash | mỗi `node.py` tự `write_node`; `tests/check_safety.py` so chéo | A, B |
| `devnet/run.py --headless` | chạy tới `max_height`, in verdict | `launch_local.py --scenario …` rồi `check_safety.py` | B |
| `devnet/run.py` dashboard | xem trực quan, bấm tắt node | **không bắt buộc** cho bài nộp (xem mục 5) | – |
| `tests/integration/test_devnet.py` | T1 tự động | `tests/e2e/test_t1_normal.py` gọi `launch_local` | B |

Khác biệt quan trọng nhất: devnet = **1 process, cổng tự cấp**. Bản thật = **mỗi node 1 process, 1 terminal, cổng cố định**, nên phải có:

1. **Địa chỉ peer trong config**: thêm khối `peers` (hoặc `host`/`port` theo validator) vào `genesis.json`/`default.json`, để `StaticPeers` (C, đã có) biết nối tới đâu. Sau này D1 thay bằng multicast discovery.
2. **Kết quả rời rạc**: mỗi process ghi `logs/results/<scenario>/V*.json` của mình; một script riêng (`check_safety.py`) đọc tất cả và ra verdict.
3. **Tự dừng ở `max_height`**: devnet dừng hộ; bản thật thì `node.py` phải tự dừng (engine hiện chạy quá `max_height`).

## 2. Thứ tự chuyển (mỗi bước chạy được ngay)

| Bước | Việc | Ai | Xong khi |
|---|---|---|---|
| 0 | C commit + PR `network/`; B rebase `feat/network-faults`, giữ `__init__.py` của B | C, B | `main` có đủ network + faults |
| 1 | Thêm `peers` vào config + `load_config` đọc nó | A | `load_config` trả địa chỉ V0..V9 |
| 2 | `node.py`: **chép** `DevNode` (đổi `NodeLog` → `EventLogger`: truyền `event_logger.event` cho **cả** engine lẫn network; `Faults` → `FaultInjector`), thêm chờ `warmup_ms` và dừng ở `max_height`, ghi `ResultWriter` khi dừng | A | test: 4 `Node` trong 1 event loop finalize được (giống `test_devnet.py`) |
| 3 | `run_node.py --id V0 --scenario …` | A | 10 terminal chạy T1, mỗi node ghi results riêng |
| 3b | `client.py` thêm `--connect host:port`: hiện CLI chỉ **in** tx ra stdout, chưa gửi vào mạng | A | gửi 20 tx của t1 vào V0 |
| 4 | `check_safety.py` đọc `logs/results/<scenario>/` | B | in pass/fail giống `verdict.json` của devnet |
| 5 | `launch_local.py --scenario …`: bật N `run_node`, gửi tx bằng `TxClient`, chờ, `check_safety` | B | `make e2e` chạy T1 không cần devnet |
| 6 | Sửa `scripts/check_determinism.sh` gọi `launch_local` thay cho `l1sim.run` | B | `make determinism` pass (T8) |
| 7 | Đối chiếu: cùng `t1_normal.json`, `logs/results/` của bản thật **giống từng byte** bản devnet | cả nhóm | `diff -r` rỗng → bỏ devnet an toàn |
| 8 | D: locking, block sync, round-skip, `byzantine.py`; B: scenario t2..t8, d1..d3 | D, B | T2–T7, D1–D3 |

Bước 2 là đường găng. Không cần viết lại từ đầu: `devnet/node.py` đã chạy đúng T1 với 10 node, 20 tx, nên copy sang rồi thay 2 chỗ (logger, faults) là đủ cho T1.

Bước 7 là cách **chứng minh** chuyển xong không làm sai gì: kết quả devnet đã được kiểm tra (5 block × 4 tx, `state_hash 6820bef7…`), bản thật cùng seed phải ra y hệt.

## 3. Demo sau khi chuyển xong

Thủ công, mỗi node một terminal (đúng đề bài):

```bash
python -m l1sim.run_node --id V0 --port 9000 --scenario config/t1_normal.json
python -m l1sim.run_node --id V1 --port 9001 --scenario config/t1_normal.json
# ... tới V9 cổng 9009
python -m l1sim.client --scenario config/t1_normal.json --connect 127.0.0.1:9000   # gửi 20 tx (bước 3b)
python tests/check_safety.py logs/results/t1_normal           # so chéo 10 node
```

Tự động:

```bash
python -m l1sim.launch_local --scenario config/t1_normal.json   # bật 10 process, gửi tx, chờ, check
make e2e                                                        # T1–T8
make determinism                                                # T8
```

Demo T6/D2: trong lúc chạy, `Ctrl+C` terminal của proposer hiện tại → các node còn lại timeout, sang round 1, chain chạy tiếp. Bật lại terminal đó → cần block sync (D) để theo kịp (D3).

Nhiều máy qua ZeroTier: thêm `--listen 0.0.0.0:<port> --advertise <zerotier-ip>:<port>` như README.

## 4. Khi nào xoá `devnet/`

Xoá khi **bước 7** xong: `make e2e` và `make determinism` pass mà không import gì từ `devnet`, và `logs/results` giống bản devnet. Khi đó:

- xoá `devnet/` và `tests/integration/test_devnet.py`;
- xoá mục "Devnet" trong README, giữ mục "Run";
- chuyển các test hay của `test_devnet.py` (tx của user, replay, tắt f node) sang `tests/e2e/`.

`devnet/` nằm **ngoài** `src/l1sim`, không package nào của nhóm import nó, nên xoá không làm gãy gì.

## 5. Có giữ dashboard không?

Không bắt buộc. Đề chấm bằng log + results + test, không cần web. Nếu muốn giữ để demo cho dễ nhìn, có thể sửa dashboard thành **chỉ đọc**: đọc `logs/events/<scenario>/*.jsonl` mà các process thật ghi ra, thay vì gọi vào `Devnet`. Việc tắt node thì làm bằng `Ctrl+C` terminal. Làm sau cùng, khi T1–T8 đã xong.
