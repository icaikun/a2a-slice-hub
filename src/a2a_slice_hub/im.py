"""IM transport is at-least-once; never claim transactional exactly-once delivery."""
from dataclasses import dataclass
import json
from typing import Callable, Protocol

import httpx

from .errors import Conflict

BOTS = {"orchestrator_bot", "dev_feed_bot", "qa_feed_bot"}


class IMAdapter(Protocol):
    def send(self, event_id: str, topic: str, payload: dict) -> None: ...


@dataclass(frozen=True)
class BotIdentity:
    app_id: str
    token: Callable[[], str]


def collaboration_summary(topic, payload):
    """Transport-neutral, human-readable event mirror; never expose raw cards/tokens."""
    label = "[{} · {} Agent]".format(payload.get("slice_id", "hub"), payload.get("owner", "coordinator"))
    item = payload.get("item", payload.get("epic_id", ""))
    if topic == "contract.patch_requested":
        request = payload.get("request", {})
        change = json.dumps(request.get("expected_change", {}), ensure_ascii=False)
        return "[{} · {} Agent] -> @{} 请求契约 {} 增量变更：{}；工单 {}，实现与单测须同步提交。".format(
            payload.get("requester_slice", "consumer"), payload.get("requester_owner", "requester"),
            label, request.get("contract", ""), change, item)
    if topic == "gate.rejected":
        return f"{label} 合并门禁拒绝工单 {item}：{payload.get('reason', '未通过校验')}。请修复后重新提交。"
    if topic.startswith("qa."):
        outcome = payload.get("outcome", topic.removeprefix("qa.").upper())
        if outcome == "RESULT":
            outcome = "PASS" if payload.get("passed") else "FAIL"
        return f"{label} QA 验收 {outcome}；工单 {item}；快照 {payload.get('sha', '见工单')}。"
    if topic == "ownership.dispute":
        return f"{label} 发起责任归属争议（有效第 {payload.get('round', 1)} 次）；工单 {item}：{payload.get('evidence', '')}。"
    if topic == "epic.l0_pending":
        return f"[hub] Epic {item} 的 L0 骨架等待签名审批，尚不可派发开发。"
    if topic == "epic.release_ready":
        return f"[hub] Epic {item} 已满足发布条件；QA 快照 {payload.get('integration_sha', '')}。等待人工发布。"
    if topic == "item.escalated":
        return f"@coordinator {label} 工单 {item} 已升级为 {payload.get('state')}：{payload.get('reason')}。请联调负责人裁决并签名解锁。"
    return f"{label} 协同事件 {topic}；工单 {item}。"


class FeishuAdapter:
    def __init__(self, bots: dict[str, BotIdentity], chat_id: str, client: httpx.Client, *, mention_ids=None):
        if set(bots) != BOTS or len({b.app_id for b in bots.values()}) != 3:
            raise ValueError("exactly three distinct registered application identities required")
        self.bots, self.chat_id, self.client = bots, chat_id, client
        self.mention_ids = mention_ids or {}

    @staticmethod
    def incoming(event):
        # Bot-originated events never become user commands, even if their body is malformed.
        if event.get("sender_type") == "app":
            return None
        inner = event.get("event", event)
        if not isinstance(inner, dict):
            return None
        sender = inner.get("sender") or {}
        if inner.get("sender_type") == "app" or (isinstance(sender, dict) and sender.get("sender_type") == "app"):
            return None
        return inner

    def send(self, event_id, topic, payload):
        bot = "qa_feed_bot" if topic.startswith("qa.") else ("dev_feed_bot" if topic.startswith(("attempt.", "merge.", "gate.")) else "orchestrator_bot")
        header = "[{} · {}]".format(payload.get("slice_id", "hub"), payload.get("owner", "coordinator"))
        if topic == "approval.requested":
            content = {"elements": [
                {"tag": "div", "text": {"tag": "plain_text", "content": header + " Approval required"}},
                {"tag": "div", "text": {"tag": "plain_text", "content": json.dumps(payload["subject"], ensure_ascii=False)}},
                {"tag": "action", "actions": [{"tag": "button", "text": {"tag": "plain_text", "content": "Approve"},
                                               "type": "primary", "value": payload["card"]}]},
            ]}
            message_type = "interactive"
        else:
            import html
            mention = ""
            if topic in {"worker.offline", "item.escalated"}:
                mention = '<at user_id="' + html.escape(self.mention_ids.get(payload["mention_user_id"], payload["mention_user_id"]), quote=True) + '">owner</at> '
            content = {"text": mention + html.escape(collaboration_summary(topic, payload))}
            message_type = "text"
        response = self.client.post("https://open.feishu.cn/open-apis/im/v1/messages",
            params={"receive_id_type": "chat_id"}, headers={"Authorization": "Bearer " + self.bots[bot].token()},
            json={"receive_id": self.chat_id, "msg_type": message_type,
                  "content": json.dumps(content, ensure_ascii=False), "uuid": event_id}, timeout=10)
        response.raise_for_status()
        if response.json().get("code") != 0:
            raise Conflict("IM provider rejected delivery")


class OutboxDispatcher:
    def __init__(self, store, adapter, max_tries=5):
        if max_tries < 1:
            raise ValueError("max_tries must be positive")
        self.store, self.adapter, self.max_tries = store, adapter, max_tries

    def once(self):
        event = self.store.claim_outbox(max_tries=self.max_tries)
        if event is None:
            return False
        try:
            payload = json.loads(event["payload"])
            if "item" in payload:
                item = self.store.get_item(payload["item"])
                payload = {"slice_id": item["slice_id"], "owner": item["owner"], **payload}
            self.adapter.send(event["id"], event["topic"], payload)
        except Exception as exc:
            # Provider exceptions can include webhook tokens/URLs: store a safe diagnostic only.
            error = type(exc).__name__
            if isinstance(exc, httpx.HTTPStatusError):
                error += ": HTTP " + str(exc.response.status_code)
            self.store.ack_outbox(event["id"], event["claim"], False, error, max_tries=self.max_tries)
            return False
        self.store.ack_outbox(event["id"], event["claim"], True, max_tries=self.max_tries)
        return True

class FeishuVerifier:
    """Verify the signature over the ORIGINAL request bytes before parsing JSON."""
    def __init__(self, encrypt_key, clock, tolerance=300):
        if not encrypt_key:
            raise ValueError("Feishu encrypt key required")
        self.key, self.clock, self.tolerance = encrypt_key, clock, tolerance

    def verify(self, headers, body):
        import hashlib
        import hmac
        from .errors import Forbidden
        timestamp = headers.get("x-lark-request-timestamp", "")
        nonce = headers.get("x-lark-request-nonce", "")
        signature = headers.get("x-lark-signature", "")
        try:
            if not timestamp.isdecimal() or abs(self.clock() - int(timestamp)) > self.tolerance or not nonce or len(nonce) > 256:
                raise ValueError()
        except ValueError as exc:
            raise Forbidden("expired or malformed Feishu signature") from exc
        expected = hashlib.sha256((timestamp + nonce + self.key).encode() + body).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise Forbidden("invalid Feishu signature")
        return "feishu:" + hashlib.sha256((timestamp + ":" + nonce).encode()).hexdigest()


class DingTalkAdapter:
    """Markdown via a configured robot webhook OR enterprise robot OpenAPI.

    Delivery is at-least-once: neither transport is assumed to deduplicate event_id.
    Secrets are injected at runtime and must not be written to event payloads.
    """
    def __init__(self, client, *, webhook_url=None, secret=None, token=None,
                 robot_code=None, conversation_id=None, mention_ids=None, clock=None):
        import time
        if bool(webhook_url) == bool(token):
            raise ValueError("configure exactly one DingTalk transport")
        if webhook_url and not webhook_url.startswith("https://oapi.dingtalk.com/robot/send?"):
            raise ValueError("expected DingTalk HTTPS robot webhook")
        if token and (not robot_code or not conversation_id):
            raise ValueError("OpenAPI requires robot and conversation identifiers")
        self.client, self.webhook_url, self.secret, self.token = client, webhook_url, secret, token
        self.robot_code, self.conversation_id = robot_code, conversation_id
        self.mention_ids, self.clock = mention_ids or {}, clock or time.time

    @staticmethod
    def sign(timestamp, secret):
        import base64
        import hashlib
        import hmac
        return base64.b64encode(hmac.new(secret.encode(), (timestamp + "\n" + secret).encode(), hashlib.sha256).digest()).decode()

    def send(self, event_id, topic, payload):
        text = collaboration_summary(topic, payload)
        staff_id = self.mention_ids.get(payload.get("mention_user_id"))
        if staff_id:
            text = "@" + staff_id + " " + text
        markdown = {"title": "协同通知 · " + topic, "text": text + "\n\n事件：" + event_id}
        if self.webhook_url:
            params = None
            if self.secret:
                timestamp = str(int(self.clock() * 1000))
                params = {"timestamp": timestamp, "sign": self.sign(timestamp, self.secret)}
            # Merge signing query parameters without dropping the webhook access_token.
            url = httpx.URL(self.webhook_url)
            if params:
                url = url.copy_merge_params(params)
            response = self.client.post(url, json={"msgtype": "markdown", "markdown": markdown,
                "at": {"atUserIds": [staff_id] if staff_id else [], "isAtAll": False}}, timeout=10)
        else:
            response = self.client.post("https://api.dingtalk.com/v1.0/robot/groupMessages/send",
                headers={"x-acs-dingtalk-access-token": self.token()}, json={
                    "robotCode": self.robot_code, "openConversationId": self.conversation_id,
                    "msgKey": "sampleMarkdown", "msgParam": json.dumps(markdown, ensure_ascii=False)}, timeout=10)
        response.raise_for_status()
        body = response.json()
        if (self.webhook_url and body.get("errcode") != 0) or (not self.webhook_url and not body.get("processQueryKey")):
            raise Conflict("IM provider rejected delivery")


class DingTalkVerifier:
    """Robot timestamp signature, NOT a body MAC or an encrypted event callback decoder.

    Callers MUST independently authenticate their callback gateway before using sender mappings
    for authorization. Possession of a timestamp signature does not attest senderStaffId.
    """
    def __init__(self, secret, clock, users=None, tolerance=300):
        if not secret:
            raise ValueError("DingTalk signing secret required")
        self.secret, self.clock, self.users, self.tolerance = secret, clock, users or {}, tolerance

    def verify(self, headers, body):
        import hashlib
        import hmac
        from .errors import Forbidden
        timestamp, signature = headers.get("timestamp", ""), headers.get("sign", "")
        if (not timestamp.isdecimal() or len(timestamp) > 16
                or abs(self.clock() - int(timestamp) / 1000) > self.tolerance):
            raise Forbidden("expired or malformed DingTalk signature")
        expected = DingTalkAdapter.sign(timestamp, self.secret)
        if not hmac.compare_digest(expected.encode(), signature.encode()):
            raise Forbidden("invalid DingTalk signature")
        event = json.loads(body)
        if not isinstance(event, dict) or "encrypt" in event:
            raise ValueError("expected robot JSON callback")
        msg_id = event.get("msgId")
        if isinstance(msg_id, str) and msg_id.strip():
            return "dingtalk:msg:" + hashlib.sha256(msg_id.encode()).hexdigest()
        if "msgId" in event:
            raise ValueError("msgId must be a nonblank string when supplied")
        return "dingtalk:ts:" + hashlib.sha256((timestamp + ":" + signature).encode()).hexdigest()

    def sender(self, event):
        from .errors import Forbidden
        staff_id = event.get("senderStaffId")
        if not isinstance(staff_id, str) or staff_id not in self.users:
            raise Forbidden("unknown DingTalk sender")
        return self.users[staff_id]
