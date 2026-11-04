"""Chạy devnet và dashboard web (chỉ thư viện chuẩn).

Từ thư mục gốc repo, cách nào cũng được:
    python devnet/run.py --nodes 4 --port 8080
    python -m devnet.run --nodes 4 --port 8080
rồi mở http://localhost:8080

Chạy theo scenario của nhóm (genesis + config/t1_normal.json, 10 node V0..V9):
    python devnet/run.py --scenario config/t1_normal.json             # có dashboard
    python devnet/run.py --scenario config/t1_normal.json --headless  # tới max_height thì thoát
Log JSONL: logs/events/<scenario>/<node>.jsonl, kết quả: logs/results/<scenario>/*.json

Gửi tx tự nhập không cần dashboard (user u0..u4, key phải bắt đầu bằng "user/"):
    curl -X POST "http://localhost:8080/api/tx/custom?user=u0&key=u0/name&value=alice"
    curl "http://localhost:8080/api/kv?node=V2"
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from dataclasses import asdict
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

# Cho phép chạy thẳng file mà không cần PYTHONPATH: thêm gốc repo (package devnet)
# và src/ (package l1sim) vào đường dẫn import.
_ROOT = Path(__file__).resolve().parent.parent
for _p in (_ROOT, _ROOT / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from devnet.net import Devnet  # noqa: E402
from devnet.node import NodeConfig  # noqa: E402
from l1sim.config import ConfigError, load_config  # noqa: E402
from l1sim.consensus.engine import TimeoutConfig  # noqa: E402

HTML = Path(__file__).with_name("dashboard.html")


class Api:
    def __init__(self, net: Devnet) -> None:
        self.net = net

    async def handle(self, method: str, path: str, q: dict[str, str]) -> tuple[int, object]:
        net = self.net
        if method == "GET" and path == "/api/state":
            return 200, net.state()
        if method == "GET" and path == "/api/events":
            return 200, net.log.since(int(q.get("after", 0)))
        if method == "POST" and path == "/api/tx":
            return 200, net.send_txs(max(1, min(int(q.get("n", 5)), 200)), q.get("via"))
        if method == "POST" and path == "/api/tx/custom":
            nonce = q.get("nonce", "")
            return 200, net.send_custom(
                q["user"],
                q["key"],
                q.get("value", ""),
                via=q.get("via") or None,
                nonce=int(nonce) if nonce != "" else None,
                sign_as=q.get("sign_as") or None,
            )
        if method == "GET" and path == "/api/kv":
            return 200, net.kv(q.get("node", "V0"))
        if method == "POST" and path == "/api/replay":
            return 200, net.replay_last()
        if method == "POST" and path == "/api/fault":
            if "drop" in q:
                net.faults.drop_rate = max(0.0, min(float(q["drop"]), 0.9))
            if "delay" in q:
                net.faults.delay_ms = max(0, min(int(q["delay"]), 5000))
            net.log(
                "fault_changed",
                node="-",
                drop_rate=net.faults.drop_rate,
                delay_ms=net.faults.delay_ms,
            )
            return 200, {"drop_rate": net.faults.drop_rate, "delay_ms": net.faults.delay_ms}
        m = re.fullmatch(r"/api/node/(V\d+)/stop", path)
        if method == "POST" and m and m[1] in net.nodes:
            await net.kill(m[1])
            return 200, {"node": m[1], "action": "stop"}
        return 404, {"error": "not found"}


async def serve_http(api: Api, port: int) -> asyncio.base_events.Server:
    async def on_conn(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = (await reader.readline()).decode("latin-1").split()
            while (await reader.readline()) not in (b"\r\n", b"\n", b""):
                pass  # bỏ header; API không đọc body
            if len(line) < 2:
                return
            method, target = line[0], line[1]
            url = urlsplit(target)
            q = {k: v[-1] for k, v in parse_qs(url.query).items()}
            if method == "GET" and url.path in ("/", "/index.html"):
                status, body, ctype = 200, HTML.read_bytes(), "text/html; charset=utf-8"
            else:
                try:
                    status, obj = await api.handle(method, url.path, q)
                except (ValueError, KeyError) as e:
                    status, obj = 400, {"error": str(e)}
                body, ctype = json.dumps(obj).encode(), "application/json"
            head = (
                f"HTTP/1.1 {status} OK\r\nContent-Type: {ctype}\r\n"
                f"Content-Length: {len(body)}\r\nCache-Control: no-store\r\n"
                "Connection: close\r\n\r\n"
            )
            writer.write(head.encode() + body)
            await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()

    return await asyncio.start_server(on_conn, "127.0.0.1", port)


def with_hold(t: TimeoutConfig, hold: int) -> TimeoutConfig:
    # Proposer prevote block của mình ngay nhưng giữ HEADER thêm block_ms, nên mọi timeout
    # phải dài hơn block_ms, nếu không proposer hết giờ prevote trước khi node khác kịp vote.
    return TimeoutConfig(
        t.propose_ms + hold, t.prevote_ms + hold, t.precommit_ms + hold, t.delta_ms
    )


async def wait_until(cond, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while not cond():
        if time.monotonic() > deadline:
            return False
        await asyncio.sleep(0.1)
    return True


async def main() -> int:
    p = argparse.ArgumentParser(description="L1 devnet + dashboard")
    p.add_argument("--nodes", type=int, default=4)
    p.add_argument("--port", type=int, default=8080)
    p.add_argument(
        "--block-ms",
        type=int,
        default=None,
        help="proposer giữ HEADER bao lâu trước khi phát (cho dễ xem). "
        "Mặc định 1000, hoặc 0 khi --headless",
    )
    p.add_argument("--propose-ms", type=int, default=3000)
    p.add_argument("--vote-ms", type=int, default=1000)
    p.add_argument("--delta-ms", type=int, default=500)
    p.add_argument("--scenario", help="file scenario JSON, vd config/t1_normal.json")
    p.add_argument("--genesis", default="config/genesis.json")
    p.add_argument("--headless", action="store_true", help="không mở web, tới max_height thì thoát")
    args = p.parse_args()
    hold = args.block_ms if args.block_ms is not None else (0 if args.headless else 1000)

    sc = None
    if args.scenario:
        try:
            genesis, sc = load_config(args.scenario, args.genesis)
        except ConfigError as e:
            print(f"config lỗi: {e}", file=sys.stderr)
            return 2
        users = sorted({t["sender"] for t in sc.transactions}) + [f"u{i}" for i in range(5)]
        net = Devnet(
            sc.num_validators,
            NodeConfig(
                with_hold(TimeoutConfig.from_config(asdict(sc.consensus)), hold),
                max_txs_per_block=sc.max_txs_per_block,
                block_ms=hold,
            ),
            seed=sc.seed,
            validators=list(genesis.validators),
            users=users,
            scenario=sc.name,
        )
        net.faults.drop_rate = sc.network.drop_rate
        net.faults.min_delay_ms = sc.network.min_delay_ms
        net.faults.delay_ms = sc.network.max_delay_ms
    else:
        timeouts = TimeoutConfig(args.propose_ms, args.vote_ms, args.vote_ms, args.delta_ms)
        net = Devnet(args.nodes, NodeConfig(with_hold(timeouts, hold), block_ms=hold))

    await net.start(hold=sc is not None)
    print(f"devnet {net.n} node, n={net.n} f={net.f} quorum={net.quorum}")
    for v, addr in net.addrs.items():
        print(f"  {v} {addr}")

    if sc is not None:
        sent = await net.run_scenario_start(sc)
        print(f"scenario {sc.name}: gửi {len(sent)} tx, nhận {sum(x['accepted'] for x in sent)}")

    if args.headless:
        max_h = sc.max_height if sc else 10
        ok = await wait_until(lambda: net.max_common_height() >= max_h, 60 + max_h * 5)
        await net.stop()
        if sc is None:
            print(f"đạt height {net.max_common_height()}, safety {net.safety_violations() or 'OK'}")
            return 0 if ok and not net.safety_violations() else 1
        verdict = net.write_results(sc.max_height)
        for k, v in verdict.items():
            print(f"  {k}: {v}")
        print(f"log: logs/events/{sc.name}/   kết quả: logs/results/{sc.name}/")
        return 0 if verdict["pass"] else 1

    server = await serve_http(Api(net), args.port)
    print(f"dashboard: http://localhost:{args.port}   (Ctrl+C để dừng)")
    try:
        async with server:
            await server.serve_forever()
    finally:
        await net.stop()
        if sc is not None:
            net.write_results(sc.max_height)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        pass
