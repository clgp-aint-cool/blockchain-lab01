"""Ghi event của tầng network ra logger của node.

Chấp nhận 2 kiểu logger đang có trong nhóm:
- object có `event(name, **fields)`, như `EventLogger` (l1sim/logger.py). Logger này tạo
  riêng cho 1 node và tự ghi `node`, nên KHÔNG truyền `node` (truyền vào nó sẽ báo lỗi,
  giống engine sau PR #15);
- hàm `logger(name, **fields)`, ví dụ Recorder của devnet: không biết node nào gọi, nên
  được truyền thêm `node`.
Không có logger thì ghi qua `logging.debug`.
"""

from __future__ import annotations

import logging

_log = logging.getLogger("l1sim.network")


def emit(logger, node_id: str, event: str, **fields) -> None:
    if logger is not None and hasattr(logger, "event"):
        logger.event(event, **fields)
    elif callable(logger):
        logger(event, node=node_id, **fields)
    else:
        _log.debug("%s %s %s", node_id, event, fields)
