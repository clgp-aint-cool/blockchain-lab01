"""Chạy T1 qua web dashboard: 10 process run_node thật, verdict của check_safety phải pass.

`dashboard/` không nằm trong file zip nộp bài, nên test tự skip nếu không có.
"""

from __future__ import annotations

import importlib.util
import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SERVER = ROOT / "dashboard" / "server.py"
if not SERVER.is_file():
    pytest.skip("không có dashboard/", allow_module_level=True)

spec = importlib.util.spec_from_file_location("l1sim_dashboard", SERVER)
dashboard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dashboard)


@pytest.fixture
def server():
    app = dashboard.App()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), dashboard.make_handler(app))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield app, f"http://127.0.0.1:{httpd.server_port}"
    if app.run is not None:
        app.run.stop()
        app.run.wait(10)
    httpd.shutdown()
    httpd.server_close()


def _call(url, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as res:
        return json.loads(res.read())


def test_scenarios_listed(server):
    _, base = server
    names = {s["file"] for s in _call(base + "/api/scenarios")}
    assert "t1_normal.json" in names


def test_rejects_path_outside_config(server):
    _, base = server
    with pytest.raises(urllib.error.HTTPError) as err:
        _call(base + "/api/run", {"file": "../pyproject.toml"})
    assert err.value.code == 400


def test_t1_through_dashboard_passes(server):
    app, base = server
    assert _call(base + "/api/run", {"file": "t1_normal.json", "timeout": 60})["ok"]
    with pytest.raises(urllib.error.HTTPError) as err:  # chỉ một lần chạy tại một thời điểm
        _call(base + "/api/run", {"file": "t1_normal.json"})
    assert err.value.code == 409

    app.run.wait(90)
    state = _call(base + "/api/state")
    assert state["run"]["status"] == "passed", state["run"]
    assert state["verdict"]["pass"] is True
    assert all(n["status"] == "done" for n in state["nodes"])
    max_height = state["run"]["max_height"]
    assert set(state["chain"]) == {str(h) for h in range(1, max_height + 1)}
    for row in state["chain"].values():
        assert len({cell["hash"] for cell in row.values()}) == 1  # mọi node cùng block
    assert state["counters"].get("finalize") == 10 * max_height
