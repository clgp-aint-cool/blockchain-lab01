#!/usr/bin/env python3
"""Chạy một validator ở chế độ "mạng chạy liên tục" cho dashboard.

Vẫn là `l1sim.run_node` của nhóm (cùng consensus, mạng, ghi log), chỉ thêm hai chỉnh nhỏ
áp dụng trong process này, không sửa file nào trong `src/`:

1. Proposer giữ proposal round 0 lại `L1DASH_PROPOSE_DELAY_MS` ms (mặc định 600) trước khi
   gửi. Không có chỉnh này mạng finalize ~3 block/giây, block gần như luôn rỗng và người dùng
   không kịp gửi TX. Độ trễ phải nhỏ hơn timeout_propose (1000 ms) để round 0 vẫn commit.
2. Không ghi event `gossip_duplicate` (~80% số dòng log) để log không phình to khi chạy lâu.

Dashboard chạy:  python dashboard/live_node.py --id V0 --scenario ... (tham số như run_node)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import l1sim.logger as logger_mod  # noqa: E402
import l1sim.node as node_mod  # noqa: E402
from l1sim.models import Kind  # noqa: E402

PROPOSE_DELAY_S = float(os.environ.get("L1DASH_PROPOSE_DELAY_MS", "600")) / 1000
SKIP_EVENTS = frozenset({"gossip_duplicate"})

_orig_broadcast = node_mod.Node._broadcast


def _broadcast(self, message):  # type: ignore[no-untyped-def]
    if (
        PROPOSE_DELAY_S > 0
        and message.kind is Kind.BODY
        and message.payload.header.round == 0
        and self.loop is not None
    ):
        self.loop.call_later(PROPOSE_DELAY_S, _orig_broadcast, self, message)
        return
    _orig_broadcast(self, message)


_orig_event = logger_mod.EventLogger.event


def _event(self, event, height=0, **fields):  # type: ignore[no-untyped-def]
    if event in SKIP_EVENTS:
        return
    _orig_event(self, event, height, **fields)


# Node gắn `broadcast=self._broadcast` lúc khởi tạo, nên phải thay trước khi run_node tạo Node.
node_mod.Node._broadcast = _broadcast
logger_mod.EventLogger.event = _event

from l1sim.run_node import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
