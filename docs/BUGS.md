# Non-trivial bugs & cách fix

Nguồn cho mục **"Non-trivial bugs & cách fix"** của REPORT (đề bắt buộc có mục này). Mỗi thành viên ghi bug của phần mình vào đúng mục bên dưới.

**Ghi bug nào:** bug mất nhiều thời gian để tìm ra, hoặc khó thấy (chỉ lộ ra khi chạy nhiều process, có lỗi mạng hoặc Byzantine). Không cần ghi lỗi gõ nhầm hay lỗi lint.

**Mẫu cho mỗi bug:**

```markdown
### X1. Tên ngắn của bug (#issue/#PR nếu có)

- **Triệu chứng:** thấy gì khi chạy (log, test fail, chain kẹt...).
- **Nguyên nhân:** vì sao xảy ra.
- **Fix:** đã sửa thế nào.
- **Test:** test nào chứng minh đã sửa (nếu có).
```

Đánh số theo vai trò: A1, A2... B1... C1... D1...

## A – Foundation, integration, T1/T3

_(A điền)_

## B – Execution, faults, T4/T5/T8

_(B điền)_

## C – TCP, HELLO, static peers, gossip, T2

_(C điền)_

## D – Consensus, locking, Byzantine, T6/T7

### D1. Engine crash ngay event đầu tiên khi ghép với `EventLogger` (#14, #15)

- **Triệu chứng:** truyền `EventLogger.event` làm `logger` cho engine thì crash ngay event đầu tiên: `ValueError: event fields cannot override core fields: node`.
- **Nguyên nhân:** `ConsensusEngine._log` tự truyền `node=self.node_id`, trong khi `EventLogger` đã tự ghi `node` và cấm field này.
- **Fix:** engine bỏ `node`, để logger sở hữu node id. `node.py` truyền thẳng `EventLogger(...).event`, không cần adapter.

### D2. Chưa có locking thì hai node finalize hai block khác nhau ở cùng height (#22)

- **Triệu chứng:** dựng lại phản ví dụ của Concept Guide trong harness: V0 finalize B ở round 0, các node khác mất precommit round 0 rồi finalize C ở round 1. **Mất safety.**
- **Nguyên nhân:** engine bản đầu chưa có rule 4–5 (lock), nên ở round mới node prevote thoải mái cho proposal mới.
- **Fix:** thêm locking. Precommit B khi thấy polka thì lock B. Ở các round sau chỉ prevote B, trừ khi thấy polka cho block khác ở round mới hơn `locked_round`.
- **Test:** `test_locking_keeps_safety_when_round_0_precommits_are_lost`.

### D3. Lock theo đúng flowchart (prevote NIL) làm chain đứng yên (#22)

- **Triệu chứng:** sau khi có locking, nhóm node đã lock prevote NIL còn nhóm chưa lock prevote block mới, nên không bên nào đủ quorum. Round nào cũng vậy.
- **Nguyên nhân:** flowchart gốc cho node đã lock prevote NIL khi proposal là block khác, và proposer luôn tạo block mới.
- **Fix:** node đã lock prevote **block đã lock**. Proposer đang lock (hoặc biết polka cũ) **gửi lại đúng block đó** (re-propose, giữ nguyên header và hash). Node chưa lock mà biết polka cũ thì prevote block của polka đó. Safety không đổi, vì node đã lock vẫn không prevote block nào khác.
- **Test:** `test_locked_proposer_re_proposes_its_block`, `test_lock_survives_rounds_and_locked_block_is_prevoted`.

### D4. T7 e2e kẹt ở `commit_waiting_for_block` (#22)

- **Triệu chứng:** T7 (10 process, V7–V9 equivocate) thỉnh thoảng có node đứng mãi với log `commit_waiting_for_block`.
- **Nguyên nhân:** proposer Byzantine gửi 2 block khác nhau cho cùng `(height, round)`. Node nhận block thứ hai sau thì bỏ hẳn nó, kể cả trong kho block theo hash. Nếu cả mạng lại commit đúng block thứ hai đó, node có đủ precommit nhưng không có block để finalize.
- **Fix:** block xung đột không chiếm slot proposal (nên node không prevote nó), nhưng vẫn được lưu theo hash (`_store_proposal`), để commit cho nó finalize được.
- **Test:** `test_conflicting_proposal_that_gets_committed_is_still_finalized`.

### D5. T7 chạy 2 lần cho kết quả khác nhau (#22)

- **Triệu chứng:** T8 cho T7 fail: `commit_round` và danh sách equivocation trong results khác nhau giữa 2 lần chạy.
- **Nguyên nhân:**
  - Thứ tự hai proposal xung đột tới mỗi node phụ thuộc gossip, nên node chọn block khác nhau và commit ở round khác nhau.
  - Số cặp equivocation mà mỗi node phát hiện phụ thuộc timing (33–38 cặp).
- **Fix:**
  - Block thứ hai của proposal equivocate được gửi trễ `conflict_delay_ms` (gấp đôi timeout dài nhất), nên mọi node đều nhận block thứ nhất trước.
  - Results chỉ ghi `equivocators` (danh sách id, đã sort), không ghi từng cặp. Cặp chi tiết vẫn có trong event log.
- **Test:** `test_t7_equivocators_are_caught_and_results_reproduce` (chạy 2 lần, so byte-identical).

### D6. Polka chỉ tới một phần node thì chain kẹt (review #22, P1)

- **Triệu chứng (A tái hiện):** 10 node trung thực. Round 0 chỉ V0–V3 nhận đủ 7 prevote nên lock B; precommit round 0 mất hết. Từ round 1 mạng giao đủ, nhưng không round nào finalize được.
- **Nguyên nhân:** V4–V9 không bao giờ biết B từng có polka, vì prevote đã mất mà gossip không gửi lại. V0–V3 luôn prevote B, V4–V9 vote block mới hoặc NIL, nên không bên nào đủ 7.
- **Fix:** **polka evidence.** Mỗi lần vào round mới, node đang theo một polka cũ gửi lại cả 2f + 1 prevote có chữ ký của polka đó. V4–V9 kiểm chữ ký, thấy polka và prevote B. Chỉ gửi vote thật, nên không đổi luật vote nào.
- **Test:** `test_polka_evidence_lets_nodes_that_missed_the_polka_converge`. Bản đối chứng `test_without_polka_evidence_the_partial_polka_stalls` cho thấy không có cơ chế này thì kẹt.

### D7. T7 thỉnh thoảng kẹt vì không đếm vote thứ hai của node equivocate (#27, #28)

- **Triệu chứng:** CI fail ngẫu nhiên ở T7 (`V0: timed out before finalizing height 9`), suite chạy 220s thay vì khoảng 110s. Event log: V6 ghi `equivocation_detected` cho precommit thật của V7, V8, V9 ở height 3, rồi kẹt 17 round ở height đó. Các node khác lên tới height 6 thì cũng kẹt vì thiếu node trung thực.
- **Nguyên nhân:** `VoteSet` chỉ giữ vote tới trước của mỗi voter. V6 nhận precommit giả (block khác) của V7–V9 trước, nên precommit thật cho block được commit bị coi là equivocation và **không đếm**. V6 thiếu quorum vĩnh viễn, và không có block sync để đuổi kịp.
- **Fix, qua 3 vòng review:**
  1. Đếm mỗi vote cho đúng giá trị của nó, mỗi voter tối đa 1 lần cho mỗi giá trị; vẫn ghi evidence. An toàn vì hai quorum chung nhau ít nhất n − 2f ≥ f + 1 voter, nên muốn 2 block cùng đạt quorum thì phải có honest node equivocate.
  2. `on_vote` trả `True` cho vote xung đột mới, để gossip forward nó đúng một lần. Nếu không, Byzantine chỉ gửi vote đó cho vài node thì các node còn lại vẫn thiếu. Bản lặp trả `duplicate`, không forward.
  3. Bỏ giới hạn số giá trị xung đột mỗi voter (đã thêm ở vòng 2): giới hạn cho phép Byzantine gửi hash rác trước để vote thật bị loại, tái tạo đúng lỗi này. Đánh đổi: Byzantine flood nhiều hash thì mọi node phải lưu và forward; flood nằm ngoài các hành vi Byzantine của lab nên ghi vào hạn chế.
- **Test:** `test_equivocators_votes_count_for_the_block_that_gets_committed` (`junk=1`, `junk=3`), `test_f_equivocators_cannot_give_two_blocks_a_quorum`, `test_equivocators_conflicting_vote_is_relayed_once` (qua Node + Gossip thật).

