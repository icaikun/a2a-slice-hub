import pytest

from a2a_slice_hub.errors import Conflict, Forbidden
from conftest import new_item, submit


def pending(store, repo, item):
    submit(store, item, repo)
    candidate = repo.git.candidate(repo.base, [repo.base])
    intent = store.prepare_merge("epic/demo", repo.git.ref("epic/demo"), candidate,
                                 {item["id"]: store.get_item(item["id"])["version"]})
    assert store.finish_merge(intent, repo.git.publish) == "COMMITTED"
    return store.get_item(item["id"])


def report(sha):
    return {"schema_version": "bug-report/v1", "target": {"integration_sha": sha, "contracts": {}, "seed": 7},
            "reproduction": {"command": ["pytest", "tests/test_acceptance.py"], "prerequisites": "clean fixture", "expected": "valid result", "actual": "invalid result"},
            "evidence": {"trace_id": "test-trace", "log_excerpt": "sanitized failure", "violated_contract": "orders"},
            "suspected_slices": ["orders"]}


def test_qa_snapshot_version_and_owner_fences(store, repo):
    item = pending(store, repo, new_item(store))
    with pytest.raises(Forbidden):
        store.qa(item["id"], item["version"], item["integration_sha"], True, item["owner"])
    with pytest.raises(Conflict):
        store.qa(item["id"], item["version"], repo.base, True, "qa")
    with pytest.raises(Conflict):
        store.qa(item["id"], item["version"] - 1, item["integration_sha"], True, "qa")


def test_qa_rework_requires_new_attempt_and_report(store, repo):
    item = pending(store, repo, new_item(store))
    result = store.qa(item["id"], item["version"], item["integration_sha"], False, "qa", report(item["integration_sha"]))
    assert result["state"] == "REWORK"
    assert result["rework_round"] == 1
    task = store.claim(item["id"], item["owner"], result["version"])
    assert task["epoch"] == 2


def test_qa_bug_closure_is_independent(store, repo):
    item = store.create_item("BUG", "orders", "worker-a", ["src/orders"])
    item = pending(store, repo, item)
    assert store.qa(item["id"], item["version"], item["integration_sha"], True, "qa")["state"] == "CLOSED"
