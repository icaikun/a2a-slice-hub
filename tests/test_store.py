from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from a2a_slice_hub.errors import Conflict, Forbidden
from a2a_slice_hub.store import Store
from conftest import new_item, submit


def test_wal_and_transaction_rollback(store):
    with store.connect() as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    with pytest.raises(RuntimeError):
        with store.transaction() as conn:
            store.emit(conn, "discarded", {})
            raise RuntimeError("fault")
    assert store.claim_outbox() is None


def test_concurrent_claim_only_one_wins(store):
    item = new_item(store)
    barrier = threading.Barrier(2)
    def claim():
        other = Store(store.database, clock=store.clock)
        barrier.wait()
        try:
            return other.claim(item["id"], item["owner"], 0)["id"]
        except Conflict:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: claim(), range(2)))
    assert sum(x is not None for x in results) == 1


def test_expired_worker_cannot_write_or_renew(store, clock):
    item = new_item(store)
    first = store.claim(item["id"], item["owner"], 0, ttl=2)
    clock.now += 3
    second = store.claim(item["id"], item["owner"], store.get_item(item["id"])["version"])
    assert second["epoch"] == first["epoch"] + 1
    with pytest.raises(Conflict):
        store.transition(first["id"], item["owner"], first["epoch"], 0, "working")
    with pytest.raises(Conflict):
        store.heartbeat(first["id"], item["owner"], first["epoch"])
    assert store.get_attempt(first["id"])["state"] == "failed"


def test_input_required_releases_slot_and_resume_respects_slot(store):
    a, b = new_item(store), new_item(store)
    task = store.claim(a["id"], a["owner"], 0)
    task = store.transition(task["id"], a["owner"], 1, 0, "working")
    task = store.transition(task["id"], a["owner"], 1, 1, "input-required")
    other = store.claim(b["id"], b["owner"], 0)
    with pytest.raises(Conflict):
        store.transition(task["id"], a["owner"], 1, 2, "working")
    store.transition(other["id"], b["owner"], 1, 0, "canceled")
    assert store.transition(task["id"], a["owner"], 1, 2, "working")["state"] == "working"


def test_terminal_attempt_never_revives(store, repo):
    item = new_item(store)
    first = submit(store, item, repo)
    with pytest.raises(Conflict):
        store.transition(first["id"], item["owner"], 1, first["version"], "working")
    store.reject(item["id"], store.get_item(item["id"])["version"], "tests failed")
    second = store.claim(item["id"], item["owner"], store.get_item(item["id"])["version"])
    assert second["id"] != first["id"]
    assert store.get_attempt(first["id"])["state"] == "completed"


def test_opposing_dag_edges_are_serialized(store):
    a, b = new_item(store, owner="a"), new_item(store, owner="b")
    barrier = threading.Barrier(2)
    def add(pair):
        barrier.wait()
        try:
            store.add_dependency(*pair)
            return True
        except Conflict:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(add, [(a["id"], b["id"]), (b["id"], a["id"])]))
    assert sorted(results) == [False, True]


def test_dependency_blocks_dispatch(store):
    a, b = new_item(store), new_item(store, owner="b")
    store.add_dependency(a["id"], b["id"])
    with pytest.raises(Conflict):
        store.claim(a["id"], a["owner"], 0)


def test_deadline_and_owner(store, clock):
    item = new_item(store, deadline=clock() + 1)
    with pytest.raises(Forbidden):
        store.claim(item["id"], "stranger", 0)
    clock.now += 2
    with pytest.raises(Conflict):
        store.claim(item["id"], item["owner"], 0)


def test_parent_dispute_fences_children(store):
    parent = store.create_item("BUG", "integration", "coordinator", ["src"], change_set_id="group")
    child = new_item(store, parent_id=parent["id"], change_set_id="group")
    task = store.claim(child["id"], child["owner"], 0)
    store.dispute(child["id"], child["owner"], "upstream trace")
    sibling = new_item(store, owner="worker-b", slice_id="payments", parent_id=parent["id"], change_set_id="group")
    store.dispute(sibling["id"], sibling["owner"], "contract ambiguity")
    assert store.get_item(parent["id"])["state"] == "BLOCKED"
    with pytest.raises(Conflict):
        store.transition(task["id"], child["owner"], 1, 0, "working")
    assert store.get_attempt(task["id"])["state"] == "failed"


def test_negotiation_limit(store):
    item = new_item(store)
    task = store.claim(item["id"], item["owner"], 0)
    for _ in range(3):
        task = store.transition(task["id"], item["owner"], 1, task["version"], "working")
        task = store.transition(task["id"], item["owner"], 1, task["version"], "input-required")
    assert task["state"] == "failed"
    assert store.get_item(item["id"])["state"] == "BLOCKED"


def test_outbox_expired_claim_cannot_ack_new_delivery(store, clock):
    with store.transaction() as conn:
        key = store.emit(conn, "test", {})
    old = store.claim_outbox(ttl=1)
    clock.now += 2
    new = store.claim_outbox()
    assert old["id"] == new["id"] == key
    with pytest.raises(Conflict):
        store.ack_outbox(key, old["claim"], True)
    store.ack_outbox(key, new["claim"], True)
    assert store.claim_outbox() is None


def test_expired_slot_does_not_deadlock_another_work_item(store, clock):
    a, b = new_item(store), new_item(store)
    old = store.claim(a["id"], a["owner"], 0, ttl=1)
    clock.now += 2
    new = store.claim(b["id"], b["owner"], 0)
    assert new["state"] == "submitted"
    assert store.get_attempt(old["id"])["state"] == "failed"
    assert store.get_item(a["id"])["state"] == "REWORK"


def test_dependencies_cannot_mutate_an_inflight_item(store):
    a, b = new_item(store), new_item(store, owner="other")
    store.claim(a["id"], a["owner"], 0)
    with pytest.raises(Conflict):
        store.add_dependency(a["id"], b["id"])


def _claim_process(database, item_id, owner, now, barrier, results):
    local = Store(database, clock=lambda: now)
    barrier.wait(timeout=30)
    try:
        local.claim(item_id, owner, 0)
        results.put("claimed")
    except Conflict:
        results.put("conflict")


def test_independent_processes_cannot_double_claim(store, clock, tmp_path):
    import subprocess
    import sys
    import time
    from pathlib import Path
    item = new_item(store)
    signal = tmp_path / "start"
    script = """
import sys,time
from pathlib import Path
from a2a_slice_hub.store import Store
from a2a_slice_hub.errors import Conflict
local=Store(sys.argv[1],clock=lambda:float(sys.argv[4]))
Path(sys.argv[6]+'.ready').write_bytes(b'1')
while not Path(sys.argv[5]).exists(): time.sleep(.01)
try:
    local.claim(sys.argv[2],sys.argv[3],0)
    result='claimed'
except Conflict:
    result='conflict'
Path(sys.argv[6]).write_text(result,encoding='utf-8',newline='\\n')
"""
    # Independent native Python processes, file barrier instead of Windows named-pipe Queue.
    outputs = [tmp_path / ("result" + str(i)) for i in range(2)]
    import os
    env = dict(os.environ, PYTHONPATH=str(Path("src").resolve()))
    processes = [subprocess.Popen([sys.executable, "-c", script, store.database, item["id"],
        item["owner"], str(clock()), str(signal), str(out)], env=env) for out in outputs]
    try:
        deadline = time.monotonic() + 30
        while not all(Path(str(out)+".ready").exists() for out in outputs):
            assert time.monotonic() < deadline
            time.sleep(.01)
        signal.write_bytes(b"go")
        for process in processes:
            assert process.wait(timeout=30) == 0
        assert sorted(out.read_text() for out in outputs) == ["claimed", "conflict"]
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait()
