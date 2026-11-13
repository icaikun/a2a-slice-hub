"""Round 2 red-team regressions: actual SQLite transactions, Git objects and HTTP routes."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import secrets

from asgi_client import ASGITestClient as TestClient
import pytest
import yaml

from a2a_slice_hub.api import Principal, create_app
from a2a_slice_hub.approvals import Approvals
from a2a_slice_hub.errors import Conflict, Forbidden, GateRejected, InfrastructureError
from a2a_slice_hub.gatekeeper import Gatekeeper
from a2a_slice_hub.paths import enforce
from a2a_slice_hub.review import schema_compatible, contract_subject
from a2a_slice_hub.runner import TrustedPytestRunner
from a2a_slice_hub.slices import SliceRegistry
from a2a_slice_hub.store import Store, SCHEMA, encode
from conftest import Approve, new_item, submit
from test_qa import pending, report


def specs():
    return [dict(slice_id=s, owner_user_id=owner, backup_user_id="backup", owned_paths=["src/" + s],
                 provides_contracts=[s], consumes_contracts=[c], required_skills=["python"])
            for s, owner, c in [("orders", "zhang", "payments"), ("checkout", "zhang", "orders"), ("payments", "li", "orders")]]


@pytest.fixture
def registered(store):
    store.registry = SliceRegistry(specs())
    return store


def online(store, worker="zhang-pc-01", owner="zhang", ttl=60):
    return store.register_worker(worker, owner, "http://localhost:9000/a2a", ["python"], ttl)


def item_for(store, slice_id="checkout", **kwargs):
    return store.create_item("DEV", slice_id, store.registry.get(slice_id).owner_user_id, ["src/" + slice_id], **kwargs)


def working(store, item, worker="zhang-pc-01"):
    a = store.claim(item["id"], worker, store.get_item(item["id"])["version"])
    return store.transition(a["id"], worker, a["epoch"], a["version"], "working")


def dependency(store, attempt, contract="orders", change=None):
    return store.transition(attempt["id"], attempt["worker_id"], attempt["epoch"], attempt["version"], "input-required",
        artifact={"dependency_request": {"schema_version": "dependency-request/v1", "contract": contract,
                  "expected_change": change or {"optional": "expire_time"}}})


def patches(store):
    with store.connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM work_items WHERE kind='CONTRACT_PATCH'")]


def test_registry_load_multiple_machine_binding_offline_mentions(registered, tmp_path, clock):
    for spec in specs():
        (tmp_path / (spec["slice_id"] + ".yaml")).write_text(yaml.safe_dump(spec), encoding="utf-8", newline="\n")
    registered.registry = SliceRegistry.load(tmp_path)
    item = item_for(registered)
    assert item["state"] == "WAITING_WORKER"
    with registered.connect() as conn:
        event = json.loads(conn.execute("SELECT payload FROM outbox WHERE topic='worker.offline'").fetchone()[0])
    assert event["mention_user_id"] == "zhang"
    assert set(online(registered)["eligible_slices"]) == {"orders", "checkout"}
    online(registered, "zhang-laptop")
    assert registered.get_item(item["id"])["state"] == "READY"
    task = working(registered, item, "zhang-laptop")
    assert task["worker_id"] != item["owner"]
    with pytest.raises(Forbidden):
        online(registered, "zhang-laptop", "li")
    other = item_for(registered, "orders")
    clock.now += 61
    registered.refresh_workers()
    assert registered.get_item(other["id"])["state"] == "WAITING_WORKER"
    with pytest.raises(Forbidden):
        registered.claim(other["id"], "zhang-pc-01", registered.get_item(other["id"])["version"])


def test_registry_skills_owner_and_path_authority(registered):
    registered.register_worker("bad", "zhang", "http://localhost", [], 60)
    item = item_for(registered)
    with pytest.raises(Forbidden):
        registered.claim(item["id"], "bad", item["version"])
    with pytest.raises(Forbidden):
        registered.create_item("DEV", "orders", "attacker", ["src/orders"])
    with pytest.raises(Forbidden):
        registered.create_item("DEV", "orders", "zhang", ["contracts/orders.json"])
    duplicate = specs()
    duplicate[1]["provides_contracts"] = ["orders"]
    with pytest.raises(ValueError, match="ambiguous"):
        SliceRegistry(duplicate)


def test_worker_http_cannot_spoof_owner(registered):
    tokens = {"x" * 32: Principal("machine", "worker", "zhang")}
    app = create_app(registered, Approvals(registered, b"a" * 32), tokens)
    with TestClient(app) as client:
        url = "/hub/workers/heartbeat"
        body = {"endpoint_url": "http://localhost", "skills": ["python"]}
        assert client.post(url, json=body).status_code == 401
        headers = {"Authorization": "Bearer " + "x" * 32}
        assert client.post(url, headers=headers, json={**body, "owner_user_id": "li"}).status_code == 422
        assert client.post(url, headers=headers, json=body).json()["owner_user_id"] == "zhang"


def test_dependency_atomic_reuse_and_unmet_resume(registered):
    online(registered)
    a = working(registered, item_for(registered))
    waiting = dependency(registered, a)
    b = working(registered, item_for(registered))
    dependency(registered, b)
    assert len(patches(registered)) == 1
    assert json.loads(patches(registered)[0]["allowed_paths"]) == ["contracts/orders.json", "src/orders", "tests/unit/orders"]
    with pytest.raises(Conflict, match="dependency"):
        registered.transition(waiting["id"], waiting["worker_id"], waiting["epoch"], waiting["version"], "working")


def test_dependency_cycle_rollback(registered):
    online(registered)
    online(registered, "li-pc", "li")
    dependency(registered, working(registered, item_for(registered)))
    orders = patches(registered)[0]
    dependency(registered, working(registered, orders), "payments")
    payments = next(p for p in patches(registered) if p["slice_id"] == "payments")
    a = working(registered, payments, "li-pc")
    with pytest.raises(Conflict, match="cycle"):
        dependency(registered, a, "orders")
    assert registered.get_attempt(a["id"])["state"] == "working"
    with registered.connect() as conn:
        assert conn.execute("SELECT count(*) FROM dependencies").fetchone()[0] == 2


def test_dependency_real_patch_merge_qa_wakes_new_attempt(registered, repo):
    online(registered)
    original = item_for(registered)
    a = working(registered, original)
    dependency(registered, a)
    patch = patches(registered)[0]
    p = working(registered, patch)
    schema = json.loads(repo.git.blob(repo.base, "contracts/orders.json"))
    schema["properties"]["expire_time"] = {"type": "string"}
    source = repo.source({"contracts/orders.json": json.dumps(schema)})
    p = registered.transition(p["id"], p["worker_id"], p["epoch"], p["version"], "completed", base_sha=repo.base, source_sha=source)
    result = Gatekeeper(registered, repo.git, TrustedPytestRunner(allow_local_execution=True), Approve()).merge("epic/demo", [p["id"]])
    assert registered.get_item(original["id"])["state"] == "WAITING"
    patch = registered.get_item(patch["id"])
    registered.qa(patch["id"], patch["version"], result["candidate_sha"], True, "independent-qa")
    woken = registered.get_item(original["id"])
    assert woken["state"] == "READY" and woken["integration_sha"] == result["candidate_sha"]
    assert registered.get_attempt(a["id"])["state"] == "canceled"
    resumed = registered.claim(woken["id"], "zhang-pc-01", woken["version"])
    assert resumed["epoch"] == a["epoch"] + 1
    resumed = registered.transition(resumed["id"], resumed["worker_id"], resumed["epoch"], resumed["version"], "working")
    source = repo.source({"src/checkout/service.py": "def checkout(): return True\n"})
    resumed = registered.transition(resumed["id"], resumed["worker_id"], resumed["epoch"], resumed["version"], "completed", base_sha=repo.base, source_sha=source)
    with pytest.raises(GateRejected, match="predates"):
        Gatekeeper(registered, repo.git, TrustedPytestRunner(allow_local_execution=True), Approve()).merge("epic/demo", [resumed["id"]])


BASE_SCHEMA = {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}


@pytest.mark.parametrize("mutation,compatible", [
    (lambda s: s["properties"].update(expire_time={"type": "string"}), True),
    (lambda s: (s["properties"].update(expire_time={"type": "string", "default": ""}), s["required"].append("expire_time")), False),
    (lambda s: (s["properties"].update(expire_time={"type": "string"}), s["required"].append("expire_time")), False),
    (lambda s: s["properties"].pop("id"), False),
    (lambda s: s["properties"]["id"].update(type="integer"), False),
    (lambda s: s["properties"]["id"].update(maxLength=3), False),
    (lambda s: s.update(required=[]), False),
    (lambda s: s.update(maxProperties=1), False),
])
def test_directional_additive_schema(mutation, compatible):
    after = deepcopy(BASE_SCHEMA)
    mutation(after)
    assert schema_compatible(BASE_SCHEMA, after) is compatible


@pytest.mark.parametrize("path", ["tests/acceptance/a.py", "tests/qa_e2e/a.py", "slices/orders.yaml", "migrations/001.sql",
    ".hub/tests.json", ".github/actions.yml", ".agents/a.md", "src/orders/conftest.py", "src/orders/pytest.ini", "tests/unit/orders/pyproject.toml"])
def test_precise_control_paths(path):
    with pytest.raises(GateRejected):
        enforce(path, [path])


def test_unit_tests_allowed_but_foreign_tests_denied():
    enforce("tests/unit/orders/test_calc.py", ["tests/unit/orders"])
    with pytest.raises(GateRejected):
        enforce("tests/unit/payments/test_calc.py", ["tests/unit/orders"])


def test_crlf_blob_roundtrip_rename_is_r100(repo):
    repo.base = repo.source({"src/orders/service.py": "def total():\r\n    return 1\r\n"})
    raw = repo.git.blob(repo.base, "src/orders/service.py")
    assert b"\r\n" in raw and b"\r\r\n" not in raw
    source = repo.source({"src/orders/service.py": None, "src/orders/renamed.py": raw.decode()})
    assert repo.git.blob(source, "src/orders/renamed.py") == raw
    assert any(status == "R100" for status, _ in repo.git.changes(repo.base, source))


def test_gate_reject_budget_and_infrastructure_retry(store, repo):
    item = new_item(store)
    source = repo.source({"src/payments/client.py": "def charge(): return -1\n"})
    a = submit(store, item, repo, source)
    g = Gatekeeper(store, repo.git, TrustedPytestRunner(allow_local_execution=True), Approve())
    with pytest.raises(GateRejected):
        g.merge("epic/demo", [a["id"]])
    item = store.get_item(item["id"])
    assert item["state"] == "REWORK" and item["rework_round"] == 1
    assert item["blocked_reason"]
    a = submit(store, item, repo)
    class Unavailable:
        def run(self, *args):
            raise InfrastructureError("runner unavailable")
    with pytest.raises(InfrastructureError):
        Gatekeeper(store, repo.git, Unavailable(), Approve()).merge("epic/demo", [a["id"]])
    assert store.get_item(item["id"])["state"] == "GATE_PENDING"
    assert store.get_item(item["id"])["rework_round"] == 1


@pytest.mark.parametrize("approved", [False, True])
def test_breaking_approval_binds_exact_diff(store, repo, approved):
    item = store.create_item("CONTRACT_PATCH", "orders", "worker-a", ["contracts/orders.json"], change_set_id="change-1")
    schema = deepcopy(BASE_SCHEMA)
    schema["properties"]["id"] = {"type": "integer"}
    source = repo.source({"contracts/orders.json": json.dumps(schema)})
    attempt = submit(store, item, repo, source)
    approval = Approvals(store, b"a" * 32)
    subject = contract_subject(repo.git, repo.base, source, {"contracts/orders.json"}, "change-1")
    if not approved:
        subject["contracts"]["contracts/orders.json"]["after"] = "0" * 64
    card = approval.issue(item["id"], subject, action="breaking_contract")
    approval.approve(card, "human-approver")
    g = Gatekeeper(store, repo.git, TrustedPytestRunner(allow_local_execution=True), Approve())
    if approved:
        assert g.merge("epic/demo", [attempt["id"]])["candidate_sha"]
    else:
        with pytest.raises(GateRejected):
            g.merge("epic/demo", [attempt["id"]])


@pytest.mark.parametrize("kind", ["DEV", "BUG"])
def test_non_patch_contract_edit_denied(store, repo, kind):
    item = store.create_item(kind, "orders", "worker-a", ["contracts/orders.json"])
    schema = deepcopy(BASE_SCHEMA)
    schema["properties"]["optional"] = {"type": "string"}
    source = repo.source({"contracts/orders.json": json.dumps(schema)})
    a = submit(store, item, repo, source)
    with pytest.raises(GateRejected, match="CONTRACT_PATCH"):
        Gatekeeper(store, repo.git, TrustedPytestRunner(allow_local_execution=True), Approve()).merge("epic/demo", [a["id"]])


def test_qa_environment_budget_no_rework(store, repo):
    item = pending(store, repo, new_item(store))
    for n in range(1, 4):
        item = store.qa(item["id"], item["version"], item["integration_sha"], None, "qa", outcome="ENV_ERROR")
        assert item["env_round"] == n and item["rework_round"] == 0
        assert item["state"] == ("BLOCKED_ENV" if n == 3 else "MERGED_PENDING_QA")
    with pytest.raises(Conflict):
        store.qa(item["id"], item["version"], item["integration_sha"], True, "qa")


@pytest.mark.parametrize("second,state", [("PASS", "FLAKY_BLOCKED"), ("FLAKY", "FLAKY_BLOCKED"), ("FAIL", "REWORK")])
def test_qa_flaky_one_controlled_rerun(store, repo, second, state):
    item = pending(store, repo, new_item(store))
    item = store.qa(item["id"], item["version"], item["integration_sha"], None, "qa", {"observed": "FAIL"}, outcome="FLAKY")
    assert item["rework_round"] == 0 and item["flaky_round"] == 1
    item = store.qa(item["id"], item["version"], item["integration_sha"], None, "qa", report(item["integration_sha"]), outcome=second)
    assert item["state"] == state
    assert item["rework_round"] == (1 if second == "FAIL" else 0)


def signed(clock, body, key="feishu-test-key", nonce="nonce"):
    raw = encode(body).encode()
    timestamp = str(int(clock()))
    signature = hashlib.sha256((timestamp + nonce + key).encode() + raw).hexdigest()
    return raw, {"content-type": "application/json", "X-Lark-Request-Timestamp": timestamp,
                 "X-Lark-Request-Nonce": nonce, "X-Lark-Signature": signature}


def test_feishu_l0_card_signature_identity_replay_and_unlock(registered, repo, clock):
    online(registered)
    registered.create_epic("epic-1", "epic/demo", repo.base)
    item = item_for(registered, epic_id="epic-1")
    assert item["state"] == "L0_PENDING_APPROVAL"
    with pytest.raises(Conflict):
        registered.claim(item["id"], "zhang-pc-01", item["version"])
    approvals = Approvals(registered, b"a" * 32)
    card = approvals.issue("epic-1", {"skeleton_sha": repo.base, "branch": "epic/demo"}, action="l0")
    app = create_app(registered, approvals, {"a" * 32: Principal("human", "approver")},
        feishu_encrypt_key="feishu-test-key", feishu_users={"ou_human": "human"})
    body = {"event": {"operator": {"open_id": "ou_human"}, "action": {"value": card}}}
    raw, headers = signed(clock, body)
    with TestClient(app) as client:
        assert client.post("/im/feishu/cards", content=raw + b" ", headers=headers).status_code == 403
        unknown = deepcopy(body)
        unknown["event"]["operator"]["open_id"] = "attacker"
        bad, bad_headers = signed(clock, unknown, nonce="unknown")
        assert client.post("/im/feishu/cards", content=bad, headers=bad_headers).status_code == 403
        first = client.post("/im/feishu/cards", content=raw, headers=headers)
        assert first.status_code == 200 and first.json()["approved"]
        assert client.post("/im/feishu/cards", content=raw, headers=headers).json() == first.json()
        assert registered.get_item(item["id"])["state"] == "READY"
        bad, bad_headers = signed(clock, {"type": "url_verification", "challenge": "changed"})
        assert client.post("/im/feishu/events", content=bad, headers=bad_headers).status_code == 409
        clock.now += 301
        assert client.post("/im/feishu/cards", content=raw, headers=headers).status_code == 403


def test_a2a_data_dependency_is_atomic_and_idempotent(registered):
    online(registered)
    item = item_for(registered)
    attempt = working(registered, item)
    app = create_app(registered, Approvals(registered, b"a" * 32), {"a" * 32: Principal("zhang-pc-01", "worker", "zhang")})
    payload = {"jsonrpc": "2.0", "id": 1, "method": "message/send", "params": {"message": {
        "role": "user", "messageId": "dependency-message", "contextId": item["id"], "taskId": attempt["id"],
        "metadata": {"lease_epoch": attempt["epoch"], "expected_state_version": attempt["version"]},
        "parts": [{"kind": "data", "data": {"schema_version": "dependency-request/v1", "contract": "orders", "expected_change": {"optional": "x"}}}]}}}
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer " + "a" * 32}
        first = client.post("/a2a", headers=headers, json=payload).json()
        assert first["result"]["status"]["state"] == "input-required"
        assert client.post("/a2a", headers=headers, json=payload).json() == first
        assert len(patches(registered)) == 1
        payload["params"]["message"]["parts"][0]["data"]["expected_change"] = {"different": True}
        assert "error" in client.post("/a2a", headers=headers, json=payload).json()


def test_load_app_absolute_paths(tmp_path, monkeypatch, repo):
    from a2a_slice_hub.config import load_app
    directory = tmp_path / "slices"
    directory.mkdir()
    for spec in specs():
        (directory / (spec["slice_id"] + ".yaml")).write_text(yaml.safe_dump(spec), encoding="utf-8", newline="\n")
    monkeypatch.setenv("TEST_CREDENTIAL", "a" * 32)
    monkeypatch.setenv("TEST_APPROVAL_KEY", "b" * 32)
    config = {"database": str(tmp_path / "data" / "hub.sqlite"), "repository": str(repo.git.repository),
              "scratch": str(tmp_path / "scratch-config"), "slices": str(directory), "approval_key_env": "TEST_APPROVAL_KEY",
              "principals": [{"name": "machine", "role": "worker", "owner_user_id": "zhang", "credential_env": "TEST_CREDENTIAL"}]}
    filename = tmp_path / "config.yaml"
    filename.write_text(yaml.safe_dump(config), encoding="utf-8", newline="\n")
    with TestClient(load_app(filename)) as client:
        assert client.get("/health").status_code == 200
        assert client.post("/hub/workers/heartbeat", headers={"Authorization": "Bearer " + "a" * 32},
            json={"endpoint_url": "http://localhost", "skills": ["python"]}).status_code == 200
    assert Path(config["database"]).exists()


def test_dynamic_accepted_regression_prevents_other_slice_breakage(store, repo):
    # Central baseline promotion before work begins: distinct slice suites.
    manifest = {"required": {"orders": ["tests/acceptance/test_orders.py::test_orders"],
                            "payments": ["tests/acceptance/test_payments.py::test_payments"]}, "accepted": [], "pending": []}
    baseline = repo.source({".hub/tests.json": json.dumps(manifest),
        "tests/acceptance/test_orders.py": "from payments.client import charge\ndef test_orders(): assert charge() >= 1\n",
        "tests/acceptance/test_payments.py": "from payments.client import charge\ndef test_payments(): assert isinstance(charge(), int)\n"})
    repo.git.run("update-ref", "refs/heads/epic/demo", baseline, repo.base)
    repo.base = baseline
    g = Gatekeeper(store, repo.git, TrustedPytestRunner(allow_local_execution=True), Approve())
    orders = new_item(store)
    a = submit(store, orders, repo)
    first = g.merge("epic/demo", [a["id"]])
    orders = store.get_item(orders["id"])
    store.qa(orders["id"], orders["version"], first["candidate_sha"], True, "qa")
    assert store.accepted_slices() == {"orders"}
    payments = new_item(store, owner="payment-worker", slice_id="payments")
    source = repo.source({"src/payments/client.py": "def charge(): return -1\n"})
    a = submit(store, payments, repo, source)
    with pytest.raises(GateRejected, match="trusted tests failed"):
        g.merge("epic/demo", [a["id"]])
    assert repo.git.ref("epic/demo") == first["candidate_sha"]
    with store.connect() as conn:
        evidence = json.loads(conn.execute("SELECT evidence_json FROM merge_intents WHERE id=?", (first["intent"],)).fetchone()[0])
    assert evidence["review"]["approved"] is True
    assert evidence["tests"]["passed"] == manifest["required"]["orders"]


def test_accepted_promotion_race_forces_gate_retry(store, repo):
    a = submit(store, new_item(store), repo)
    class PromoteDuringReview(Approve):
        def review(self, context):
            with store.transaction() as conn:
                conn.execute("INSERT INTO accepted_slices VALUES('payments',?)", (repo.base,))
            return super().review(context)
    with pytest.raises(Conflict, match="regression set changed"):
        Gatekeeper(store, repo.git, TrustedPytestRunner(allow_local_execution=True), PromoteDuringReview()).merge("epic/demo", [a["id"]])
    assert repo.git.ref("epic/demo") == repo.base


@pytest.mark.parametrize("pending_tests,ready", [([], True), (["not-implemented"], False), (None, False)])
def test_epic_release_requires_all_accepted_and_pending_zero(store, repo, pending_tests, ready):
    manifest = json.loads(repo.git.blob(repo.base, ".hub/tests.json"))
    if pending_tests is not None:
        manifest["pending"] = pending_tests
    baseline = repo.source({".hub/tests.json": json.dumps(manifest)})
    repo.git.run("update-ref", "refs/heads/epic/demo", baseline, repo.base)
    repo.base = baseline
    store.create_epic("epic", "epic/demo", baseline)
    item = new_item(store, epic_id="epic")
    approvals = Approvals(store, b"a" * 32)
    card = approvals.issue("epic", {"skeleton_sha": baseline, "branch": "epic/demo"}, action="l0")
    approvals.approve(card, "human")
    with pytest.raises(Conflict):
        store.check_release_ready("epic", repo.git)
    item = pending(store, repo, item)
    store.qa(item["id"], item["version"], item["integration_sha"], True, "qa")
    if ready:
        result = store.check_release_ready("epic", repo.git)
        assert result["state"] == "READY_FOR_RELEASE" and result["release_sha"] == item["integration_sha"]
        with pytest.raises(Conflict, match="sealed"):
            new_item(store, epic_id="epic")
    else:
        with pytest.raises(Conflict, match="PENDING"):
            store.check_release_ready("epic", repo.git)


def test_schema_v1_migrates_without_dropping_journal_or_foreign_keys(tmp_path, clock):
    import sqlite3
    old = SCHEMA.replace("'WAITING_WORKER','L0_PENDING_APPROVAL','BLOCKED_ENV','FLAKY_BLOCKED',", "")
    old = old.replace(",\n epic_id TEXT REFERENCES epics(id), dependency_key TEXT, dependency_request TEXT,\n env_round INTEGER NOT NULL DEFAULT 0, flaky_round INTEGER NOT NULL DEFAULT 0, flaky_observed TEXT", "")
    old = old.replace("evidence_json TEXT NOT NULL DEFAULT '{}', ", "")
    old = old.replace("id TEXT PRIMARY KEY, item TEXT NOT NULL,\n subject_digest", "id TEXT PRIMARY KEY, item TEXT NOT NULL REFERENCES work_items(id),\n subject_digest")
    path = tmp_path / "old.sqlite"
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.executescript(old)
        conn.execute("INSERT INTO work_items(id,kind,slice_id,owner,allowed_paths,deadline) VALUES('old','DEV','orders','worker','[]',9999)")
        conn.execute("INSERT INTO attempts(id,work_item_id,worker_id,epoch,state,expires) VALUES('attempt','old','worker',1,'completed',9999)")
        conn.execute("INSERT INTO merge_intents(id,branch,base_sha,candidate_sha,items) VALUES('intent','epic/demo','base','candidate','[\"old\"]')")
    upgraded = Store(path, clock=clock)
    assert upgraded.get_item("old")["kind"] == "DEV"
    with upgraded.connect() as conn:
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("SELECT evidence_json FROM merge_intents").fetchone()[0] == "{}"
    assert Store(path, clock=clock).get_attempt("attempt")["work_item_id"] == "old"


def test_signed_feishu_event_and_precise_offline_at(store, clock):
    import httpx
    from a2a_slice_hub.im import BOTS, BotIdentity, FeishuAdapter
    app = create_app(store, Approvals(store, b"a" * 32), {"a" * 32: Principal("human", "approver")}, feishu_encrypt_key="feishu-test-key")
    body = {"event": {"sender": {"sender_type": "app"}}}
    raw, headers = signed(clock, body)
    with TestClient(app) as client:
        assert client.post("/im/feishu/events", content=raw, headers=headers).json()["ignored"]
        assert client.post("/im/feishu/events", content=raw).status_code == 403
    sent = []
    with httpx.Client(transport=httpx.MockTransport(lambda r: (sent.append(json.loads(r.content)) or httpx.Response(200, json={"code": 0})))) as client:
        adapter = FeishuAdapter({b: BotIdentity(b, lambda: "test") for b in BOTS}, "chat", client)
        adapter.send("event", "worker.offline", {"mention_user_id": "ou_zhang"})
    assert '<at user_id="ou_zhang">' in json.loads(sent[0]["content"])["text"]


def test_data_bug_report_requires_qa_role(store, repo):
    item = pending(store, repo, new_item(store))
    app = create_app(store, Approvals(store, b"a" * 32), {
        "a" * 32: Principal("qa", "qa"), "b" * 32: Principal("worker-a", "worker")})
    body = {"jsonrpc": "2.0", "id": "bug", "method": "message/send", "params": {"message": {
        "role": "user", "messageId": "bug-msg", "contextId": item["id"],
        "metadata": {"expected_state_version": item["version"]},
        "parts": [{"kind": "data", "data": report(item["integration_sha"])}]}}}
    with TestClient(app) as client:
        assert "error" in client.post("/a2a", headers={"Authorization": "Bearer " + "b" * 32}, json=body).json()
        result = client.post("/a2a", headers={"Authorization": "Bearer " + "a" * 32}, json=body).json()
        assert client.post("/a2a", headers={"Authorization": "Bearer " + "a" * 32}, json=body).json() == result
    assert result["result"]["parts"][0]["data"]["state"] == "REWORK"


def test_env_recovery_signed_approval_preserves_developer_budget(store, repo):
    item = pending(store, repo, new_item(store))
    for _ in range(3):
        item = store.qa(item["id"], item["version"], item["integration_sha"], None, "qa", outcome="ENV_ERROR")
    approvals = Approvals(store, b"a" * 32)
    approvals.approve(approvals.issue(item["id"], {"reason": "environment repaired"}), "human")
    result = store.get_item(item["id"])
    assert result["state"] == "MERGED_PENDING_QA"
    assert result["rework_round"] == result["env_round"] == 0
    assert result["integration_sha"] == item["integration_sha"]


def test_callback_nonce_conflict_has_no_second_approval_side_effect(store):
    from test_api_im import block
    one, two = block(store), block(store)
    approvals = Approvals(store, b"a" * 32)
    a = approvals.issue(one["id"], {"reason": "one"})
    b = approvals.issue(two["id"], {"reason": "two"})
    approvals.approve(a, "human", receipt="nonce", digest="first")
    with pytest.raises(Conflict):
        approvals.approve(b, "human", receipt="nonce", digest="second")
    assert store.get_item(two["id"])["state"] == "BLOCKED"


def test_gate_rejection_third_round_blocks(store, repo):
    item = new_item(store)
    for n in range(1, 4):
        submit(store, item, repo)
        item = store.get_item(item["id"])
        store.reject(item["id"], item["version"], "candidate defect")
        item = store.get_item(item["id"])
        assert item["rework_round"] == n
    assert item["state"] == "BLOCKED"


def test_authorized_candidate_unit_test_can_merge(store, repo):
    item = store.create_item("DEV", "orders", "worker-a", ["src/orders", "tests/unit/orders"])
    source = repo.source({"tests/unit/orders/test_calc.py": "def test_calculation(): assert 1 + 1 == 2\n"})
    a = submit(store, item, repo, source)
    result = Gatekeeper(store, repo.git, TrustedPytestRunner(allow_local_execution=True), Approve()).merge("epic/demo", [a["id"]])
    assert "tests/unit/orders/test_calc.py" in repo.git.tree(result["candidate_sha"])


def test_non_epic_item_cannot_bypass_l0_branch_fence(store, repo):
    store.create_epic("epic", "epic/demo", repo.base)
    item = new_item(store)
    attempt = submit(store, item, repo)
    with pytest.raises(Conflict, match="epic branch"):
        Gatekeeper(store, repo.git, TrustedPytestRunner(allow_local_execution=True), Approve()).merge("epic/demo", [attempt["id"]])


def test_unknown_closed_schema_evolution_is_not_auto_approved():
    before = deepcopy(BASE_SCHEMA)
    before["additionalProperties"] = False
    after = deepcopy(before)
    after["properties"]["new"] = {"type": "string"}
    assert not schema_compatible(before, after)


def test_concurrent_dependency_requests_create_one_patch(registered):
    online(registered)
    online(registered, "zhang-laptop")
    a = working(registered, item_for(registered))
    b = working(registered, item_for(registered), "zhang-laptop")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda attempt: dependency(registered, attempt), [a, b]))
    assert all(a["state"] == "input-required" for a in results)
    assert len(patches(registered)) == 1
    assert json.loads(patches(registered)[0]["dependency_request"])["expected_change"] == {"optional": "expire_time"}


def test_batch_rejection_returns_all_submitted_items_to_rework(store, repo):
    one = new_item(store)
    two = new_item(store, owner="pay-worker", slice_id="payments")
    bad = repo.source({"src/checkout/foreign.py": "value = 1\n"})
    first, second = submit(store, one, repo, bad), submit(store, two, repo)
    with pytest.raises(GateRejected):
        Gatekeeper(store, repo.git, TrustedPytestRunner(allow_local_execution=True), Approve()).merge("epic/demo", [first["id"], second["id"]])
    assert all(store.get_item(i["id"])["state"] == "REWORK" for i in [one, two])


def test_breaking_approval_expiring_during_review_cannot_publish(store, repo, clock):
    item = store.create_item("CONTRACT_PATCH", "orders", "worker-a", ["contracts/orders.json"], change_set_id="expiring")
    schema = deepcopy(BASE_SCHEMA)
    schema["properties"]["id"] = {"type": "integer"}
    source = repo.source({"contracts/orders.json": json.dumps(schema)})
    attempt = submit(store, item, repo, source)
    approvals = Approvals(store, b"a" * 32)
    card = approvals.issue(item["id"], contract_subject(repo.git, repo.base, source, {"contracts/orders.json"}, "expiring"), ttl=1, action="breaking_contract")
    approvals.approve(card, "human")
    class SlowReview(Approve):
        def review(self, context):
            clock.now += 2
            return super().review(context)
    with pytest.raises(Conflict, match="approval expired"):
        Gatekeeper(store, repo.git, TrustedPytestRunner(allow_local_execution=True), SlowReview()).merge("epic/demo", [attempt["id"]])
    assert repo.git.ref("epic/demo") == repo.base
    assert store.get_item(item["id"])["state"] == "GATE_PENDING"
