"""Milestone B: real SQLite/Git boundaries, mock provider HTTP (no live credentials)."""
from dataclasses import replace
import hashlib
import json
import secrets
import sqlite3
import sys
import time
from contextlib import closing

import httpx
import pytest

from a2a_slice_hub.api import Principal, create_app
from a2a_slice_hub.approvals import Approvals
from a2a_slice_hub.channels import (ChannelGate, ChatEnvelope, DingTalkCardIngress,
    DingTalkChannelAdapter, DingTalkChatIngress, FeishuChannelAdapter, FeishuIngress, MultiOutboxDispatcher)
from a2a_slice_hub.coding import CodexCliAdapter, ClaudeCliAdapter
from a2a_slice_hub.errors import Conflict, Forbidden, GateRejected
from a2a_slice_hub.im import BotIdentity, OutboxDispatcher
from a2a_slice_hub.policy import ExternalAccount, IdentityPolicy, PersonRecord
from a2a_slice_hub.store import Store
from a2a_slice_hub.worker import Worker, WorkResult
from conftest import new_item
from test_milestone_a_cli_governance import MOCK_CLI, execution, external_scratch


@pytest.mark.parametrize("key", ["FEISHU_ENCRYPT_KEY", "DB_PASSWORD", "SERVICE_PRIVATE_KEY", "HUB_CREDENTIAL", "HUB_TOKEN", "API_SECRET"])
def test_environment_scrubs_extended_secrets(key, monkeypatch):
    monkeypatch.setenv(key, "fixture-sensitive")
    assert key not in CodexCliAdapter().environment()
    assert CodexCliAdapter(allowed_env_keys=[key.lower()]).environment()[key] == "fixture-sensitive"
    assert key not in CodexCliAdapter(env={key: "override"}).environment()


def test_download_wrapper_is_preserved():
    assert CodexCliAdapter(env={"HUB_DOWNLOAD_WRAPPER": "download-wrapper"}).environment()["HUB_DOWNLOAD_WRAPPER"] == "download-wrapper"


@pytest.mark.parametrize("adapter", [CodexCliAdapter, ClaudeCliAdapter])
@pytest.mark.parametrize("state", ["completed", "input-required"])
@pytest.mark.parametrize("foreign", [False, True])
def test_ignored_cli_caches_vs_foreign_files(repo, external_scratch, adapter, state, foreign):
    repo.base = repo.source({".gitignore": "__pycache__/\n.pytest_cache/\n"})
    script = external_scratch / "cache_cli.py"
    cache = '''
import pathlib
for name in ("__pycache__/mod.pyc", ".pytest_cache/v/cache"):
    p = pathlib.Path(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("cache")
'''
    if foreign:
        cache += 'pathlib.Path("foreign.txt").write_text("foreign")\n'
    script.write_text(cache + MOCK_CLI, encoding="utf-8")
    cli = adapter(executable=sys.executable, extra_args=[str(script)], env={"MOCK_MODE": state})
    with repo.git.worktree(repo.base) as directory:
        ctx = execution(repo, directory, external_scratch)
        if foreign:
            with pytest.raises(GateRejected):
                cli(directory, ctx)
        else:
            result = cli(directory, ctx)
            assert result.state == state
            assert not (directory / "__pycache__").exists()
            assert not (directory / ".pytest_cache").exists()
            assert repo.git.text("status", "--porcelain", cwd=directory) == ""
            assert not repo.git.run("ls-files", "--others", "-z", cwd=directory).stdout
            if state == "input-required":
                assert b"src/orders/service.py" in (ctx.attempt_dir / "checkpoint.patch").read_bytes()


def test_worker_forwards_rework_feedback(store, repo, external_scratch):
    item = new_item(store)
    attempt = store.claim(item["id"], item["owner"], 0)
    feedback = {"reason": "fix rounding", "required_checks": ["unit"]}
    def coding(directory, ctx):
        assert ctx.task_bundle.rework_feedback == feedback
        return WorkResult("input-required")
    assert Worker(item["owner"], store, repo.git).execute(attempt["id"], repo.base, "Task", coding,
        rework_feedback=feedback)["state"] == "input-required"


@pytest.fixture
def identity():
    people = [PersonRecord("reviewer", {"approver"}), PersonRecord("alice", {"approver", "developer"}),
              PersonRecord("paired", {"developer"})]
    bindings = {ExternalAccount(provider, tenant, app, user): person for provider, tenant, app in
                [("feishu", "tenant", "orchestrator"), ("dingtalk", "corp", "robot")]
                for user, person in [("reviewer-user", "reviewer"), ("alice-user", "alice"), ("paired-user", "paired")]}
    return IdentityPolicy(people, bindings, contacts={account: "pairing" for account in bindings})


@pytest.fixture
def bots():
    return {name: BotIdentity(app, lambda: "fixture-token") for name, app in
            [("orchestrator_bot", "orchestrator"), ("dev_feed_bot", "dev-feed"), ("qa_feed_bot", "qa-feed")]}


def blocked_card(store):
    item = new_item(store, owner="alice")
    with store.transaction() as conn:
        conn.execute("UPDATE work_items SET state='BLOCKED',blocked_reason='needs review' WHERE id=?", (item["id"],))
    approvals = Approvals(store, secrets.token_bytes(32))
    return approvals, approvals.issue(item["id"], {"reason": "resume"}), item


def feishu_callback(card, user="reviewer-user", event_id="feishu-click"):
    return {"header": {"app_id": "orchestrator", "tenant_key": "tenant", "event_id": event_id,
                       "event_type": "p2.card.action.trigger"},
            "event": {"operator": {"open_id": user}, "action": {"value": card}}}


def ding_callback(card, user="reviewer-user", event_id="ding-click"):
    return {"corpId": "corp", "userId": user, "eventId": event_id,
            "outTrackId": "approval-" + card["approval_id"] + "-dingtalk-main",
            "cardPrivateData": {"params": {"card": json.dumps(card)}}}


class ProviderHTTP:
    def __init__(self):
        self.calls = []
        self.fail_ding = False
        self.during_create = None

    def __call__(self, request):
        body = json.loads(request.content)
        self.calls.append((request.method, str(request.url), body, dict(request.headers)))
        if self.fail_ding and request.url.host == "api.dingtalk.com":
            return httpx.Response(503, json={"error": "provider unavailable"})
        if request.url.path.endswith("createAndDeliver"):
            if self.during_create:
                self.during_create()
            return httpx.Response(200, json={"outTrackId": body["outTrackId"]})
        if request.url.path.endswith("/messages"):
            return httpx.Response(200, json={"code": 0, "data": {"message_id": "message-" + body["uuid"]}})
        if request.url.path.endswith("groupMessages/send"):
            return httpx.Response(200, json={"processQueryKey": "query"})
        return httpx.Response(200, json={"code": 0})


@pytest.fixture
def channels(store, bots):
    provider = ProviderHTTP()
    with httpx.Client(transport=httpx.MockTransport(provider)) as client:
        adapters = {"feishu-main": FeishuChannelAdapter(bots, "chat", client, store=store, destination_id="feishu-main"),
                    "dingtalk-main": DingTalkChannelAdapter(client, token=lambda: "fixture-token", robot_code="robot",
                        conversation_id="conversation", card_template_id="approval-template", store=store, destination_id="dingtalk-main")}
        yield MultiOutboxDispatcher(store, adapters, max_tries=2), provider


def drain(dispatcher, destination=None):
    for _ in range(100):
        if not dispatcher.once(destination):
            return
    pytest.fail("outbox failed to drain")


@pytest.mark.parametrize("first", ["feishu", "dingtalk"])
def test_shared_approval_cross_platform_sync_and_restart(store, identity, bots, channels, first, monkeypatch):
    dispatcher, provider = channels
    approvals, card, item = blocked_card(store)
    drain(dispatcher)
    projections = store.get_card_projections(card["approval_id"])
    assert len(projections) == 2 and all(p["rendered_version"] == 1 for p in projections)
    feishu = FeishuIngress(approvals, identity, bots=bots)
    ding = DingTalkCardIngress(approvals, identity, app_id="robot")
    callback = feishu_callback(card) if first == "feishu" else ding_callback(card)
    ingress = feishu if first == "feishu" else ding
    assert ingress.handle_card(callback) == {"approved": True, "duplicate": False}
    version = store.get_item(item["id"])["version"]
    drain(dispatcher)
    assert all(p["status"] == "APPROVED" and p["rendered_version"] == 2 and p["approved_by"] == "reviewer"
               for p in store.get_card_projections(card["approval_id"]))
    updates = [c for c in provider.calls if c[0] in {"PATCH", "PUT"}]
    assert {c[0] for c in updates} == {"PATCH", "PUT"}
    fbody = json.loads(next(c[2]["content"] for c in updates if c[0] == "PATCH"))
    assert "Approved by reviewer" in json.dumps(fbody) and "button" not in json.dumps(fbody)
    dbody = next(c[2]["cardData"]["cardParamMap"] for c in updates if c[0] == "PUT")
    assert dbody["showApprove"] == "false" and dbody["card"] == ""
    other = ding if first == "feishu" else feishu
    assert other.handle_card(ding_callback(card) if first == "feishu" else feishu_callback(card)) == {"approved": True, "duplicate": True}
    assert store.get_item(item["id"])["version"] == version
    def no_approval(*args, **kwargs):
        pytest.fail("restart replay must use persisted inbox")
    monkeypatch.setattr(approvals, "approve", no_approval)
    restarted = FeishuIngress(approvals, identity, bots=bots) if first == "feishu" else DingTalkCardIngress(approvals, identity, app_id="robot")
    assert restarted.handle_card(callback) == {"approved": True, "duplicate": False}
    with store.connect() as conn:
        accepted = [json.loads(r[0]) for r in conn.execute("SELECT payload FROM outbox WHERE topic='approval.accepted'")]
        assert len(accepted) == 1
        assert accepted[0]["approval_id"] == card["approval_id"]
        assert accepted[0]["action"] == "resume" and accepted[0]["expected_state_version"] == card["expected_state_version"]


@pytest.mark.parametrize("user", ["alice-user", "paired-user"])
@pytest.mark.parametrize("provider", ["feishu", "dingtalk"])
def test_cross_platform_self_approval_and_contact_only_forbidden(store, identity, bots, user, provider):
    approvals, card, item = blocked_card(store)
    ingress = FeishuIngress(approvals, identity, bots=bots) if provider == "feishu" else DingTalkCardIngress(approvals, identity, app_id="robot")
    with pytest.raises(Forbidden):
        ingress.handle_card(feishu_callback(card, user) if provider == "feishu" else ding_callback(card, user))
    assert store.get_item(item["id"])["state"] == "BLOCKED"


def test_channel_gates_and_topology(store, identity, bots):
    gate = ChannelGate(dm_policy="pairing", identity_policy=identity, group_allow_from={"group"}, bot_ids={"echo"})
    envelope = ChatEnvelope("feishu", "tenant", "orchestrator", "dm", "dm", "paired-user", "msg", "hello")
    assert gate.evaluate(envelope).allowed and gate.evaluate(envelope).person_id == "paired"
    assert not ChannelGate().evaluate(envelope).allowed
    assert ChannelGate(dm_policy="allowlist", dm_allow_from={"paired-user"}).evaluate(envelope).allowed
    assert not gate.evaluate(replace(envelope, is_bot=True)).allowed
    assert not gate.evaluate(replace(envelope, sender_id="echo")).allowed
    assert not gate.evaluate(replace(envelope, chat_type="group", chat_id="group", mentions_all=True)).allowed
    assert gate.evaluate(replace(envelope, chat_type="group", chat_id="group", mentions_bot=True)).allowed
    approvals, card, _ = blocked_card(store)
    ingress = FeishuIngress(approvals, identity, gate, bots=bots, bot_open_id="bot-open")
    event = {"header": {"app_id": "orchestrator", "tenant_key": "tenant"}, "event": {
        "sender": {"sender_id": {"open_id": "paired-user"}, "sender_type": "user"},
        "message": {"chat_type": "group", "chat_id": "group", "message_id": "msg", "content": '{"text":"@_all hello"}',
                    "mentions": [{"key": "@_all", "id": {"open_id": "all"}}]}}}
    assert not ingress.handle_chat(event).allowed
    event["event"]["message"]["mentions"].append({"key": "@_user_1", "id": {"open_id": "bot-open"}})
    assert ingress.handle_chat(event).allowed
    for app in ("dev-feed", "qa-feed"):
        event["header"]["app_id"] = app
        assert not ingress.handle_chat(event).allowed
        body = feishu_callback(card)
        body["header"]["app_id"] = app
        with pytest.raises(Forbidden):
            ingress.handle_card(body)
    ding = DingTalkChatIngress(gate, app_id="robot", chatbot_user_id="bot-open")
    body = {"senderStaffId": "paired-user", "senderCorpId": "corp", "robotCode": "robot", "conversationType": "2",
        "conversationId": "group", "msgId": "msg", "text": {"content": "all"}, "isAtAll": True,
        "atUsers": [{"dingtalkId": "all"}]}
    assert not ding.handle_chat(body).allowed
    body["atUsers"].append({"dingtalkId": "bot-open"})
    assert ding.handle_chat(body).allowed
    body["atUsers"] = []
    body["isInAtList"] = True
    assert not ding.handle_chat(body).allowed
    body["isAtAll"] = False
    assert ding.handle_chat(body).allowed
    body["isBot"] = True
    assert not ding.handle_chat(body).allowed


def test_independent_dlq_trigger_fanout_replay_and_prune(store, clock, channels):
    dispatcher, provider = channels
    item = new_item(store, owner="alice")
    drain(dispatcher)
    with store.transaction() as conn:
        conn.execute("UPDATE work_items SET state='BLOCKED' WHERE id=?", (item["id"],))
        event = conn.execute("SELECT id FROM outbox WHERE topic='item.escalated'").fetchone()[0]
        assert conn.execute("SELECT count(*) FROM outbox_deliveries WHERE event_id=?", (event,)).fetchone()[0] == 2
    provider.calls.clear()
    provider.fail_ding = True
    drain(dispatcher)
    clock.now += 6
    drain(dispatcher)
    letters = store.destination_dead_letters("dingtalk-main")
    assert len(letters) == 1 and letters[0]["event_id"] == event and letters[0]["tries"] == 2
    assert store.destination_dead_letters("feishu-main") == []
    assert len([c for c in provider.calls if "feishu.cn" in c[1]]) == 1
    with store.transaction() as conn:
        conn.execute("UPDATE outbox SET delivered=1 WHERE id=?", (event,))  # legacy consumer acknowledgement
    store.prune_history(delivered_before=clock.now + 1)
    with store.connect() as conn:
        assert conn.execute("SELECT 1 FROM outbox WHERE id=?", (event,)).fetchone()
    provider.fail_ding = False
    store.replay_destination_dead_letter(event, "dingtalk-main", "coordinator")
    drain(dispatcher)
    assert len([c for c in provider.calls if "feishu.cn" in c[1]]) == 1
    with store.connect() as conn:
        rows = conn.execute("SELECT * FROM outbox_deliveries WHERE event_id=?", (event,)).fetchall()
        assert all(r["delivered"] for r in rows)
        assert next(r for r in rows if r["destination_id"] == "feishu-main")["tries"] == 1
    assert store.prune_history(delivered_before=clock.now + 1) >= 1
    assert store.destination_dead_letters() == []


def test_delayed_initial_delivery_never_renders_pending(store, identity, bots, channels, clock):
    dispatcher, provider = channels
    approvals, card, _ = blocked_card(store)
    provider.fail_ding = True
    drain(dispatcher)
    FeishuIngress(approvals, identity, bots=bots).handle_card(feishu_callback(card))
    provider.fail_ding = False
    clock.now += 6
    drain(dispatcher)
    creations = [c[2] for c in provider.calls if c[1].endswith("createAndDeliver")]
    assert creations[-1]["cardData"]["cardParamMap"]["status"] == "APPROVED"
    assert creations[-1]["cardData"]["cardParamMap"]["showApprove"] == "false"
    for p in store.get_card_projections(card["approval_id"]):
        assert p["rendered_version"] == 2 and p["status"] == "APPROVED"
        assert not store.record_card_projection(card["approval_id"], p["destination_id"], p["provider"], "stale", rendered_version=3)


def test_approval_commits_during_card_creation(store, identity, bots, channels):
    dispatcher, provider = channels
    approvals, card, _ = blocked_card(store)
    drain(dispatcher, "feishu-main")
    provider.during_create = lambda: FeishuIngress(approvals, identity, bots=bots).handle_card(feishu_callback(card))
    drain(dispatcher, "dingtalk-main")
    assert next(p for p in store.get_card_projections(card["approval_id"]) if p["provider"] == "dingtalk")["status"] == "APPROVED"
    assert any(c[0] == "PUT" for c in provider.calls)


@pytest.mark.parametrize("receipt", [None, "", " "])
def test_ding_card_requires_persistent_receipt(store, identity, receipt):
    approvals, card, _ = blocked_card(store)
    body = ding_callback(card)
    body["eventId"] = receipt
    with pytest.raises(Forbidden):
        DingTalkCardIngress(approvals, identity, app_id="robot").handle_card(body)


def test_callback_receipt_collision_fails_closed(store, identity):
    approvals, card, _ = blocked_card(store)
    ingress = DingTalkCardIngress(approvals, identity, app_id="robot")
    ingress.handle_card(ding_callback(card))
    changed = dict(card, signature="0" * 64)
    with pytest.raises(Conflict):
        ingress.handle_card(ding_callback(changed))


def test_card_projection_monotonic_and_terminal_guards(store):
    approvals, card, _ = blocked_card(store)
    key = card["approval_id"]
    assert store.record_card_projection(key, "dest", "feishu", "msg")
    assert not store.record_card_projection(key, "dest", "feishu", "other")
    assert store.record_card_projection(key, "dest", "feishu", "msg", rendered_version=2, status="EXPIRED")
    assert not store.record_card_projection(key, "dest", "feishu", "other", rendered_version=3)
    assert store.get_card_projections(key)[0]["provider_card_id"] == "msg"


def test_interactive_busy_timeout_rolls_back(store):
    approvals, card, item = blocked_card(store)
    with store.connect() as locked:
        locked.execute("BEGIN IMMEDIATE")
        start = time.monotonic()
        with pytest.raises(Conflict):
            approvals.approve(card, "reviewer", timeout=0.05)
        assert time.monotonic() - start < 1.5
        locked.rollback()
    assert store.get_item(item["id"])["state"] == "BLOCKED"
    assert approvals.approve(card, "reviewer", timeout=2.0)["approved"]


def test_v4_migration_in_place_and_future_rejection(tmp_path, clock, monkeypatch):
    path = tmp_path / "v4.sqlite"
    original = Store.migrate_round5
    with monkeypatch.context() as patch:
        patch.setattr(Store, "migrate_round5", staticmethod(lambda conn: None))
        old = Store(path, clock=clock)
        with old.transaction() as conn:
            event = old.emit(conn, "old.event", {})
            conn.execute("PRAGMA user_version=4")
    store = Store(path, clock=clock)
    assert Store.migrate_round5 is original
    store.register_destination("main", "feishu")
    assert store.claim_delivery("main")["id"] == event
    with store.connect() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 6
        assert not conn.execute("PRAGMA foreign_key_check").fetchall()
        conn.execute("PRAGMA user_version=7")
    with pytest.raises(Conflict):
        Store(path, clock=clock)


def test_v5_migration_failure_is_atomic(tmp_path, clock, monkeypatch):
    path = tmp_path / "migration.sqlite"
    original = Store.migrate_round5
    def broken(conn):
        original(conn)
        raise RuntimeError("migration interrupted")
    with monkeypatch.context() as patch:
        patch.setattr(Store, "migrate_round5", staticmethod(broken))
        with pytest.raises(RuntimeError):
            Store(path, clock=clock)
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
        assert not conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    Store(path, clock=clock)


def test_delivery_priority_crash_fencing_and_replay_budget(store, clock):
    store.register_destination("main", "feishu")
    with store.transaction() as conn:
        low = store.emit(conn, "normal", {})
    clock.now += 301
    with store.transaction() as conn:
        store.emit(conn, "approval.requested", {})
    row = store.claim_delivery("main", ttl=1, max_tries=1)
    assert row["id"] == low  # starvation promotion
    clock.now += 2
    with pytest.raises(Conflict):
        store.ack_delivery(low, "main", row["claim"], True)
    store.claim_delivery("main", ttl=1, max_tries=1)
    assert store.destination_dead_letters("main")[0]["event_id"] == low
    store.replay_destination_dead_letter(low, "main", "coordinator", max_replays=1)
    row = store.claim_delivery("main", max_tries=1)
    assert row["id"] == low
    store.ack_delivery(low, "main", row["claim"], False, error="https://example.invalid/?token=private", max_tries=1)
    assert store.destination_dead_letters("main")[0]["last_error"] == "delivery failed"
    with pytest.raises(Conflict):
        store.replay_destination_dead_letter(low, "main", "coordinator", max_replays=1)


def test_register_backfill_provider_binding_and_legacy_dispatch(store):
    with store.transaction() as conn:
        event = store.emit(conn, "legacy", {})
    class Adapter:
        def __init__(self):
            self.sent = []
        def send(self, *args):
            self.sent.append(args)
    adapter = Adapter()
    assert OutboxDispatcher(store, adapter).once()
    assert len(adapter.sent) == 1
    store.register_destination("main", "feishu")
    assert store.claim_delivery("main") is None
    with pytest.raises(Conflict):
        store.register_destination("main", "dingtalk")
    with pytest.raises(ValueError):
        store.register_destination("../unsafe", "feishu")
    with store.transaction() as conn:
        event = store.emit(conn, "new", {})
        conn.execute("DELETE FROM outbox_deliveries WHERE event_id=?", (event,))
    assert store.claim_delivery("main")["id"] == event


def test_dingtalk_card_api_gateway_and_dlq_coordinator(store, identity):
    from asgi_client import ASGITestClient
    approvals, card, _ = blocked_card(store)
    tokens = {role: secrets.token_urlsafe(32) for role in ("im_gateway", "coordinator", "worker")}
    app = create_app(store, approvals, {token: Principal(role, role) for role, token in tokens.items()},
        identity_policy=identity, dingtalk_card_ingress=DingTalkCardIngress(approvals, identity, app_id="robot"))
    with ASGITestClient(app) as client:
        for role in (None, "worker"):
            headers = {} if role is None else {"Authorization": "Bearer " + tokens[role]}
            assert client.post("/im/dingtalk/cards", headers=headers, json=ding_callback(card)).status_code in {401, 403}
        headers = {"Authorization": "Bearer " + tokens["im_gateway"]}
        result = client.post("/im/dingtalk/cards", headers=headers, json=ding_callback(card))
        assert result.status_code == 200 and result.json()["approved"]
        assert result.json()["cardData"]["cardParamMap"]["disabled"] == "true"
        assert client.get("/hub/outbox/destinations/dead-letters", headers=headers).status_code == 403
        headers = {"Authorization": "Bearer " + tokens["coordinator"]}
        assert client.get("/hub/outbox/destinations/dead-letters", headers=headers).json() == []


@pytest.mark.parametrize("use_ingress", [False, True])
def test_feishu_api_identity_and_ingress_preserve_signed_transport(store, identity, bots, clock, use_ingress):
    from asgi_client import ASGITestClient
    approvals, card, _ = blocked_card(store)
    key = secrets.token_hex(16)
    app = create_app(store, approvals, {secrets.token_urlsafe(32): Principal("coordinator", "coordinator")},
        feishu_encrypt_key=key, identity_policy=identity,
        feishu_card_ingress=FeishuIngress(approvals, identity, bots=bots) if use_ingress else None)
    raw = json.dumps(feishu_callback(card)).encode()
    timestamp, nonce = str(int(clock())), "callback-nonce"
    headers = {"x-lark-request-timestamp": timestamp, "x-lark-request-nonce": nonce,
        "x-lark-signature": hashlib.sha256((timestamp + nonce + key).encode() + raw).hexdigest(), "Content-Type": "application/json"}
    with ASGITestClient(app) as client:
        assert client.post("/im/feishu/cards", content=raw).status_code == 403
        response = client.post("/im/feishu/cards", headers=headers, content=raw)
        assert response.status_code == 200 and response.json()["approved"]
        assert client.post("/im/feishu/cards", headers=headers, content=raw).json() == response.json()


def test_simultaneous_cross_platform_click_commits_once(store, identity, bots):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    approvals, card, item = blocked_card(store)
    barrier = Barrier(2)
    ingresses = [FeishuIngress(approvals, identity, bots=bots), DingTalkCardIngress(approvals, identity, app_id="robot")]
    bodies = [feishu_callback(card), ding_callback(card)]
    def click(index):
        barrier.wait(timeout=5)
        return ingresses[index].handle_card(bodies[index])
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(click, [0, 1]))
    assert sorted(r["duplicate"] for r in results) == [False, True]
    assert store.get_item(item["id"])["version"] == item["version"] + 1
    with store.connect() as conn:
        assert conn.execute("SELECT count(*) FROM outbox WHERE topic='approval.accepted'").fetchone()[0] == 1


def test_ignored_filesystem_link_rejected_before_clean(repo, external_scratch, monkeypatch):
    # Simulate a platform junction without requiring symlink privileges. The walk
    # exposes the directory before recursion; no clean or external traversal is allowed.
    from pathlib import Path
    with repo.git.worktree(repo.base) as directory:
        junction = directory / "cache-junction"
        junction.mkdir()
        real = Path.is_symlink
        monkeypatch.setattr(Path, "is_symlink", lambda p: p == junction or real(p))
        with pytest.raises(GateRejected, match="filesystem link"):
            CodexCliAdapter._clean_ignored(type("Git", (), {"repository": directory,
                "run": lambda *args: pytest.fail("must not clean unsafe worktree")})())


def test_multi_destination_lease_isolation_and_disabled_destination(store, clock):
    for destination in ("first", "second"):
        store.register_destination(destination, "feishu")
    with store.transaction() as conn:
        event = store.emit(conn, "event", {})
    first, second = store.claim_delivery("first"), store.claim_delivery("second")
    assert first["id"] == second["id"] == event
    assert first["claim"] != second["claim"]
    with pytest.raises(Conflict):
        store.ack_delivery(event, "second", first["claim"], True)
    store.ack_delivery(event, "first", first["claim"], True)
    assert store.claim_delivery("first") is None
    with store.transaction() as conn:
        conn.execute("UPDATE outbox_destinations SET enabled=0 WHERE destination_id='second'")
    assert store.claim_delivery("second") is None
    assert store.prune_history(delivered_before=clock.now + 1) == 0
    store.register_destination("second", "feishu")
    store.ack_delivery(event, "second", second["claim"], True)
    assert store.prune_history(delivered_before=clock.now + 1) == 1


def test_optional_ding_sdk_separate_topics_persistent_header_receipt(store, identity, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from a2a_slice_hub.channels import register_dingtalk_stream_handlers
    approvals, card, _ = blocked_card(store)
    sdk = SimpleNamespace(CallbackHandler=type("CallbackHandler", (), {}), AckMessage=SimpleNamespace(STATUS_OK=200))
    monkeypatch.setitem(sys.modules, "dingtalk_stream", sdk)
    handlers = {}
    client = SimpleNamespace(register_callback_handler=lambda topic, handler: handlers.update({topic: handler}))
    gate = ChannelGate(dm_policy="pairing", identity_policy=identity)
    ingress = DingTalkCardIngress(approvals, identity, app_id="robot")
    register_dingtalk_stream_handlers(client, DingTalkChatIngress(gate, app_id="robot"), ingress)
    assert set(handlers) == {"/v1.0/im/bot/messages/get", "/v1.0/card/instances/callback"}
    body = ding_callback(card)
    body.pop("eventId")
    body["content"] = json.dumps({"cardPrivateData": body.pop("cardPrivateData")})
    message = SimpleNamespace(data=body, headers=SimpleNamespace(message_id="persistent-sdk-message"))
    status, response = asyncio.run(handlers["/v1.0/card/instances/callback"].process(message))
    assert status == 200 and response["cardData"]["cardParamMap"]["status"] == "APPROVED"
    monkeypatch.setattr(approvals, "approve", lambda *args, **kwargs: pytest.fail("SDK replay must use inbox"))
    assert asyncio.run(handlers["/v1.0/card/instances/callback"].process(message))[0] == 200


def test_optional_lark_sdk_wires_chat_and_card_callbacks(store, identity, bots, monkeypatch):
    from types import SimpleNamespace, ModuleType
    from a2a_slice_hub.channels import build_lark_ws_dispatcher
    approvals, card, _ = blocked_card(store)
    callbacks = {}
    class Builder:
        def register_p2_im_message_receive_v1(self, callback):
            callbacks["chat"] = callback
            return self
        def register_p2_card_action_trigger(self, callback):
            callbacks["card"] = callback
            return self
        def build(self):
            return callbacks
    sdk = SimpleNamespace(EventDispatcherHandler=SimpleNamespace(builder=lambda *args: Builder()),
                          JSON=SimpleNamespace(marshal=json.dumps))
    monkeypatch.setitem(sys.modules, "lark_oapi", sdk)
    module = ModuleType("lark_oapi.event.callback.model.p2_card_action_trigger")
    module.P2CardActionTriggerResponse = lambda data: data
    monkeypatch.setitem(sys.modules, module.__name__, module)
    dispatcher = build_lark_ws_dispatcher(FeishuIngress(approvals, identity, bots=bots))
    result = dispatcher["card"](feishu_callback(card))
    assert result["toast"]["type"] == "success"
    assert "button" not in json.dumps(result["card"])


def test_feishu_v1_callback_deterministic_receipt(store, identity, bots, monkeypatch):
    approvals, card, _ = blocked_card(store)
    body = {"app_id": "orchestrator", "tenant_key": "tenant", "open_id": "reviewer-user",
            "open_message_id": "msg", "action": {"value": card}}
    ingress = FeishuIngress(approvals, identity, bots=bots)
    assert ingress.handle_card(body)["approved"]
    monkeypatch.setattr(approvals, "approve", lambda *args, **kwargs: pytest.fail("legacy callback replay"))
    assert FeishuIngress(approvals, identity, bots=bots).handle_card(body)["approved"]


def test_feishu_role_split_uses_three_bots(store, channels):
    dispatcher, provider = channels
    for topic in ("qa.result", "attempt.working", "merge.done", "gate.result", "notice"):
        with store.transaction() as conn:
            store.emit(conn, topic, {})
    adapter = dispatcher.adapters["feishu-main"]
    adapter.bots = {name: BotIdentity(bot.app_id, lambda name=name: name) for name, bot in adapter.bots.items()}
    drain(dispatcher, "feishu-main")
    tokens = [c[3]["authorization"] for c in provider.calls]
    assert tokens.count("Bearer qa_feed_bot") == 1
    assert tokens.count("Bearer dev_feed_bot") == 3
    assert tokens.count("Bearer orchestrator_bot") == 1


def test_destination_replay_api_role_and_scope(store):
    from asgi_client import ASGITestClient
    store.register_destination("main", "feishu")
    with store.transaction() as conn:
        event = store.emit(conn, "event", {})
    row = store.claim_delivery("main", max_tries=1)
    store.ack_delivery(event, "main", row["claim"], False, max_tries=1)
    token = secrets.token_urlsafe(32)
    app = create_app(store, Approvals(store, secrets.token_bytes(32)), {token: Principal("coordinator", "coordinator")})
    with ASGITestClient(app) as client:
        path = f"/hub/outbox/destinations/main/dead-letters/{event}/replay"
        assert client.post(path).status_code in {401, 403}
        response = client.post(path, headers={"Authorization": "Bearer " + token})
        assert response.status_code == 200 and response.json()["replay_count"] == 1
        assert store.destination_dead_letters("main") == []
