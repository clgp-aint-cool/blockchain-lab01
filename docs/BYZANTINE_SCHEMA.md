# Byzantine schema trong scenario

Schema được kiểm tra bởi `parse_byzantine` trong `consensus/byzantine.py`.

Mục tiêu: một scenario ghi rõ **node nào, làm sai gì, ở height/round nào** (TEAM_PLAN, mục Reproducibility), đủ cho T3, T6, T7, và hoàn toàn tất định để giữ T8.

## Định dạng

`byzantine` là danh sách **quy tắc**. Mỗi quy tắc áp dụng cho một node:

```json
"byzantine": [
  {"node": "V2", "behavior": "silent", "messages": ["proposal"], "heights": [2], "rounds": [0]}
]
```

| Field | Bắt buộc | Giá trị | Mặc định |
|---|---|---|---|
| `node` | ✓ | id có trong `validators` | |
| `behavior` | ✓ | `silent`, `equivocate`, `bad_signature`, `wrong_domain`, `invalid_proposal`, `delay` | |
| `messages` | | tập con của `proposal`, `prevote`, `precommit` | cả ba |
| `heights` | | danh sách height | mọi height |
| `from_height`, `to_height` | | khoảng height, dùng **thay cho** `heights` | |
| `rounds` | | danh sách round | mọi round |
| `domain` | chỉ với `wrong_domain` | `TX`, `HEADER`, `HELLO` | `TX` |
| `delay_ms` | ✓ với `delay` | số nguyên dương, mili giây | |

Một message gửi đi bị áp dụng quy tắc khi khớp **cả** `messages`, height và round (round của proposal là `header.round`). Nhiều quy tắc khớp thì quy tắc đứng trước trong danh sách thắng. Message không khớp quy tắc nào thì gửi bình thường: ngoài các message bị áp dụng, node Byzantine chạy như node trung thực.

## Behavior

| Behavior | Node làm gì | Node khác thấy gì |
|---|---|---|
| `silent` | Không gửi message | Timeout. Proposal bị im thì cả mạng prevote NIL, sang round sau |
| `equivocate` | Gửi **hai** bản, cả hai ký hợp lệ. Proposal: block thật và block cùng height/round nhưng **không có tx** (block thật đã rỗng thì bản thứ hai đổi `state_hash`). Vote: vote thật và vote cho block khác (vote thật là NIL thì bản thứ hai cho proposal của round, chưa có proposal thì một hash tất định; vote thật cho block thì bản thứ hai là NIL). Bản thứ hai của **proposal** gửi trễ 2 × timeout dài nhất, để mọi node trung thực thấy block thật trước (T8); vote xung đột gửi ngay | `proposal_conflict`, `equivocation_detected`; mỗi vote được đếm cho giá trị của nó, bản lặp bị chặn |
| `bad_signature` | Gửi message với chữ ký bị sửa (đảo bit cuối) | `vote_rejected` / `proposal_rejected`, reason `bad_signature` |
| `wrong_domain` | Ký đúng nội dung nhưng bằng `domain` khác | `vote_rejected`, reason `wrong_domain` |
| `invalid_proposal` | Chỉ áp dụng cho `proposal`. Gửi block có `state_hash` sai (đảo bit cuối); header vẫn được ký lại hợp lệ, nên block qua được bước kiểm chữ ký và chỉ sai ở nội dung | `invalid_block`, reason `bad_state_hash`; cả mạng prevote NIL, sang round sau |
| `delay` | Gửi message đúng nội dung nhưng trễ đúng `delay_ms` | Trễ ít hơn timeout: message vẫn được đếm. Trễ quá timeout: với round đó giống `silent`, message tới muộn bị bỏ qua |

Không có gì ngẫu nhiên: mọi bản sai đều suy ra được từ message gốc, và `delay_ms` là số cố định.

**Chọn `delay_ms` theo quy tắc "delay hai cực"** (TEAM_PLAN, mục Reproducibility): ≤ 10% timeout hoặc ≥ 2× timeout của bước bị trễ. Giá trị sát timeout làm message lúc kịp lúc không, và T8 không còn giống từng byte.

## Ví dụ

**T6.** Proposer của height 2 round 0 im lặng. Proposer là validator thứ `(height + round) mod n`, nên là V2.

```json
"byzantine": [
  {"node": "V2", "behavior": "silent", "messages": ["proposal"], "heights": [2], "rounds": [0]}
]
```

Mong đợi: height 2 finalize ở round 1, với proposer V3.

**T7.** f = 3 node equivocate.

```json
"byzantine": [
  {"node": "V7", "behavior": "equivocate"},
  {"node": "V8", "behavior": "equivocate"},
  {"node": "V9", "behavior": "equivocate"}
]
```

Mong đợi: các node trung thực không finalize hai block khác nhau ở cùng height; results có danh sách equivocation.

**T3** (phần vote; HELLO mạo danh nằm ở tầng mạng).

```json
"byzantine": [
  {"node": "V5", "behavior": "bad_signature", "messages": ["prevote"], "heights": [1]},
  {"node": "V6", "behavior": "wrong_domain", "domain": "TX", "messages": ["prevote"], "heights": [1]}
]
```

**Invalid proposal.** Proposer của height 3 round 0 (V3) đề xuất block sai nội dung.

```json
"byzantine": [
  {"node": "V3", "behavior": "invalid_proposal", "heights": [3], "rounds": [0]}
]
```

Mong đợi: rule 3 (`validate_block`) chặn block; height 3 finalize ở round 1, với proposer V4.

**Delay.** Proposer của height 4 round 0 (V4) gửi proposal trễ 2500 ms, tức ≥ 2× `timeout_propose_ms` (1000 ms).

```json
"byzantine": [
  {"node": "V4", "behavior": "delay", "delay_ms": 2500, "messages": ["proposal"], "heights": [4], "rounds": [0]}
]
```

Mong đợi: cả mạng hết timeout và prevote NIL; height 4 finalize ở round 1, với proposer V5. Proposal tới muộn không làm node nào prevote lần hai.

## Kiểm khi đọc config

- `node` có trong `validators`. Số node **khác nhau** ≤ f. Một node được có nhiều quy tắc.
- `behavior` và `messages` nằm trong danh sách cho phép.
- `heights` nằm trong `1..max_height`, không dùng chung với `from_height`/`to_height`.
- `domain` chỉ dùng với `wrong_domain`, và không được là `VOTE`.
- `invalid_proposal` chỉ đi với `messages: ["proposal"]`; không ghi `messages` thì mặc định là `["proposal"]`.
- `delay_ms` bắt buộc với `delay` và chỉ dùng với `delay`.
- Dạng cũ `["V2"]` (chỉ có id) bị báo lỗi, vì không cho biết node làm gì.

## Tích hợp

`load_scenario` kiểm tra schema bằng `parse_byzantine`. `Node._broadcast` gọi
`Byzantine.outgoing` trước khi chuyển BODY thành HEADER, để các block gửi ra
đều trả lời được BODY_REQUEST. Message có delay được hẹn bằng event loop.
Mỗi lần áp dụng quy tắc có event `byzantine_action`.

`silent` không ghi `messages` áp dụng cho cả proposal, prevote và precommit;
T6 chỉ im lặng proposal. T3 thực tế dùng `config/t3_bad_sig.json`.

## Results

`logs/results/<scenario>/<node>.json` có thêm:

- `blocks`: mỗi height một mục `height`, `hash`, `round`, `commit_round`, `proposer`, `state_hash`, `txs` (số tx), `tx_ids`.
- `equivocators`: các validator bị bắt equivocate, đã sort. Chỉ ghi id chứ không ghi từng cặp vote, vì cặp nào được phát hiện phụ thuộc timing; từng lần phát hiện nằm trong event log (`equivocation_detected`).
