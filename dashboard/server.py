#!/usr/bin/env python3
"""Web dashboard chạy scenario bằng code thật của nhóm và hiển thị trực tiếp.

Không có logic blockchain riêng. Mỗi validator là một process `python -m l1sim.run_node`
(giống `launch_local`), server chỉ:
- bật / tắt process (nút Kill = Ctrl+C terminal của node đó);
- đọc dần `logs/events/<scenario>/V*.jsonl` mà node ghi ra để vẽ trạng thái;
- khi mọi process dừng, gọi `check_safety` (của B) để ra verdict.

Chế độ "chạy liên tục" (live): node chạy qua `dashboard/live_node.py` (vẫn là run_node, chỉ
giãn nhịp propose và bớt log trùng), max_height rất lớn; người dùng tự gửi TX qua
`POST /api/tx` (đi đường client thật: `l1sim.client.send_transactions` tới cổng của node).

Chạy:  python dashboard/server.py   rồi mở http://127.0.0.1:8080
Thư mục `dashboard/` không nằm trong file zip nộp bài.
"""

from __future__ import annotations

import argparse
import asyncio
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

from l1sim.client import build_scenario_transactions, make_signed_tx, send_transactions  # noqa: E402,I001
from l1sim.config import ConfigError, load_config  # noqa: E402
from l1sim.crypto import keypair_from_seed, sign  # noqa: E402
from l1sim.encoding import hash_with_domain  # noqa: E402
from l1sim.models import Domain, Tx  # noqa: E402
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
LIVE_NODE = Path(__file__).resolve().parent / "live_node.py"
LIVE_MAX_HEIGHT = 1_000_000  # "vô hạn": mạng chạy tới khi bấm Dừng
LIVE_TIMEOUT = 7 * 24 * 3600
LIVE_CHAIN_TAIL = 40
# Sau Ctrl+C node ghi results ngay, nhưng đôi khi kẹt lúc đóng kết nối TCP; quá hạn thì terminate.
SIGINT_GRACE_S = 5.0  # chế độ live chỉ gửi / xem lại các height gần nhất
TAMPERS = ("none", "bad_signature", "wrong_domain", "replay")
# Sender không có trong scenario: node không biết public key nên từ chối (bad_signature).
OUTSIDER = "Mallory"


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


def tx_id_of(tx: Tx) -> str:
    return hash_with_domain("TX", tx)


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


# ---- xem lại theo giai đoạn ---------------------------------------------------------------
# `time` trong event là bộ đếm riêng của từng node nên không xếp chung các node theo thời gian
# được. Thay vào đó gom event theo giai đoạn consensus (height, round, bước): mỗi node đi qua
# các giai đoạn theo thứ tự, nên trạng thái mạng ở giai đoạn S = trạng thái mới nhất của mỗi
# node mà chưa vượt quá S.
STEPS = ("start", "propose", "prevote", "precommit", "finalize")
# Event kết nối / lặp lại không giúp hiểu giai đoạn, bỏ khỏi danh sách event của giai đoạn.
STAGE_HIDDEN = NOISY | {
    "ignored_old_height",
    "conn_open",
    "conn_closed",
    "listen",
    "connect_failed",
}


def _read_events(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("event") not in NOISY:
            out.append(ev)
    return out


def _node_track(events: list[dict[str, Any]]) -> list[tuple[tuple[int, int, int], dict, dict]]:
    """Đi qua event của một node, trả [(giai đoạn, trạng thái sau event, event)]."""
    st: dict[str, Any] = {
        "height": 0, "round": 0, "step": "", "proposer": "", "peers": set(),
        "prevote": None, "precommit": None, "locked": None,
    }  # fmt: skip
    stage = (0, 0, 0)
    track = []
    for ev in events:
        name = ev.get("event", "")
        h = int(ev.get("height") or stage[0])
        r = int(ev.get("round", stage[1]) or 0)
        if name == "new_round":
            stage = (h, r, 1)
            st.update(round=r, step="propose", proposer=str(ev.get("proposer") or ""))
        elif name == "propose":
            stage = (h, r, 1)
            st["step"] = "propose"
        elif name in ("prevote", "precommit"):
            stage = (h, r, STEPS.index(name))
            st["step"] = name
            st[name] = {"h": h, "r": r, "hash": ev.get("block_hash")}
        elif name == "lock":
            st["locked"] = {"h": h, "r": r, "hash": ev.get("block_hash")}
        elif name == "finalize":
            stage = (h, stage[1], 4)
            st["height"] = max(st["height"], h)
            st["step"] = "finalize"
        elif name == "peer_added":
            st["peers"].add(str(ev.get("peer")))
        elif name == "peer_removed":
            st["peers"].discard(str(ev.get("peer")))
        # Giữ thứ tự không giảm (phòng event lạ của node Byzantine) để cắt theo giai đoạn được.
        if track and stage < track[-1][0]:
            stage = track[-1][0]
        snap = {**st, "peers": sorted(st["peers"])}
        track.append((stage, snap, ev))
    return track


def build_timeline(
    events_dir: Path,
    validators: list[str],
    byz: dict[str, str],
    killed: set[str],
    chain_tail: int | None = None,
) -> list[dict[str, Any]]:
    """Mỗi giai đoạn một frame. `chain_tail`: chỉ giữ frame và chain của các height gần nhất
    (chế độ live chạy hàng nghìn height, giữ hết thì timeline quá lớn)."""
    tracks = {v: _node_track(_read_events(events_dir / f"{v}.jsonl")) for v in validators}
    stages = sorted({stage for t in tracks.values() for stage, _, _ in t})
    min_height = 0
    if chain_tail and stages:
        min_height = max(0, stages[-1][0] - chain_tail + 1)
    empty = {"height": 0, "round": 0, "step": "", "proposer": "", "peers": [],
             "prevote": None, "precommit": None, "locked": None}  # fmt: skip
    frames = []
    pos = dict.fromkeys(validators, 0)  # số entry của mỗi node đã nằm trong giai đoạn <= S
    chain: dict[str, dict[str, Any]] = {}  # chain cộng dồn tới giai đoạn hiện tại
    for stage in stages:
        h, r, step = stage
        nodes, events = [], []
        for v in validators:
            t = tracks[v]
            # Giai đoạn tăng dần nên các entry mới tới đây đều thuộc đúng giai đoạn này.
            while pos[v] < len(t) and t[pos[v]][0] <= stage:
                _, _, ev = t[pos[v]]
                pos[v] += 1
                if ev.get("event") == "finalize":
                    chain.setdefault(str(ev["height"]), {})[v] = {
                        "hash": ev.get("block_hash", ""),
                        "proposer": ev.get("proposer"),
                        "round": ev.get("commit_round", ev.get("round")),
                        "txs": ev.get("txs"),
                        "state_hash": ev.get("state_hash", ""),
                    }
                if ev.get("event") not in STAGE_HIDDEN:
                    events.append({**ev, "node": v})
            state = t[pos[v] - 1][1] if pos[v] else empty
            last_stage = t[-1][0] if t else (0, 0, 0)
            if stage > last_stage:
                status = "killed" if v in killed else "done"
            else:
                status = "running"
            reached = pos[v] > 0 and t[pos[v] - 1][0] == stage
            nodes.append(
                {"id": v, "byzantine": byz.get(v), "status": status, "reached": reached, **state}
            )
        if h < min_height:
            continue
        proposers = Counter(n["proposer"] for n in nodes if n["round"] == r and n["proposer"])
        frames.append({
            "height": h, "round": r, "step": STEPS[step],
            "proposer": proposers.most_common(1)[0][0] if proposers else "",
            "nodes": nodes,
            "chain": {k: dict(row) for k, row in chain.items() if int(k) >= min_height},
            "events": events,
        })  # fmt: skip
    return frames


class Run:
    """Một lần chạy scenario: N process run_node + theo dõi log."""

    def __init__(
        self, scenario_file: str, overrides: dict[str, Any], timeout: float, live: bool = False
    ) -> None:
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
        if live:
            # Giữ transactions của scenario: node chỉ biết public key của các sender có trong đó.
            raw["max_height"] = LIVE_MAX_HEIGHT
            applied["live"] = True
            timeout = LIVE_TIMEOUT

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
        self.live = live
        self.genesis = genesis
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
        self.sigint_at: dict[str, float] = {}  # node -> lúc gửi SIGINT
        self._timeline: list[dict[str, Any]] | None = None
        self.lock = threading.Lock()

        # ---- giao dịch người dùng gửi + state đã commit (đọc từ results của một node trung thực)
        self.senders = sorted({tx["sender"] for tx in scenario.transactions})
        self.txs: list[dict[str, Any]] = []  # TX gửi từ UI, theo thứ tự gửi
        self.tx_by_id: dict[str, dict[str, Any]] = {}
        preset = build_scenario_transactions(scenario)
        self.tx_objs: dict[str, Tx] = {tx_id_of(t): t for t in preset}
        # Nonce của TX scenario (mọi node nạp sẵn vào mempool lúc khởi động).
        self.preset_nonces: dict[str, set[int]] = {}
        for t in preset:
            self.preset_nonces.setdefault(t.sender, set()).add(t.nonce)
        self.state: dict[str, dict[str, Any]] = {}  # key -> {value, height, sender, nonce}
        self.committed_nonce: dict[str, int] = {}  # sender -> nonce kế tiếp đã commit
        self.last_committed: dict[str, str] = {}  # sender -> tx_id commit gần nhất (cho replay)
        self.state_height = 0  # đã áp các block tới height này
        self.committed_txs = 0
        self._last_results_read = 0.0
        self.tx_lock = threading.Lock()  # gửi TX tuần tự (nonce tự động không bị trùng)

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
        entry = [str(LIVE_NODE)] if self.live else ["-m", "l1sim.run_node"]
        cmd = [
            sys.executable,
            "-X",
            "utf8",
            *entry,
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
                    sent = self.sigint_at.get(node_id)
                    if sent is not None and time.monotonic() - sent > SIGINT_GRACE_S:
                        proc.terminate()
                        self.sigint_at.pop(node_id)
                elif view.exit_code is None:
                    view.exit_code = code
                    if node_id in self.killed:
                        view.status = "killed"
                    elif self.live and self.status == "stopping":
                        view.status = "done"  # Ctrl+C khi bấm Dừng mạng: thoát 130 là bình thường
                    else:
                        view.status = "done" if code == 0 else "failed"
        return alive

    def _monitor(self) -> None:
        try:
            while True:
                self._ingest()
                self._read_commits()
                if not self._poll_procs():
                    break
                time.sleep(0.1)
            time.sleep(0.1)
            self._ingest()
            self._read_commits(force=True)
            if self.status != "stopped":
                # Node bị Kill coi như node lỗi (crash), không tính vào kiểm tra nhất quán.
                faulty = set(self.byz) | self.killed
                max_height = self.scenario.max_height
                if self.live:
                    # Không có height đích: kiểm tra tới height mà mọi node trung thực còn sống
                    # đều đã finalize lúc dừng.
                    honest = [n.height for v, n in self.nodes.items() if v not in faulty]
                    max_height = max(1, min(honest, default=1))
                verdict = check_safety(
                    self.name,
                    results_root=str(ROOT / "logs" / "results"),
                    max_height=max_height,
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
                self.sigint_at[node_id] = time.monotonic()
            proc.send_signal(signal.SIGINT)  # như Ctrl+C: node vẫn ghi results phần đã có

    def stop(self) -> None:
        if self.live and self.status == "running":
            # Dừng mạng liên tục như Ctrl+C mọi terminal: node ghi results rồi thoát, sau đó
            # _monitor vẫn chạy check_safety cho phần chain đã có.
            with self.lock:
                self.status = "stopping"
                for node_id, proc in self.procs.items():
                    if proc.poll() is None:
                        self.sigint_at[node_id] = time.monotonic()
                        proc.send_signal(signal.SIGINT)
            return
        with self.lock:
            self.status = "stopped"
        for proc in self.procs.values():
            if proc.poll() is None:
                proc.terminate()

    # ---- giao dịch ------------------------------------------------------------------------

    def _ref_node(self) -> str | None:
        """Node trung thực, còn chạy, finalize xa nhất: lấy chain của node này làm chuẩn."""
        best = None
        for v, view in self.nodes.items():
            if v in self.byz or v in self.killed:
                continue
            if best is None or view.height > self.nodes[best].height:
                best = v
        return best

    def _read_commits(self, force: bool = False) -> None:
        """Đọc results của node chuẩn, áp TX trong các block mới vào bảng state."""
        now = time.monotonic()
        if not force and now - self._last_results_read < 0.5:
            return
        self._last_results_read = now
        ref = self._ref_node()
        if ref is None:
            return
        try:
            data = json.loads((self.results_dir / f"{ref}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return  # chưa có file, hoặc node đang ghi dở
        with self.lock:
            for block in sorted(data.get("blocks", []), key=lambda b: b["height"]):
                height = int(block["height"])
                if height <= self.state_height:
                    continue
                for tx_id in block.get("tx_ids", []):
                    self.committed_txs += 1
                    record = self.tx_by_id.get(tx_id)
                    if record is not None and record["status"] != "committed":
                        record["status"] = "committed"
                        record["height"] = height
                    tx = self.tx_objs.get(tx_id)
                    if tx is not None:
                        self.state[tx.key] = {
                            "value": tx.value, "height": height,
                            "sender": tx.sender, "nonce": tx.nonce,
                        }  # fmt: skip
                        self.committed_nonce[tx.sender] = tx.nonce + 1
                        self.last_committed[tx.sender] = tx_id
                self.state_height = height

    def next_nonce(self, sender: str) -> int:
        """Nonce kế tiếp liên tục: sau nonce đã commit và các TX đang chờ nối tiếp nó.

        TX có nonce "nhảy cóc" vẫn được nhận vào mempool nhưng chỉ vào block khi các nonce
        trước đó đã commit, nên không tính nó khi gợi ý nonce."""
        nonce = self.committed_nonce.get(sender, 0)
        pending = {r["nonce"] for r in self.txs if r["sender"] == sender and r["accepted"]}
        pending |= self.preset_nonces.get(sender, set())
        while nonce in pending:
            nonce += 1
        return nonce

    def send_tx(self, body: dict[str, Any]) -> dict[str, Any]:
        """Ký và gửi một TX tới cổng client của node, giống `python -m l1sim.client`."""
        if self.ended is not None or self.status != "running":
            raise RuntimeError("mạng không còn chạy, bấm Chạy trước")
        node = str(body.get("node") or self.validators[0])
        if node not in self.procs:
            raise ValueError(f"không có node {node}")
        if self.procs[node].poll() is not None:
            raise RuntimeError(f"{node} đã dừng, chọn node khác")
        tamper = str(body.get("tamper") or "none")
        if tamper not in TAMPERS:
            raise ValueError(f"tamper phải là một trong {', '.join(TAMPERS)}")
        sender = str(body.get("sender") or "").strip()
        if not sender:
            raise ValueError("thiếu sender")
        with self.tx_lock:
            if tamper == "replay":
                # Gửi lại nguyên văn TX đã commit gần nhất của sender (cùng chữ ký, cùng nonce).
                with self.lock:
                    old_id = self.last_committed.get(sender)
                if old_id is None:
                    raise ValueError(f"{sender} chưa có TX nào được commit để gửi lại")
                tx = self.tx_objs[old_id]
            else:
                key = str(body.get("key") or f"{sender}/message").strip()
                value = str(body.get("value") or "")
                if not key.startswith(f"{sender}/"):
                    # Tx của l1sim không cho tạo key của người khác (kiểm tra ownership ngay
                    # ở client), nên chặn ở đây với thông báo rõ.
                    raise ValueError(f"key phải bắt đầu bằng '{sender}/' (chỉ ghi key của mình)")
                raw_nonce = body.get("nonce")
                with self.lock:
                    nonce = self.next_nonce(sender) if raw_nonce in (None, "") else int(raw_nonce)
                if nonce < 0:
                    raise ValueError("nonce không được âm")
                if tamper == "wrong_domain":
                    unsigned = Tx(sender=sender, key=key, value=value, nonce=nonce)
                    signature = sign(
                        keypair_from_seed(self.scenario.seed, sender).private_key,
                        Domain.HEADER,
                        unsigned,
                    )
                    tx = Tx(sender=sender, key=key, value=value, nonce=nonce, signature=signature)
                else:
                    tx = make_signed_tx(self.scenario.seed, sender, key, value, nonce)
                    if tamper == "bad_signature":
                        sig = tx.signature
                        flipped = sig[:-1] + ("0" if sig[-1] != "0" else "1")
                        tx = Tx(sender=sender, key=key, value=value, nonce=nonce, signature=flipped)
            (result,) = asyncio.run(
                send_transactions(self.node_address(node), [tx], self.genesis, timeout_s=5)
            )
        with self.lock:
            record = {
                "id": result.tx_id,
                "sender": tx.sender,
                "key": tx.key,
                "value": tx.value,
                "nonce": tx.nonce,
                "node": node,
                "tamper": tamper,
                "accepted": result.accepted,
                "reason": result.reason,
                "status": "pending" if result.accepted else "rejected",
                "height": None,
                "sent_height": self.nodes[node].height + 1,
                "at": round(time.monotonic() - self.started, 1),
            }
            if result.accepted and result.tx_id in self.tx_by_id:
                record["status"] = self.tx_by_id[result.tx_id]["status"]
                record["height"] = self.tx_by_id[result.tx_id]["height"]
            self.txs.append(record)
            if result.accepted:
                self.tx_by_id[result.tx_id] = record
            self.tx_objs.setdefault(result.tx_id, tx)
            return dict(record)

    def node_address(self, node: str) -> str:
        peers = self.scenario.network.peers
        if peers:
            host, _, port = peers[node].rpartition(":")
            return f"{'127.0.0.1' if host in ('0.0.0.0', '') else host}:{port}"
        return f"127.0.0.1:{self.base_port + self.validators.index(node)}"

    def cleanup(self) -> None:
        if self.tmpdir is not None:
            for f in self.tmpdir.glob("*"):
                f.unlink(missing_ok=True)
            self.tmpdir.rmdir()
            self.tmpdir = None

    def timeline(self) -> list[dict[str, Any]]:
        if self.ended is None:
            raise RuntimeError("đang chạy, chờ chạy xong mới xem lại được")
        if self._timeline is None:
            self._timeline = build_timeline(
                self.events_dir,
                self.validators,
                self.byz,
                self.killed,
                chain_tail=LIVE_CHAIN_TAIL if self.live else None,
            )
        return self._timeline

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
            chain = sorted(self.chain.items())
            if self.live:
                chain = chain[-LIVE_CHAIN_TAIL:]
            return {
                "run": {
                    "file": self.file,
                    "name": self.name,
                    "status": self.status,
                    "elapsed": round(end - self.started, 1),
                    "base_port": self.base_port,
                    "max_height": self.scenario.max_height,
                    "live": self.live,
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
                "chain": {str(h): row for h, row in chain},
                "counters": dict(self.counters),
                "links": [
                    [a, b, self.link_msgs[(a, b)], self.link_drops[(a, b)]]
                    for a, b in sorted(set(self.link_msgs) | set(self.link_drops))
                ],
                "feed": [e for e in self.feed if e["seq"] > since],
                "seq": self.seq,
                "verdict": self.verdict,
                "errors": errors,
                "tx": {
                    "senders": [
                        {"name": s, "next_nonce": self.next_nonce(s)} for s in self.senders
                    ],
                    "outsider": OUTSIDER,
                    "sent": self.txs[-200:][::-1],
                    "total_sent": len(self.txs),
                    "accepted": sum(1 for r in self.txs if r["accepted"]),
                    "committed": sum(1 for r in self.txs if r["status"] == "committed"),
                    "committed_all": self.committed_txs,
                    "committed_nonce": dict(self.committed_nonce),
                    "state_height": self.state_height,
                    "state": [{"key": k, **v} for k, v in sorted(self.state.items())],
                },
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
                live=bool(body.get("live")),
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
            elif url.path == "/api/timeline":
                if app.run is None:
                    self._json(404, {"error": "chưa chạy scenario nào"})
                    return
                try:
                    self._json(200, {"frames": app.run.timeline()})
                except RuntimeError as exc:
                    self._json(409, {"error": str(exc)})
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
                elif url.path == "/api/tx":
                    if app.run is None:
                        raise RuntimeError("chưa chạy scenario nào")
                    self._json(200, app.run.send_tx(body))
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
    # Khi server được mở nền (`... &`), shell cho SIGINT bị bỏ qua và process con kế thừa điều
    # đó: Kill / Dừng mạng (gửi SIGINT như Ctrl+C) sẽ không có tác dụng. Đặt lại handler thì
    # node con nhận SIGINT bình thường. SIGTERM cũng dừng các node trước khi server thoát.
    signal.signal(signal.SIGINT, signal.default_int_handler)

    def _term(*_: Any) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _term)
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
