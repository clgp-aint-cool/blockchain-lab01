"""CLI startup ordering for task A4."""

import asyncio
import json

from l1sim.node import NodeNetwork
from l1sim.run_node import run_node
from tests.unit.test_node import ROOT


def test_cli_waits_warmup_before_start(tmp_path, monkeypatch):
    raw = json.loads((ROOT / "config/t1_normal.json").read_text())
    raw["warmup_ms"] = 30
    path = tmp_path / "scenario.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    networks = []

    class ReadyNetwork(NodeNetwork):
        async def start(self):
            networks.append(self)
            self.ready_at = asyncio.get_running_loop().time()

        async def wait_ready(self):
            assert not self.node._started

        def activate(self):
            assert self.node._started
            assert asyncio.get_running_loop().time() - self.ready_at >= 0.03
            self.node.completed.set()

    monkeypatch.chdir(tmp_path)
    assert (
        asyncio.run(
            run_node(
                "V0",
                str(path),
                str(ROOT / "config/genesis.json"),
                network_factory=ReadyNetwork,
            )
        )
        == 0
    )
    assert networks[0].node._stopped
    assert (tmp_path / "logs/results/t1_normal/V0.json").is_file()
