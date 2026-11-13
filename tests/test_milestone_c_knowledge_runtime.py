"""Milestone C: local protocol E2E, governance fences and daemon recovery.

Provider HTTP and coding executables are deterministic test doubles; Git, SQLite,
subprocesses, adapters, trusted pytest gates and state transitions are real.
"""
from dataclasses import asdict
import hashlib
import json
import secrets
import sys

import httpx
import pytest

from a2a_slice_hub.approvals import Approvals
from a2a_slice_hub.channels import ChannelGate, DingTalkChatIngress, FeishuIngress, MultiOutboxDispatcher
from a2a_slice_hub.coding import CodexCliAdapter, ClaudeCliAdapter
from a2a_slice_hub.errors import Conflict, Forbidden, GateRejected, NotFound
from a2a_slice_hub.experience import EverOSHook
from a2a_slice_hub.gatekeeper import Gatekeeper
from a2a_slice_hub.knowledge import KnowledgeCenter, ScopedKnowledgeHook, redact_secrets
from a2a_slice_hub.policy import ExternalAccount, IdentityPolicy, MemoryScope, PersonRecord
from a2a_slice_hub.runner import TrustedPytestRunner
from a2a_slice_hub.runtime import HubRuntime
from a2a_slice_hub.store import Store
from a2a_slice_hub.worker import Worker, WorkResult
from conftest import Approve, new_item
from test_milestone_a_cli_governance import external_scratch, MOCK_CLI
from test_milestone_b_dual_im import bots, channels, drain, feishu_callback
from test_qa import pending, report

SCOPE = MemoryScope("org", "project-a", "repo")
OTHER = MemoryScope("org", "project-b", "repo")


def propose(k, item, scope=SCOPE, **kwargs):
    return k.propose_case(scope, item_id=item["id"], slice_id=item["slice_id"],
        failure_signature="decimal rounding", root_cause="binary arithmetic", resolution_steps="use Decimal", **kwargs)


def accepted(store, repo, item=None):
    item = pending(store, repo, item or new_item(store, owner="alice"))
    return store.qa(item["id"], item["version"], item["integration_sha"], True, "carol")


def promote(k, item, **kwargs):
    return k.promote_verified_case(SCOPE, item_id=item["id"], integration_sha=item["integration_sha"], qa_reviewer="carol", **kwargs)


def test_dispatch_enrichment_and_safe_error(store):
    item = new_item(store, owner="alice")
    class Adapter:
        provider = "feishu"
        def send(self, event_id, topic, payload):
            assert payload["slice_id"] == "orders" and payload["owner"] == "alice"
            response = httpx.Response(502, request=httpx.Request("POST", "https://example.invalid/?token=private-fixture"))
            response.raise_for_status()
    dispatcher = MultiOutboxDispatcher(store, {"main": Adapter()})
    with store.transaction() as conn:
        conn.execute("UPDATE outbox SET delivered=1")
        conn.execute("UPDATE outbox_deliveries SET delivered=1")
        event = store.emit(conn, "qa.result", {"item": item["id"], "passed": False})
    assert dispatcher.once()
    with store.connect() as conn:
        row = dict(conn.execute("SELECT * FROM outbox_deliveries WHERE event_id=?", (event,)).fetchone())
    assert row["last_error"] == "HTTPStatusError: HTTP 502"
    assert "private-fixture" not in json.dumps(row) and "example.invalid" not in row["last_error"]


def test_dingtalk_at_all_cannot_impersonate_explicit_mention():
    ingress = DingTalkChatIngress(ChannelGate(group_allow_from={"group"}, require_mention=True), app_id="robot")
    body = dict(senderStaffId="user", senderCorpId="corp", robotCode="robot", conversationType="2",
                conversationId="group", msgId="msg", isAtAll=True, isInAtList=True, atUsers=[])
    assert not ingress.handle_chat(body).allowed
    body["atUsers"] = [{"robotCode": "robot"}]
    assert ingress.handle_chat(body).allowed
    body.update(isAtAll=False, atUsers=[])
    assert ingress.handle_chat(body).allowed


@pytest.mark.parametrize("prefix", ["github_" + "pat_", "ghp_", "sk-", "xoxb-", "Bearer "])
def test_evidence_redaction_and_ttl(store, clock, prefix):
    k = KnowledgeCenter(store)
    secret = prefix + "a" * 32
    value = k.record_evidence(SCOPE, kind="cli_failure", summary=secret, environment={"message": secret}, payload={"text": secret}, ttl_seconds=10)
    assert secret not in json.dumps(value)
    assert value["authority"] == "unverified_evidence"
    assert k.list_evidence(SCOPE)[0]["id"] == value["id"]
    assert k.search_cases(SCOPE, "") == []
    k.record_evidence(OTHER, kind="env_error", summary="keep", ttl_seconds=20)
    clock.now += 10
    assert k.list_evidence(SCOPE) == []
    assert k.prune_evidence(SCOPE) == 1 and len(k.list_evidence(OTHER)) == 1


def test_recursive_redaction_private_keys_and_credential_urls(store):
    key = "-----BEGIN " + "PRIVATE KEY-----\nprivate-fixture\n-----END " + "PRIVATE KEY-----"
    value = {"nested": [key, "https://user:pass@example.invalid/path", "https://example.invalid/?signed=private", "https://example.invalid/#secret"],
             "authorization": "private", "clean": "https://example.invalid/docs"}
    k = KnowledgeCenter(store, redact=lambda data: redact_secrets(data))
    evidence = k.record_evidence(SCOPE, kind="env_error", summary="failure", payload=value)
    text = json.dumps(evidence)
    assert "private" not in text and "user:pass" not in text
    assert evidence["payload"]["clean"] == value["clean"]
    with store.connect() as conn:
        assert "private" not in conn.execute("SELECT payload_json FROM runtime_evidence").fetchone()[0]


def test_candidates_require_snapshot_and_independent_qa(store, repo):
    k = KnowledgeCenter(store)
    item = new_item(store, owner="alice")
    case = propose(k, item)
    assert propose(k, item)["case_id"] == case["case_id"]
    assert k.search_cases(SCOPE, "decimal") == []
    with pytest.raises(Conflict):
        k.promote_verified_case(SCOPE, item_id=item["id"], integration_sha=repo.base, qa_reviewer="carol")
    item = accepted(store, repo, item)
    for actor in ("alice", "", "  "):
        with pytest.raises(Forbidden):
            k.promote_verified_case(SCOPE, item_id=item["id"], integration_sha=item["integration_sha"], qa_reviewer=actor)
    with pytest.raises(Conflict):
        k.promote_verified_case(SCOPE, item_id=item["id"], integration_sha=repo.base, qa_reviewer="carol")
    result = promote(k, item)
    assert result["case_id"] == case["case_id"] and result["integration_sha"] == item["integration_sha"]
    assert promote(k, item) == result
    with store.connect() as conn:
        assert conn.execute("SELECT count(*) FROM troubleshooting_cases").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM everos_export_queue").fetchone()[0] == 1
    with pytest.raises(Conflict):
        propose(k, item, integration_sha=item["integration_sha"])


def test_provisional_and_explicit_case_selection(store, repo):
    k = KnowledgeCenter(store)
    item = accepted(store, repo)
    a = propose(k, item, integration_sha="provisional")
    b = propose(k, item, integration_sha="another")
    with pytest.raises(Conflict):
        promote(k, item)
    assert promote(k, item, case_id=a["case_id"])["case_id"] == a["case_id"]
    assert k.search_cases(SCOPE, "decimal")[0]["case_id"] != b["case_id"]


def test_export_failure_recovery_is_nonblocking_and_redacted(store, repo):
    k = KnowledgeCenter(store)
    item = accepted(store, repo)
    propose(k, item, environment={"password": "private-fixture"})
    promote(k, item)
    def unavailable(method, arguments):
        # A second writer can acquire the DB: the optional hook holds no write lock.
        with store.transaction() as conn:
            conn.execute("UPDATE runtime_evidence SET summary=summary")
        raise RuntimeError("private URL/token must not be persisted")
    hook = EverOSHook(unavailable, lambda data: data)
    assert k.flush_everos_exports(hook, scope=SCOPE) == {"exported": 0, "failed": 1}
    assert k.search_cases(SCOPE, "decimal") and store.get_item(item["id"])["state"] == "ACCEPTED"
    with store.connect() as conn:
        row = dict(conn.execute("SELECT * FROM everos_export_queue").fetchone())
    assert row["tries"] == 1 and row["last_error"] == "RuntimeError" and not row["dead_letter"]
    calls = []
    recovered = EverOSHook(lambda method, arguments: calls.append((method, arguments)), lambda data: data)
    assert k.flush_everos_exports(recovered, scope=OTHER)["exported"] == 0
    assert k.flush_everos_exports(recovered, scope=SCOPE)["exported"] == 1
    assert k.flush_everos_exports(recovered, scope=SCOPE)["exported"] == 0
    assert len(calls) == 1 and "private-fixture" not in json.dumps(calls)


def test_export_dead_letter_and_invalidation_cancels_pending(store, repo):
    k = KnowledgeCenter(store)
    item = accepted(store, repo)
    propose(k, item)
    case = promote(k, item)
    class Down:
        def record_trajectory(self, **kwargs):
            raise OSError("private")
    for _ in range(3):
        assert k.flush_everos_exports(Down(), scope=SCOPE)["failed"] == 1
    assert k.flush_everos_exports(Down(), scope=SCOPE)["failed"] == 0
    with store.connect() as conn:
        row = conn.execute("SELECT tries,dead_letter,last_error FROM everos_export_queue").fetchone()
        assert tuple(row) == (3, 1, "OSError")
    k.invalidate_case(SCOPE, case["case_id"], "obsolete", superseded_by_adr="0002")
    assert not k.search_cases(SCOPE, "decimal")
    with store.connect() as conn:
        assert conn.execute("SELECT count(*) FROM everos_export_queue").fetchone()[0] == 0
    with pytest.raises(Conflict):
        promote(k, item)


@pytest.mark.parametrize("other", [OTHER, MemoryScope("other-org", "project-a", "repo"), MemoryScope("org", "project-a", "other-repo")])
def test_scope_isolation_and_authorization(store, repo, other):
    k = KnowledgeCenter(store)
    item = accepted(store, repo)
    propose(k, item)
    case = promote(k, item)
    k.record_evidence(SCOPE, kind="env_error", summary="decimal rounding")
    assert k.search_cases(other, "decimal rounding") == [] and k.list_evidence(other) == []
    assert k.prune_evidence(other) == 0
    with pytest.raises(NotFound):
        k.invalidate_case(other, case["case_id"], "no")
    principal = PersonRecord("reader", {"developer"}, scopes={other})
    policy = IdentityPolicy([principal])
    guarded = KnowledgeCenter(store, identity_policy=policy, principal=PersonRecord("reader", {"qa"}, scopes={SCOPE}))
    operations = [lambda: guarded.search_cases(SCOPE, "decimal"), lambda: guarded.list_evidence(SCOPE),
                  lambda: guarded.prune_evidence(SCOPE), lambda: propose(guarded, item),
                  lambda: promote(guarded, item), lambda: guarded.invalidate_case(SCOPE, case["case_id"], "no"),
                  lambda: guarded.record_evidence(SCOPE, kind="env_error", summary="no"),
                  lambda: guarded.flush_everos_exports(None, scope=SCOPE)]
    for operation in operations:
        with pytest.raises(Forbidden):
            operation()
    with pytest.raises(Forbidden):
        k.search_cases(asdict(SCOPE), "decimal")


def test_temporal_adr_filtering_and_search_literal_tokens(store, repo):
    k = KnowledgeCenter(store)
    item = accepted(store, repo)
    propose(k, item, linked_adr_ids=["0001"])
    case = promote(k, item)
    assert k.search_cases(SCOPE, "DECIMAL rounding")[0]["case_id"] == case["case_id"]
    assert not k.search_cases(SCOPE, "unknown% OR 1=1")
    assert not k.search_cases(SCOPE, "decimal", active_adrs=[{"id": "0002", "supersedes": ["0001"]}])
    assert not k.search_cases(SCOPE, "decimal", active_adrs={"superseded": {"0001"}})
    assert not k.search_cases(SCOPE, "decimal", active_adrs={"all_accepted_adr_ids": []})
    assert k.search_cases(SCOPE, "decimal", active_adrs={"all_accepted_adr_ids": ["0001"]})
    for field in ("checks", "invalidations"):
        assert not k.search_cases(SCOPE, "decimal", active_adrs=[{"id": "0002", field: [{"case_id": case["case_id"]}]}])
    assert ScopedKnowledgeHook(k, OTHER).recall_experience(task="decimal")["verified_cases"] == []
    hook = ScopedKnowledgeHook(k, SCOPE)
    hook.record_trajectory(messages=[{"role": "assistant", "content": "I claim verified"}])
    assert hook.recall_experience(task="decimal")["recent_unverified_evidence"][0]["authority"] == "unverified_evidence"
    k.invalidate_case(SCOPE, case["case_id"], "ADR changed")
    assert hook.recall_experience(task="decimal")["verified_cases"] == []


def test_runtime_recover_failure_and_branch_binding(store, repo, external_scratch):
    k = KnowledgeCenter(store)
    item = new_item(store, owner="alice")
    def broken(directory):
        raise RuntimeError("do not persist private URL")
    worker = Worker("alice", store, repo.git)
    runtime = HubRuntime(store, repo.git, workers={"alice": (worker, broken)}, knowledge=k, scope=SCOPE)
    result = runtime.tick("epic/demo")
    assert result["coding"]["error"] == "RuntimeError"
    assert store.get_item(item["id"])["state"] == "REWORK"
    assert k.list_evidence(SCOPE)[0]["summary"] == "RuntimeError"
    assert "private" not in json.dumps(k.list_evidence(SCOPE))
    store.create_epic("different", "epic/other", repo.base)
    assert runtime.dispatch_coding_once("epic/other") is None


def test_latest_checkpoint_only_current_baseline(store, repo, external_scratch):
    item = new_item(store)
    script = external_scratch / "mock_cli.py"
    script.write_text(MOCK_CLI, encoding="utf-8")
    adapter = CodexCliAdapter(executable=sys.executable, extra_args=[str(script)], env={"MOCK_MODE": "input-required"})
    runtime = HubRuntime(store, repo.git, workers={item["owner"]: (Worker(item["owner"], store, repo.git), adapter)})
    result = runtime.tick("epic/demo")["coding"]
    assert result["state"] == "input-required"
    patch, base = runtime.latest_checkpoint(item["id"], repo.base)
    assert b"src/orders/service.py" in patch and base == repo.base
    assert runtime.latest_checkpoint(item["id"], "f" * 40) == (None, None)
    (external_scratch / "attempts" / result["attempt_id"] / "checkpoint.patch").unlink()
    assert runtime.latest_checkpoint(item["id"], repo.base) == (None, None)


def test_schema_v6_restart_foreign_keys_and_future_version(store):
    Store(store.database)
    with store.connect() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 6
        assert not conn.execute("PRAGMA foreign_key_check").fetchall()
        conn.execute("PRAGMA user_version=7")
    with pytest.raises(Conflict):
        Store(store.database)


def test_cli_legacy_serve_and_verify(repo, monkeypatch, capsys):
    from a2a_slice_hub import __main__ as cli
    calls = []
    monkeypatch.setattr(cli, "load_app", lambda path: path)
    monkeypatch.setattr(cli.uvicorn, "run", lambda app, **kwargs: calls.append((app, kwargs)))
    options = ["--config", "examples/hub.yaml", "--host", "localhost", "--port", "8000"]
    cli.main(options)
    cli.main(["serve", *options])
    assert calls[0] == calls[1] == ("examples/hub.yaml", {"host": "localhost", "port": 8000})
    cli.main(["verify-skills", "--repo", str(repo.git.repository), "--rev", "epic/demo"])
    assert json.loads(capsys.readouterr().out)["revision"] == repo.base
    with pytest.raises(SystemExit) as error:
        cli.main(["verify-skills", "--repo", str(repo.git.repository), "--rev", "missing"])
    assert error.value.code == 1


LOOP_CLI = r'''
import json, pathlib, sys
text = sys.stdin.read()
task = json.loads(text[text.index("{"):])
assert task["skills_manifest"][0]["name"] == "python-slice"
assert task["active_adrs"][0]["id"] == "0001"
args = sys.argv[1:]
if "exec" in args:
    feedback = task["rework_feedback"]
    if feedback:
        assert feedback["report"]["schema_version"] == "bug-report/v1"
        assert feedback["recent_unverified_evidence"][0]["authority"] == "unverified_evidence"
        content = "from decimal import Decimal\ndef total(): return Decimal('0.1') + Decimal('0.2')\n"
    else:
        content = "def total(): return 0.1 + 0.2\n"
    path = pathlib.Path("src/orders/service.py")
else:
    memory = task["advisory_memory_block"]
    assert memory.startswith("<<<BEGIN_STORED_MEMORY_ADVISORY_ONLY>>>")
    assert memory.count("<<<END_STORED_MEMORY_ADVISORY_ONLY>>>") == 1
    assert '"status":"VERIFIED"' in memory and '"project_id":"project-a"' in memory
    assert "binary arithmetic" in memory
    path = pathlib.Path("src/payments/service.py")
    content = "from decimal import Decimal\ndef total(): return Decimal('0.3')\n"
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(content, encoding="utf-8")
result = {"state": "completed", "summary": "decimal recovery", "commit_message": "Use decimal arithmetic",
          "dependency_request": None, "clarification_question": None}
if "exec" in args:
    pathlib.Path(args[args.index("-o") + 1]).write_text(json.dumps(result), encoding="utf-8")
    print(json.dumps({"type": "turn.completed"}))
else:
    print(json.dumps({"structured_output": result}))
'''


def test_full_stack_multi_agent_closed_loop(store, repo, external_scratch, bots, channels):
    skill = "# Python slice\nUse trusted ADR decisions and owned paths.\n"
    lock = {"skills": [{"name": "python-slice", "claude_mirror": True,
                        "files": {"SKILL.md": hashlib.sha256(skill.encode()).hexdigest()}}]}
    baseline = repo.source({
        "docs/adr/0001-decimal.md": "---\nid: '0001'\nstatus: accepted\n---\n# Decimal\n## Decision\nUse Decimal for money.\n",
        ".agents/skills/python-slice/SKILL.md": skill, ".claude/skills/python-slice/SKILL.md": skill,
        ".hub/skills.lock.json": json.dumps(lock),
        "tests/test_acceptance.py": "from src.orders.service import total\ndef test_total():\n    assert total() > 0\n",
        ".hub/tests.json": json.dumps({"required": {"orders": ["tests/test_acceptance.py::test_total"],
            "payments": ["tests/test_acceptance.py::test_total"]}, "accepted": [], "pending": []})})
    repo.git.run("update-ref", "refs/heads/epic/v2", baseline)
    repo.base = baseline
    policy = IdentityPolicy([PersonRecord("lead", {"approver"}, scopes={SCOPE}), PersonRecord("carol", {"qa"}, scopes={SCOPE}),
        PersonRecord("alice", {"developer"}, scopes={SCOPE}), PersonRecord("bob", {"developer"}, scopes={SCOPE})],
        {ExternalAccount("feishu", "tenant", "orchestrator", "reviewer-user"): "lead"})
    epic = store.create_epic("v2", "epic/v2", baseline)
    approvals = Approvals(store, secrets.token_bytes(32))
    card = approvals.issue(epic["id"], {"skeleton_sha": baseline, "branch": "epic/v2"}, action="l0")
    orders = new_item(store, owner="alice", epic_id="v2")
    payments = new_item(store, owner="bob", slice_id="payments", epic_id="v2")
    store.add_dependency(payments["id"], orders["id"])
    dispatcher, provider = channels
    drain(dispatcher)
    assert {p["status"] for p in store.get_card_projections(card["approval_id"])} == {"PENDING"}
    ingress = FeishuIngress(approvals, policy, ChannelGate(identity_policy=policy), bots=bots)
    assert ingress.handle_card(feishu_callback(card))["approved"]
    drain(dispatcher)
    assert store.get_epic("v2")["state"] == "ACTIVE"
    assert {p["status"] for p in store.get_card_projections(card["approval_id"])} == {"APPROVED"}
    assert any(method == "PUT" and "dingtalk" in url for method, url, _, _ in provider.calls)
    k = KnowledgeCenter(store, identity_policy=policy)
    script = external_scratch / "closed_loop_cli.py"
    script.write_text(LOOP_CLI, encoding="utf-8")
    workers = {}
    for owner, slice_id, adapter in [("alice", "orders", CodexCliAdapter), ("bob", "payments", ClaudeCliAdapter)]:
        hook = ScopedKnowledgeHook(k, SCOPE, git=repo.git, base_sha_provider=lambda: repo.git.ref("epic/v2"),
                                   slice_id=slice_id, paths=["src/" + slice_id])
        workers[owner] = (Worker(owner, store, repo.git, hook), adapter(executable=sys.executable, extra_args=[str(script)]))
    gate = Gatekeeper(store, repo.git, TrustedPytestRunner(allow_local_execution=True), Approve())
    runtime = HubRuntime(store, repo.git, approvals, gate, workers, dispatcher, k, SCOPE, policy)
    prompts = {orders["id"]: "decimal", payments["id"]: "decimal"}
    first = runtime.tick("epic/v2", task_prompts=prompts)
    assert first["coding"]["state"] == "completed" and "error" not in first["gate"], first
    orders = store.get_item(orders["id"])
    assert orders["state"] == "MERGED_PENDING_QA"
    bug = report(orders["integration_sha"])
    bug["reproduction"].update(expected="exact decimal 0.3", actual="binary rounding drift")
    orders = runtime.submit_qa(orders["id"], orders["version"], orders["integration_sha"], "FAIL", "carol", report=bug)
    assert orders["state"] == "REWORK"
    assert k.list_evidence(SCOPE, item_id=orders["id"])[0]["kind"] == "qa_bug_report"
    assert store.get_item(payments["id"])["state"] == "WAITING"
    second = runtime.tick("epic/v2", task_prompts=prompts)
    assert second["coding"]["state"] == "completed" and "error" not in second["gate"], second
    task = json.loads((external_scratch / "attempts" / second["coding"]["attempt_id"] / "task.json").read_text())
    assert task["rework_feedback"]["report"] == bug
    orders = store.get_item(orders["id"])
    assert task["base_sha"] == first["gate"]["result"]["candidate_sha"]
    with pytest.raises(Forbidden):
        runtime.submit_qa(orders["id"], orders["version"], orders["integration_sha"], "PASS", "alice")
    orders = runtime.submit_qa(orders["id"], orders["version"], orders["integration_sha"], "PASS", "carol",
        candidate_case={"failure_signature": "decimal rounding", "root_cause": "binary arithmetic",
                        "resolution_steps": "use Decimal", "linked_adr_ids": ["0001"]})
    assert orders["state"] == "ACCEPTED"
    case = k.search_cases(SCOPE, "decimal")[0]
    assert promote(k, orders)["case_id"] == case["case_id"]
    assert store.get_item(payments["id"])["state"] == "READY"
    third = runtime.tick("epic/v2", task_prompts=prompts)
    assert third["coding"]["state"] == "completed" and "error" not in third["gate"], third
    assert workers["bob"][0].hook_errors == 0
    payments = store.get_item(payments["id"])
    assert payments["state"] == "MERGED_PENDING_QA"
    runtime.submit_qa(payments["id"], payments["version"], payments["integration_sha"], "PASS", "carol")
    assert store.check_release_ready("v2", repo.git)["state"] == "READY_FOR_RELEASE"
    assert runtime.tick("epic/v2")["coding"] is None
    assert len(k.search_cases(SCOPE, "decimal")) == 1


def test_runtime_changeset_waits_and_aggregate_qa_is_independent(store, repo, external_scratch):
    parent = store.create_item("BUG", "orders", "coordinator", ["src/orders", "src/payments"], change_set_id="pair")
    children = [new_item(store, owner=owner, slice_id=slice_id, parent_id=parent["id"], change_set_id="pair")
                for owner, slice_id in [("alice", "orders"), ("bob", "payments")]]
    k = KnowledgeCenter(store)
    propose(k, parent)
    def coding(directory):
        return WorkResult()  # A committed no-op is valid; the trusted gates still run.
    gate = Gatekeeper(store, repo.git, TrustedPytestRunner(allow_local_execution=True), Approve())
    runtime = HubRuntime(store, repo.git, gatekeeper=gate,
        workers={owner: (Worker(owner, store, repo.git), coding) for owner in ("alice", "bob")}, knowledge=k, scope=SCOPE)
    first = runtime.tick("epic/demo")
    assert first["coding"]["item_id"] == children[0]["id"] and first["gate"] is None
    assert store.get_item(parent["id"])["state"] == "READY"
    second = runtime.tick("epic/demo")
    assert set(second["gate"]["item_ids"]) == {c["id"] for c in children}
    parent = store.get_item(parent["id"])
    assert parent["state"] == "MERGED_PENDING_QA"
    for owner in ("coordinator", "alice", "bob", ""):
        with pytest.raises(Forbidden):
            runtime.submit_qa(parent["id"], parent["version"], parent["integration_sha"], "PASS", owner)
    parent = runtime.submit_qa(parent["id"], parent["version"], parent["integration_sha"], "PASS", "carol")
    assert parent["state"] == "CLOSED"
    assert all(store.get_item(c["id"])["state"] == "ACCEPTED" for c in children)
    assert len(k.search_cases(SCOPE, "decimal")) == 1
    for owner in ("alice", "bob"):
        with pytest.raises(Forbidden):
            k.promote_verified_case(SCOPE, item_id=parent["id"], integration_sha=parent["integration_sha"], qa_reviewer=owner)


def test_runtime_gate_rejection_is_unverified_and_rework_feedback(store, repo, external_scratch):
    from a2a_slice_hub.review import ReviewVerdict
    class Reject:
        def review(self, context):
            return ReviewVerdict(False, "semantic check failed")
    item = new_item(store, owner="alice")
    k = KnowledgeCenter(store)
    gate = Gatekeeper(store, repo.git, TrustedPytestRunner(allow_local_execution=True), Reject())
    runtime = HubRuntime(store, repo.git, gatekeeper=gate,
        workers={"alice": (Worker("alice", store, repo.git), lambda directory: WorkResult())}, knowledge=k, scope=SCOPE)
    result = runtime.tick("epic/demo")
    assert result["gate"]["error"] == "GateRejected"
    assert store.get_item(item["id"])["state"] == "REWORK"
    evidence = k.list_evidence(SCOPE)[0]
    assert evidence["kind"] == "gate_rejected" and evidence["authority"] == "unverified_evidence"
    feedback = runtime.latest_rework_feedback(item["id"])
    assert feedback["event"] == "gate.rejected" and "semantic check failed" in json.dumps(feedback)
    assert not k.search_cases(SCOPE, "")


@pytest.mark.parametrize("outcome", ["ENV_ERROR", "FLAKY"])
def test_runtime_qa_environment_and_flaky_are_never_verified(store, repo, outcome):
    k = KnowledgeCenter(store)
    item = pending(store, repo, new_item(store, owner="alice"))
    propose(k, item)
    runtime = HubRuntime(store, repo.git, knowledge=k, scope=SCOPE)
    result = runtime.submit_qa(item["id"], item["version"], item["integration_sha"], outcome, "carol", report={"observed": "FAIL"})
    assert result["state"] == "MERGED_PENDING_QA"
    assert k.list_evidence(SCOPE)[0]["payload"]["outcome"] == outcome
    assert not k.search_cases(SCOPE, "decimal")


@pytest.mark.parametrize("moved", [False, True])
def test_runtime_checkpoint_resumption_and_moved_baseline(store, repo, external_scratch, moved):
    item = new_item(store)
    script = external_scratch / "checkpoint_cli.py"
    script.write_text(MOCK_CLI, encoding="utf-8")
    worker = Worker(item["owner"], store, repo.git)
    cli = CodexCliAdapter(executable=sys.executable, extra_args=[str(script)], env={"MOCK_MODE": "input-required"})
    runtime = HubRuntime(store, repo.git, workers={item["owner"]: (worker, cli)})
    first = runtime.tick("epic/demo")
    assert first["coding"]["state"] == "input-required"
    # Human clarification is modeled via the existing signed resume path, not a new task state.
    with store.transaction() as conn:
        conn.execute("UPDATE work_items SET state='BLOCKED',blocked_reason='clarification' WHERE id=?", (item["id"],))
    approvals = Approvals(store, secrets.token_bytes(32))
    approvals.approve(approvals.issue(item["id"], {"answer": "continue"}), "lead")
    if moved:
        new_base = repo.source({"src/payments/service.py": "def total(): return 1\n"})
        repo.git.run("update-ref", "refs/heads/epic/demo", new_base)
        def coding(directory, ctx):
            assert ctx.checkpoint_patch is None and ctx.checkpoint_base_sha is None
            assert ctx.base_sha == new_base
            return WorkResult()
        runtime.workers[item["owner"]] = (worker, coding)
    else:
        runtime.workers[item["owner"]] = (worker, CodexCliAdapter(executable=sys.executable, extra_args=[str(script)], env={"MOCK_MODE": "restore"}))
    result = runtime.tick("epic/demo")
    assert result["coding"]["state"] == "completed", result
    if not moved:
        attempt = store.get_attempt(result["coding"]["attempt_id"])
        assert b"return 8" in repo.git.blob(attempt["source_sha"], "src/orders/service.py")
    # A later completed attempt must never recycle a checkpoint from an older input request.
    assert runtime.latest_checkpoint(item["id"], repo.base) == (None, None)


def test_runtime_registered_worker_and_recover(store, repo, external_scratch, monkeypatch):
    from a2a_slice_hub.slices import SliceRegistry
    store.registry = SliceRegistry([dict(slice_id="orders", owner_user_id="alice", backup_user_id="backup",
        owned_paths=["src/orders"], provides_contracts=["orders"], consumes_contracts=[], required_skills=["python"])])
    item = new_item(store, owner="alice")
    assert item["state"] == "WAITING_WORKER"
    store.register_worker("alice-machine", "alice", "http://localhost:9000/a2a", ["python"])
    calls = []
    gate = Gatekeeper(store, repo.git, TrustedPytestRunner(allow_local_execution=True), Approve())
    original = gate.recover
    monkeypatch.setattr(gate, "recover", lambda: (calls.append("recover"), original())[1])
    runtime = HubRuntime(store, repo.git, gatekeeper=gate, workers={"alice-machine":
        (Worker("alice-machine", store, repo.git), lambda directory: WorkResult())})
    result = runtime.tick("epic/demo")
    assert calls == ["recover"] and result["coding"]["state"] == "completed"
    assert store.get_attempt(result["coding"]["attempt_id"])["worker_id"] == "alice-machine"


def test_scope_adr_recall_tracks_trusted_git_removal(store, repo):
    k = KnowledgeCenter(store)
    item = accepted(store, repo)
    propose(k, item, linked_adr_ids=["0001"])
    promote(k, item)
    adr = "---\nid: '0001'\nstatus: accepted\nscope: {slices: [orders]}\n---\n# Money\n## Decision\nUse Decimal.\n"
    base = repo.source({"docs/adr/0001.md": adr})
    hook = ScopedKnowledgeHook(k, SCOPE, git=repo.git, base_sha_provider=lambda: base, slice_id="payments", paths=["src/payments"])
    # Scope mismatch is not ADR deletion: the complete accepted-ID set preserves the case.
    assert hook.recall_experience(task="decimal")["verified_cases"]
    base = repo.source({"docs/adr/0001.md": None}, base)
    assert not hook.recall_experience(task="decimal")["verified_cases"]


def test_schema_round6_failure_rolls_back_and_preserves_v5(store, monkeypatch):
    item = new_item(store)
    with store.connect() as conn:
        for table in ("everos_export_queue", "troubleshooting_cases", "runtime_evidence"):
            conn.execute("DROP TABLE " + table)
        conn.execute("PRAGMA user_version=5")
    migrate = Store.migrate_round6
    def broken(conn):
        migrate(conn)
        raise RuntimeError("injected migration failure")
    monkeypatch.setattr(Store, "migrate_round6", staticmethod(broken))
    with pytest.raises(RuntimeError):
        Store(store.database)
    with store.connect() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 5
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='runtime_evidence'").fetchone()
        assert conn.execute("SELECT id FROM work_items").fetchone()[0] == item["id"]
    monkeypatch.setattr(Store, "migrate_round6", staticmethod(migrate))
    restored = Store(store.database)
    assert restored.get_item(item["id"])["state"] == "READY"


def test_concurrent_promotion_and_export_reservation(store, repo):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    k = KnowledgeCenter(store)
    item = accepted(store, repo)
    propose(k, item)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: promote(k, item), range(2)))
    assert results[0]["case_id"] == results[1]["case_id"]
    entered, release = Event(), Event()
    class Slow:
        def record_trajectory(self, **kwargs):
            entered.set()
            assert release.wait(10)
    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(k.flush_everos_exports, Slow(), scope=SCOPE)
        try:
            assert entered.wait(10)
            assert k.flush_everos_exports(Slow(), scope=SCOPE) == {"exported": 0, "failed": 0}
        finally:
            release.set()
        assert future.result()["exported"] == 1


def test_store_rejects_unsafe_delivery_diagnostics(store):
    class Adapter:
        provider = "feishu"
    MultiOutboxDispatcher(store, {"test": Adapter()})
    new_item(store)
    row = store.claim_delivery("test")
    store.ack_delivery(row["id"], "test", row["claim"], False, error="RuntimeError: https://example.invalid/?token=private")
    with store.connect() as conn:
        assert conn.execute("SELECT last_error FROM outbox_deliveries").fetchone()[0] == "delivery failed"

# Final adversarial audit: real Git/SQLite boundaries, deterministic hostile inputs.
def test_git_adr_invalidations_roundtrip(store, repo):
    from a2a_slice_hub.context import load_adrs
    k = KnowledgeCenter(store)
    item = accepted(store, repo)
    propose(k, item)
    case = promote(k, item)
    base = repo.source({"docs/adr/disable.md":
        "---\nid: disable\nstatus: accepted\ninvalidations: [" + case["case_id"] + "]\nchecks: null\n---\n# Obsolete\n"})
    records = load_adrs(repo.git, base)
    assert records[0]["invalidations"] == [case["case_id"]] and records[0]["checks"] == []
    assert not k.search_cases(SCOPE, "decimal", active_adrs=records)
    hook = ScopedKnowledgeHook(k, SCOPE, git=repo.git, base_sha_provider=lambda: base)
    assert not hook.recall_experience(task="decimal")["verified_cases"]
    assert hook.recall_experience(task="decimal", base_sha=repo.base)["verified_cases"]


@pytest.mark.parametrize("field", ["checks", "invalidations"])
@pytest.mark.parametrize("value", ["true", "42", "[okay, null]", "{case: obsolete}"])
def test_git_adr_rejects_invalid_string_lists(repo, field, value):
    from a2a_slice_hub.context import load_adrs
    base = repo.source({"docs/adr/bad.md": f"---\nid: bad\nstatus: accepted\n{field}: {value}\n---\n# Bad\n"})
    with pytest.raises(GateRejected):
        load_adrs(repo.git, base)


def test_worker_per_attempt_recall_baseline_and_scope(store, repo, external_scratch):
    k = KnowledgeCenter(store)
    item = accepted(store, repo)
    propose(k, item)
    case = promote(k, item)
    base = repo.source({"docs/adr/disable.md": "---\nid: disable\nstatus: accepted\nscope: {slices: [payments]}\n"
        "invalidations: [" + case["case_id"] + "]\n---\n# Retire\n"})
    hook = ScopedKnowledgeHook(k, SCOPE, git=repo.git, base_sha_provider=lambda: repo.base,
                               slice_id="wrong", paths=["wrong"])
    worker = Worker("worker-a", store, repo.git, hook)
    for sha, slice_id, visible in [(repo.base, "payments", True), (base, "orders", True), (base, "payments", False)]:
        work = new_item(store, slice_id=slice_id)
        attempt = store.claim(work["id"], work["owner"], 0)
        def coding(directory, ctx):
            assert bool(ctx.recalled_experience["verified_cases"]) is visible
            return WorkResult("failed")
        worker.execute(attempt["id"], sha, "decimal", coding)
    assert worker.hook_errors == 0


@pytest.mark.parametrize("kind", ["legacy", "kwargs", "partial", "opaque"])
def test_worker_legacy_hook_signature_is_preserved(store, repo, kind, monkeypatch):
    calls = []
    class Hook:
        def recall_experience(self, *, task):
            calls.append({"task": task})
    hook = Hook()
    if kind == "kwargs":
        def recall(**kwargs):
            calls.append(kwargs)
        hook.recall_experience = recall
    elif kind == "partial":
        def recall(*, task, paths=None):
            calls.append({"task": task, "paths": paths})
        hook.recall_experience = recall
    elif kind == "opaque":
        hook.recall_experience.__func__.__signature__ = "opaque"
    work = new_item(store)
    attempt = store.claim(work["id"], work["owner"], 0)
    worker = Worker(work["owner"], store, repo.git, hook)
    worker.execute(attempt["id"], repo.base, "task", lambda directory: WorkResult("failed"))
    assert calls == [{"task": "task", **({"paths": ["src/orders"]} if kind == "partial" else {})}]
    assert worker.hook_errors == 0


def test_declarative_identity_policy_and_legacy_config(tmp_path, monkeypatch):
    import yaml
    import a2a_slice_hub.config as module
    from a2a_slice_hub.channels import DingTalkCardIngress
    captured = {}
    def app(*args, **kwargs):
        captured.update(kwargs)
        return kwargs
    monkeypatch.setattr(module, "create_app", app)
    monkeypatch.setenv("AUDIT_BEARER", secrets.token_urlsafe(32))
    monkeypatch.setenv("AUDIT_APPROVAL", secrets.token_urlsafe(32))
    config = {"database": "hub.sqlite", "approval_key_env": "AUDIT_APPROVAL",
        "principals": [{"name": "coordinator", "role": "coordinator", "credential_env": "AUDIT_BEARER"}]}
    filename = tmp_path / "hub.yaml"
    filename.write_text(yaml.safe_dump(config), encoding="utf-8")
    assert module.load_app(filename)["identity_policy"] is None
    account = asdict(ExternalAccount("feishu", "tenant", "orchestrator", "reviewer"))
    config.update(identity_policy={"persons": [{"person_id": "reviewer", "roles": ["qa", "approver"],
        "scopes": [asdict(SCOPE)]}], "bindings": [{"account": account, "person_id": "reviewer"}],
        "contacts": [{"account": account, "admission": "pairing"}]},
        feishu_card_ingress={"orchestrator_app_id": "orchestrator"}, dingtalk_card_ingress={"app_id": "robot"})
    filename.write_text(yaml.safe_dump(config), encoding="utf-8")
    result = module.load_app(filename)
    policy = result["identity_policy"]
    assert policy.resolve_person(ExternalAccount(**account)).scopes == {SCOPE}
    assert policy.can_contact(ExternalAccount(**account))
    assert isinstance(result["feishu_card_ingress"], FeishuIngress)
    assert isinstance(result["dingtalk_card_ingress"], DingTalkCardIngress)
    assert result["feishu_card_ingress"].identity_policy is policy
    config["identity_policy"]["bindings"].append(config["identity_policy"]["bindings"][0])
    filename.write_text(yaml.safe_dump(config), encoding="utf-8")
    with pytest.raises(ValueError):
        module.load_app(filename)


def test_memory_visibility_is_a_real_storage_boundary(store, repo):
    private = MemoryScope(SCOPE.org_id, SCOPE.project_id, SCOPE.repo_id, "private")
    k = KnowledgeCenter(store)
    item = accepted(store, repo)
    propose(k, item)
    public_case = promote(k, item)
    k.record_evidence(SCOPE, kind="env_error", summary="project-only")
    assert not k.list_evidence(private) and not k.search_cases(private, "decimal")
    assert k.prune_evidence(private) == 0
    with pytest.raises(NotFound):
        k.invalidate_case(private, public_case["case_id"], "wrong visibility")
    assert k.flush_everos_exports(None, scope=private) == {"exported": 0, "failed": 0}
    propose(k, item, scope=private)
    private_case = k.promote_verified_case(private, item_id=item["id"], integration_sha=item["integration_sha"], qa_reviewer="carol")
    assert private_case["case_id"] != public_case["case_id"]
    assert [c["case_id"] for c in k.search_cases(private, "decimal")] == [private_case["case_id"]]
    guarded = KnowledgeCenter(store, identity_policy=IdentityPolicy([PersonRecord("reader", {"developer"}, scopes={private})]), principal="reader")
    assert [c["case_id"] for c in guarded.search_cases(private, "decimal")] == [private_case["case_id"]]


@pytest.mark.parametrize("failure", ["proposal", "promotion", "evidence"])
def test_runtime_qa_and_knowledge_roll_back_together(store, repo, monkeypatch, failure):
    k = KnowledgeCenter(store)
    item = pending(store, repo, new_item(store, owner="alice"))
    runtime = HubRuntime(store, repo.git, knowledge=k, scope=SCOPE)
    method = {"proposal": "propose_case", "promotion": "promote_verified_case", "evidence": "record_evidence"}[failure]
    original = getattr(k, method)
    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise ValueError("injected failure after knowledge write")
    monkeypatch.setattr(k, method, fail)
    candidate = {"failure_signature": "decimal", "root_cause": "binary", "resolution_steps": "Decimal"}
    outcome = "ENV_ERROR" if failure == "evidence" else "PASS"
    with store.connect() as conn:
        count = conn.execute("SELECT count(*) FROM outbox").fetchone()[0]
    with pytest.raises(ValueError):
        runtime.submit_qa(item["id"], item["version"], item["integration_sha"], outcome, "carol", candidate_case=candidate)
    assert store.get_item(item["id"]) == item
    with store.connect() as conn:
        assert conn.execute("SELECT count(*) FROM outbox").fetchone()[0] == count
        for table in ("troubleshooting_cases", "runtime_evidence", "everos_export_queue"):
            assert conn.execute("SELECT count(*) FROM " + table).fetchone()[0] == 0
    monkeypatch.setattr(k, method, original)
    assert runtime.submit_qa(item["id"], item["version"], item["integration_sha"], "PASS", "carol", candidate_case=candidate)["state"] == "ACCEPTED"


@pytest.mark.parametrize("key,value", [("header", None), ("event", []), ("operator", None),
    ("action", None), ("approval_id", []), ("expected_state_version", True), ("expires_at", True), ("receipt", "x" * 513)])
def test_feishu_malformed_cards_fail_closed(store, key, value):
    from test_milestone_b_dual_im import blocked_card
    approvals, card, item = blocked_card(store)
    policy = IdentityPolicy([PersonRecord("reviewer", {"approver"})],
        {ExternalAccount("feishu", "tenant", "orchestrator", "reviewer-user"): "reviewer"})
    ingress = FeishuIngress(approvals, policy, orchestrator_app_id="orchestrator")
    body = feishu_callback(card)
    if key in {"header", "event"}:
        body[key] = value
    elif key in {"operator", "action"}:
        body["event"][key] = value
    elif key == "receipt":
        body["header"]["event_id"] = value
    else:
        body["event"]["action"]["value"][key] = value
    with pytest.raises((ValueError, Forbidden)):
        ingress.handle_card(body)
    assert store.get_item(item["id"])["state"] == "BLOCKED"
    with store.transaction() as conn:
        assert conn.execute("SELECT count(*) FROM inbox").fetchone()[0] == 0


@pytest.mark.parametrize("provider", ["feishu", "dingtalk"])
@pytest.mark.parametrize("body", [None, [], True, {"content": None}, {"header": None, "content": []}])
def test_malformed_http_cards_return_client_errors(store, clock, provider, body):
    from a2a_slice_hub.api import Principal, create_app
    from a2a_slice_hub.channels import DingTalkCardIngress
    from asgi_client import ASGITestClient
    token, key = secrets.token_urlsafe(32), secrets.token_hex(16)
    approvals, policy = Approvals(store, secrets.token_bytes(32)), IdentityPolicy()
    app = create_app(store, approvals, {token: Principal("gateway", "im_gateway")}, feishu_encrypt_key=key,
        identity_policy=policy, feishu_card_ingress=FeishuIngress(approvals, policy, orchestrator_app_id="orchestrator"),
        dingtalk_card_ingress=DingTalkCardIngress(approvals, policy, app_id="robot"))
    raw = json.dumps(body).encode()
    timestamp, nonce = str(int(clock())), "audit"
    headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json",
        "x-lark-request-timestamp": timestamp, "x-lark-request-nonce": nonce,
        "x-lark-signature": hashlib.sha256((timestamp + nonce + key).encode() + raw).hexdigest()}
    with ASGITestClient(app) as client:
        assert client.post("/im/" + provider + "/cards", content=raw, headers=headers).status_code in {403, 422}


def test_canonical_policy_cannot_be_bypassed_by_bearer_alias(store, repo):
    from a2a_slice_hub.api import Principal, create_app
    from asgi_client import ASGITestClient
    from test_milestone_b_dual_im import blocked_card
    approvals, card, blocked = blocked_card(store)
    item = pending(store, repo, new_item(store, owner="alice"))
    token_a, token_q = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    policy = IdentityPolicy([PersonRecord("alice", {"approver", "qa"}), PersonRecord("visitor", set())])
    for canonical in ("alice", "visitor"):
        app = create_app(store, approvals, {token_a: Principal("alias", "approver", canonical),
            token_q: Principal("alias", "qa", canonical)}, identity_policy=policy)
        with ASGITestClient(app) as client:
            assert client.post("/hub/approvals/accept", json={"card": card}, headers={"Authorization": "Bearer " + token_a}).status_code == 403
            response = client.post("/hub/items/" + item["id"] + "/qa", json={"expected_version": item["version"],
                "integration_sha": item["integration_sha"], "passed": True}, headers={"Authorization": "Bearer " + token_q})
            assert response.status_code == 403
    assert store.get_item(blocked["id"])["state"] == "BLOCKED"
    assert store.get_item(item["id"])["state"] == "MERGED_PENDING_QA"


@pytest.mark.parametrize("manifest", [None, '{"skills":[],"skills":[]}', '{"skills":{"x":{"name":"other","files":{}}}}'])
def test_skills_lock_missing_or_ambiguous_is_rejected(repo, manifest):
    from a2a_slice_hub.context import verify_skills_lock
    files = {".agents/skills/example/SKILL.md": "trusted package\n"}
    if manifest is not None:
        files[".hub/skills.lock.json"] = manifest
    sha = repo.source(files)
    with pytest.raises(GateRejected):
        verify_skills_lock(repo.git, sha)


@pytest.mark.parametrize("ttl,tries", [(True, 1), (float("nan"), 1), (float("inf"), 1), (1, True), (1, 1.5)])
def test_delivery_retry_parameters_are_strict(store, ttl, tries):
    with pytest.raises(ValueError):
        store.claim_delivery("test", ttl=ttl, max_tries=tries)
    with store.transaction():
        pass


def test_expired_dispatch_ack_does_not_starve_other_destinations(store, clock):
    calls = []
    class Slow:
        provider = "feishu"
        def send(self, event_id, topic, payload):
            calls.append("slow")
            clock.now += 31
            return "slow-receipt"
    class Fast:
        provider = "dingtalk"
        def send(self, event_id, topic, payload):
            calls.append("fast")
            return "fast-receipt"
    dispatcher = MultiOutboxDispatcher(store, {"slow": Slow(), "fast": Fast()})
    new_item(store)
    assert dispatcher.once() and calls == ["slow", "fast"]
    with store.connect() as conn:
        rows = {r["destination_id"]: dict(r) for r in conn.execute("SELECT * FROM outbox_deliveries")}
    assert rows["slow"]["delivered"] == 0 and rows["fast"]["delivered"] == 1


def test_card_projection_cannot_change_provider_or_receipt(store):
    from test_milestone_b_dual_im import blocked_card
    _, card, _ = blocked_card(store)
    store.register_destination("main", "feishu")
    key = card["approval_id"]
    with pytest.raises(Conflict):
        store.record_card_projection(key, "main", "dingtalk", "foreign")
    assert store.record_card_projection(key, "main", "feishu", "original")
    assert not store.record_card_projection(key, "main", "feishu", "replacement", rendered_version=2, status="APPROVED")
    assert store.get_card_projections(key)[0]["provider_card_id"] == "original"
    assert store.record_card_projection(key, "legacy", "feishu", "legacy-receipt")
    with pytest.raises(Conflict):
        store.register_destination("legacy", "dingtalk")
    store.register_destination("legacy", "feishu")


@pytest.mark.parametrize("value", [True, float("inf"), float("nan")])
def test_cli_timeout_requires_finite_number(value):
    with pytest.raises(ValueError):
        CodexCliAdapter(timeout_seconds=value)


@pytest.mark.parametrize("name", ["SERVICE_API_KEY", "SERVICE_APIKEY"])
def test_cli_environment_strips_api_key_credentials(name, monkeypatch):
    monkeypatch.setenv(name, "sensitive-fixture")
    assert name not in CodexCliAdapter().environment()
    assert CodexCliAdapter(allowed_env_keys=[name]).environment()[name] == "sensitive-fixture"


def _audit_directory_link(link, target):
    import os
    import subprocess
    if os.name == "nt":
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                       check=True, capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
    else:
        link.symlink_to(target, target_is_directory=True)


def test_checkpoint_rejects_in_root_parent_links(store, repo, external_scratch):
    import os
    from a2a_slice_hub.context import external_directory
    item = new_item(store)
    attempt = store.claim(item["id"], item["owner"], 0)
    with store.transaction() as conn:
        conn.execute("UPDATE attempts SET artifact=? WHERE id=?", (json.dumps({
            "checkpoint_saved": True, "checkpoint_base_sha": repo.base}), attempt["id"]))
    attempts = external_scratch / "attempts"
    attempts.mkdir()
    other = attempts / "other-attempt"
    other.mkdir()
    (other / "checkpoint.patch").write_bytes(b"foreign checkpoint")
    link = attempts / attempt["id"]
    _audit_directory_link(link, other)
    try:
        assert HubRuntime(store, repo.git).latest_checkpoint(item["id"], repo.base) == (None, None)
        with pytest.raises(GateRejected):
            external_directory(link)
    finally:
        if os.name == "nt":
            os.rmdir(link)
        else:
            link.unlink()


@pytest.mark.parametrize("artifact", [None, [], True, {"checkpoint_saved": 1}, {"checkpoint_saved": "true"}])
def test_checkpoint_malformed_metadata_is_ignored(store, repo, external_scratch, artifact):
    item = new_item(store)
    attempt = store.claim(item["id"], item["owner"], 0)
    with store.transaction() as conn:
        conn.execute("UPDATE attempts SET artifact=? WHERE id=?", (json.dumps(artifact), attempt["id"]))
    assert HubRuntime(store, repo.git).latest_checkpoint(item["id"], repo.base) == (None, None)


def test_cli_cannot_overwrite_hardlinked_artifacts(repo, external_scratch):
    from test_milestone_a_cli_governance import execution
    victim = external_scratch / "untouched.txt"
    victim.write_text("do not overwrite", encoding="utf-8")
    script = external_scratch / "link_cli.py"
    script.write_text('import os, pathlib, sys\n'
        'output = pathlib.Path(sys.argv[sys.argv.index("-o") + 1])\n'
        'os.link(os.environ["AUDIT_VICTIM"], output.parent / "stderr.log")\n' + MOCK_CLI, encoding="utf-8")
    with repo.git.worktree(repo.base) as directory:
        ctx = execution(repo, directory, external_scratch)
        adapter = CodexCliAdapter(executable=sys.executable, extra_args=[str(script)], env={"AUDIT_VICTIM": str(victim)})
        with pytest.raises(GateRejected):
            adapter(directory, ctx)
    assert victim.read_text(encoding="utf-8") == "do not overwrite"


def test_visibility_migration_preserves_early_v6_cases_and_exports(store, repo):
    from a2a_slice_hub.knowledge import SCHEMA_V6
    k = KnowledgeCenter(store)
    item = accepted(store, repo)
    propose(k, item)
    case = promote(k, item)
    k.record_evidence(SCOPE, kind="env_error", summary="legacy evidence")
    # Reconstruct the early v6 layout without visibility; retain the export FK.
    with store.connect() as conn:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("BEGIN IMMEDIATE")
        for table in ("runtime_evidence", "troubleshooting_cases"):
            definition = next(p for p in SCHEMA_V6.split(";") if "CREATE TABLE IF NOT EXISTS " + table + " (" in p)
            definition = definition.replace(" visibility TEXT NOT NULL DEFAULT 'project',", "")
            definition = definition.replace("repo_id, visibility, item_id", "repo_id, item_id")
            conn.execute(definition.replace(table + " (", table + "_legacy ("))
            columns = ",".join(r[1] for r in conn.execute("PRAGMA table_info(" + table + ")") if r[1] != "visibility")
            conn.execute(f"INSERT INTO {table}_legacy({columns}) SELECT {columns} FROM {table}")
            conn.execute("DROP TABLE " + table)
            conn.execute(f"ALTER TABLE {table}_legacy RENAME TO {table}")
        conn.commit()
    reopened = Store(store.database, clock=store.clock)
    k = KnowledgeCenter(reopened)
    assert k.search_cases(SCOPE, "decimal")[0]["case_id"] == case["case_id"]
    assert k.list_evidence(SCOPE)[0]["summary"] == "legacy evidence"
    with reopened.connect() as conn:
        assert not conn.execute("PRAGMA foreign_key_check").fetchall()
        assert conn.execute("SELECT case_id FROM everos_export_queue").fetchone()[0] == case["case_id"]


@pytest.mark.parametrize("fails", [False, True])
def test_export_invalidation_in_flight_is_fenced_and_not_counted(store, repo, fails):
    k = KnowledgeCenter(store)
    item = accepted(store, repo)
    propose(k, item)
    case = promote(k, item)
    class InFlight:
        def record_trajectory(self, *, messages):
            k.invalidate_case(SCOPE, case["case_id"], "retired while exporting")
            if fails:
                raise OSError("provider failed")
    assert k.flush_everos_exports(InFlight(), scope=SCOPE) == {"exported": 0, "failed": 0}
    with store.transaction() as conn:
        assert conn.execute("SELECT count(*) FROM everos_export_queue").fetchone()[0] == 0


def test_task_bundle_writer_rejects_hardlinks(repo, external_scratch):
    import os
    from a2a_slice_hub.context import write_task_bundle
    from test_milestone_a_cli_governance import bundle_for
    victim = external_scratch / "other-data.txt"
    victim.write_text("unchanged", encoding="utf-8")
    with repo.git.worktree(repo.base) as directory:
        bundle = bundle_for(repo, directory)
        target = external_scratch / "attempts" / bundle.attempt_id / "task.json"
        target.parent.mkdir(parents=True)
        os.link(victim, target)
        with pytest.raises(GateRejected):
            write_task_bundle(external_scratch, bundle)
    assert victim.read_text(encoding="utf-8") == "unchanged"


def test_sqlite_connection_setup_failure_always_closes(store, monkeypatch):
    import sqlite3
    opened, closed = [], []
    original = sqlite3.connect
    class FailingConnection(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            if sql == "PRAGMA synchronous=FULL":
                raise sqlite3.OperationalError("injected connection setup failure")
            return super().execute(sql, *args, **kwargs)
        def close(self):
            closed.append(self)
            return super().close()
    def connect(*args, **kwargs):
        conn = original(*args, **kwargs, factory=FailingConnection)
        opened.append(conn)
        return conn
    monkeypatch.setattr(sqlite3, "connect", connect)
    with pytest.raises(sqlite3.OperationalError):
        with store.connect():
            pytest.fail("setup must not yield a connection")
    assert closed == opened and len(closed) == 1
    with pytest.raises(sqlite3.ProgrammingError):
        closed[0].execute("SELECT 1")


def test_runtime_allows_separate_store_handles_to_same_database(store, repo, tmp_path):
    shared = Store(store.database, clock=store.clock)
    assert HubRuntime(store, repo.git, knowledge=KnowledgeCenter(shared), scope=SCOPE).knowledge.store is shared
    different = Store(tmp_path / "separate.sqlite", clock=store.clock)
    with pytest.raises(ValueError):
        HubRuntime(store, repo.git, knowledge=KnowledgeCenter(different), scope=SCOPE)


@pytest.mark.parametrize("provider", ["feishu", "dingtalk", "a2a"])
@pytest.mark.parametrize("attack", ["deep", "large"])
def test_http_json_depth_and_stream_size_are_bounded(store, clock, provider, attack):
    from a2a_slice_hub.api import Principal, create_app
    from a2a_slice_hub.channels import DingTalkCardIngress
    from asgi_client import ASGITestClient
    token, key = secrets.token_urlsafe(32), secrets.token_hex(16)
    approvals, policy = Approvals(store, secrets.token_bytes(32)), IdentityPolicy()
    app = create_app(store, approvals, {token: Principal("gateway", "im_gateway")}, feishu_encrypt_key=key,
        identity_policy=policy, feishu_card_ingress=FeishuIngress(approvals, policy, orchestrator_app_id="orchestrator"),
        dingtalk_card_ingress=DingTalkCardIngress(approvals, policy, app_id="robot"))
    raw = b'[' * 3000 + b'0' + b']' * 3000 if attack == "deep" else b'"' + b'x' * 1_000_001 + b'"'
    timestamp, nonce = str(int(clock())), "audit-bound"
    headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json",
        "x-lark-request-timestamp": timestamp, "x-lark-request-nonce": nonce,
        "x-lark-signature": hashlib.sha256((timestamp + nonce + key).encode() + raw).hexdigest()}
    path = "/a2a" if provider == "a2a" else "/im/" + provider + "/cards"
    with ASGITestClient(app) as client:
        response = client.post(path, content=raw, headers=headers)
        if provider == "a2a":
            assert response.status_code == 200 and "error" in response.json()
        else:
            assert response.status_code == (422 if attack == "deep" else 413)
    with store.transaction() as conn:
        assert conn.execute("SELECT count(*) FROM inbox").fetchone()[0] == 0
