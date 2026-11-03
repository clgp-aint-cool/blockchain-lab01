# Ghép network (Role C) với `models` / `encoding` / `crypto` của A

Ngày: 29/10/2026. Phạm vi: `src/l1sim/network/` và test network. **Không sửa file nào của A.**

## Vì sao phải sửa

Trước khi sửa, code network không import module nào của A và tự làm lại phần encode, ký:

| Thử | Trước khi sửa |
|---|---|
| `gossip.encode` một `Vote` của A | `TypeError: Vote is not JSON serializable` |
| `gossip.decode` bytes từ `encoding.encode(Message)` | `unknown kind 'VOTE'` |
| `encoding.decode` bytes do gossip tạo | `invalid Message fields` |
| `crypto.verify(Domain.HELLO, ...)` trên chữ ký của `hello.py` | `False` |

Sau khi sửa, cả 4 trường hợp đều chạy đúng.

## `src/l1sim/network/gossip.py`

- **Bỏ** các hàm `encode`, `decode`, `message_id` tự viết. Gossip dùng `encode`, `decode`, `message_id` trong `l1sim.encoding`.
- **Message trên dây** đổi từ `{"kind","payload"}` sang `encoding.encode(Message)`, tức có thêm `origin` và `height`. Bytes không canonical hoặc sai schema bị `decode()` từ chối và được log `gossip_malformed`.
- **Kind** dùng `models.Kind`:
  - `FLOOD = {TX, HEADER, VOTE}`. Trước đây là `PREVOTE` và `PRECOMMIT`; giờ phase nằm trong `Vote.phase`.
  - `DIRECT = {BODY_REQUEST, BODY, GET_BLOCKS, BLOCKS}`.
  - `HELLO` không đi qua gossip. Nếu nhận HELLO sau handshake thì bỏ, log `gossip_unexpected_kind`.
- **API**:
  - `broadcast(kind, payload)` → `broadcast(msg: Message) -> str` (trả `message_id`).
  - `send_to(peer, kind, payload)` → `send_to(peer, msg: Message)`.
  - `on(kind, handler)` nhận `Kind` hoặc chuỗi. Handler giờ nhận `(msg: Message, from_peer)`, không còn nhận payload thô.
- **Dedup** theo `encoding.message_id(msg)` thay vì `sha256(bytes)`. Khi chuyển tiếp, node vẫn gửi lại đúng bytes đã nhận, và bytes đó chắc chắn canonical vì đã qua `decode()`.
- Giữ nguyên `sender` (chỗ B gắn FaultInjector), logic chuyển tiếp khi `ok is True` và cách log event.

## `src/l1sim/network/hello.py`

- **Frame HELLO** là `encoding.encode(Message(Kind.HELLO, node_id, finalized_height, models.Hello(...)))`, cùng định dạng với mọi message khác.
- **Chữ ký** dùng `crypto.sign` / `crypto.verify` với `Domain.HELLO`, prefix `HELLO:lab01`. Trường `signature` không được ký (do `crypto` tự bỏ). Hàm `_signed_bytes` tự viết đã bị xoá.
- **HELLO mang thêm** `advertise_addr` và `finalized_height`, đủ cho `peer_heights()` / block sync sau này.
- **Đổi API**:
  - `make_hello(chain_id, node_id, sk)` → `make_hello(node_id, sk, advertise_addr, finalized_height=0)`.
  - `check_hello(data, chain_id, validators, me) -> str` → `check_hello(data, validators, me) -> Hello`.
  - `handshake_fn(chain_id, node_id, sk, validators)` → `handshake_fn(node_id, sk, validators, advertise_addr, finalized_height=lambda: 0, on_hello=None)`. `advertise_addr` và `finalized_height` là hàm vì port thật chỉ có sau `start()` và height tăng dần. `on_hello(hello)` được gọi khi HELLO của peer hợp lệ.
  - Bỏ tham số `chain_id`, vì `models.Hello` chỉ chấp nhận `CHAIN_ID = "lab01"`.
- **Thứ tự reason** giữ nguyên: `malformed → wrong_chain → unknown_peer → self_connect → bad_signature`. Lý do là `models.Hello` ném lỗi khi gặp chain lạ. Khi `decode()` lỗi, code đọc thô `payload.chain_id` để vẫn trả được `wrong_chain` thay vì `malformed`.
- `validators` vẫn là `node_id → 32 bytes public key`, khớp với `KeyPair.public_bytes`.

## `src/l1sim/network/tcp_transport.py`

- `MAX_FRAME` đổi từ 4 MB thành `encoding.MAX_MESSAGE_BYTES` (1 MB). Một frame là một message, nên frame lớn hơn giới hạn này thì `decode()` cũng sẽ từ chối.
- Ngoài ra không đổi gì.

## `src/l1sim/network/static_peers.py`

Không đổi.

## Test

- `tests/unit/p2p_cluster.py`:
  - Key sinh bằng `crypto.keypair_from_seed(42, "vN")` để test chạy ra kết quả như nhau mỗi lần.
  - `Node` dùng `handshake_fn` mới và lưu HELLO của peer vào `node.hellos`.
  - Bỏ `CHAIN = "lab01-test"`.
- `tests/unit/test_gossip.py`:
  - Viết lại với `Message` thật (`Tx`, `Vote`, `BlockHeader`, `BodyRequest`, `Block`).
  - Thêm test bytes không canonical bị từ chối.
  - Thêm test HELLO sau handshake không được gossip.
  - Thêm test `on(Kind.HELLO)` bị từ chối.
- `tests/unit/test_hello.py`:
  - Viết lại theo API mới.
  - Thêm test: HELLO không ký, HELLO bị sửa `finalized_height` sau khi ký, message không phải HELLO, HELLO không canonical.
  - Thêm test cluster 3 node kiểm `advertise_addr` mà mỗi node nhận được.
- `tests/unit/test_tcp_transport.py`: test frame lớn nhất dùng `MAX_FRAME` thay vì `1 << 20`, vì 1 MiB giờ vượt giới hạn.

Kết quả: `PYTHONPATH=src pytest` → **62 passed** (trước là 56). Chạy 5 lần liên tiếp đều pass. `ruff check src/l1sim/network tests` không báo lỗi.

## Còn lại / cần chốt với nhóm

- **Logger:** A chưa có `logger.py`. Các module vẫn giả định `logger.event(name, **fields)`.
- **Interface Gossip trong TEAM_PLAN** là `Gossip(node_id, discovery, sender, logger)` + `set_handler(callback)`. Code hiện dùng `Gossip(node_id, transport, logger)` + `on(kind, handler)`, cùng `StaticPeers` thay cho `Discovery` theo `ROLE_C_P2P_LITE.md`. Cần cập nhật TEAM_PLAN cho khớp.
- **`on_hello`** được gọi trước khi transport gắn kết nối. Nếu transport bỏ kết nối ngay sau đó (đang `stop()`), tầng trên có thể đã nhận một HELLO của kết nối không dùng.
- **Cài package:** cần `pip install -e .[dev]` hoặc `PYTHONPATH=src`, nếu không `pytest` sẽ báo `No module named 'l1sim'`.
- **File của A:** `ruff` báo `UP038` ở `encoding.py:56`, `encoding.py:62`, `models.py:69`. `static_peers.py` cũng lệch `ruff format` từ trước. Mình không sửa các chỗ này.

---

# Lần 2 (31/10/2026): ghép với bản mới nhất (execution #8, consensus #10)

Bản mới không đổi `models` / `encoding` / `crypto` của A. Nhưng engine của D (`consensus/engine.py`) gọi tầng mạng theo cách khác với API network hiện có. **Lần này chỉ sửa file trong `src/l1sim/network/` và test network**, không sửa file nào của A, B hay D. Trước khi sửa, 130 test vẫn pass, vì chưa có code nào gọi qua lại giữa network và consensus.

## Chỗ lệch tìm thấy

| Phía D / B | Phía network trước khi sửa | Cách xử lý |
|---|---|---|
| `ConsensusEngine(broadcast: Callable[[Message], None])`: hàm thường, không `await` | `Gossip.broadcast` là `async` | Thêm `broadcast_nowait` / `send_to_nowait` |
| `ConsensusEngine(logger: Callable[..., None])`: gọi `logger(event, **fields)` | network gọi `logger.event(event, **fields)` | `events.emit` nhận cả hai kiểu |
| `validator_keys: Mapping[str, Ed25519PublicKey]` (engine, VoteSet) | `hello.py` chỉ nhận `bytes` thô | `check_hello` / `handshake_fn` nhận cả hai kiểu |

## File đã sửa

### Mới: `src/l1sim/network/events.py`

- Hàm `emit(logger, node_id, event, **fields)`:
  - logger có `.event` → gọi `logger.event(...)`;
  - logger là hàm → gọi `logger(event, node=..., ...)`, giống engine;
  - không có logger → `logging.debug`.
- `tcp_transport.py`, `static_peers.py` và `gossip.py` bỏ 3 bản `_event` copy giống nhau, giờ cùng gọi `emit`. Nhờ vậy `node.py` truyền cùng một logger cho network và engine được.

### `gossip.py`

- Thêm `broadcast_nowait(msg) -> str` và `send_to_nowait(peer, msg)`.
  - Kiểm kind và đánh dấu seen chạy **ngay** (sai kind thì ném `ValueError` luôn), còn việc gửi chạy trong task do gossip giữ. `stop()` huỷ các task này.
  - `node.py` truyền thẳng `broadcast=gossip.broadcast_nowait` cho engine được.
  - Phải gọi khi event loop đang chạy.
- Tách phần chung thành `_prepare_broadcast`, `_prepare_send`, `_spawn`. `broadcast` / `send_to` bản async giữ nguyên hành vi.
- Gossip không gửi lại message cho chính node phát, đúng yêu cầu "must not deliver the message back to this engine" của engine.

### `hello.py`

- `validators` nhận `Mapping[str, bytes | Ed25519PublicKey]`. `node.py` dùng chung được map `validator_keys` của engine/VoteSet, không phải giữ thêm một map bytes.

### `static_peers.py`, `tcp_transport.py`

- Chỉ đổi `_event` sang `emit`. `static_peers.py` bỏ `import logging` không còn dùng.

## Test

- `test_gossip.py`: thêm `test_nowait_api_for_engine_callback` (gửi được, node phát không nhận lại, sai kind ném lỗi ngay).
- `test_hello.py`: thêm `test_accepts_public_key_objects`.
- Mới: `tests/unit/test_network_events.py`, kiểm `emit` với logger dạng object, dạng hàm và `None`.

Kết quả: `PYTHONPATH=src pytest` → **133 passed** (130 trước đó + 3 test mới), chạy 3 lần đều pass. `ruff check` trên phần network không báo lỗi.

## Cần báo nhóm (không sửa, vì không phải code của mình)

- **Engine phát `Kind.BODY` qua `broadcast`** (`consensus/engine.py:675`, có `TODO(node.py)`). Gossip coi BODY là DIRECT, nên `broadcast_nowait(BODY)` sẽ ném `ValueError`. Khi viết `node.py`, A cần bọc callback, ví dụ: BODY → lưu body rồi `broadcast_nowait(Message(HEADER, ...))`, khi nhận BODY_REQUEST thì `send_to_nowait(peer, BODY)`. Không nên đưa BODY vào FLOOD, vì đề yêu cầu header trước body.
- **Trùng tên event `duplicate_ignored`:** engine log event này khi VoteSet gặp vote trùng (`engine.py:452`), gossip cũng log khi bỏ bản trùng ở tầng mạng. Phân biệt được qua field: bản của gossip có `peer` và `mid`. Khi đếm cho T2 trong results, cần chốt đếm bản nào hoặc cộng cả hai. *(Đã xử lý ở Lần 8: gossip đổi sang `gossip_duplicate`.)*
- `logger.py` của A vẫn chưa có. Network giờ chấp nhận cả 2 kiểu nên không bị chặn.

## Bổ sung (31/10/2026, khi lên kế hoạch test)

- `static_peers.py`: chạy `ruff format`. Chỉ đổi định dạng: dòng `self._dial = {...}` gộp lại thành 1 dòng, logic không đổi. Phần "File của A" ở Lần 1 ghi là chưa format; giờ đã sửa xong. `ruff format --check src/l1sim/network` sạch, 133 test vẫn pass.
- Viết `docs/TEST_PLAN.md`: kế hoạch test toàn bộ ứng dụng và danh sách những gì còn thiếu. Chỉ thêm tài liệu, không sửa code của ai.

---

# Lần 3 (31/10/2026): devnet + dashboard

Thêm mới, **không sửa file cũ nào** (kể cả `src/l1sim/network/`):

- `devnet/` (ngoài package `l1sim`): bản ghép tạm network + consensus + execution và dashboard web. Chi tiết và kết quả ở `docs/DEVNET_TONIGHT_PLAN.md`.
- `tests/integration/` (`__init__.py`, `test_devnet.py`): 3 test tích hợp, chạy trong `pytest` mặc định (~2 s).
- Phía network không phải sửa gì để ghép với engine: `broadcast_nowait`, `send_to_nowait`, `events.emit` (logger dạng hàm) và `gossip.sender` dùng được ngay.

## Lần 4: ghép với PR #13 (config, logger, client) và PR #15 (logger của engine)

| File (của C) | Sửa | Vì sao |
|---|---|---|
| `network/events.py` | `emit` gọi `logger.event(event, **fields)` **không kèm `node`** khi logger là object | `EventLogger` (PR #13) mỗi node một cái, tự ghi `node` và báo lỗi nếu caller truyền `node`. PR #15 sửa engine theo cùng quy ước. Logger kiểu hàm vẫn nhận `node`. |
| `tests/unit/test_network_events.py` | Sửa kỳ vọng, thêm test ghi vào `EventLogger` thật | |
| `tests/unit/p2p_cluster.py` | Logger giả dùng chung cluster đổi sang kiểu hàm (`__call__`) | Logger dùng chung nhiều node phải nhận `node` |

Ngoài network chỉ sửa `devnet/` và `tests/integration/test_devnet.py`; không đụng file của A/B/D. Chi tiết và danh sách việc còn thiếu: `docs/DEVNET_GAP_ANALYSIS.md`.

⚠️ Branch `feat/network-faults` (B) tạo `src/l1sim/network/__init__.py`, trùng file `__init__.py` (rỗng, chưa commit) của C, nên sẽ xung đột khi merge. Giữ nội dung export của B.

## Lần 5: README + hướng dẫn bỏ devnet

| File | Sửa |
|---|---|
| `README.md` (file chung, theo yêu cầu) | Thêm mục "Devnet: test the chain today": demo 4 node, `--scenario` có/không `--headless`, log/results ở đâu, T8 bằng tay, thao tác dashboard, `curl`, pytest. Mục "Run" cũ giữ nội dung, chỉ đổi tên "Run (target, without devnet)" (Lần 6 dịch lại, Lần 8 chuyển phần devnet sang `devnet/README.md`). |
| `docs/DEVNET_TO_PROJECT.md` (mới) | Map từng phần devnet → file thật của nhóm, thứ tự chuyển, lệnh demo sau khi chuyển, khi nào xoá `devnet/`. |
| `devnet/run.py` | Sửa ví dụ curl trong docstring `node=v2` → `node=V2`. |

## Lần 6: tài liệu toàn tiếng Việt

| File | Sửa |
|---|---|
| `README.md` | Dịch toàn bộ sang tiếng Việt (nội dung, lệnh, bảng T1–T8 giữ nguyên ý); thêm link `DEVNET_GAP_ANALYSIS.md`, `DEVNET_TO_PROJECT.md` và dòng `devnet/` trong cấu trúc thư mục. |
| `docs/ROLE_C_P2P.md`, `docs/ROLE_C_P2P_LITE.md` | "Step N" → "Bước N" (cả link mục lục). |

Các file khác trong `docs/` vốn đã là tiếng Việt; thuật ngữ kỹ thuật (block, vote, gossip, proposer…) và câu trích đề bài trong `overview.html` giữ nguyên tiếng Anh. `CONTRIBUTING.md` (không phải file của C) chưa dịch.

## Lần 7: viết lại `docs/p2p_network.html`

Bản cũ còn ghi hello/gossip/static_peers "chưa làm" và dùng pseudo-code. Bản mới giải thích từng bước **cạnh code thật** (file + số dòng, dòng đang nói được tô vàng), trích tự động từ commit `f61a866`:

1. Tổng quan 5 file, 2. khởi động & kết nối (11 bước), 3. HELLO + lý do từ chối, 4. framing (demo tương tác), 5. hành trình một VOTE từ engine V0 tới engine V5 (16 bước), 6. header trước body (devnet → A), 7. mô phỏng gossip 10 node, 8. event log + chỗ gắn FaultInjector, 9. bảng 38 unit test.

Code sửa sau này thì số dòng trong HTML có thể lệch; sinh lại khi cần.

## Lần 8: sửa theo review PR #16 (vtnHAN-SOLO)

| Note review | Sửa |
|---|---|
| `test_devnet.py` import `devnet`, zip nộp bài không có → pytest dừng lúc collection | `pytest.importorskip("devnet.net")`: trong repo vẫn chạy, trong zip thì skip. Không chuyển sang `devnet/tests/` vì `testpaths = ["tests"]` sẽ làm CI không chạy T1 nữa. |
| README (có trong zip) chỉ tới `devnet/` và `docs/` | Phần devnet chuyển sang `devnet/README.md` (mới). README chính bỏ link `DEVNET_*.md`, bỏ dòng `devnet/` trong cấu trúc thư mục, chỉ còn 1 câu nói devnet có trong repo nhưng không có trong bản nộp; "Chạy (mục tiêu, không cần devnet)" → "Chạy". Sửa câu sai ở Lần 5 ("Run cũ giữ nguyên"). |
| HELLO replay + kết nối mới thay kết nối cũ → cắt được peer thật (eclipse) | `TcpTransport.set_inbound_filter(fn)`: chạy **trước** bước thay kết nối, từ chối với reason `unexpected_inbound`. `StaticPeers` đặt filter: chỉ nhận kết nối đi vào từ node đứng sau. Docstring `hello.py` ghi rõ rủi ro eclipse còn lại và cách sửa triệt để (nonce challenge–response, cần đổi `Hello` của A). |
| Network và engine dùng 2 kiểu logger | `emit` chỉ còn 1 quy ước, giống engine: `logger(event, **fields)`, không bao giờ truyền `node`. `node.py` truyền `event_logger.event` cho cả hai. `p2p_cluster.py` bọc logger chung bằng `EventLog.for_node(id)`. `NodeLog` của devnet không cần đổi (vẫn gọi được như hàm). |
| Chỉ đăng ký được 1 disconnect handler | `set_disconnect_handler` → `add_disconnect_handler` (thêm vào list, gọi theo thứ tự, handler lỗi không chặn handler sau). Block sync của D đăng ký thêm được mà không đè `StaticPeers._on_lost`. |
| Thứ tự quay số: docstring nói "id lớn nối id nhỏ", code dùng thứ tự dict | `StaticPeers` dùng `sorted(peers)`; docstring `tcp_transport.py` và `static_peers.py` sửa cho khớp. |
| Trùng tên `duplicate_ignored` (gossip vs engine) | Gossip đổi sang `gossip_duplicate`; `duplicate_ignored` chỉ còn của engine. `NOISY` của devnet lọc `gossip_duplicate`, nên event của engine giờ được ghi vào file. |
| Nit: message bị từ chối không được xét lại | Docstring gossip + comment `Handler`: handler chỉ trả False khi message sai vĩnh viễn. |

Tài liệu cập nhật theo: `ROLE_C_P2P_LITE.md`, `ROLE_C_P2P.md`, `DEVNET_GAP_ANALYSIS.md`, `DEVNET_TO_PROJECT.md` (cách nối logger), và 1 dòng interface `TcpTransport` trong `TEAM_PLAN.md` (`add_disconnect_handler`). `p2p_network.html` là ảnh chụp code ở commit `f61a866`, chưa sinh lại.

Test mới: `test_all_disconnect_handlers_are_called`, `test_inbound_filter_rejects_before_replacing` (`test_tcp_transport.py`); `test_dial_order_is_sorted_not_dict_order`, `test_replayed_hello_cannot_cut_a_real_peer` (`test_static_peers.py`, phát lại HELLO của v0 vào v2 và kiểm kết nối thật vẫn còn). `test_network_events.py` viết lại theo quy ước logger mới.

Chưa làm (không phải phần C): `on_vote`/`on_proposal` trả `bool` (D), chuyển ngoại lệ nonce tương lai trong `_accept_tx` vào `Mempool` (B, khi viết `node.py`). `requested` trong `devnet/node.py` không được dọn khi prune `bodies`: devnet sắp bị xoá nên ghi lại để sửa khi chép sang `node.py`.
