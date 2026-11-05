# Kiểm chứng T1–T8

## Cài đặt và chạy

Từ thư mục gốc repo, trong môi trường Python đã kích hoạt:

```bash
python -m pip install -r tests/requirements.txt
python tests/run_tests.py
python tests/run_tests.py --unit
python tests/run_tests.py --e2e
python tests/run_tests.py --scenario T3
```

Runner dùng cùng interpreter cho pytest và node con, tự đặt đường dẫn import,
và giữ exit code của pytest. Không có test được chọn cũng trả mã khác 0.
Các tùy chọn pytest đặt sau `--`, ví dụ `-- -v --cov=l1sim`.

## Ma trận kịch bản

| ID | Test | Bằng chứng bắt buộc |
|---|---|---|
| T1 | `tests/e2e/test_local_nodes.py` | Process validator dùng TCP/HELLO/gossip, cùng chain và state; thử cả địa chỉ tùy chỉnh |
| T2 | `tests/e2e/test_t2_dup_reorder.py` | Duplicate/reorder không làm thực thi trùng tx; chain và state nhất quán. Unit VoteSet kiểm tra không đếm vote hai lần |
| T3 | `tests/e2e/test_t3_invalid_signature.py` | V5 gửi prevote sai chữ ký, V6 ký prevote bằng domain TX; mọi node trung thực log đúng reason và vẫn finalize cùng chain |
| T4 | `tests/e2e/test_t4_replay_tx.py` | 20 tx đầu vào, 11 cặp sender/nonce riêng biệt; chỉ 11 tx được commit, tx_id không lặp |
| T5 | `tests/e2e/test_t5_pre_gst.py` | Có fault_drop thực sự trước GST; safety giữ và các node đạt max_height |
| T6 | `tests/e2e/test_byzantine_scenarios.py::test_t6_silent_proposer_is_replaced_in_round_1` | Proposer height 2 im lặng; timeout sang round 1 với V3 |
| T7 | `tests/e2e/test_byzantine_scenarios.py::test_t7_equivocators_are_caught_and_results_reproduce` | Tối đa f equivocator được phát hiện; node trung thực cùng chain, results tái lập |
| T8 | `tests/e2e/test_t8_reproducible.py` | Cùng seed chạy hai lần cho results giống từng byte |

Unit test kiểm tra thêm chữ ký/domain, nonce, ownership, state hash, quorum,
locking, round-skip, evidence, framing, peer authentication, gossip và fault decisions.
`tests/e2e/test_client_ingress.py` kiểm tra TX đi từ client qua TCP tới mempool và chain.
`tests/integration/test_devnet.py` kiểm tra dashboard backend trong một process;
được skip nếu bản phân phối không có devnet.

T3 có barrier giữa các process: kết nối đủ peer, gửi vote sai qua rule và gossip
thật, chờ mỗi receiver ghi reason và xác nhận vote không được đếm, rồi mới bắt đầu
consensus. Cách này giữ việc kiểm tra chữ ký độc lập với cuộc đua finalize height.

## Log và reproduce

Hướng dẫn lưu và đọc log nằm trong phần [Đọc log và reproduce của README](../README.md#4-đọc-log-và-reproduce).
Event `vote_rejected` phải có reason tương ứng với lỗi được tạo;
`byzantine_action` chứng minh fault đã thực sự chạy.
Không suy ra pass chỉ từ exit code của launcher: test còn kiểm tra nội dung
results, state, số giao dịch và event log tùy yêu cầu từng kịch bản.

Không chạy đồng thời nhiều E2E trong cùng checkout: một số test dùng chung log.
Các test dùng TCP loopback; cấu hình nhiều máy qua ZeroTier được mô tả riêng
trong README và cần kiểm tra thủ công trên môi trường đó.
