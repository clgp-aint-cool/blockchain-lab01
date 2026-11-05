#!/usr/bin/env python3
"""Web dashboard chạy scenario bằng code thật của nhóm và hiển thị trực tiếp.

Không có logic blockchain riêng. Mỗi validator là một process `python -m l1sim.run_node`
(giống `launch_local`), server chỉ:
- bật / tắt process (nút Kill = Ctrl+C terminal của node đó);
- đọc dần `logs/events/<scenario>/V*.jsonl` mà node ghi ra để vẽ trạng thái;
- khi mọi process dừng, gọi `check_safety` (của B) để ra verdict.

Chạy:  python dashboard/server.py   rồi mở http://127.0.0.1:8080
Thư mục `dashboard/` không nằm trong file zip nộp bài.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from collections import Counter, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from l1sim.config import ConfigError, load_config  # noqa: E402
from l1sim.launch_local import available_base_port  # noqa: E402
from l1sim.verification.check_safety import check_safety  # noqa: E402

CONFIG_DIR = ROOT / "config"
GENESIS = CONFIG_DIR / "genesis.json"
INDEX_HTML = Path(__file__).resolve().parent / "index.html"

# Event rất nhiều (10 node full mesh: mỗi message tới 9 lần), chỉ đếm, không đưa vào feed.
NOISY = frozenset({"gossip_duplicate", "gossip_recv", "gossip_send", "gossip_broadcast"})
FEED_SIZE = 600
# Tham số mạng được phép sửa từ UI (ghi vào bản sao tạm của scenario).
NETWORK_OVERRIDES = {"drop_rate": float, "duplicate_rate": float, "gst_height": int}


def list_scenarios() -> list[dict[str, Any]]:
    out = []
    for path in sorted(CONFIG_DIR.glob("t*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        net = raw.get("network", {})
        out.append(
            {
                "file": path.name,
                "name": raw.get("name", path.stem),
                "n": len(raw.get("validators", [])),
                "max_height": raw.get("max_height"),
                "txs": len(raw.get("transactions", [])),
                "network": {k: net.get(k, 0) for k in NETWORK_OVERRIDES},
                "byzantine": [
                    {"node": b.get("node"), "behavior": b.get("behavior") or b.get("type")}
                    for b in raw.get("byzantine", [])
                ],
            }
        )
    return out


def _pair(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a < b else (b, a)


class EventTail:
    """Đọc phần mới của một file JSONL (node ghi nối dần từng dòng)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.offset = 0
        self.partial = b""

    def read_new(self) -> list[dict[str, Any]]:
        try:
            with self.path.open("rb") as fh:
                fh.seek(self.offset)
                chunk = fh.read()
        except FileNotFoundError:
            return []
        if not chunk:
            return []
        self.offset += len(chunk)
        data = self.partial + chunk
        lines = data.split(b"\n")
        self.partial = lines.pop()  # dòng cuối có thể chưa ghi xong
        records = []
        for line in lines:
            if line.strip():
                try:
                    records.append(json.loads(line))
                except ValueError:
                    pass
        return records


class NodeView:
    def __init__(self, node_id: str, byzantine: str | None) -> None:
        self.id = node_id
        self.byzantine = byzantine
        self.status = "starting"  # starting | running | done | failed | killed
        self.exit_code: int | None = None
        self.height = 0  # height cao nhất đã finalize
        self.round = 0
        self.step = ""
        self.peers: set[str] = set()
        self.last_event = ""
        self.proposer = ""  # proposer của round hiện tại (event new_round)
        self.counts: Counter[str] = Counter()

    def apply(self, ev: dict[str, Any]) -> None:
        name = ev.get("event", "")
        self.counts[name] += 1
        if name in NOISY:
            return
        self.last_event = name
        if self.status == "starting":
            self.status = "running"
        if name == "finalize":
            self.height = max(self.height, int(ev.get("height", 0)))
            self.step = "finalize"
        elif name == "new_round":
            self.round = int(ev.get("round", 0))
            self.proposer = str(ev.get("proposer") or "")
            self.step = "propose"
        elif name in ("propose", "prevote", "precommit"):
            self.step = name
        elif name == "peer_added":
            self.peers.add(str(ev.get("peer")))
        elif name == "peer_removed":
            self.peers.discard(str(ev.get("peer")))

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "byzantine": self.byzantine,
            "status": self.status,
            "exit_code": self.exit_code,
            "height": self.height,
            "round": self.round,
            "step": self.step,
            "peers": sorted(self.peers),
            "proposer": self.proposer,
            "last_event": self.last_event,
        }


class Run:
    """Một lần chạy scenario: N process run_node + theo dõi log."""

    def __init__(self, scenario_file: str, overrides: dict[str, Any], timeout: float) -> None:
        source = CONFIG_DIR / scenario_file
        if source.parent != CONFIG_DIR or not source.is_file():
            raise ValueError(f"không có scenario {scenario_file}")
        raw = json.loads(source.read_text(encoding="utf-8"))
        applied: dict[str, Any] = {}
        for key, cast in NETWORK_OVERRIDES.items():
            if overrides.get(key) not in (None, ""):
                applied[key] = raw.setdefault("network", {})[key] = cast(overrides[key])
        if overrides.get("max_height") not in (None, ""):
            applied["max_height"] = raw["max_height"] = int(overrides["max_height"])

        self.tmpdir: Path | None = None
        scenario_path = source
        if applied:
            self.tmpdir = Path(tempfile.mkdtemp(prefix="l1dash_"))
            scenario_path = self.tmpdir / source.name
            scenario_path.write_text(json.dumps(raw), encoding="utf-8")
        try:
            genesis, scenario = load_config(scenario_path, GENESIS)
        except ConfigError:
            self.cleanup()
            raise

        self.file = scenario_file
        self.scenario_path = scenario_path
        self.scenario = scenario
        self.name = scenario.name
        self.validators = list(genesis.validators)
        self.timeout = timeout
        self.applied = applied
        self.byz = {r.node: r.behavior for r in scenario.byzantine}
        self.nodes = {v: NodeView(v, self.byz.get(v)) for v in self.validators}
        self.chain: dict[int, dict[str, dict[str, Any]]] = {}
        self.counters: Counter[str] = Counter()
        # Lưu lượng mỗi cạnh (cặp node, không chiều): số message nhận được và số bị drop.
        self.link_msgs: Counter[tuple[str, str]] = Counter()
        self.link_drops: Counter[tuple[str, str]] = Counter()
        self.feed: deque[dict[str, Any]] = deque(maxlen=FEED_SIZE)
        self.seq = 0
        self.verdict: dict[str, Any] | None = None
        self.error: str | None = None
        self.status = "running"
        self.started = time.monotonic()
        self.ended: float | None = None
        self.killed: set[str] = set()
        self.lock = threading.Lock()

        self.events_dir = ROOT / "logs" / "events" / self.name
        self.results_dir = ROOT / "logs" / "results" / self.name
        self.stderr_dir = ROOT / "logs" / "nodes" / self.name
        for d in (self.results_dir, self.events_dir):
            if d.is_dir():
                for f in d.glob("*.json*"):
                    f.unlink(missing_ok=True)
        self.stderr_dir.mkdir(parents=True, exist_ok=True)
        self.tails = {v: EventTail(self.events_dir / f"{v}.jsonl") for v in self.validators}

        n = len(self.validators)
        self.base_port = None if scenario.network.peers else available_base_port(n)
        self.procs: dict[str, subprocess.Popen] = {}
        self._files: list[Any] = []
        for v in self.validators:
            self.procs[v] = self._spawn(v)
        self._thread = threading.Thread(target=self._monitor, daemon=True)
        self._thread.start()

    def _spawn(self, node_id: str) -> subprocess.Popen:
        cmd = [
            sys.executable,
            "-X",
            "utf8",
            "-m",
            "l1sim.run_node",
            "--id",
            node_id,
            "--scenario",
            str(self.scenario_path),
            "--genesis",
            str(GENESIS),
            "--timeout",
            str(self.timeout),
        ]
        if self.base_port is not None:
            cmd += ["--base-port", str(self.base_port)]
        out = open(self.stderr_dir / f"{node_id}.stdout.log", "w", encoding="utf-8")
        err = open(self.stderr_dir / f"{node_id}.stderr.log", "w", encoding="utf-8")
        self._files += [out, err]
        env = dict(os.environ, PYTHONPATH=str(SRC) + os.pathsep + os.environ.get("PYTHONPATH", ""))
        return subprocess.Popen(cmd, cwd=ROOT, stdout=out, stderr=err, env=env)

    # ---- theo dõi -------------------------------------------------------------------------

    def _ingest(self) -> None:
        for node_id, tail in self.tails.items():
            for ev in tail.read_new():
                name = ev.get("event", "")
                with self.lock:
                    self.nodes[node_id].apply(ev)
                    self.counters[name] += 1
                    if name in ("gossip_recv", "gossip_duplicate") and ev.get("peer"):
                        self.link_msgs[_pair(node_id, str(ev["peer"]))] += 1
                    elif name == "fault_drop" and ev.get("dst"):
                        self.link_drops[_pair(node_id, str(ev["dst"]))] += 1
                    if name == "finalize":
                        self.chain.setdefault(int(ev["height"]), {})[node_id] = {
                            "hash": ev.get("block_hash", ""),
                            "proposer": ev.get("proposer"),
                            "round": ev.get("commit_round", ev.get("round")),
                            "txs": ev.get("txs"),
                            "state_hash": ev.get("state_hash", ""),
                        }
                    if name not in NOISY:
                        self.seq += 1
                        self.feed.append({"seq": self.seq, **ev, "node": node_id})

    def _poll_procs(self) -> bool:
        alive = False
        with self.lock:
            for node_id, proc in self.procs.items():
                code = proc.poll()
                view = self.nodes[node_id]
                if code is None:
                    alive = True
                elif view.exit_code is None:
                    view.exit_code = code
                    if node_id in self.killed:
                        view.status = "killed"
                    else:
                        view.status = "done" if code == 0 else "failed"
        return alive

    def _monitor(self) -> None:
        try:
            while True:
                self._ingest()
                if not self._poll_procs():
                    break
                time.sleep(0.1)
            time.sleep(0.1)
            self._ingest()
            if self.status != "stopped":
                # Node bị Kill coi như node lỗi (crash), không tính vào kiểm tra nhất quán.
                faulty = set(self.byz) | self.killed
                verdict = check_safety(
                    self.name,
                    results_root=str(ROOT / "logs" / "results"),
                    max_height=self.scenario.max_height,
                    num_validators=self.scenario.num_validators,
                    byzantine_nodes=sorted(faulty),
                    write_verdict=True,
                )
                with self.lock:
                    self.verdict = verdict
                    self.status = "passed" if verdict.get("pass") else "failed"
        except Exception as exc:  # hiện lỗi lên UI thay vì chết thầm trong thread
            with self.lock:
                self.error = f"{type(exc).__name__}: {exc}"
                self.status = "error"
        finally:
            self.ended = time.monotonic()
            for f in self._files:
                f.close()
            self.cleanup()

    # ---- điều khiển -----------------------------------------------------------------------

    def kill(self, node_id: str) -> None:
        proc = self.procs.get(node_id)
        if proc is None:
            raise ValueError(f"không có node {node_id}")
        if proc.poll() is None:
            with self.lock:
                self.killed.add(node_id)
            proc.send_signal(signal.SIGINT)  # như Ctrl+C: node vẫn ghi results phần đã có

    def stop(self) -> None:
        with self.lock:
            self.status = "stopped"
        for proc in self.procs.values():
            if proc.poll() is None:
                proc.terminate()

    def cleanup(self) -> None:
        if self.tmpdir is not None:
            for f in self.tmpdir.glob("*"):
                f.unlink(missing_ok=True)
            self.tmpdir.rmdir()
            self.tmpdir = None

    def logs_zip(self) -> bytes:
        """Gói log của lần chạy: event từng node, stdout/stderr, results (nếu đã có)."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for d in (self.events_dir, self.stderr_dir, self.results_dir):
                if not d.is_dir():
                    continue
                for f in sorted(d.iterdir()):
                    if f.is_file():
                        zf.write(f, f"{self.name}/{d.parent.name}/{f.name}")
        return buf.getvalue()

    def wait(self, timeout: float | None = None) -> None:
        self._thread.join(timeout)

    def snapshot(self, since: int) -> dict[str, Any]:
        with self.lock:
            errors = {}
            for v, view in self.nodes.items():
                if view.status == "failed":
                    path = self.stderr_dir / f"{v}.stderr.log"
                    text = path.read_text(encoding="utf-8") if path.is_file() else ""
                    errors[v] = text[-800:]
            end = self.ended if self.ended is not None else time.monotonic()
            return {
                "run": {
                    "file": self.file,
                    "name": self.name,
                    "status": self.status,
                    "elapsed": round(end - self.started, 1),
                    "base_port": self.base_port,
                    "max_height": self.scenario.max_height,
                    "n": len(self.validators),
                    "applied": self.applied,
                    "network": {
                        "drop_rate": self.scenario.network.drop_rate,
                        "duplicate_rate": self.scenario.network.duplicate_rate,
                        "gst_height": self.scenario.network.gst_height,
                    },
                    "killed": sorted(self.killed),
                    "error": self.error,
                },
                "nodes": [self.nodes[v].to_json() for v in self.validators],
                "chain": {str(h): row for h, row in sorted(self.chain.items())},
                "counters": dict(self.counters),
                "links": [
                    [a, b, self.link_msgs[(a, b)], self.link_drops[(a, b)]]
                    for a, b in sorted(set(self.link_msgs) | set(self.link_drops))
                ],
                "feed": [e for e in self.feed if e["seq"] > since],
                "seq": self.seq,
                "verdict": self.verdict,
                "errors": errors,
            }


class App:
    def __init__(self) -> None:
        self.run: Run | None = None
        self.lock = threading.Lock()

    def start(self, body: dict[str, Any]) -> Run:
        with self.lock:
            if self.run is not None and self.run.ended is None:
                raise RuntimeError("đang có một lần chạy, bấm Dừng trước")
            self.run = Run(
                body.get("file", "t1_normal.json"),
                body.get("overrides") or {},
                float(body.get("timeout") or 60),
            )
            return self.run


def make_handler(app: App) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:  # tắt log mỗi request
            pass

        def _json(self, code: int, payload: Any) -> None:
            data = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            url = urlparse(self.path)
            if url.path == "/":
                data = INDEX_HTML.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            elif url.path == "/api/scenarios":
                self._json(200, list_scenarios())
            elif url.path == "/api/state":
                since = int(parse_qs(url.query).get("since", ["0"])[0] or 0)
                run = app.run
                self._json(200, run.snapshot(since) if run else {"run": None})
            elif url.path == "/api/logs":
                run = app.run
                if run is None:
                    self._json(404, {"error": "chưa chạy scenario nào"})
                    return
                data = run.logs_zip()
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                name = f"{run.name}-logs.zip"
                self.send_header("Content-Disposition", f'attachment; filename="{name}"')
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self) -> None:
            url = urlparse(self.path)
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
                if url.path == "/api/run":
                    run = app.start(body)
                    self._json(200, {"ok": True, "name": run.name})
                elif url.path == "/api/kill":
                    if app.run is None:
                        raise ValueError("chưa chạy scenario nào")
                    app.run.kill(str(body.get("node")))
                    self._json(200, {"ok": True})
                elif url.path == "/api/stop":
                    if app.run is not None:
                        app.run.stop()
                    self._json(200, {"ok": True})
                else:
                    self._json(404, {"error": "not found"})
            except RuntimeError as exc:
                self._json(409, {"error": str(exc)})
            except (ValueError, ConfigError, OSError) as exc:
                self._json(400, {"error": str(exc)})

    return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dashboard web cho l1sim (chạy code thật)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args(argv)
    app = App()
    server = ThreadingHTTPServer((args.host, args.port), make_handler(app))
    print(f"dashboard: http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if app.run is not None:
            app.run.stop()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
