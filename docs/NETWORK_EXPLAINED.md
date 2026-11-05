# Tầng network hiện tại

## TCP và địa chỉ peer

Mỗi validator chạy trong một process, có TCP port riêng. `network.peers` ánh xạ
validator ID sang `host:port`; CLI có fallback localhost theo thứ tự genesis.
`StaticPeers` để validator đứng sau chủ động kết nối tới validator đứng trước,
tránh hai phía cùng tạo kết nối. Với 10 validator, full mesh có 45 kết nối TCP.

`TcpTransport` chỉ truyền bytes. Một frame gồm độ dài 4 byte big-endian rồi payload.
Payload là canonical encoding của `models.Message`; giới hạn kích thước frame
theo `encoding.MAX_MESSAGE_BYTES`. Asyncio quản lý các kết nối trong từng process.

## HELLO và quyền gửi message

Ngay sau khi kết nối, hai bên trao đổi HELLO chứa `node_id`, `advertise_addr`,
`chain_id`, `finalized_height` và chữ ký Ed25519 thuộc domain HELLO.
Public key được lấy từ genesis. Nội dung sai schema, sai chain, ID lạ,
self-connect hoặc chữ ký sai bị từ chối với reason.
Sai ID so với địa chỉ được cấu hình bị từ chối bằng `wrong_peer_at_addr`.

Client bên ngoài có thể gửi TX qua cùng TCP port nhưng không có quyền validator:
không được gửi vote và không được thêm vào bảng peer. Chữ ký TX được kiểm tra
bằng user key, riêng với domain VOTE/HEADER/HELLO.

## Gossip và header-before-body

| Message | Cách gửi |
|---|---|
| TX, HEADER, VOTE | FLOOD: gửi các peer, dedup theo message ID, forward khi handler trả True |
| BODY_REQUEST, BODY | DIRECT: gửi một peer, không dedup hoặc forward |
| HELLO | Chỉ trong handshake |

Node nhận HEADER hợp lệ thì hỏi BODY_REQUEST nếu chưa có body. Node có body trả BODY;
node nhận kiểm tra header/tx root trước khi chuyển block tới consensus.
Header/body phục vụ việc truyền proposal trong consensus đang chạy.

Gossip đánh dấu message ID đã thấy trước khi gọi handler. Message bị từ chối sẽ
không được xét lại khi nhận nguyên bản lần nữa. Node vì vậy chỉ từ chối các input
không hợp lệ hoặc đã cũ/trùng, và trả True cho vote mới được đếm, kể cả vote xung đột.
Engine có lớp dedup riêng theo voter/height/round/phase/giá trị.

## Fault injection, log và test

Mọi lần gửi qua `gossip.sender`. `NodeNetwork` gắn fault sender để mô phỏng
delay/drop/duplicate theo scenario và seed. Gossip duplicate log bằng
`gossip_duplicate`; engine duplicate log bằng `duplicate_ignored`.
Message sai chữ ký/domain không được forward và có reason trong event log.

Các test liên quan: `test_tcp_transport.py`, `test_hello.py`, `test_static_peers.py`,
`test_gossip.py`, `test_fault_sender.py`, `test_node_integration.py` và E2E T1/T2/T3/T5.
Hướng dẫn cấu hình nhiều máy và chạy test nằm trong [README](../README.md).
