"""I1/I2 trong TEST_PLAN: lần đầu A + B + C + D chạy chung, TCP thật, 1 process.

`devnet/` không có trong file zip nộp bài, nên ở đó cả file này được bỏ qua (skip)
thay vì làm pytest dừng lúc collection.
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("devnet.net", reason="devnet/ không có trong bản nộp")

from devnet.net import Devnet  # noqa: E402
from devnet.node import NodeConfig  # noqa: E402
from devnet.run import Api  # noqa: E402
from l1sim.consensus.engine import TimeoutConfig  # noqa: E402

TIMEOUTS = TimeoutConfig(propose_ms=800, prevote_ms=400, precommit_ms=400, delta_ms=200)


async def until(cond, timeout: float = 15.0) -> None:
    async def poll():
        while not cond():
            await asyncio.sleep(0.02)

    await asyncio.wait_for(poll(), timeout)


def run(coro):
    return asyncio.run(coro)


def test_four_nodes_agree_with_txs():
    async def main():
        net = Devnet(4, NodeConfig(TIMEOUTS, retry=0.05))
        await net.start()
        try:
            await until(lambda: net.max_common_height() >= 1)
            sent = net.send_txs(10)
            assert all(s["accepted"] for s in sent)
            await until(lambda: net.max_common_height() >= 4)
            await until(lambda: all(len(n.mempool) == 0 for n in net.nodes.values()))
        finally:
            await net.stop()

        assert net.safety_violations() == []
        h = net.max_common_height()
        state_hashes = {net.history[v][h]["state_hash"] for v in net.nodes}
        assert len(state_hashes) == 1
        assert sum(net.history["V0"][x]["txs"] for x in range(1, h + 1)) == 10
        # Header trước body: không có BODY nào bị flood, mọi BODY đều được xin trước.
        assert net.log.counts["body_unrequested"] == 0
        assert net.log.counts["gossip_handler_error"] == 0

    run(main())


def test_replayed_tx_is_rejected():
    async def main():
        net = Devnet(4, NodeConfig(TIMEOUTS, retry=0.05))
        await net.start()
        try:
            net.send_txs(1)
            await until(lambda: all(len(n.mempool) == 0 for n in net.nodes.values()))
            assert net.replay_last()["accepted"] is False
        finally:
            await net.stop()
        rejected = [e for e in net.log.events if e["event"] == "tx_rejected"]
        assert rejected and rejected[0]["reason"] == "bad_nonce"

    run(main())


@pytest.mark.parametrize("killed", [["V1"]])
def test_survives_f_crashed_nodes(killed):
    async def main():
        net = Devnet(4, NodeConfig(TIMEOUTS, retry=0.05))
        await net.start()
        try:
            await until(lambda: net.max_common_height() >= 1)
            api = Api(net)
            for v in killed:
                status, result = await api.handle("POST", f"/api/node/{v}/stop", {})
                assert status == 200 and result == {"node": v, "action": "stop"}
                assert not net.nodes[v].alive
                status, _ = await api.handle("POST", f"/api/node/{v}/start", {})
                assert status == 404
                assert not net.nodes[v].alive
            start = net.max_common_height()
            # v1 là proposer của một số height: các height đó phải sang round 1.
            await until(lambda: net.max_common_height() >= start + 4, timeout=30)
        finally:
            await net.stop()
        assert net.safety_violations() == []
        alive = [v for v in net.nodes if v not in killed]
        h = net.max_common_height(alive)
        assert any(net.history["V0"][x]["round"] > 0 for x in range(start + 1, h + 1))

    run(main())


def test_user_custom_tx_lands_in_state():
    async def main():
        net = Devnet(4, NodeConfig(TIMEOUTS, retry=0.05))
        await net.start()
        try:
            ok = net.send_custom("u0", "u0/name", "alice", via="V2")
            assert ok["accepted"] and ok["nonce"] == 0
            assert net.send_custom("u0", "u1/x", "hack")["reason"].startswith("not_owner")
            forged = net.send_custom("u0", "u0/y", "z", sign_as="u1")
            assert forged["reason"] == "bad_signature"
            await until(lambda: all(net.kv(v)["data"].get("u0/name") == "alice" for v in net.nodes))
            assert net.send_custom("u0", "u0/name", "bob", nonce=0)["reason"] == "bad_nonce"
            assert net.send_custom("u0", "u0/name", "bob")["nonce"] == 1
        finally:
            await net.stop()
        assert net.safety_violations() == []

    run(main())


def test_t1_normal_scenario_end_to_end(tmp_path):
    """Config (genesis + t1_normal) → TxClient → network C → engine D → execution B,
    log bằng EventLogger, kết quả bằng ResultWriter: mảnh ghép của cả nhóm chạy chung."""
    from dataclasses import asdict

    from l1sim.config import load_config

    genesis, sc = load_config("config/t1_normal.json", "config/genesis.json")
    users = sorted({t["sender"] for t in sc.transactions})
    net = Devnet(
        sc.num_validators,
        NodeConfig(
            TimeoutConfig.from_config(asdict(sc.consensus)),
            max_txs_per_block=sc.max_txs_per_block,
            retry=0.05,
        ),
        seed=sc.seed,
        validators=list(genesis.validators),
        users=users,
        scenario=sc.name,
        log_root=str(tmp_path),
    )

    async def main():
        await net.start(hold=True)
        try:
            sent = await net.run_scenario_start(sc)
            assert all(s["accepted"] for s in sent)
            # 20 tx, tối đa 4 tx/block → cần ít nhất 5 block có tx.
            await until(
                lambda: net.max_common_height() >= sc.max_height,
                timeout=30,
            )
        finally:
            await net.stop()

    run(main())
    verdict = net.write_results(sc.max_height)
    assert verdict["pass"], verdict
    assert verdict["total_txs"] == 20
    # Tx gửi trước height 1 nên 5 block đầu đầy đúng 4 tx/block.
    assert [net.history["V0"][h]["txs"] for h in range(1, 6)] == [4, 4, 4, 4, 4]
    state = net.nodes["V3"].engine.state
    assert state.get("Alice/account") == "hello-19"
    assert state.get("Charlie/account") == "hello-18"
    assert len(list((tmp_path / "events" / "t1_normal").glob("V*.jsonl"))) == 10
    assert (tmp_path / "results" / "t1_normal" / "verdict.json").is_file()
    assert net.log.counts["log_write_error"] == 0
