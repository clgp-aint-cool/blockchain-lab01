"""Ghi event của tầng network ra logger của node.

Cùng quy ước với engine (sau PR #15): logger là một hàm `logger(event, **fields)` tạo
riêng cho 1 node và tự ghi `node`, nên network KHÔNG BAO GIỜ truyền `node`. Với logger
chính thức thì truyền `event_logger.event` (của `EventLogger`) cho cả engine lẫn network.
Không có logger thì ghi qua `logging.debug`.
"""

from __future__ import annotations

import logging

_log = logging.getLogger("l1sim.network")


def emit(logger, node_id: str, event: str, **fields) -> None:
    if logger is None:
        _log.debug("%s %s %s", node_id, event, fields)
    else:
        logger(event, **fields)
