# Devnet: test chain ngay hôm nay (tạm thời)

Thư mục `devnet/` **không có trong file zip nộp bài**. Nó chỉ để nhóm chạy thử cả chain trong
lúc chưa có `node.py`, và sẽ bị xoá theo [docs/DEVNET_TO_PROJECT.md](../docs/DEVNET_TO_PROJECT.md).
Phần nhóm còn thiếu so với devnet: [docs/DEVNET_GAP_ANALYSIS.md](../docs/DEVNET_GAP_ANALYSIS.md).

`devnet/` chạy mọi node trong **một process**, nối nhau bằng TCP thật, có dashboard web.
Nó dùng code thật của nhóm (network, consensus, execution, config, logger, client), chỉ đứng
thay những phần chưa viết (`node.py`, `run_node.py`, `launch_local.py`, `faults.py`).

## 1. Demo nhanh (4 node, tx ngẫu nhiên)

```bash
python devnet/run.py
```

Mở http://localhost:8080. Block được cố tình làm chậm (`--block-ms 1000`) để xem từng bước;
dùng `--block-ms 0` để chạy hết tốc độ. Tuỳ chọn khác: `--nodes`, `--port`, `--propose-ms`,
`--vote-ms`, `--delta-ms`.

## 2. Chạy scenario của nhóm (10 validator trong `config/genesis.json`)

```bash
python devnet/run.py --scenario config/t1_normal.json              # có dashboard
python devnet/run.py --scenario config/t1_normal.json --headless   # chạy tới max_height, in verdict
```

Chế độ headless thoát với mã 0 khi verdict pass (mọi node cùng hash từng block, cùng state
hash, đạt `max_height`). Output:

| Đường dẫn | Nội dung |
|---|---|
| `logs/events/<scenario>/V*.jsonl` | event log của từng node (`EventLogger`) |
| `logs/results/<scenario>/V*.json` | block, state, state hash của từng node (`ResultWriter`) |
| `logs/results/<scenario>/verdict.json` | tóm tắt pass/fail |

Thử T8 bằng tay: chạy headless 2 lần rồi so sánh.

```bash
python devnet/run.py --scenario config/t1_normal.json --headless && cp -r logs/results /tmp/r1
python devnet/run.py --scenario config/t1_normal.json --headless && diff -r /tmp/r1 logs/results && echo T8 OK
```

## 3. Thử gì trên dashboard

| Thao tác | Kết quả mong đợi |
|---|---|
| **Gửi tx** ngẫu nhiên, hoặc form **tx của người dùng** (user, key, value, gửi qua node, nonce, ký bằng) | tx vào block kế tiếp; bảng **state của node** hiện giá trị mới |
| Sai nonce / ký bằng user khác | bị từ chối: `bad_nonce` / `bad_signature` |
| **Replay** tx cuối | bị từ chối, áp dụng tối đa 1 lần (T4) |
| **Tắt** tối đa f node (10 node thì f = 3) | chain vẫn chạy; tắt trúng proposer thì timeout sang round mới (T6) |
| Tắt f + 1 node | chain dừng, không finalize gì mâu thuẫn (safety) |
| Thanh **drop / delay** | round chậm hơn, chain vẫn giống nhau |
| **Bật lại** node đã tắt | thiếu sót đã biết: chưa có block sync nên node đó bị tụt lại |

Làm tương tự bằng `curl` (dashboard phải đang chạy):

```bash
curl -X POST "http://localhost:8080/api/tx/custom?user=Alice&key=Alice/name&value=alice&via=V2"
curl "http://localhost:8080/api/kv?node=V2"
curl -X POST "http://localhost:8080/api/node/V0/stop"
curl -X POST "http://localhost:8080/api/fault?drop=0.2&delay=200"
```

Không có `--scenario` thì user là `u0`..`u4`, node là `V0`..`V3`. Key phải bắt đầu bằng
`<user>/`.

## 4. Test tự động

```bash
PYTHONPATH=src pytest tests/unit tests/integration
```

`tests/integration/test_devnet.py` chạy T1 trên `config/t1_normal.json` từ đầu tới cuối. Trong
bản nộp không có `devnet/` nên file test này tự skip.
