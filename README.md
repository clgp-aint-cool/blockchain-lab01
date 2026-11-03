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

## Cấu trúc thư mục

```
src/l1sim/      mã nguồn node (crypto, network, discovery, consensus, execution)
tests/unit/     unit test: chữ ký/domain, luật state, đếm vote, kiểm tra block
tests/e2e/      kịch bản end-to-end T1–T8
config/         cấu hình scenario (seed, validator, tham số mạng)
logs/           output khi chạy (git bỏ qua)
scripts/        công cụ phụ: thử multicast, kiểm tra determinism, đóng gói (không nộp)
docs/           ghi chú thiết kế, sơ đồ, nguồn report
```

## Cài đặt

```bash
python3 -m venv .venv && source .venv/bin/activate
make install
```

Trong repo (không có trong bản nộp) còn có `devnet/` để chạy thử cả chain trong 1 process kèm
dashboard web; hướng dẫn ở `devnet/README.md`.

## Chạy

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
