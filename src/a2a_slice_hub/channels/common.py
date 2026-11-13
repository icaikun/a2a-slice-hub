"""Shared authenticated callback persistence. SDK dedup is not authoritative."""
import hashlib
import json
import math
from functools import wraps

from ..errors import Conflict, Forbidden
from ..store import encode


def validated_ingress(method):
    @wraps(method)
    def call(self, body, *args, **kwargs):
        payload_object(body)
        try:
            return method(self, body, *args, **kwargs)
        except (TypeError, AttributeError, RecursionError) as exc:
            raise ValueError("malformed callback fields") from exc
    return call


def payload_object(value):
    if not isinstance(value, dict):
        raise ValueError("callback object required")
    try:
        if len(json.dumps(value, allow_nan=False).encode("utf-8")) > 1_000_000:
            raise ValueError("callback too large")
    except (TypeError, RecursionError) as exc:
        raise ValueError("invalid callback JSON") from exc
    return value


def object_field(value, key, *, decode=False):
    result = value.get(key, {})
    if decode and isinstance(result, str):
        result = json.loads(result)
    if not isinstance(result, dict):
        raise ValueError("callback field must be an object")
    return result


def object_list(value, key):
    result = value.get(key, [])
    if not isinstance(result, list) or any(not isinstance(entry, dict) for entry in result):
        raise ValueError("callback field must be an object list")
    return result


def signed_card(value):
    if isinstance(value, str):
        value = json.loads(value)
    if isinstance(value, dict) and "card" in value:
        value = value["card"]
        if isinstance(value, str):
            value = json.loads(value)
    fields = {"approval_id", "item_id", "action", "subject_digest", "signature",
              "expected_state_version", "expires_at"}
    if not isinstance(value, dict) or set(value) != fields:
        raise Forbidden("signed approval card required")
    if (any(not isinstance(value[key], str) or not value[key] or len(value[key]) > 512
            for key in fields - {"expected_state_version", "expires_at"})
            or type(value["expected_state_version"]) is not int or value["expected_state_version"] < 0
            or type(value["expires_at"]) not in (int, float) or not math.isfinite(value["expires_at"])):
        raise Forbidden("invalid signed approval fields")
    return value


def accept_callback(approvals, policy, account, card, receipt_id):
    if not isinstance(receipt_id, str) or not receipt_id.strip() or len(receipt_id) > 512:
        raise Forbidden("persistent callback receipt required")
    actor = policy.authorize_approval(account, item_owner=approvals.target_owner(card["approval_id"]), action=card["action"])
    # Scope receipt to the authenticated application; bind actor AND complete signed card.
    scope = [account.provider, account.tenant_or_corp, account.app_id, receipt_id]
    receipt = "channel:" + hashlib.sha256(encode(scope).encode()).hexdigest()
    digest = hashlib.sha256(encode([account.external_user_id, card]).encode()).hexdigest()
    with approvals.store.connect() as conn:
        old = conn.execute("SELECT * FROM inbox WHERE id=?", (receipt,)).fetchone()
        if old:
            if old["digest"] != digest:
                raise Conflict("callback nonce reused with different content")
            return json.loads(old["result"])
    return approvals.approve(card, actor, receipt=receipt, digest=digest, timeout=2.0)
