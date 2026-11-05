# Dashboard web

Chạy scenario bằng **code thật trên main** và xem trực tiếp trên trình duyệt. Thư mục này
không có logic blockchain riêng và không nằm trong file zip nộp bài.

```bash
python dashboard/server.py            # rồi mở http://127.0.0.1:8080
python dashboard/server.py --port 9090
```

Chỉ dùng thư viện chuẩn Python; server tự thêm `src/` vào `sys.path`.

## Cách hoạt động

- Bấm **Chạy**: server bật 10 process `python -m l1sim.run_node` giống hệt `launch_local.py`
  (cổng lấy từ `available_base_port`, stdout/stderr vào `logs/nodes/<scenario>/`).
- Mỗi node tự ghi `logs/events/<scenario>/V*.jsonl`. Server đọc dần phần mới của các file
  này mỗi 0,1 giây để vẽ trạng thái node, bảng chain và danh sách event.
- Khi mọi process dừng, server gọi `check_safety` (của B) và hiện verdict, cũng ghi
  `logs/results/<scenario>/verdict.json` như launcher.

## Vòng tròn mạng

10 node xếp thành vòng tròn. Mỗi đường nối là một kết nối TCP: càng nhiều message thì đường
càng sáng và dày, đường đỏ là kết nối có message bị drop. Màu node là bước hiện tại
(PROPOSE / PREVOTE / PRECOMMIT), xanh lá khi vừa finalize hoặc đã xong, xám khi bị kill.
Node Byzantine có viền đỏ nét đứt, ★ đánh dấu proposer của round hiện tại. Bấm vào node đang
chạy để kill nó.

## Xem lại từng giai đoạn

Khi lần chạy kết thúc, phía trên vòng tròn có thanh **⏮ ◀ Lùi ▶ Phát Tiến ▶ ⏭** và thanh kéo
(phím ← / → cũng dùng được). Mỗi bước là một giai đoạn consensus: khởi động, rồi với từng
height và round là PROPOSE → PREVOTE → PRECOMMIT → FINALIZE.

Ở mỗi giai đoạn, vòng tròn, thẻ validator và bảng chain hiện trạng thái mạng lúc đó. Node chưa
tới giai đoạn này bị làm mờ, và nhãn dưới node là phiếu nó bỏ (`pv 2be1bc`, `pc nil`…). Bên dưới
có tóm tắt (bao nhiêu phiếu cho block nào, đã đủ 2f+1 chưa, proposer có gửi đề xuất không) và
các event của giai đoạn đó. Bấm **Về kết quả cuối** để thoát.

Event chỉ có bộ đếm `time` riêng của từng node, không có giờ chung, nên không xếp được thứ tự
tuyệt đối giữa các node. Vì vậy dashboard gom theo giai đoạn: trạng thái mạng ở một giai đoạn là
trạng thái mới nhất của từng node mà chưa vượt quá giai đoạn đó.

## Điều khiển

| Ô / nút | Ý nghĩa |
|---|---|
| Scenario | các file `config/t*.json` |
| drop, dup, GST, height | sửa `network.drop_rate`, `duplicate_rate`, `gst_height`, `max_height`. Giá trị khác file gốc thì server ghi một bản sao tạm (giữ nguyên `name`), file trong `config/` không bị sửa |
| timeout | `--timeout` của mỗi `run_node` (giây) |
| Kill | gửi SIGINT cho node đó (như Ctrl+C). Node bị kill được tính là node lỗi khi kiểm tra, nên kill quá `f` node trung thực thì verdict FAIL đúng như lý thuyết |
| Dừng | dừng mọi node, không ra verdict |

Lưu ý: FaultInjector chỉ drop message có height **nhỏ hơn GST**. GST = 0 thì `drop` không có
tác dụng; muốn thấy drop thì đặt GST lớn hơn 1 (ví dụ drop 0.2, GST 3).

Main chưa có block sync, nên node đã kill không bật lại được để đuổi kịp chain.

## API

| Request | Kết quả |
|---|---|
| `GET /api/scenarios` | danh sách scenario |
| `POST /api/run` `{file, overrides, timeout}` | bắt đầu; 409 nếu đang có lần chạy khác |
| `POST /api/kill` `{node}` | kill một node |
| `POST /api/stop` | dừng tất cả |
| `GET /api/state?since=<seq>` | trạng thái node, chain, bộ đếm event, event mới sau `seq`, verdict |
| `GET /api/timeline` | các giai đoạn của lần chạy đã xong (409 khi đang chạy) |
| `GET /api/logs` | tải file zip log của lần chạy gần nhất: `events/V*.jsonl`, stdout/stderr từng node, `results/` |

Event gossip (`gossip_duplicate`, `gossip_recv`, `gossip_send`, `gossip_broadcast`) rất nhiều
nên chỉ được đếm, không đưa vào danh sách event. Trang chỉ giữ 800 dòng event gần nhất; muốn xem đầy đủ
(kể cả gossip) thì bấm **Tải log (.zip)** ở khung Event.

Test: `tests/integration/test_dashboard.py` chạy T1 qua API và kiểm verdict.
