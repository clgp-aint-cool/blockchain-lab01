# Lab01 – Blockchain Layer-1 tối giản

Môn Blockchain 2026 – Khoa Công nghệ Tri thức.

Một blockchain L1 tối giản, đạt **finality kiểu BFT** (Prevote/Precommit, `n = 3f + 1`).
Mỗi node là một process riêng (một terminal, một cổng riêng), giữ kết nối TCP tới vài node
hàng xóm, tìm peer bằng **UDP multicast beacon có chữ ký**, và lan message bằng **gossip**.
Mạng không tin cậy (trễ, trùng, đảo thứ tự, mất gói) được tạo bởi **lớp fault injection tất
định**. Chạy trên một máy, hoặc nhiều máy qua ZeroTier.

## Nhóm

| MSSV | Họ tên | GitHub | Phần chính |
|---|---|---|---|
| ID1 | | | |
| ID2 | | | |
| ID3 | | | |
| ID4 | | | |

## Tài liệu

- [docs/CONCEPT_GUIDE.md](docs/CONCEPT_GUIDE.md) – các khái niệm dùng trong dự án
- [docs/TEAM_PLAN.md](docs/TEAM_PLAN.md) – thiết kế, phân vai, interface, lịch
- [docs/DEVNET_GAP_ANALYSIS.md](docs/DEVNET_GAP_ANALYSIS.md) – kết quả demo trên devnet, nhóm còn thiếu gì
- [docs/DEVNET_TO_PROJECT.md](docs/DEVNET_TO_PROJECT.md) – cách bỏ devnet, chạy 100% bằng code của nhóm

## Cấu trúc thư mục

```
src/l1sim/      mã nguồn node (crypto, network, discovery, consensus, execution)
tests/unit/     unit test: chữ ký/domain, luật state, đếm vote, kiểm tra block
tests/e2e/      kịch bản end-to-end T1–T8
config/         cấu hình scenario (seed, validator, tham số mạng)
devnet/         chạy thử tạm thời: mọi node trong 1 process + dashboard web
logs/           output khi chạy (git bỏ qua)
scripts/        công cụ phụ: thử multicast, kiểm tra determinism, đóng gói (không nộp)
docs/           ghi chú thiết kế, sơ đồ, nguồn report
```

## Cài đặt

```bash
python3 -m venv .venv && source .venv/bin/activate
make install
```

## Devnet: test chain ngay hôm nay (tạm thời)

`devnet/` chạy mọi node trong **một process**, nối nhau bằng TCP thật, có dashboard web.
Nó dùng code thật của nhóm (network, consensus, execution, config, logger, client), chỉ đứng
thay những phần chưa viết (`node.py`, `run_node.py`, `launch_local.py`, `faults.py`). Cách bỏ
devnet: xem [docs/DEVNET_TO_PROJECT.md](docs/DEVNET_TO_PROJECT.md).

### 1. Demo nhanh (4 node, tx ngẫu nhiên)

```bash
python devnet/run.py
```

Mở http://localhost:8080. Block được cố tình làm chậm (`--block-ms 1000`) để xem từng bước;
dùng `--block-ms 0` để chạy hết tốc độ. Tuỳ chọn khác: `--nodes`, `--port`, `--propose-ms`,
`--vote-ms`, `--delta-ms`.

### 2. Chạy scenario của nhóm (10 validator trong `config/genesis.json`)

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

### 3. Thử gì trên dashboard

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

### 4. Test tự động

```bash
PYTHONPATH=src pytest tests/unit tests/integration
```

`tests/integration/test_devnet.py` chạy T1 trên `config/t1_normal.json` từ đầu tới cuối.

## Chạy (mục tiêu, không cần devnet)

Mỗi node một terminal (đang làm, cần `node.py` và `run_node.py`):

```bash
python -m l1sim.run_node --id V0 --port 9000 --scenario config/t1_normal.json
python -m l1sim.run_node --id V1 --port 9001 --scenario config/t1_normal.json
# ... tới V9 ở cổng 9009
```

Chạy nhiều máy qua ZeroTier, thêm:

```bash
--listen 0.0.0.0:<port> --advertise <zerotier-ip>:<port> --mcast-iface <zerotier-ip>
```

## Kiểm tra mạng

Trên một máy:

```bash
python scripts/mcast_test.py --self-test 4
```

Trên hai máy qua ZeroTier, chạy ở cả hai:

```bash
python scripts/mcast_test.py --id <tên> --iface <zerotier-ip của máy đó> --duration 60
```

Lệnh in ra các beacon nghe được và thời gian khứ hồi (round-trip).

## Test

```bash
make test          # mọi test (một lệnh duy nhất)
make unit          # chỉ unit test
make e2e           # T1–T8
make determinism   # T8: cùng seed chạy 2 lần -> log + state hash giống từng byte
```

| Test | Kịch bản | Kết quả bắt buộc |
|---|---|---|
| T1 | Chạy bình thường, không lỗi mạng | Mọi node đúng finalize cùng chain và cùng state hash |
| T2 | Message bị trùng/đảo thứ tự | Không đếm vote 2 lần, không finalize mâu thuẫn |
| T3 | Chữ ký sai / sai domain | Message bị từ chối, log lý do |
| T4 | Tx bị replay/trùng | Áp dụng tối đa 1 lần |
| T5 | Mất/trễ gói trước khi mạng đồng bộ | Vẫn giữ safety |
| T6 | Proposer im lặng/sập | Timeout round, chain chạy tiếp |
| T7 | Tối đa f node Byzantine equivocate | Không finalize mâu thuẫn |
| T8 | Cùng seed chạy lại 2 lần | `logs/results/` giống từng byte; event log chỉ được khác timestamp và thứ tự event |
| D1 | Tìm peer: node bật theo thứ tự ngẫu nhiên, không biết trước địa chỉ | Node nào cũng đủ hàng xóm, đồ thị liên thông, T1 pass |
| D2 | Một hàng xóm chết | Node thay hàng xóm khác, chain chạy tiếp |
| D3 | Một node khởi động lại bị tụt | Block sync đuổi kịp, cùng chain |

### Cho người chấm

```bash
pip install -r tests/requirements.txt
python tests/run_tests.py   # (đang làm, thêm sau)
```

## Quy trình làm việc

Xem [CONTRIBUTING.md](CONTRIBUTING.md). Nhánh `main` được bảo vệ: mọi thay đổi đi qua Pull
Request, cần 1 approve và CI xanh.
