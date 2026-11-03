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

`node.py` đã ghép `TcpTransport`, handshake `HELLO` có chữ ký, `StaticPeers`,
`Gossip`. Mỗi cặp validator giữ một kết nối TCP hai chiều. `NodeNetwork` đã
khởi tạo `FaultInjector` có sẵn; việc gắn vào `gossip.sender` thuộc task 9 của
C + B, chưa thực hiện ở đây. Hiện dùng danh sách peer tĩnh; multicast discovery
và block sync chưa được ghép.

Chạy đủ 10 validator, mỗi node một terminal:

```bash
python -m l1sim.run_node --id V0 --scenario config/t1_normal.json
python -m l1sim.run_node --id V1 --scenario config/t1_normal.json
# ... tới V9
```

Khi không có `network.peers`, địa chỉ mặc định là `127.0.0.1:9000+i`, với `i` là
vị trí validator trong genesis. Port tự chọn theo node; có thể ghi rõ `--port`.
Dùng cùng `--base-port` trên mọi node để đổi dải port local khi không có bảng peers.
`config/default.json` có sẵn bảng `network.peers` đủ V0–V9; sửa bảng này để đổi
địa chỉ khi dùng scenario đó. `config/t1_normal.json` giữ fallback local.

Node chờ kết nối có HELLO hợp lệ tới đủ các peer, nạp tx của scenario rồi bắt đầu
consensus sau `warmup_ms`. Kết thúc thành công khi finalize đủ `max_height`.

Chờ **đủ mọi peer** là chủ ý, để kết quả tái lập được (T8): khi mọi node đã nối
nhau trước block đầu tiên thì không node nào lỡ message, và `logs/results` giống
từng byte giữa các lần chạy. Vì vậy thiếu một node lúc khởi động thì các node
khác báo `timed out waiting for authenticated peers`, không phải lỗi consensus.
Sau khi đã chạy, consensus vẫn chịu được tới f node hỏng (n = 10 thì f = 3).

Mặc định mỗi node **tự nạp** tx của scenario vào mempool của mình, không gửi qua
mạng, để mọi node có cùng tx trước block đầu tiên. Lệnh mặc định vì thế không
kiểm tra đường tx qua mạng. Muốn kiểm tra client và tx gossip, xem mục
"Gửi tx bằng client qua TCP" bên dưới (`--no-auto-txs` + `client --connect`).
`--timeout 60` giới hạn riêng bước chờ peer và bước consensus, tính bằng giây;
quá hạn trả exit code 1 và ghi kết quả hiện tại. Ctrl+C đóng kết nối và timer.

Kết quả: `logs/results/<scenario>/<node>.json`.
Event consensus và network: `logs/events/<scenario>/<node>.jsonl`.

Khi đủ `max_height`, engine dừng ngay, không mở height kế tiếp. `ResultWriter`
ghi kết quả cả khi timeout hoặc Ctrl+C. Trong lúc task 9 chưa ghép sender, CLI
từ chối scenario có drop/duplicate khác 0 để tránh chạy mà bỏ qua cấu hình lỗi mạng.

### Địa chỉ tùy chỉnh và nhiều máy qua ZeroTier

Thêm `network.peers` vào scenario, đủ mọi validator, mỗi địa chỉ là `host:port`:

```json
"peers": {
  "V0": "10.147.17.10:9000",
  "V1": "10.147.17.10:9001",
  "V2": "10.147.17.10:9002",
  "V3": "10.147.17.10:9003",
  "V4": "10.147.17.10:9004",
  "V5": "10.147.17.20:9005",
  "V6": "10.147.17.20:9006",
  "V7": "10.147.17.20:9007",
  "V8": "10.147.17.20:9008",
  "V9": "10.147.17.20:9009"
}
```

Mọi máy dùng cùng genesis và scenario. Ví dụ trên máy có IP `10.147.17.20`:

```bash
python -m l1sim.run_node --id V5 --scenario config/my_scenario.json --listen 0.0.0.0:9005 --advertise 10.147.17.20:9005
```

`--advertise` mặc định lấy từ `network.peers` và phải khớp địa chỉ đã cấu hình.
Chế độ peer tĩnh không dùng `--mcast-iface`. Hướng dẫn trên mô tả cấu hình; kiểm
thử tự động hiện chạy TCP trên loopback, chưa xác minh môi trường ZeroTier.

### Kiểm thử tích hợp

```bash
python -m pytest tests/e2e/test_local_nodes.py
```

Test chạy 10 process bằng network thật, rồi lặp lại với `network.peers` và port
tùy chỉnh; mọi node phải finalize cùng chain, đủ tx và có kết quả giống từng byte.
`NodeNetwork` quản lý vòng đời network; `Node.receive_message(message, from_peer)`
trả bool cho Gossip. `Node` vẫn cho phép truyền callback broadcast/send, logger
và result writer để kiểm thử độc lập.

### Gửi tx bằng client qua TCP

Để gửi từ terminal riêng, chạy **mọi node** với `--no-auto-txs`:

```bash
python -m l1sim.run_node --id V0 --scenario config/t1_normal.json --no-auto-txs
python -m l1sim.run_node --id V1 --scenario config/t1_normal.json --no-auto-txs
# ... tới V9, mỗi node một terminal
```

Sau đó gửi scenario vào một node bất kỳ:

```bash
python -m l1sim.client --connect 127.0.0.1:9000 --scenario config/t1_normal.json
```

Client kiểm tra HELLO của server bằng public key trong genesis, ký từng tx và
gửi frame TX qua cùng cổng TCP của node. Node kiểm tra chữ ký/nonce, nhận tx vào
mempool và phát qua Gossip. Client không có quyền validator và không được thêm
vào bảng peer. Không dùng private key validator để gửi tx.

Mỗi dòng stdout là một phản hồi JSON gồm `tx_id`, `accepted`, `reason`.
`accepted: true` xác nhận **đã vào mempool**, chưa xác nhận finalize. Chữ ký sai,
tx trùng hoặc node đã hoàn tất được trả reason tương ứng. CLI trả exit code 0
khi tất cả tx được nhận; có tx bị từ chối, lỗi kết nối, timeout hoặc HELLO giả
thì trả 1. `--timeout 10` là timeout mỗi thao tác mạng; `--genesis` chọn file
khóa tin cậy nếu không dùng `config/genesis.json`.

Với `--no-auto-txs`, mỗi node chờ đủ số tx duy nhất theo `(sender, nonce)` của
scenario trước warmup và consensus; quá hạn chờ trả exit code 1. Cách này giữ
tx có mặt trước block đầu tiên. Nếu bỏ flag, node vẫn tự nạp tx như trước.
Nếu không truyền `--connect`, client vẫn chỉ ký và in tx ra stdout.

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
