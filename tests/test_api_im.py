from concurrent.futures import ThreadPoolExecutor
import copy
import json
import secrets
import threading

import httpx
import pytest

from a2a_slice_hub.approvals import Approvals
from a2a_slice_hub.errors import Conflict, Forbidden
from a2a_slice_hub.im import BOTS, BotIdentity, FeishuAdapter, OutboxDispatcher
from conftest import new_item, submit


def test_api_auth_task_lifecycle_and_terminal_fence(api, store, repo):
    client, headers, _ = api
    body = {"kind": "DEV", "slice_id": "orders", "owner": "worker-a", "allowed_paths": ["src/orders"]}
    assert client.post("/hub/items", json=body).status_code == 401
    assert client.post("/hub/items", json=body, headers=headers["worker"]).status_code == 403
    response = client.post("/hub/items", json=body, headers=headers["coordinator"])
    assert response.status_code == 201
    item = response.json()
    claim = {"item_id": item["id"], "expected_version": 0}
    assert client.post("/a2a/tasks", json=claim, headers=headers["other"]).status_code == 403
    response = client.post("/a2a/tasks", json=claim, headers=headers["worker"])
    assert response.status_code == 201
    task = response.json()
    assert task["contextId"] == item["id"]
    endpoint = "/a2a/tasks/" + task["id"]
    assert client.get(endpoint, headers=headers["other"]).status_code == 403
    assert client.post(endpoint + "/status", headers=headers["worker"],
                       json={"epoch": 1, "expected_version": 0, "state": "working"}).status_code == 200
    response = client.post(endpoint + "/status", headers=headers["worker"], json={
        "epoch": 1, "expected_version": 1, "state": "completed", "base_sha": repo.base, "source_sha": repo.base})
    assert response.status_code == 200
    assert response.json()["status"]["state"] == "completed"
    assert client.post(endpoint + "/status", headers=headers["worker"], json={
        "epoch": 1, "expected_version": 2, "state": "working"}).status_code == 409
    assert client.post("/hub/items/" + item["id"] + "/qa", headers=headers["worker"], json={
        "expected_version": 3, "integration_sha": repo.base, "passed": True}).status_code == 403


def test_agent_card_and_rpc_idempotency(api, store):
    client, headers, _ = api
    card = client.get("/.well-known/agent-card.json").json()
    assert card["preferredTransport"] == "JSONRPC"
    assert card["capabilities"]["streaming"] is False
    item = new_item(store)
    request = {"jsonrpc": "2.0", "id": 1, "method": "message/send", "params": {"message": {
        "kind": "message", "messageId": "request-one", "role": "user", "parts": [{"kind": "text", "text": "work"}],
        "metadata": {"work_item_id": item["id"], "expected_state_version": 0}}}}
    first = client.post("/a2a", json=request, headers=headers["coordinator"]).json()
    second = client.post("/a2a", json=request, headers=headers["coordinator"]).json()
    assert first["result"]["id"] == second["result"]["id"]
    request["params"]["message"]["parts"][0]["text"] = "different"
    assert "error" in client.post("/a2a", json=request, headers=headers["coordinator"]).json()
    unknown = {"jsonrpc": "2.0", "id": 2, "method": "unknown", "params": {}}
    assert client.post("/a2a", json=unknown, headers=headers["worker"]).json()["error"]["code"] == -32601


def block(store):
    item = new_item(store)
    # Approval tests arrange blocked state; duplicate disputes no longer manufacture it.
    with store.transaction() as conn:
        conn.execute("UPDATE work_items SET state='BLOCKED',blocked_reason='fixture' WHERE id=?", (item["id"],))
    return item


def test_approval_concurrent_double_click_exactly_one_event(store):
    item = block(store)
    approvals = Approvals(store, secrets.token_bytes(32))
    card = approvals.issue(item["id"], {"decision": "resume same owner after clarification"})
    barrier = threading.Barrier(2)
    def accept(_):
        barrier.wait()
        return approvals.approve(card, "human")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(accept, range(2)))
    assert sorted(r["duplicate"] for r in results) == [False, True]
    with store.connect() as conn:
        assert conn.execute("SELECT count(*) FROM outbox WHERE topic='approval.accepted'").fetchone()[0] == 1


@pytest.mark.parametrize("field,value", [("subject_digest", "tampered"), ("expected_state_version", 999), ("expires_at", 999999)])
def test_signed_approval_tampering(store, field, value):
    item = block(store)
    approvals = Approvals(store, secrets.token_bytes(32))
    card = approvals.issue(item["id"], {"decision": "resume"})
    card[field] = value
    with pytest.raises(Forbidden):
        approvals.approve(card, "human")


def test_expired_and_stale_approval(store, clock):
    item = block(store)
    approvals = Approvals(store, secrets.token_bytes(32))
    card = approvals.issue(item["id"], {"decision": "resume"}, ttl=1)
    clock.now += 2
    with pytest.raises(Conflict):
        approvals.approve(card, "human")
    card = approvals.issue(item["id"], {"decision": "resume"})
    with store.transaction() as conn:
        conn.execute("UPDATE work_items SET version=version+1 WHERE id=?", (item["id"],))
    with pytest.raises(Conflict):
        approvals.approve(card, "human")


def test_api_bot_echo_and_approval_authorization(api, store):
    client, headers, approvals = api
    response = client.post("/im/feishu/events", headers=headers["im_gateway"], json={
        "event": {"sender": {"sender_type": "app"}, "message": {"text": "approve everything"}}})
    assert response.json() == {"ignored": True, "accepted_as_command": False}
    item = block(store)
    card = approvals.issue(item["id"], {"decision": "resume"})
    assert client.post("/hub/approvals/accept", headers=headers["worker"], json={"card": card}).status_code == 403
    assert client.post("/hub/approvals/accept", headers=headers["approver"], json={"card": card}).status_code == 200


def test_feishu_three_real_identities_and_stable_delivery_key():
    sent = []
    def handler(request):
        sent.append((request.headers["authorization"], json.loads(request.content)))
        return httpx.Response(200, json={"code": 0})
    credentials = {name: secrets.token_urlsafe(32) for name in BOTS}
    bots = {name: BotIdentity("application-" + name, lambda n=name: credentials[n]) for name in BOTS}
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        adapter = FeishuAdapter(bots, "test-chat", client)
        adapter.send("delivery-one", "attempt.completed", {"slice_id": "orders", "owner": "worker-a"})
        adapter.send("delivery-one", "attempt.completed", {"slice_id": "orders", "owner": "worker-a"})
        adapter.send("delivery-two", "qa.result", {})
        adapter.send("delivery-three", "approval.requested", {"subject": {"decision": "resume"}, "card": {"signature": "test-only"}})
    assert sent[0][0] == "Bearer " + credentials["dev_feed_bot"]
    assert sent[2][0] == "Bearer " + credentials["qa_feed_bot"]
    assert sent[3][0] == "Bearer " + credentials["orchestrator_bot"]
    assert sent[0][1]["uuid"] == sent[1][1]["uuid"]
    assert "[orders · worker-a Agent]" in json.loads(sent[0][1]["content"])["text"]
    assert sent[3][1]["msg_type"] == "interactive"


def test_outbox_failed_send_retries_with_same_id(store, clock):
    with store.transaction() as conn:
        key = store.emit(conn, "example", {})
    class Transport:
        def __init__(self): self.ids = []
        def send(self, event_id, topic, payload):
            self.ids.append(event_id)
            if len(self.ids) == 1:
                raise OSError("temporary transport failure")
    transport = Transport()
    dispatcher = OutboxDispatcher(store, transport)
    assert dispatcher.once() is False
    clock.now += 6
    assert dispatcher.once() is True
    assert transport.ids == [key, key]
    assert dispatcher.once() is False
