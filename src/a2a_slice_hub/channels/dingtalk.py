"""DingTalk chatbot messages and interactive-card callbacks are distinct protocols.

Call ingress only from an authenticated Stream session or trusted IM gateway.
"""
import json

from ..errors import Conflict, Forbidden
from ..im import DingTalkAdapter
from ..policy import ExternalAccount, safe_identifier
from .common import accept_callback, signed_card, payload_object, object_field, object_list, validated_ingress
from .gating import ChannelGate, ChatEnvelope

CHAT_TOPIC = "/v1.0/im/bot/messages/get"
CARD_TOPIC = "/v1.0/card/instances/callback"


def card_data(card=None, *, status="PENDING", approved_by=None):
    # The configured provider template must bind showApprove/disabled to its button.
    return {"cardParamMap": {"status": status, "approved_by": approved_by or "",
        "text": "Approval required" if status == "PENDING" else (
            "Approved by " + str(approved_by) if status == "APPROVED" else "Approval expired"),
        "showApprove": "true" if status == "PENDING" else "false",
        "disabled": "false" if status == "PENDING" else "true",
        "card": json.dumps(card) if status == "PENDING" else ""}}


class DingTalkChannelAdapter(DingTalkAdapter):
    provider = "dingtalk"

    def __init__(self, client, *, token, robot_code, conversation_id, card_template_id,
                 destination_id, store=None, mention_ids=None):
        super().__init__(client, token=token, robot_code=robot_code, conversation_id=conversation_id, mention_ids=mention_ids)
        safe_identifier(destination_id)
        if not card_template_id:
            raise ValueError("interactive card template required")
        self.card_template_id, self.destination_id, self.store = card_template_id, destination_id, store

    def send(self, event_id, topic, payload):
        if topic != "approval.requested":
            return super().send(event_id, topic, payload)
        track = f"approval-{payload['card']['approval_id']}-{self.destination_id}"
        status, actor = payload.get("status", "PENDING"), payload.get("approved_by")
        response = self.client.post("https://api.dingtalk.com/v1.0/card/instances/createAndDeliver",
            headers={"x-acs-dingtalk-access-token": self.token()}, json={
                "cardTemplateId": self.card_template_id, "outTrackId": track,
                "cardData": card_data(payload["card"], status=status, approved_by=actor),
                "callbackType": "STREAM", "openSpaceId": "dtv1.card//IM_GROUP." + self.conversation_id,
                "imGroupOpenSpaceModel": {"supportForward": False},
                "imGroupOpenDeliverModel": {"robotCode": self.robot_code}}, timeout=10)
        response.raise_for_status()
        body = response.json()
        if body.get("success") is False or body.get("code") not in (None, 0, "0", "OK"):
            raise Conflict("IM provider rejected card delivery")
        track = body.get("outTrackId") or (body.get("result") or {}).get("outTrackId") or track
        if self.store is not None:
            self.store.record_card_projection(payload["card"]["approval_id"], self.destination_id, self.provider,
                track, rendered_version=payload.get("rendered_version", 1), status=status, approved_by=actor)
        return track

    def update_card(self, projection, status, approved_by):
        if projection["rendered_version"] >= 2 or (projection["status"] != "PENDING" and status == "PENDING"):
            return False
        if self.store is not None:
            current = next((p for p in self.store.get_card_projections(projection["approval_id"])
                            if p["destination_id"] == projection["destination_id"]), projection)
            if current["rendered_version"] >= 2 or (current["status"] != "PENDING" and status == "PENDING"):
                return False
        response = self.client.put("https://api.dingtalk.com/v1.0/card/instances",
            headers={"x-acs-dingtalk-access-token": self.token()}, json={"outTrackId": projection["provider_card_id"],
                "cardData": card_data(status=status, approved_by=approved_by)}, timeout=10)
        response.raise_for_status()
        body = response.json()
        if body.get("success") is False or body.get("code") not in (None, 0, "0", "OK"):
            raise Conflict("IM provider rejected card update")
        if self.store is not None:
            self.store.record_card_projection(projection["approval_id"], projection["destination_id"], self.provider,
                projection["provider_card_id"], rendered_version=2, status=status, approved_by=approved_by)
        return True


class DingTalkChatIngress:
    topic = CHAT_TOPIC

    def __init__(self, gate: ChannelGate, *, app_id=None, chatbot_user_id=None):
        self.gate, self.app_id, self.chatbot_user_id = gate, app_id, chatbot_user_id

    @validated_ingress
    def normalize_chat(self, body):
        payload_object(body)
        app_id = body.get("robotCode", "")
        if self.app_id is not None and app_id != self.app_id:
            raise Forbidden("unexpected DingTalk application")
        bot_ids = {app_id, self.chatbot_user_id or body.get("chatbotUserId")} - {None, ""}
        mentions = object_list(body, "atUsers")
        mentioned = any(any(user.get(key) in bot_ids for key in ("dingtalkId", "staffId", "userId", "robotCode"))
                        for user in mentions)
        return ChatEnvelope("dingtalk", body.get("senderCorpId", ""), app_id,
            {"1": "dm", "2": "group"}.get(str(body.get("conversationType")), "unknown"),
            body.get("conversationId", ""), body.get("senderStaffId", ""), body.get("msgId", ""),
            object_field(body, "text").get("content", ""), mentions_bot=mentioned or (body.get("isInAtList") is True and body.get("isAtAll") is not True),
            mentions_all=body.get("isAtAll") is True,
            is_bot=body.get("isBot") is True or body.get("senderType") in {"app", "bot"})

    def handle_chat(self, body):
        return self.gate.evaluate(self.normalize_chat(body))


class DingTalkCardIngress:
    topic = CARD_TOPIC

    def __init__(self, approvals, identity_policy, *, app_id):
        safe_identifier(app_id)
        self.approvals, self.identity_policy, self.app_id = approvals, identity_policy, app_id

    @staticmethod
    def _card(body):
        # Stream SDK wraps the interactive payload in a JSON-encoded content field;
        # authenticated gateways may already have unwrapped cardPrivateData.
        content = body.get("content", {})
        if isinstance(content, str):
            content = json.loads(content)
        if not isinstance(content, dict):
            raise ValueError("invalid interactive callback content")
        private = body.get("cardPrivateData", content.get("cardPrivateData", {}))
        if isinstance(private, str):
            private = json.loads(private)
        if not isinstance(private, dict):
            raise ValueError("invalid private card data")
        params = private.get("params", {})
        if isinstance(params, str):
            params = json.loads(params)
        if not isinstance(params, dict):
            raise ValueError("invalid card parameters")
        return signed_card(params.get("card"))

    @validated_ingress
    def handle_card(self, body, *, receipt_id=None):
        payload_object(body)
        if (body.get("app_id") or body.get("robotCode") or self.app_id) != self.app_id:
            raise Forbidden("unexpected DingTalk application")
        if not isinstance(body.get("outTrackId"), str) or not body["outTrackId"].strip():
            raise Forbidden("card tracking identity required")
        card = self._card(body)
        receipt = body.get("eventId") or body.get("msgId") or receipt_id
        return accept_callback(self.approvals, self.identity_policy,
            ExternalAccount("dingtalk", body.get("corpId"), self.app_id, body.get("userId")), card, receipt)

    def card_response(self, body, *, receipt_id=None):
        result = self.handle_card(body, receipt_id=receipt_id)
        card = self._card(body)
        with self.approvals.store.connect() as conn:
            actor = self.approvals.store.one(conn, "approvals", card["approval_id"])["approved_by"]
        return {**result, "cardData": card_data(status="APPROVED", approved_by=actor)}


def register_dingtalk_stream_handlers(stream_client, chat_ingress, card_ingress):
    import asyncio
    import dingtalk_stream

    class ChatHandler(dingtalk_stream.CallbackHandler):
        async def process(self, message):
            await asyncio.to_thread(chat_ingress.handle_chat, message.data)
            return dingtalk_stream.AckMessage.STATUS_OK, "OK"

    class CardHandler(dingtalk_stream.CallbackHandler):
        async def process(self, message):
            # Header message_id is a persistent provider receipt, not SDK dedup state.
            result = await asyncio.to_thread(card_ingress.card_response, message.data,
                                            receipt_id=message.headers.message_id)
            return dingtalk_stream.AckMessage.STATUS_OK, {"cardData": result["cardData"]}

    stream_client.register_callback_handler(CHAT_TOPIC, ChatHandler())
    stream_client.register_callback_handler(CARD_TOPIC, CardHandler())
