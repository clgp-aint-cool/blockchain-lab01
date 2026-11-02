import json

from l1sim.logger import EventLogger
from l1sim.network.events import emit


def test_emit_supports_object_and_callable_logger():
    seen = []

    class Obj:
        def event(self, name, **fields):
            seen.append(("obj", name, fields))

    emit(Obj(), "v0", "peer_added", peer="v1")
    emit(lambda name, **f: seen.append(("fn", name, f)), "v0", "peer_removed", peer="v1")
    emit(None, "v0", "listen")  # không có logger: không lỗi
    assert seen == [
        ("obj", "peer_added", {"peer": "v1"}),
        ("fn", "peer_removed", {"node": "v0", "peer": "v1"}),
    ]


def test_emit_into_real_event_logger(tmp_path):
    """EventLogger tự ghi node và cấm field `node`: emit không được truyền node vào."""
    log = EventLogger("V1", "t_net", log_root=tmp_path)
    emit(log, "V1", "peer_added", peer="V2")
    rec = json.loads(log.path.read_text().splitlines()[0])
    assert rec["node"] == "V1" and rec["event"] == "peer_added" and rec["peer"] == "V2"
