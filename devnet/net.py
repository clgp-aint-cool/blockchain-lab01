"""Devnet: N DevNode trong 1 event loop, TCP thật trên 127.0.0.1. Dùng cho test và dashboard."""

from __future__ import annotations

import asyncio
from dataclasses import replace

from devnet.node import DevNode, Faults, Keys, NodeConfig, Recorder
from l1sim.client import TxClient
from l1sim.config import ScenarioConfig
from l1sim.crypto import sign
from l1sim.encoding import block_hash
from l1sim.logger import EventLogger, ResultWriter
from l1sim.models import Block, Domain, Tx


class Devnet:
    def __init__(
        self,
        n: int,
        cfg: NodeConfig,
        seed: int = 42,
        validators: list[str] | None = None,
        users: list[str] | None = None,
        scenario: str | None = None,
        log_root: str = "logs",
    ) -> None:
        """scenario: tên scenario; có thì mỗi node ghi EventLogger vào
        `{log_root}/events/<scenario>/<node>.jsonl` và `write_results` ghi `{log_root}/results`.
        """
        self.n = n
        self.f = (n - 1) // 3
        self.quorum = 2 * self.f + 1
        self.cfg = cfg
        self.seed = seed
        self.keys = Keys.make(validators or n, users or 5, seed=seed)
        self.log = Recorder()
        self.faults = Faults(seed=seed)
        self.scenario = scenario
        self.log_root = log_root
        # Mỗi node dùng một EventLogger trong suốt lần chạy.
        self.event_logs: dict[str, EventLogger] = {}
        self.nodes: dict[str, DevNode] = {}
        self.addrs: dict[str, str] = {}
        # history[node][height] = {hash, state_hash, txs, round}: không xoá khi node tắt,
        # để kiểm safety cả trên chain của node đã chết.
        self.history: dict[str, dict[int, dict]] = {v: {} for v in self.keys.validators}
        self.nonces: dict[str, int] = {u: 0 for u in self.keys.users}  # nonce phía client
        self.last_tx: Tx | None = None
        self._tx_counter = 0

    def _new_node(self, node_id: str) -> DevNode:
        if self.scenario and node_id not in self.event_logs:
            self.event_logs[node_id] = EventLogger(
                node_id, self.scenario, log_root=f"{self.log_root}/events"
            )
        return DevNode(
            node_id,
            self.keys,
            self.cfg,
            self.log,
            self.faults,
            on_finalize=self._on_finalize,
            event_log=self.event_logs.get(node_id),
        )

    async def start(self, hold: bool = False) -> None:
        """hold=True: dựng mạng nhưng chưa cho engine chạy, đợi `release()`.

        Scenario dùng để gửi hết tx vào mempool trước height 1 (20 tx, 4 tx/block → đúng
        5 block đầy cho max_height 5). Không hold thì engine chạy luôn, tạo block rỗng.
        """
        for v in self.keys.validators:
            self.nodes[v] = self._new_node(v)
            self.addrs[v] = await self.nodes[v].listen()
            if not hold:
                self.nodes[v].go.set()
        await asyncio.gather(*(node.start(self.addrs) for node in self.nodes.values()))

    async def stop(self) -> None:
        await asyncio.gather(*(n.stop() for n in self.nodes.values() if n.alive))

    async def run_scenario_start(self, sc: ScenarioConfig) -> list[dict]:
        """Sau `start(hold=True)`: đợi mesh, gửi tx của scenario, dùng warmup_ms cho tx lan tới
        mempool mọi node, rồi `release()`. Nếu không đợi, proposer height 1 chỉ có vài tx."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.cfg.mesh_wait
        while loop.time() < deadline and any(
            len(n.transport.peers()) < self.n - 1 for n in self.nodes.values()
        ):
            await asyncio.sleep(0.05)
        sent = self.submit_scenario(sc)
        want = sum(s["accepted"] for s in sent)
        deadline = loop.time() + sc.warmup_ms / 1000
        while loop.time() < deadline and any(len(n.mempool) < want for n in self.nodes.values()):
            await asyncio.sleep(0.02)
        self.release()
        return sent

    def release(self) -> None:
        for node in self.nodes.values():
            node.go.set()

    async def kill(self, node_id: str) -> None:
        node = self.nodes[node_id]
        if node.alive:
            await node.stop()

    def _on_finalize(self, node: DevNode, block: Block, cert) -> None:
        self.history[node.id][block.header.height] = {
            "hash": block_hash(block.header),
            "state_hash": node.engine.state.state_hash(),
            "txs": len(block.txs),
            "round": block.header.round,
        }

    # ------------------------------------------------------------------ tx

    def make_tx(self) -> Tx:
        users = sorted(self.keys.users)
        user = users[self._tx_counter % len(users)]
        self._tx_counter += 1
        nonce = self.nonces[user]
        self.nonces[user] += 1
        tx = Tx(user, f"{user}/k{nonce}", f"value-{self._tx_counter}", nonce)
        return replace(tx, signature=sign(self.keys.users[user].private_key, Domain.TX, tx))

    def alive_ids(self) -> list[str]:
        return [v for v, node in self.nodes.items() if node.alive]

    def send_txs(self, count: int, via: str | None = None) -> list[dict]:
        alive = self.alive_ids()
        out = []
        for i in range(count):
            tx = self.make_tx()
            target = via if via in alive else alive[i % len(alive)]
            out.append(
                {
                    "sender": tx.sender,
                    "nonce": tx.nonce,
                    "via": target,
                    "accepted": self.nodes[target].submit_tx(tx),
                }
            )
            self.last_tx = tx
        return out

    def send_custom(
        self,
        user: str,
        key: str,
        value: str,
        via: str | None = None,
        nonce: int | None = None,
        sign_as: str | None = None,
    ) -> dict:
        """Tx do người dùng tự nhập (dashboard / curl).

        nonce=None: lấy nonce kế tiếp phía client. sign_as: ký bằng key của user khác để
        thử chữ ký giả. key không bắt đầu bằng "user/" để thử not_owner.
        """
        if user not in self.keys.users:
            raise ValueError(f"không có user {user}; có: {sorted(self.keys.users)}")
        signer = sign_as or user
        if signer not in self.keys.users:
            raise ValueError(f"không có user {signer}")
        alive = self.alive_ids()
        if not alive:
            raise ValueError("không còn node nào đang chạy")
        target = via if via in alive else alive[0]
        auto = nonce is None
        n = self.nonces[user] if auto else nonce
        try:
            tx = Tx(user, key, value, n)
        except ValueError as e:  # model của A chặn từ lúc tạo tx (vd key của người khác)
            reason = "not_owner" if not key.startswith(f"{user}/") else str(e)
            return {
                "sender": user,
                "key": key,
                "value": value,
                "nonce": n,
                "via": target,
                "accepted": False,
                "reason": f"{reason} (client không tạo được tx)",
            }
        tx = replace(tx, signature=sign(self.keys.users[signer].private_key, Domain.TX, tx))
        seq = self.log.seq
        accepted = self.nodes[target].submit_tx(tx)
        if accepted and auto:
            self.nonces[user] += 1
        if accepted:
            self._tx_counter += 1
            self.last_tx = tx
        reason = next(
            (e["reason"] for e in self.log.since(seq) if e["event"] == "tx_rejected"),
            "" if accepted else "mempool từ chối (trùng / đầy)",
        )
        return {
            "sender": user,
            "key": key,
            "value": value,
            "nonce": n,
            "via": target,
            "accepted": accepted,
            "reason": reason,
        }

    def kv(self, node_id: str) -> dict:
        """State (key -> value, nonce) mà node đó thấy sau block cuối nó finalize."""
        node = self.nodes[node_id]
        if not node.alive or node.engine is None:
            return {"node": node_id, "alive": False}
        st = node.engine.state
        return {
            "node": node_id,
            "alive": True,
            "height": node.engine.height - 1,
            "state_hash": st.state_hash(),
            "data": dict(sorted(st.data.items())),
            "nonces": dict(sorted(st.nonces.items())),
            "client_nonces": dict(self.nonces),
        }

    def submit_scenario(self, sc: ScenarioConfig) -> list[dict]:
        """Gửi danh sách tx của scenario qua `TxClient` (client của nhóm), xoay vòng các node."""
        alive = self.alive_ids()
        out: list[dict] = []

        def handler(tx: Tx) -> None:
            target = alive[len(out) % len(alive)]
            accepted = self.nodes[target].submit_tx(tx)
            out.append(
                {"sender": tx.sender, "nonce": tx.nonce, "via": target, "accepted": accepted}
            )
            self.last_tx = tx
            if accepted:
                self._tx_counter += 1
                self.nonces[tx.sender] = max(self.nonces.get(tx.sender, 0), tx.nonce + 1)

        TxClient(sc.seed, handler).submit_scenario(sc)
        self.log("scenario_txs", node="-", sent=len(out), accepted=sum(o["accepted"] for o in out))
        return out

    def write_results(self, max_height: int | None = None) -> dict:
        """Ghi kết quả từng node + verdict bằng ResultWriter của nhóm (logs/results/<scenario>)."""
        if not self.scenario:
            raise ValueError("chỉ ghi kết quả khi chạy theo scenario")
        writer = ResultWriter(self.scenario, results_root=f"{self.log_root}/results")
        writer.clear()
        for v, hist in self.history.items():
            node = self.nodes[v]
            st = node.engine.state if node.engine is not None else None
            writer.write_node(
                v,
                {
                    "node": v,
                    "finalized_height": max(hist, default=0),
                    "blocks": [{"height": h, **hist[h]} for h in sorted(hist)],
                    "state_hash": st.state_hash() if st else "",
                    "state": dict(sorted(st.data.items())) if st else {},
                },
            )
        common = self.max_common_height(list(self.nodes))
        state_hashes = (
            sorted({self.history[v][common]["state_hash"] for v in self.nodes}) if common else []
        )
        verdict = {
            "scenario": self.scenario,
            "n": self.n,
            "f": self.f,
            "common_height": common,
            "max_height": max_height or 0,
            "reached_max_height": bool(max_height) and common >= max_height,
            "safety_violations": self.safety_violations(),
            "same_state_at_common_height": len(state_hashes) == 1,
            "total_txs": sum(
                self.history[min(self.nodes)].get(h, {}).get("txs", 0) for h in range(1, common + 1)
            ),
        }
        verdict["pass"] = (
            not verdict["safety_violations"]
            and verdict["same_state_at_common_height"]
            and (not max_height or verdict["reached_max_height"])
        )
        writer.write_verdict(verdict)
        return verdict

    def replay_last(self) -> dict:
        """Gửi lại tx cuối qua 1 node khác: phải bị từ chối nếu tx đó đã vào chain."""
        if self.last_tx is None:
            return {"error": "chưa gửi tx nào"}
        alive = self.alive_ids()
        target = alive[-1]
        tx = self.last_tx
        return {
            "sender": tx.sender,
            "nonce": tx.nonce,
            "via": target,
            "accepted": self.nodes[target].submit_tx(tx),
        }

    # --------------------------------------------------------------- views

    def chain(self) -> dict[int, dict[str, str]]:
        heights = sorted({h for hist in self.history.values() for h in hist})
        return {
            h: {v: hist[h]["hash"] for v, hist in self.history.items() if h in hist}
            for h in heights
        }

    def safety_violations(self) -> list[dict]:
        bad = []
        for h, by_node in self.chain().items():
            if len(set(by_node.values())) > 1:
                bad.append({"height": h, "hashes": by_node})
        return bad

    def state(self) -> dict:
        blocks = {}
        for h, by_node in self.chain().items():
            any_node = next(iter(by_node))
            info = self.history[any_node][h]
            blocks[h] = {"by_node": by_node, "txs": info["txs"], "round": info["round"]}
        return {
            "n": self.n,
            "f": self.f,
            "quorum": self.quorum,
            "nodes": [self.nodes[v].snapshot() for v in sorted(self.nodes, key=_vkey)],
            "blocks": blocks,
            "violations": self.safety_violations(),
            "faults": {
                "drop_rate": self.faults.drop_rate,
                "delay_ms": self.faults.delay_ms,
                "dropped": self.faults.dropped,
            },
            "traffic": [[a, b, c] for (a, b), c in sorted(self.faults.sent.items())],
            "counts": dict(self.log.counts),
            "tx_sent": self._tx_counter,
            "users": sorted(self.keys.users),
        }

    def max_common_height(self, ids: list[str] | None = None) -> int:
        ids = ids or self.alive_ids() or list(self.nodes)
        return min((max(self.history[v], default=0) for v in ids), default=0)


def _vkey(v: str) -> int:
    return int(v[1:])
