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

## Chạy liên tục và gửi giao dịch

Tick **Chạy liên tục** rồi bấm **Chạy**: mạng không dừng ở `max_height` mà chạy tới khi bấm
**Dừng mạng**. Node chạy qua [`live_node.py`](live_node.py), tức vẫn là `l1sim.run_node`
(cùng consensus, mạng, log). Wrapper này chỉ đổi ba thứ, áp dụng trong process của dashboard,
không sửa file nào trong `src/`:

- `max_height` rất lớn và `--timeout` 7 ngày;
- proposer giữ block round 0 lại 0,6 giây (`L1DASH_PROPOSE_DELAY_MS`, phải nhỏ hơn
  `timeout_propose`) để mạng finalize khoảng 1 block mỗi giây, đủ chậm để kịp gửi TX;
- không ghi event `gossip_duplicate` (khoảng 80% số dòng log) để log không phình to.

Khung **Giao dịch** dùng được cả ở chế độ thường lẫn chế độ liên tục, miễn là mạng đang chạy.
Server ký TX bằng khoá của sender (`keypair_from_seed(seed, sender)`), rồi gửi tới cổng của
node đã chọn bằng `l1sim.client.send_transactions`, giống `python -m l1sim.client`. Node trả
lại `accepted` / `reason` và gossip TX cho cả mạng.

| Loại | Gửi gì | Node trả |
|---|---|---|
| hợp lệ | TX ký đúng, nonce kế tiếp (để trống ô Nonce) | nhận, vài giây sau thành `commit h=…` |
| Nonce nhỏ hơn | TX ký đúng nhưng nonce đã dùng | `bad_nonce` |
| Nonce lớn hơn | TX ký đúng, nonce nhảy cóc | nhận vào mempool nhưng `chờ nonce N` tới khi các nonce trước được commit |
| sửa chữ ký | đổi một ký tự của chữ ký | `bad_signature` |
| ký nhầm domain | ký bằng domain `HEADER` thay vì `TX` | `wrong_domain` |
| replay | gửi lại nguyên văn TX đã commit gần nhất của sender | `bad_nonce` |
| sender `Mallory` | người không có trong scenario, node không biết public key | `bad_signature` |

Key luôn có dạng `<sender>/...`, vì `Tx` của l1sim không cho tạo TX ghi key của người khác.
Sender hợp lệ là những người có trong `transactions` của scenario (Alice, Bob, Charlie); TX của
scenario vẫn được nạp sẵn lúc khởi động.

**Tự gửi TX hợp lệ mỗi X giây** chọn ngẫu nhiên sender và node còn sống. Bảng **TX đã gửi**
hiện kết quả của từng TX. Bảng **State đã commit** là `key → value` sau khi áp các block mà một
node trung thực đã finalize (server đọc `logs/results/<scenario>/V*.json`, lấy `tx_ids` của mỗi
block).

Ở chế độ liên tục, bảng chain chỉ hiện 40 height gần nhất, mới nhất trên cùng. Kill node vẫn
dùng được: còn ≥ 7 node thì mạng tiếp tục commit. **Dừng mạng** gửi Ctrl+C cho mọi node. Node
ghi results rồi thoát; node nào quá 5 giây chưa thoát thì bị terminate. Sau đó server chạy
`check_safety` tới height mà mọi node trung thực còn sống đều đã finalize. Xem lại từng giai
đoạn chỉ giữ 40 height cuối.

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
| Chạy liên tục | mạng chạy tới khi bấm **Dừng mạng** (xem phần trên); ô height, timeout không dùng |
| Dừng | dừng mọi node, không ra verdict |
| Dừng mạng | (chế độ liên tục) Ctrl+C mọi node rồi ra verdict cho phần chain đã có |

Lưu ý: FaultInjector chỉ drop message có height **nhỏ hơn GST**. GST = 0 thì `drop` không có
tác dụng; muốn thấy drop thì đặt GST lớn hơn 1 (ví dụ drop 0.2, GST 3).

Main chưa có block sync, nên node đã kill không bật lại được để đuổi kịp chain.

## API

| Request | Kết quả |
|---|---|
| `GET /api/scenarios` | danh sách scenario |
| `POST /api/run` `{file, overrides, timeout, live}` | bắt đầu (`live: true` = chạy liên tục); 409 nếu đang có lần chạy khác |
| `POST /api/tx` `{sender, key, value, nonce, node, tamper}` | ký và gửi một TX tới `node`; `nonce` null = tự động; `tamper` là `none`, `bad_signature`, `wrong_domain` hoặc `replay`. Trả `accepted`, `reason`, `id` (tx_id) |
| `POST /api/kill` `{node}` | kill một node |
| `POST /api/stop` | dừng tất cả |
| `GET /api/state?since=<seq>` | trạng thái node, chain, bộ đếm event, event mới sau `seq`, verdict, `tx` (TX đã gửi, state đã commit, nonce kế tiếp) |
| `GET /api/timeline` | các giai đoạn của lần chạy đã xong (409 khi đang chạy) |
| `GET /api/logs` | tải file zip log của lần chạy gần nhất: `events/V*.jsonl`, stdout/stderr từng node, `results/` |

Event gossip (`gossip_duplicate`, `gossip_recv`, `gossip_send`, `gossip_broadcast`) rất nhiều
nên chỉ được đếm, không đưa vào danh sách event. Trang chỉ giữ 800 dòng event gần nhất; muốn xem đầy đủ
(kể cả gossip) thì bấm **Tải log (.zip)** ở khung Event.

Test: `tests/integration/test_dashboard.py` chạy T1 qua API và kiểm verdict. Test này cũng
chạy chế độ liên tục: gửi TX hợp lệ, sai chữ ký, sai domain, replay và TX của Mallory, chờ TX
hợp lệ được commit, rồi Dừng mạng và kiểm verdict.
