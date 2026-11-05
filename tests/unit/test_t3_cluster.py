"""T3 failure handling must surface worker errors and reap every child process."""

import multiprocessing

import pytest

from tests.e2e import t3_cluster


def _failed_validator(node_id, _peers, _workdir, _inject, _start, _stop, statuses):
    statuses.put((node_id, "error", "forced worker failure"))


def test_worker_failure_stops_the_cluster_without_leaking_processes(tmp_path, monkeypatch):
    existing = {child.pid for child in multiprocessing.active_children()}
    monkeypatch.setattr(t3_cluster, "_validator", _failed_validator)
    with pytest.raises(AssertionError, match="forced worker failure"):
        t3_cluster.run_t3(tmp_path)
    assert {child.pid for child in multiprocessing.active_children()} == existing
