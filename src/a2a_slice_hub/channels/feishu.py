"""Feishu role-split outbound transport and authenticated WS/webhook normalization.

Ingress must be called only after transport signature verification or by the SDK's
application-authenticated WS session. Normalization itself is not authentication.
"""
import hashlib
import json
from urllib.parse import quote

from ..errors import Conflict, Forbidden
from ..im import BOTS, FeishuAdapter
from ..policy import ExternalAccount
from ..store import encode
from .common import accept_callback, signed_card, payload_object, object_field, object_list, validated_ingress
from .gating import ChannelGate, ChatEnvelope, GatedDecision


def render_card(card=None, *, status="PENDING", approved_by=None):
    text = "Approval required" if status == "PENDING" else (
        "Approved by " + str(approved_by) if status == "APPROVED" else "Approval expired")
    elements = [{"tag": "div", "text": {"tag": "plain_text", "content": text}}]
    if status == "PENDING":
        elements.append({"tag": "action", "actions": [{"tag": "button", "type": "primary",
            "text": {"tag": "plain_text", "content": "Approve"}, "value": card}]})
    return {"elements": elements}


class FeishuChannelAdapter(FeishuAdapter):
    provider = "feishu"

    def __init__(self, bots, chat_id, client, *, store=None, destination_id=None, mention_ids=None):
        super().__init__(bots, chat_id, client, mention_ids=mention_ids)
        self.store, self.destination_id = store, destination_id

    def send(self, event_id, topic, payload):
        if topic != "approval.requested":
            return super().send(event_id, topic, payload)
        status, actor = payload.get("status", "PENDING"), payload.get("approved_by")
        response = self.client.post("https://open.feishu.cn/open-apis/im/v1/messages",
            params={"receive_id_type": "chat_id"}, headers={"Authorization": "Bearer " + self.bots["orchestrator_bot"].token()},
            json={"receive_id": self.chat_id, "msg_type": "interactive", "uuid": event_id,
                  "content": json.dumps(render_card(payload["card"], status=status, approved_by=actor))}, timeout=10)
        response.raise_for_status()
        body = response.json()
        if body.get("code", 0) != 0 or not body.get("data", {}).get("message_id"):
            raise Conflict("IM provider rejected card delivery")
        receipt = body["data"]["message_id"]
        if self.store is not None and self.destination_id is not None:
            self.store.record_card_projection(payload["card"]["approval_id"], self.destination_id, self.provider,
                receipt, rendered_version=payload.get("rendered_version", 1), status=status, approved_by=actor)
        return receipt

    def update_card(self, projection, status, approved_by):
        if projection["rendered_version"] >= 2 or (projection["status"] != "PENDING" and status == "PENDING"):
            return False
        if self.store is not None:
            current = next((p for p in self.store.get_card_projections(projection["approval_id"])
                            if p["destination_id"] == projection["destination_id"]), projection)
            if current["rendered_version"] >= 2 or (current["status"] != "PENDING" and status == "PENDING"):
                return False
        response = self.client.patch("https://open.feishu.cn/open-apis/im/v1/messages/" + quote(projection["provider_card_id"], safe=""),
            headers={"Authorization": "Bearer " + self.bots["orchestrator_bot"].token()},
            json={"content": json.dumps(render_card(status=status, approved_by=approved_by))}, timeout=10)
        response.raise_for_status()
        if response.json().get("code", 0) != 0:
            raise Conflict("IM provider rejected card update")
        if self.store is not None:
            self.store.record_card_projection(projection["approval_id"], projection["destination_id"], self.provider,
                projection["provider_card_id"], rendered_version=2, status=status, approved_by=approved_by)
        return True


class FeishuIngress:
    def __init__(self, approvals, identity_policy, gate=None, *, bots=None, orchestrator_app_id=None, bot_open_id=None):
        if bots is not None:
            if set(bots) != BOTS or len({b.app_id for b in bots.values()}) != 3:
                raise ValueError("three distinct Feishu bot identities required")
            orchestrator_app_id = bots["orchestrator_bot"].app_id
        if not orchestrator_app_id:
            raise ValueError("orchestrator application identity required")
        self.approvals, self.identity_policy = approvals, identity_policy
        self.gate = gate or ChannelGate()
        self.app_id = orchestrator_app_id
        self.bot_ids = {orchestrator_app_id, bot_open_id} - {None}

    def _scope(self, body):
        payload_object(body)
        header = object_field(body, "header")
        event = object_field(body, "event") if "event" in body else body
        app_id = header.get("app_id") or event.get("app_id") or body.get("app_id")
        if app_id != self.app_id:
            raise Forbidden("only orchestrator_bot accepts inbound events")
        tenant = header.get("tenant_key") or event.get("tenant_key") or body.get("tenant_key")
        return event, app_id, tenant

    @validated_ingress
    def normalize_chat(self, body):
        event, app_id, tenant = self._scope(body)
        sender, message = object_field(event, "sender"), object_field(event, "message")
        mentions = object_list(message, "mentions")
        bot_mention = any((object_field(m, "id").get("open_id") in self.bot_ids or object_field(m, "id").get("app_id") in self.bot_ids)
                          and m.get("key") != "@_all" for m in mentions)
        content = object_field(message, "content", decode=True)
        text = content.get("text", "")
        if not isinstance(text, str):
            raise ValueError("message text must be a string")
        return ChatEnvelope("feishu", tenant or sender.get("tenant_key", ""), app_id,
            {"p2p": "dm", "group": "group"}.get(message.get("chat_type"), "unknown"), message.get("chat_id", ""),
            object_field(sender, "sender_id").get("open_id", ""), message.get("message_id", ""), text,
            mentions_bot=bot_mention, mentions_all="@_all" in text or any(m.get("key") == "@_all" for m in mentions),
            is_bot=sender.get("sender_type") in {"app", "bot"})

    def handle_chat(self, body):
        try:
            envelope = self.normalize_chat(body)
        except Forbidden:
            return GatedDecision(False, "outbound-only application")
        return self.gate.evaluate(envelope)

    @validated_ingress
    def handle_card(self, body):
        event, app_id, tenant = self._scope(body)
        operator = object_field(event, "operator")
        open_id = operator.get("open_id") or event.get("open_id")
        tenant = tenant or operator.get("tenant_key")
        card = signed_card(object_field(event, "action").get("value"))
        receipt = body.get("header", {}).get("event_id") or event.get("event_id") or event.get("token")
        if not receipt:
            # Legacy callbacks lack event_id. Bind a deterministic receipt to card,
            # application, user and message, not the mutable HTTP signature timestamp.
            receipt = hashlib.sha256(encode([tenant, app_id, open_id, event.get("open_message_id"), card]).encode()).hexdigest()
        return accept_callback(self.approvals, self.identity_policy,
            ExternalAccount("feishu", tenant, app_id, open_id), card, receipt)

    def card_response(self, body):
        result = self.handle_card(body)
        event = body.get("event", body)
        card = signed_card(event["action"]["value"])
        with self.approvals.store.connect() as conn:
            actor = self.approvals.store.one(conn, "approvals", card["approval_id"])["approved_by"]
        return {"toast": {"type": "success", "content": "Approval accepted"},
                "card": {"type": "raw", "data": render_card(status="APPROVED", approved_by=actor)}, **result}


def build_lark_ws_dispatcher(ingress, *, encrypt_key="", verification_token=""):
    import lark_oapi as lark
    from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTriggerResponse

    def card_callback(data):
        response = ingress.card_response(json.loads(lark.JSON.marshal(data)))
        return P2CardActionTriggerResponse({k: response[k] for k in ("toast", "card")})

    return (lark.EventDispatcherHandler.builder(encrypt_key, verification_token)
            .register_p2_im_message_receive_v1(lambda data: ingress.handle_chat(json.loads(lark.JSON.marshal(data))))
            .register_p2_card_action_trigger(card_callback).build())
