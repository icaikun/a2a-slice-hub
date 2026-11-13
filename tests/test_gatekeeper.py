from concurrent.futures import ThreadPoolExecutor
import json
import threading

import pytest

from a2a_slice_hub.errors import Conflict, GateRejected
from a2a_slice_hub.gatekeeper import Gatekeeper
from a2a_slice_hub.paths import safe_path
from a2a_slice_hub.runner import TrustedPytestRunner
from conftest import Approve, new_item, submit


def gate(store, repo, reviewer=None):
    return Gatekeeper(store, repo.git, TrustedPytestRunner(allow_local_execution=True), reviewer or Approve())


def test_real_merge_tests_review_and_independent_qa(store, repo):
    item = new_item(store)
    source = repo.source({"src/orders/service.py": "def total():\n    return 2\n"})
    attempt = submit(store, item, repo, source)
    reviewer = Approve()
    result = gate(store, repo, reviewer).merge("epic/demo", [attempt["id"]])
    candidate = result["candidate_sha"]
    assert repo.git.ref("epic/demo") == candidate
    assert repo.git.ancestor(repo.base, candidate)
    assert repo.git.ancestor(source, candidate)
    assert result["tests"]["passed"] == ["tests/test_acceptance.py::test_total"]
    assert "total" in reviewer.contexts[0].changed_symbols
    assert "+    return 2" in reviewer.contexts[0].diff
    assert any(c["path"] == "src/payments/client.py" for c in reviewer.contexts[0].callers)
    item = store.get_item(item["id"])
    assert item["state"] == "MERGED_PENDING_QA"
    item = store.qa(item["id"], item["version"], candidate, True, "independent-qa")
    assert item["state"] == "ACCEPTED"
    assert store.get_attempt(attempt["id"])["state"] == "completed"
    assert len(repo.git.text("worktree", "list", "--porcelain").split("worktree ")) == 2


@pytest.mark.parametrize("name", ["tests/test_acceptance.py", "pytest.ini", "src/orders/conftest.py",
                                    "src/payments/client.py", ".hub/tests.json", "src/orders/.gitattributes"])
def test_protected_and_foreign_paths_rejected(store, repo, name):
    source = repo.source({name: "# unauthorized edit\n"})
    with pytest.raises(GateRejected):
        gate(store, repo).check_paths(repo.base, source, ["src/orders"])
    assert repo.git.ref("epic/demo") == repo.base


def test_rename_checks_both_endpoints(store, repo):
    original = repo.git.blob(repo.base, "src/payments/client.py").decode()
    source = repo.source({"src/payments/client.py": None, "src/orders/stolen.py": original})
    changes = repo.git.changes(repo.base, source)
    assert any(status.startswith("R") for status, _ in changes)
    with pytest.raises(GateRejected):
        gate(store, repo).check_paths(repo.base, source, ["src/orders"])


def test_symlink_rejected_without_needing_windows_symlink_privileges(store, repo):
    with repo.git.worktree(repo.base) as work:
        blob = repo.git.text("hash-object", "-w", "--stdin", data=b"../../tests/test_acceptance.py")
        repo.git.run("update-index", "--add", "--cacheinfo", "120000," + blob + ",src/orders/link", cwd=work)
        tree = repo.git.text("write-tree", cwd=work)
        source = repo.git.text("commit-tree", tree, "-p", repo.base, data=b"Symlink fixture\n")
    with pytest.raises(GateRejected, match="mode|link"):
        gate(store, repo).check_paths(repo.base, source, ["src/orders"])


def test_breaking_schema_is_not_auto_approved(store, repo):
    source = repo.source({"contracts/orders.json": json.dumps({"type": "object", "required": ["new"]})})
    with pytest.raises(GateRejected, match="compatibility"):
        gate(store, repo).check_paths(repo.base, source, ["contracts"])


@pytest.mark.parametrize("bad", ["../escape", "/absolute", "src/../tests/x", "src/CON.py", "src/name.", "src//x", "src/x:y", "src/.git/config"])
def test_nonportable_paths_rejected(bad):
    with pytest.raises(GateRejected):
        safe_path(bad)


def test_real_test_failure_does_not_advance_ref(store, repo):
    item = new_item(store)
    source = repo.source({"src/orders/service.py": "def total():\n    return -1\n"})
    attempt = submit(store, item, repo, source)
    with pytest.raises(GateRejected, match="trusted tests"):
        gate(store, repo).merge("epic/demo", [attempt["id"]])
    assert repo.git.ref("epic/demo") == repo.base
    assert store.get_item(item["id"])["state"] == "REWORK"


@pytest.mark.parametrize("body", [
    "import pytest\npytest.skip('not ready', allow_module_level=True)\n",
    "import pytest\n@pytest.mark.skip\ndef test_total(): pass\n",
    "import pytest\n@pytest.mark.xfail\ndef test_total(): assert False\n",
    "def renamed_test(): pass\n",
])
def test_skip_xfail_or_zero_collection_never_passes(repo, body):
    baseline = repo.source({"tests/test_acceptance.py": body})
    with pytest.raises(GateRejected):
        TrustedPytestRunner(allow_local_execution=True).run(repo.git, baseline, baseline, {"orders"})


def test_local_runner_requires_explicit_opt_in(repo):
    with pytest.raises(GateRejected, match="isolated runner"):
        TrustedPytestRunner().run(repo.git, repo.base, repo.base, {"orders"})


def test_semantic_reviewer_missing_is_fail_closed(store, repo):
    item = new_item(store)
    attempt = submit(store, item, repo)
    keeper = Gatekeeper(store, repo.git, TrustedPytestRunner(allow_local_execution=True))
    with pytest.raises(GateRejected, match="not configured"):
        keeper.merge("epic/demo", [attempt["id"]])
    assert repo.git.ref("epic/demo") == repo.base


def test_concurrent_candidates_only_one_publishes(store, repo):
    a, b = new_item(store, owner="a"), new_item(store, owner="b", slice_id="payments")
    sa = repo.source({"src/orders/service.py": "def total():\n    return 2\n"})
    sb = repo.source({"src/payments/client.py": "from orders.service import total\ndef charge(): return total() + 1\n"})
    aa, ab = submit(store, a, repo, sa), submit(store, b, repo, sb)
    barrier = threading.Barrier(2)
    class Reviewer(Approve):
        def review(self, context):
            verdict = super().review(context)
            barrier.wait(timeout=30)
            return verdict
    def merge(attempt):
        try:
            return gate(store, repo, Reviewer()).merge("epic/demo", [attempt["id"]])
        except Conflict:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(merge, [aa, ab]))
    assert sum(r is not None for r in results) == 1
    winner = next(r for r in results if r)
    assert repo.git.ref("epic/demo") == winner["candidate_sha"]
    states = [store.get_item(i["id"])["state"] for i in (a, b)]
    assert states.count("MERGED_PENDING_QA") == 1
    assert all(state in {"MERGED_PENDING_QA", "REWORK", "GATE_PENDING"} for state in states)


def test_crash_after_git_cas_recovers_from_durable_intent(store, repo):
    item = new_item(store)
    source = repo.source({"src/orders/service.py": "def total(): return 2\n"})
    submit(store, item, repo, source)
    candidate = repo.git.candidate(repo.base, [source])
    key = store.prepare_merge("epic/demo", repo.base, candidate, {item["id"]: store.get_item(item["id"])["version"]})
    def publish_then_crash(intent):
        assert repo.git.publish(intent)
        raise RuntimeError("injected crash after ref update")
    with pytest.raises(RuntimeError):
        store.finish_merge(key, publish_then_crash)
    assert repo.git.ref("epic/demo") == candidate
    assert store.get_item(item["id"])["state"] == "GATING"
    assert gate(store, repo).recover() == {key: "COMMITTED"}
    assert store.get_item(item["id"])["state"] == "MERGED_PENDING_QA"
    assert gate(store, repo).recover() == {}


def test_group_cannot_merge_partially_and_parent_uses_same_snapshot(store, repo):
    parent = store.create_item("BUG", "integration", "coordinator", ["src"], change_set_id="fix")
    a = new_item(store, owner="a", parent_id=parent["id"], change_set_id="fix")
    b = new_item(store, owner="b", slice_id="payments", parent_id=parent["id"], change_set_id="fix")
    sa = repo.source({"src/orders/service.py": "def total(): return 2\n"})
    sb = repo.source({"src/payments/client.py": "from orders.service import total\ndef charge(): return total() + 1\n"})
    aa, ab = submit(store, a, repo, sa), submit(store, b, repo, sb)
    with pytest.raises(Conflict, match="partial"):
        gate(store, repo).merge("epic/demo", [aa["id"]])
    result = gate(store, repo).merge("epic/demo", [aa["id"], ab["id"]])
    assert all(store.get_item(i["id"])["integration_sha"] == result["candidate_sha"] for i in (parent, a, b))
    p = store.get_item(parent["id"])
    assert store.qa(p["id"], p["version"], result["candidate_sha"], True, "independent-qa")["state"] == "CLOSED"


def _publish_and_exit(database, repository, scratch, intent):
    import os
    from a2a_slice_hub.git import Git
    from a2a_slice_hub.store import Store
    local, git = Store(database), Git(repository, scratch)
    def crash(row):
        if git.publish(row):
            os._exit(73)  # Actual abrupt process death: no Python transaction cleanup runs.
        os._exit(74)
    local.finish_merge(intent, crash)


def test_real_process_death_between_git_and_sqlite_is_recoverable(store, repo):
    import multiprocessing
    item = new_item(store)
    source = repo.source({"src/orders/service.py": "def total(): return 3\n"})
    submit(store, item, repo, source)
    candidate = repo.git.candidate(repo.base, [source])
    key = store.prepare_merge("epic/demo", repo.base, candidate,
                              {item["id"]: store.get_item(item["id"])["version"]})
    process = multiprocessing.get_context("spawn").Process(target=_publish_and_exit,
        args=(store.database, str(repo.git.repository), str(repo.git.scratch), key))
    try:
        process.start()
        process.join(timeout=30)
        assert process.exitcode == 73
    finally:
        if process.is_alive():
            process.kill()
            process.join(timeout=10)
    assert repo.git.ref("epic/demo") == candidate
    assert store.get_item(item["id"])["state"] == "GATING"
    assert gate(store, repo).recover() == {key: "COMMITTED"}
