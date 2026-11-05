# Lab01 – Blockchain Layer-1 tối giản

Môn Blockchain 2026 – Khoa Công nghệ Tri thức.

Hệ thống mô phỏng blockchain với trạng thái key–value, giao dịch có chữ ký Ed25519
và đồng thuận BFT theo ba bước Propose → Prevote → Precommit. Mỗi validator chạy
trong một process riêng, kết nối TCP tới các peer được cấu hình sẵn, xác thực bằng
HELLO và truyền message qua gossip.

Cấu hình mẫu gồm 10 validator, chịu tối đa 3 validator Byzantine và cần 7 phiếu
cùng giá trị để đạt quorum. Các kịch bản kiểm thử mô phỏng giao dịch trùng,
chữ ký sai, message trễ/mất/trùng, proposer im lặng và validator equivocate.

## Thành viên

| MSSV | Họ tên | GitHub | Phụ trách |
|---|---|---|---|
| — | — | — | A – Foundation, integration, T1/T3 |
| — | — | — | B – Execution, faults, T4/T5/T8 |
| — | — | — | C – TCP, HELLO, static peers, gossip, T2 |
| — | — | — | D – Consensus, locking, Byzantine, T6/T7 |

## 1. Cài đặt

Yêu cầu **Python 3.10 trở lên**. Chạy các lệnh tại thư mục chứa `src/`, `tests/`
và `config/`. Môi trường kiểm thử tự động dùng TCP loopback trên cùng một máy.

```bash
python -m venv .venv
```

Kích hoạt môi trường theo hệ điều hành:

```powershell
# Windows PowerShell
.venv\Scripts\Activate.ps1
```

```bash
# Linux/macOS
source .venv/bin/activate
```

Cài dependencies và chạy toàn bộ kiểm thử:

```bash
python -m pip install -r tests/requirements.txt
python tests/run_tests.py
```

Runner tự thiết lập đường dẫn import và khởi tạo các process cần thiết. Toàn bộ
bộ test mất khoảng vài phút. Pytest hiển thị kết quả `passed`, `failed` hoặc `error`;
exit code `0` nghĩa là các test được chọn đều pass, mã khác `0` nghĩa là chưa đạt.

## 2. Kiểm chứng T1–T8

Mỗi test tự tạo dữ liệu chạy, kiểm tra kết quả và đóng các node sau khi hoàn tất.
Chọn từng kịch bản bằng các lệnh sau:

```bash
python tests/run_tests.py --scenario T1
python tests/run_tests.py --scenario T2
python tests/run_tests.py --scenario T3
python tests/run_tests.py --scenario T4
python tests/run_tests.py --scenario T5
python tests/run_tests.py --scenario T6
python tests/run_tests.py --scenario T7
python tests/run_tests.py --scenario T8
```

| ID | Kịch bản | Tiêu chí kiểm chứng |
|---|---|---|
| T1 | Hoạt động bình thường | Các node trung thực finalize cùng chain và state hash |
| T2 | Message trùng hoặc đảo thứ tự | Không đếm vote trùng, không finalize mâu thuẫn |
| T3 | Vote sai chữ ký hoặc sai domain | Từ chối vote, ghi reason `bad_signature` hoặc `wrong_domain` |
| T4 | Giao dịch replay/trùng nonce | Mỗi giao dịch chỉ được thực thi tối đa một lần |
| T5 | Mất/trễ message trước GST | Giữ safety và tiếp tục finalize khi mạng ổn định |
| T6 | Proposer im lặng | Timeout và chuyển sang round có proposer tiếp theo |
| T7 | Tối đa f validator equivocate | Phát hiện equivocation, không finalize mâu thuẫn |
| T8 | Chạy hai lần với cùng seed | Results giống từng byte và cùng final state hash |

Có thể chạy nhiều kịch bản hoặc một nhóm test:

```bash
python tests/run_tests.py --scenario T3 T4
python tests/run_tests.py --unit
python tests/run_tests.py --e2e
```

## 3. Chạy mô phỏng và xem kết quả

Để dùng các lệnh `python -m l1sim...`, đặt đường dẫn source trong terminal hiện tại:

```powershell
# Windows PowerShell
$env:PYTHONPATH = (Resolve-Path src).Path
```

```bash
# Linux/macOS
export PYTHONPATH="$PWD/src"
```

Chạy kịch bản bình thường:

```bash
python -m l1sim.launch_local --scenario config/t1_normal.json --base-port 19000
```

Lệnh này khởi tạo 10 process validator, chờ hoàn tất 5 block, rồi kiểm tra chain và
state của các node. Kết quả JSON được in ra terminal; trường `pass: true` và exit
code `0` cho biết lần chạy đạt các kiểm tra của launcher.

Mặc định các node tự nạp giao dịch của scenario. Để gửi giao dịch qua client TCP:

```bash
python -m l1sim.launch_local --scenario config/t1_normal.json --base-port 19000 --no-auto-txs
```

Launcher sẽ khởi động client để gửi các giao dịch vào một node, sau đó giao dịch
được lan qua gossip và đưa vào block.

Muốn chạy node trong các terminal riêng, dùng cùng scenario và base port:

```bash
python -m l1sim.run_node --id V0 --scenario config/t1_normal.json --base-port 19000
python -m l1sim.run_node --id V1 --scenario config/t1_normal.json --base-port 19000
# Tiếp tục V2 đến V9, mỗi node một terminal.
```

Phải khởi động đủ validator trước khi consensus bắt đầu. Nếu thiếu node hoặc
địa chỉ sai, CLI có thể báo `timed out waiting for authenticated peers`.

## 4. Đọc log và reproduce

Khi chạy mô phỏng bằng launcher:

| Đường dẫn | Nội dung |
|---|---|
| `logs/events/<scenario>/V*.jsonl` | Event consensus và network của từng node |
| `logs/results/<scenario>/V*.json` | Chain đã finalize, giao dịch trong block và final state hash |
| `logs/results/<scenario>/verdict.json` | Tổng hợp kiểm tra safety và tính nhất quán |
| `logs/nodes/<scenario>/` | Stdout/stderr của các process |

Trong results, dùng `finalized_height` để kiểm tra tiến độ, `block_hashes` để so
chain, `state_hash` để so trạng thái và `blocks[].tx_ids` để kiểm tra giao dịch.
T8 so sánh các file trong `logs/results/` từng byte. Event log dùng để truy vết;
timestamp và thứ tự event có thể khác giữa các lần chạy.

Các test E2E dùng thư mục tạm riêng. Có thể chọn nơi lưu dữ liệu tạm cho toàn bộ
lần kiểm thử:

```bash
python tests/run_tests.py -- -v --basetemp=logs/test-review
```

Bên trong `logs/test-review/`, mỗi test có thư mục con riêng. Tùy kịch bản, log và
results nằm trong thư mục của test hoặc các đường dẫn chung ở bảng trên. Đọc event
theo node, height và round để theo dõi tiến trình; với message bị từ chối,
trường `reason` cho biết nguyên nhân.
Pytest xóa thư mục `--basetemp` trước mỗi lần chạy, nên dùng thư mục riêng cho test.
Chạy các bộ E2E tuần tự vì một số kịch bản dùng chung đường dẫn log.

## 5. Cấu hình

`config/genesis.json` xác định validator set và public key. Các file scenario
trong `config/` xác định seed, giao dịch, số block, timeout, lỗi mạng và hành vi
Byzantine. Mọi node của cùng một lần chạy phải dùng cùng genesis và scenario.

Nếu không có `network.peers`, địa chỉ mặc định là `127.0.0.1:base_port+i`, với `i`
là vị trí validator trong genesis. Khi chạy nhiều máy, khai báo đầy đủ
`network.peers` dưới dạng `{"V0": "host:port", ...}` và dùng IP có thể truy cập
giữa các máy, chẳng hạn IP ZeroTier. Các kiểm thử tự động được chạy trên loopback.

## Cấu trúc mã nguồn

```text
src/l1sim/          models, chữ ký, encoding, node và CLI
  consensus/        VoteSet, proposer rotation, locking, timeout, Byzantine rules
  execution/        mempool, kiểm tra giao dịch và chuyển trạng thái
  network/          TCP, HELLO, static peers, gossip, fault injection
  verification/     kiểm tra safety và tính tái lập của results
tests/              runner, dependencies, unit test và E2E test
config/             genesis và các scenario
logs/               kết quả và log của các lần chạy
```
