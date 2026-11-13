import pytest

from a2a_slice_hub.errors import Conflict
from a2a_slice_hub.experience import EverOSHook
from a2a_slice_hub.worker import Worker, WorkResult
from conftest import new_item


class Hooks:
    def __init__(self):
        self.recalls, self.records = [], []
    def recall_experience(self, *, task): self.recalls.append(task)
    def record_trajectory(self, *, messages): self.records.append(messages)


def test_worker_real_commit_isolated_worktree_and_hooks(store, repo):
    item = new_item(store)
    attempt = store.claim(item["id"], item["owner"], 0)
    hooks = Hooks()
    worker = Worker(item["owner"], store, repo.git, hooks)
    directories = []
    def coding(directory):
        directories.append(directory)
        (directory / "src/orders/service.py").write_text("def total(): return 2\n", encoding="utf-8", newline="\n")
        repo.git.run("add", ".", cwd=directory)
        repo.git.run("commit", "-m", "Worker implementation", cwd=directory)
        return WorkResult(trajectory=[{"role": "user", "content": "sanitized task"}])
    result = worker.execute(attempt["id"], repo.base, "orders regression", coding, record_recovery=True)
    assert result["state"] == "completed"
    assert result["source_sha"] != repo.base
    assert repo.git.commit(result["source_sha"]) == result["source_sha"]
    assert repo.git.ref("epic/demo") == repo.base
    assert all(not p.exists() for p in directories)
    assert hooks.recalls == ["orders regression"]
    assert len(hooks.records) == 1


def test_worker_input_required_releases_real_slot(store, repo):
    a, b = new_item(store), new_item(store)
    worker = Worker(a["owner"], store, repo.git)
    first = store.claim(a["id"], a["owner"], 0)
    result = worker.execute(first["id"], repo.base, "question", lambda _: WorkResult("input-required"))
    assert result["state"] == "input-required"
    second = store.claim(b["id"], b["owner"], 0)
    assert worker.execute(second["id"], repo.base, "patch", lambda _: WorkResult())["state"] == "completed"


def test_worker_exception_cleans_worktree_and_fails_attempt(store, repo):
    item = new_item(store)
    worker = Worker(item["owner"], store, repo.git)
    attempt = store.claim(item["id"], item["owner"], 0)
    directories = []
    def fail(directory):
        directories.append(directory)
        raise RuntimeError("injected coding failure")
    with pytest.raises(RuntimeError):
        worker.execute(attempt["id"], repo.base, "work", fail)
    assert store.get_attempt(attempt["id"])["state"] == "failed"
    assert all(not d.exists() for d in directories)


def test_expired_worker_cannot_submit_even_if_local_git_succeeds(store, clock, repo):
    item = new_item(store)
    worker = Worker(item["owner"], store, repo.git)
    attempt = store.claim(item["id"], item["owner"], 0, ttl=1)
    def coding(_):
        clock.now += 2
        return WorkResult()
    with pytest.raises(Conflict):
        worker.execute(attempt["id"], repo.base, "slow work", coding)
    assert store.get_item(item["id"])["state"] != "GATE_PENDING"


def test_everos_hook_uses_explicit_redaction():
    calls = []
    hook = EverOSHook(lambda name, args: calls.append((name, args)), lambda value: value)
    hook.recall_experience(task="lease race")
    hook.record_trajectory(messages=[])
    assert [c[0] for c in calls] == ["recall_agent_experience", "record_trajectory"]
