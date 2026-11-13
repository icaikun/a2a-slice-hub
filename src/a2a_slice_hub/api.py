"""Authenticated Hub API plus a deliberately bounded A2A 0.3 JSON-RPC subset."""
from dataclasses import dataclass
import hashlib
import hmac
import json
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from .errors import Conflict, Forbidden, HubError, NotFound
from .im import FeishuAdapter, FeishuVerifier, DingTalkVerifier
from .models import (ApprovalCreate, ApprovalUse, Claim, Dependency, Dispute, Heartbeat,
                     ItemCreate, MergeRequest, QAResult, Transition, WorkerRegistration, EpicCreate, BugReport, DependencyRequest)
from .models import TaskStatus, DevSubmission, QAVerdict, GateMerge
from .store import encode, strict_integer


@dataclass(frozen=True)
class Principal:
    name: str
    role: str
    owner_user_id: str | None = None


def task_view(attempt):
    return {"kind": "task", "id": attempt["id"], "contextId": attempt["work_item_id"],
            "status": {"state": attempt["state"]},
            "metadata": {"lease_epoch": attempt["epoch"], "state_version": attempt["version"]}}


def create_app(store, approvals, principals: dict[str, Principal], *, gatekeeper=None,
               public_url="http://localhost:8000", a2a_worker_id=None, feishu_encrypt_key=None, feishu_users=None,
               dingtalk_secret=None, dingtalk_users=None):
    if not principals or any(len(k) < 32 for k in principals):
        raise ValueError("configure strong bearer credentials")
    if store.registry is not None and any(p.role == "qa" and
            (not isinstance(p.owner_user_id, str) or not p.owner_user_id.strip()) for p in principals.values()):
        raise ValueError("QA principals require owner_user_id")
    verifier = FeishuVerifier(feishu_encrypt_key, store.clock) if feishu_encrypt_key else None
    feishu_users = feishu_users or {}
    ding_verifier = DingTalkVerifier(dingtalk_secret, store.clock, dingtalk_users) if dingtalk_secret else None
    app = FastAPI(title="A2A Slice Hub", version="0.1.0")

    def authenticate(authorization: Annotated[str | None, Header()] = None):
        if authorization and authorization.startswith("Bearer "):
            token = authorization[7:]
            for secret, actor in principals.items():
                if hmac.compare_digest(secret.encode("utf-8"), token.encode("utf-8")):
                    return actor
        raise HTTPException(401, "authentication required", headers={"WWW-Authenticate": "Bearer"})

    def require(*roles):
        def dependency(actor=Depends(authenticate)):
            if actor.role not in roles:
                raise Forbidden("role not authorized")
            return actor
        return dependency

    def authorize_attempt(attempt, actor):
        if actor.role not in {"coordinator", "gatekeeper", "qa"} and (
                actor.role != "worker" or attempt["worker_id"] != actor.name):
            raise Forbidden("task belongs to another worker")

    @app.exception_handler(HubError)
    async def hub_error(request, exc):
        status = 403 if isinstance(exc, Forbidden) else 404 if isinstance(exc, NotFound) else 409
        return JSONResponse(status_code=status, content={"error": type(exc).__name__, "detail": str(exc)})

    @app.exception_handler(ValueError)
    async def invalid_value(request, exc):
        return JSONResponse(status_code=422, content={"error": "invalid request data"})

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/.well-known/agent-card.json")
    def card():
        return {"name": "Slice Worker" if a2a_worker_id else "Slice Hub",
                "description": "Authenticated task lifecycle; constrained A2A 0.3 JSON-RPC subset",
                "url": public_url.rstrip("/") + "/a2a", "version": "0.1.0", "protocolVersion": "0.3.0",
                "preferredTransport": "JSONRPC", "capabilities": {"streaming": False, "pushNotifications": False},
                "defaultInputModes": ["text/plain", "application/json"], "defaultOutputModes": ["text/plain"],
                "skills": [{"id": "slice-development", "name": "Slice development", "description": "Leased slice work", "tags": ["git"]}],
                "securitySchemes": {"bearer": {"type": "http", "scheme": "bearer"}}, "security": [{"bearer": []}]}

    @app.get("/hub/outbox/dead-letters")
    def dead_letters(limit: int = 100, actor=Depends(require("coordinator"))):
        return store.dead_letters(limit)

    @app.post("/hub/outbox/dead-letters/{key}/replay")
    def replay_dead_letter(key: str, actor=Depends(require("coordinator"))):
        return store.replay_dead_letter(key, actor.name)

    @app.post("/hub/items", status_code=201)
    def create_item(body: ItemCreate, actor=Depends(require("coordinator"))):
        return store.create_item(**body.model_dump())

    @app.get("/hub/items/{key}")
    def get_item(key: str, actor=Depends(authenticate)):
        item = store.get_item(key)
        if actor.role == "worker":
            with store.connect() as conn:
                store.authorize_worker(conn, item, actor.name)
        return item

    @app.post("/hub/items/{key}/dependencies")
    def dependency(key: str, body: Dependency, actor=Depends(require("coordinator"))):
        store.add_dependency(key, body.needs)
        return {"added": True}

    @app.post("/hub/items/{key}/disputes")
    def dispute(key: str, body: Dispute, actor=Depends(require("worker"))):
        store.dispute(key, actor.name, body.evidence)
        return {"recorded": True}

    @app.post("/hub/items/{key}/qa")
    def qa(key: str, body: QAResult, actor=Depends(require("qa"))):
        if (body.outcome == "FAIL" or (body.outcome is None and body.passed is False)) and body.report is None:
            raise HTTPException(422, "failed QA requires bug-report/v1")
        return store.qa(key, body.expected_version, body.integration_sha, body.passed, actor.owner_user_id or actor.name,
                        body.report, outcome=body.outcome)

    @app.post("/hub/merge")
    def merge(body: MergeRequest, actor=Depends(require("gatekeeper"))):
        if gatekeeper is None:
            raise HTTPException(503, "gatekeeper is not configured")
        return gatekeeper.merge(body.branch, body.attempt_ids)

    @app.post("/hub/approvals", status_code=201)
    def issue(body: ApprovalCreate, actor=Depends(require("coordinator"))):
        return approvals.issue(body.item_id, body.subject, body.ttl, body.action)

    @app.post("/hub/approvals/accept")
    def approve(body: ApprovalUse, actor=Depends(require("approver"))):
        return approvals.approve(body.card, actor.name)

    @app.post("/hub/workers/heartbeat")
    def register_worker(body: WorkerRegistration, actor=Depends(require("worker"))):
        return store.register_worker(actor.name, actor.owner_user_id, **body.model_dump())

    @app.post("/hub/workers/refresh")
    def refresh_workers(actor=Depends(require("coordinator"))):
        store.refresh_workers()
        return {"refreshed": True}

    @app.post("/hub/epics", status_code=201)
    def create_epic(body: EpicCreate, actor=Depends(require("coordinator"))):
        if gatekeeper is None or gatekeeper.git.ref(body.branch) != body.skeleton_sha:
            raise Conflict("L0 skeleton must be the integration head")
        return store.create_epic(**body.model_dump())

    @app.post("/hub/epics/{key}/release")
    def release(key: str, actor=Depends(require("coordinator"))):
        if gatekeeper is None:
            raise HTTPException(503, "gatekeeper not configured")
        return store.check_release_ready(key, gatekeeper.git)

    async def feishu_request(request):
        if verifier is None:
            raise HTTPException(503, "Feishu verifier not configured")
        raw = await request.body()
        if len(raw) > 1_000_000:
            raise HTTPException(413, "callback too large")
        receipt = verifier.verify(request.headers, raw)
        digest = hashlib.sha256(raw).hexdigest()
        with store.connect() as conn:
            old = conn.execute("SELECT * FROM inbox WHERE id=?", (receipt,)).fetchone()
            if old:
                if old["digest"] != digest:
                    raise Conflict("callback nonce reused with different payload")
                return receipt, digest, None, json.loads(old["result"])
        try:
            body = json.loads(raw)
            if not isinstance(body, dict) or "encrypt" in body:
                raise ValueError()
        except ValueError:
            raise HTTPException(422, "expected signed plaintext JSON; encrypted envelopes unsupported")
        return receipt, digest, body, None

    def save_receipt(receipt, digest, result):
        with store.transaction() as conn:
            old = conn.execute("SELECT digest FROM inbox WHERE id=?", (receipt,)).fetchone()
            if old and old["digest"] != digest:
                raise Conflict("callback nonce reused")
            conn.execute("INSERT OR IGNORE INTO inbox VALUES(?,?,?)", (receipt, digest, encode(result)))
        return result

    @app.post("/im/feishu/events")
    async def im_event(request: Request):
        if verifier is None:
            # Legacy gateway is read-only, never an approval bypass.
            actor = authenticate(request.headers.get("authorization"))
            if actor.role != "im_gateway":
                raise Forbidden("gateway required")
            event = FeishuAdapter.incoming(await request.json())
            return {"ignored": event is None, "accepted_as_command": False}
        receipt, digest, body, old = await feishu_request(request)
        if old is not None:
            return old
        result = ({"challenge": body["challenge"]} if body.get("type") == "url_verification" else
                  {"ignored": FeishuAdapter.incoming(body) is None, "accepted_as_command": False})
        return save_receipt(receipt, digest, result)

    @app.post("/im/feishu/cards")
    async def im_card(request: Request):
        receipt, digest, body, old = await feishu_request(request)
        if old is not None:
            return old
        if body.get("type") == "url_verification":
            return save_receipt(receipt, digest, {"challenge": body["challenge"]})
        try:
            event = body.get("event", body)
            open_id = event["operator"]["open_id"]
            name = feishu_users[open_id]
            if not any(p.name == name and p.role == "approver" for p in principals.values()):
                raise KeyError()
            result = approvals.approve(event["action"]["value"], name, receipt=receipt, digest=digest)
        except (KeyError, TypeError):
            raise Forbidden("unknown or unauthorized Feishu approver")
        return save_receipt(receipt, digest, result)

    @app.post("/im/dingtalk/events")
    async def dingtalk_event(request: Request, actor=Depends(require("im_gateway"))):
        # The timestamp signature alone does not bind the sender or body.
        if ding_verifier is None:
            raise HTTPException(503, "DingTalk verifier not configured")
        raw = await request.body()
        if len(raw) > 1_000_000:
            raise HTTPException(413, "callback too large")
        receipt = ding_verifier.verify(request.headers, raw)
        digest = hashlib.sha256(raw).hexdigest()
        body = json.loads(raw)
        if not isinstance(body, dict) or "encrypt" in body:
            raise ValueError("expected robot JSON callback")
        with store.connect() as conn:
            old = conn.execute("SELECT * FROM inbox WHERE id=?", (receipt,)).fetchone()
            if old:
                if old["digest"] != digest:
                    raise Conflict("callback nonce reused")
                return json.loads(old["result"])
        sender = ding_verifier.sender(body)
        result = {"sender": sender, "accepted_as_command": False}
        return save_receipt(receipt, digest, result)

    @app.post("/a2a/tasks", status_code=201)
    def claim(body: Claim, actor=Depends(require("worker"))):
        return task_view(store.claim(body.item_id, actor.name, body.expected_version, body.ttl))

    @app.get("/a2a/tasks/{key}")
    def get_task(key: str, actor=Depends(authenticate)):
        attempt = store.get_attempt(key)
        authorize_attempt(attempt, actor)
        return task_view(attempt)

    @app.post("/a2a/tasks/{key}/status")
    def transition(key: str, body: Transition, actor=Depends(require("worker"))):
        return task_view(store.transition(key, actor.name, body.epoch, body.expected_version, body.state,
                                         base_sha=body.base_sha, source_sha=body.source_sha, artifact=body.artifact))

    @app.post("/a2a/tasks/{key}/heartbeat")
    def heartbeat(key: str, body: Heartbeat, actor=Depends(require("worker"))):
        store.heartbeat(key, actor.name, body.epoch, body.ttl)
        return {"renewed": True}

    @app.post("/a2a")
    async def rpc(request: Request, actor=Depends(authenticate)):
        request_id = None
        def error(code, message):
            return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 1_000_000:
                return error(-32600, "request exceeds 1MB limit")
        try:
            body = json.loads(raw)
        except (ValueError, UnicodeError):
            return error(-32700, "parse error")
        if not isinstance(body, dict):
            return error(-32600, "invalid request; notifications and batches unsupported")
        value = body.get("id")
        request_id = value if isinstance(value, (str, int)) and not isinstance(value, bool) else None
        if not isinstance(body.get("method"), str):
            return error(-32600, "method must be a string")
        return await run_in_threadpool(rpc_dispatch, body, actor, request_id, error)

    def rpc_dispatch(body, actor, request_id, error):
        if body.get("jsonrpc") != "2.0" or not isinstance(request_id, (str, int)) or isinstance(request_id, bool):
            return error(-32600, "invalid request; notifications and batches unsupported")
        params = body.get("params", {})
        if not isinstance(params, dict):
            return error(-32602, "invalid params")
        try:
            method = body.get("method")
            if method == "message/send":
                if actor.role not in {"worker", "coordinator", "qa", "gatekeeper"}:
                    raise Forbidden("cannot submit work")
                message = params["message"]
                if not isinstance(message, dict) or message.get("role") != "user" or not isinstance(message.get("messageId"), str) or not message["messageId"]:
                    return error(-32602, "user message and messageId required")
                parts = message.get("parts")
                if not isinstance(parts, list) or not parts or any(not isinstance(p, dict) or not (
                    (p.get("kind") == "text" and isinstance(p.get("text"), str)) or
                    (p.get("kind") == "data" and isinstance(p.get("data"), dict))) for p in parts):
                    return error(-32602, "text or structured data parts required")
                data = [p["data"] for p in parts if p["kind"] == "data"]
                if len(data) > 1:
                    return error(-32602, "one structured command per message")
                if data:
                    payload = data[0]
                    metadata = message.get("metadata", {})
                    digest = hashlib.sha256(encode(message).encode()).hexdigest()
                    if not isinstance(metadata, dict):
                        return error(-32602, "metadata must be an object")
                    for field, minimum in (("expected_state_version", 0), ("lease_epoch", 1)):
                        if field in metadata:
                            strict_integer(metadata[field], minimum)
                    schema = payload.get("schema_version")
                    receipt = actor.name + ":" + message["messageId"]
                    if schema in {"task-status/v1", "dev-submission/v1"}:
                        if actor.role != "worker":
                            raise Forbidden("development transition requires worker")
                        command = (TaskStatus if schema == "task-status/v1" else DevSubmission).model_validate(payload)
                        attempt = store.get_attempt(message["taskId"])
                        authorize_attempt(attempt, actor)
                        if message.get("contextId") != attempt["work_item_id"]:
                            return error(-32602, "task context mismatch")
                        attempt = store.transition(attempt["id"], actor.name, metadata["lease_epoch"],
                            metadata["expected_state_version"], command.state,
                            **command.model_dump(exclude={"schema_version", "state"}),
                            request_id=receipt, request_digest=digest)
                        return {"jsonrpc": "2.0", "id": request_id, "result": task_view(attempt)}
                    if schema in {"qa-verdict/v1", "bug-report/v1"}:
                        if actor.role != "qa":
                            raise Forbidden("verdict requires independent QA")
                        if schema == "bug-report/v1":
                            BugReport.model_validate(payload)
                            verdict = QAVerdict(schema_version="qa-verdict/v1", outcome="FAIL",
                                                integration_sha=payload["target"]["integration_sha"], report=payload)
                        else:
                            verdict = QAVerdict.model_validate(payload)
                        if message.get("taskId"):
                            attempt = store.get_attempt(message["taskId"])
                            if attempt["work_item_id"] != message.get("contextId"):
                                return error(-32602, "task context mismatch")
                        item = store.qa(message["contextId"], metadata["expected_state_version"],
                            verdict.integration_sha, None, actor.owner_user_id or actor.name, verdict.report,
                            outcome=verdict.outcome, request_id=receipt, request_digest=digest)
                        return {"jsonrpc": "2.0", "id": request_id, "result": {"kind": "message", "role": "agent",
                            "messageId": message["messageId"], "parts": [{"kind": "data", "data": item}]}}
                    if schema == "gate-merge/v1":
                        if actor.role not in {"coordinator", "gatekeeper"}:
                            raise Forbidden("merge requires gatekeeper authority")
                        if gatekeeper is None:
                            raise Conflict("gatekeeper is not configured")
                        command = GateMerge.model_validate(payload)
                        result = gatekeeper.merge(command.branch, command.attempt_ids,
                            request_id=receipt, request_digest=digest)
                        return {"jsonrpc": "2.0", "id": request_id, "result": result}
                    if payload.get("schema_version") == "dependency-request/v1":
                        if actor.role != "worker":
                            raise Forbidden("dependency request requires worker")
                        DependencyRequest.model_validate(payload)
                        attempt = store.get_attempt(message["taskId"])
                        authorize_attempt(attempt, actor)
                        if message.get("contextId") != attempt["work_item_id"]:
                            return error(-32602, "task context mismatch")
                        attempt = store.transition(attempt["id"], actor.name, metadata["lease_epoch"],
                            metadata["expected_state_version"], "input-required", artifact={"dependency_request": payload},
                            request_id=actor.name + ":" + message["messageId"], request_digest=digest)
                        return {"jsonrpc": "2.0", "id": request_id, "result": task_view(attempt)}
                    return error(-32602, "unsupported data schema")
                if actor.role not in {"worker", "coordinator"}:
                    raise Forbidden("only worker or coordinator may claim development work")
                metadata = message["metadata"]
                if not isinstance(metadata, dict):
                    return error(-32602, "metadata must be an object")
                strict_integer(metadata["expected_state_version"])
                item = store.get_item(metadata["work_item_id"])
                worker_id = a2a_worker_id or actor.name
                if actor.role == "worker" and actor.name != worker_id:
                    raise Forbidden("wrong worker endpoint")
                if message.get("contextId", item["id"]) != item["id"] or message.get("taskId"):
                    return error(-32602, "new task must use its work item context and no taskId")
                digest = hashlib.sha256(encode(message).encode()).hexdigest()
                attempt = store.claim(item["id"], worker_id, metadata["expected_state_version"],
                    request_id=actor.name + ":" + message["messageId"], request_digest=digest)
                result = task_view(attempt)
            elif method in {"tasks/get", "tasks/cancel"}:
                attempt = store.get_attempt(params["id"])
                authorize_attempt(attempt, actor)
                if method == "tasks/cancel":
                    if actor.role != "worker" or actor.name != attempt["worker_id"]:
                        raise Forbidden("only owning worker may cancel")
                    if attempt["state"] == "canceled":
                        return {"jsonrpc": "2.0", "id": request_id, "result": task_view(attempt)}
                    if attempt["state"] in {"completed", "failed", "rejected"}:
                        return error(-32002, "task is not cancelable")
                    attempt = store.transition(attempt["id"], actor.name, attempt["epoch"], attempt["version"], "canceled")
                result = task_view(attempt)
            else:
                return error(-32601, "method not supported")
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        except NotFound:
            return error(-32001, "task or work item not found")
        except (KeyError, TypeError, ValueError):
            return error(-32602, "invalid params")
        except HubError as exc:
            return error(-32000, str(exc))

    return app
