"""Channel admission is separate from canonical authorization."""
from dataclasses import dataclass

from ..errors import Forbidden
from ..policy import ExternalAccount, IdentityPolicy


@dataclass(frozen=True)
class ChatEnvelope:
    provider: str
    tenant_or_corp: str
    app_id: str
    chat_type: str
    chat_id: str
    sender_id: str
    message_id: str
    text: str
    mentions_bot: bool = False
    mentions_all: bool = False
    is_bot: bool = False


@dataclass(frozen=True)
class GatedDecision:
    allowed: bool
    reason: str
    person_id: str | None = None


class ChannelGate:
    def __init__(self, *, dm_policy="disabled", dm_allow_from=(), group_allow_from=(),
                 require_mention=True, bot_ids=(), identity_policy: IdentityPolicy | None = None):
        if dm_policy not in {"disabled", "allowlist", "pairing"}:
            raise ValueError("invalid DM policy")
        self.dm_policy = dm_policy
        self.dm_allow_from, self.group_allow_from = set(dm_allow_from), set(group_allow_from)
        self.require_mention, self.bot_ids = require_mention, set(bot_ids)
        self.identity_policy = identity_policy

    def evaluate(self, envelope):
        e = envelope
        if (any(not isinstance(getattr(e, field), str) for field in
                ("provider", "tenant_or_corp", "app_id", "chat_type", "chat_id", "sender_id", "message_id", "text"))
                or any(type(getattr(e, field)) is not bool for field in ("mentions_bot", "mentions_all", "is_bot"))):
            return GatedDecision(False, "invalid message fields")
        if e.is_bot or e.sender_id in self.bot_ids:
            return GatedDecision(False, "bot echo")
        try:
            account = ExternalAccount(e.provider, e.tenant_or_corp, e.app_id, e.sender_id)
        except ValueError:
            return GatedDecision(False, "invalid sender identity")
        if not e.message_id or not e.chat_id:
            return GatedDecision(False, "missing message identity")
        if e.chat_type == "group":
            if e.chat_id not in self.group_allow_from:
                return GatedDecision(False, "group not allowed")
            if self.require_mention and not e.mentions_bot:
                return GatedDecision(False, "explicit bot mention required")
        elif e.chat_type == "dm":
            if self.dm_policy == "disabled":
                return GatedDecision(False, "DM disabled")
            if self.dm_policy == "allowlist" and e.sender_id not in self.dm_allow_from:
                return GatedDecision(False, "sender not allowed")
            if self.dm_policy == "pairing" and not (self.identity_policy and self.identity_policy.can_contact(account)):
                return GatedDecision(False, "pairing required")
        else:
            return GatedDecision(False, "unsupported chat type")
        person_id = None
        if self.identity_policy:
            try:
                person_id = self.identity_policy.resolve_person(account).person_id
            except Forbidden:
                pass  # Contact admission never creates an identity or role.
        return GatedDecision(True, "allowed", person_id)
