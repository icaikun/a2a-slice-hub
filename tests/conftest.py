"""All Git gate/worker tests use actual subprocesses and a new bare repository."""
from dataclasses import dataclass
import json
from pathlib import Path
import secrets

import pytest

from a2a_slice_hub.api import Principal, create_app
from a2a_slice_hub.approvals import Approvals
from a2a_slice_hub.git import Git
from a2a_slice_hub.review import ReviewVerdict
from a2a_slice_hub.store import Store


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def store(tmp_path, clock):
    return Store(tmp_path / "hub.sqlite", clock=clock)


class Approve:
    def __init__(self):
        self.contexts = []

    def review(self, context):
        self.contexts.append(context)
        return ReviewVerdict(True, "test reviewer approved")


@dataclass
class Repository:
    git: Git
    base: str

    def source(self, changes, base=None):
        with self.git.worktree(base or self.base) as directory:
            for name, content in changes.items():
                target = directory / name
                if content is None:
                    target.unlink()
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(content, encoding="utf-8", newline="\n")
            self.git.run("add", "-A", cwd=directory)
            self.git.run("commit", "--allow-empty", "-m", "Fixture change", cwd=directory)
            sha = self.git.text("rev-parse", "HEAD", cwd=directory)
            self.git.run("update-ref", "refs/test-submissions/" + sha, sha)
            return sha


@pytest.fixture
def repo(tmp_path):
    bootstrap = Git(tmp_path, tmp_path / "scratch")
    bare, seed = tmp_path / "origin.git", tmp_path / "seed"
    bootstrap.run("init", "--bare", str(bare))
    bootstrap.run("init", "-b", "epic/demo", str(seed))
    files = {
        "src/orders/service.py": "def total():\n    return 1\n",
        "src/payments/client.py": "from orders.service import total\n\ndef charge():\n    return total()\n",
        "tests/test_acceptance.py": "from orders.service import total\n\ndef test_total():\n    assert total() >= 1\n",
        "pytest.ini": "[pytest]\n",
        ".hub/tests.json": json.dumps({"required": {"orders": ["tests/test_acceptance.py::test_total"],
                                                  "payments": ["tests/test_acceptance.py::test_total"]}, "accepted": []}),
        "contracts/orders.json": json.dumps({"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}),
    }
    for name, content in files.items():
        target = seed / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")
    bootstrap.run("add", ".", cwd=seed)
    bootstrap.run("commit", "-m", "Trusted baseline", cwd=seed)
    # Transfer actual Git objects without an MSYS shell / network transport.
    pack = bootstrap.run("pack-objects", "--stdout", "--all", cwd=seed).stdout
    bootstrap.run("index-pack", "--stdin", data=pack, cwd=bare)
    sha = bootstrap.text("rev-parse", "HEAD", cwd=seed)
    bootstrap.run("update-ref", "refs/heads/epic/demo", sha, cwd=bare)
    git = Git(bare, tmp_path / "scratch")
    return Repository(git, git.ref("epic/demo"))


def new_item(store, owner="worker-a", slice_id="orders", **kwargs):
    return store.create_item("DEV", slice_id, owner, ["src/" + slice_id], **kwargs)


def submit(store, item, repo, source=None):
    attempt = store.claim(item["id"], item["owner"], store.get_item(item["id"])["version"])
    attempt = store.transition(attempt["id"], item["owner"], attempt["epoch"], attempt["version"], "working")
    return store.transition(attempt["id"], item["owner"], attempt["epoch"], attempt["version"], "completed",
                            base_sha=repo.base, source_sha=source or repo.base)


@pytest.fixture
def api(store):
    from asgi_client import ASGITestClient as TestClient
    tokens = {role: secrets.token_urlsafe(32) for role in ("coordinator", "worker", "other", "qa", "approver", "im_gateway")}
    principals = {tokens[role]: Principal("worker-a" if role == "worker" else role, "worker" if role == "other" else role)
                  for role in tokens}
    approvals = Approvals(store, secrets.token_bytes(32))
    app = create_app(store, approvals, principals, a2a_worker_id="worker-a")
    with TestClient(app) as client:
        yield client, {role: {"Authorization": "Bearer " + token} for role, token in tokens.items()}, approvals
