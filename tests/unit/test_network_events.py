import json

from l1sim.logger import EventLogger
from l1sim.network.events import emit


def test_emit_calls_logger_without_node():
    seen = []
    emit(lambda name, **f: seen.append((name, f)), "v0", "peer_removed", peer="v1")
    emit(None, "v0", "listen")  # không có logger: không lỗi
    assert seen == [("peer_removed", {"peer": "v1"})]


def test_emit_into_real_event_logger(tmp_path):
    """node.py truyền `lg.event` cho cả engine lẫn network: EventLogger tự ghi node."""
    log = EventLogger("V1", "t_net", log_root=tmp_path)
    emit(log.event, "V1", "peer_added", peer="V2")
    rec = json.loads(log.path.read_text().splitlines()[0])
    assert rec["node"] == "V1" and rec["event"] == "peer_added" and rec["peer"] == "V2"
