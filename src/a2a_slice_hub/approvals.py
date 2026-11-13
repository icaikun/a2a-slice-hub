"""Version-bound, expiring HMAC cards; approval and outbox commit atomically."""
import hashlib
import json
import hmac
import re

from .errors import Conflict, Forbidden
from .store import encode, uid


class Approvals:
    def __init__(self, store, secret):
        if len(secret) < 32:
            raise ValueError("HMAC key requires at least 32 bytes")
        self.store, self.secret = store, secret

    def _mac(self, payload):
        return hmac.new(self.secret, encode(payload).encode(), hashlib.sha256).hexdigest()

    def issue(self, item_id, subject, ttl=300, action="resume"):
        if not 1 <= ttl <= 3600:
            raise ValueError("invalid approval lifetime")
        with self.store.transaction() as conn:
            if action not in {"resume", "l0", "breaking_contract"}:
                raise ValueError("unknown approval action")
            item = self.store.one(conn, "epics" if action == "l0" else "work_items", item_id)
            self._check_target(item, action)
            if action == "l0" and subject != {"skeleton_sha": item["skeleton_sha"], "branch": item["branch"]}:
                raise Conflict("L0 subject must bind skeleton and branch")
            if action == "breaking_contract" and (not subject.get("contracts") or subject.get("change_set_id") != item["change_set_id"]):
                raise Conflict("breaking approval must bind contract diff and change set")
            payload = {"approval_id": uid(), "item_id": item_id, "action": action,
                       "subject_digest": hashlib.sha256(encode(subject).encode()).hexdigest(),
                       "expected_state_version": item["version"], "expires_at": self.store.clock() + ttl}
            conn.execute("""INSERT INTO approvals(id,item,subject_digest,expected_version,expires,action)
                VALUES(?,?,?,?,?,?)""", (payload["approval_id"], item_id, payload["subject_digest"],
                item["version"], payload["expires_at"], payload["action"]))
            card = payload | {"signature": self._mac(payload)}
            self.store.emit(conn, "approval.requested", {"card": card, "subject": subject,
                            "slice_id": item.get("slice_id", "epic"), "owner": item.get("owner", "coordinator")})
        return card

    def approve(self, card, actor, *, receipt=None, digest=None, timeout: float | None = None):
        fields = {"approval_id", "item_id", "action", "subject_digest", "expected_state_version", "expires_at", "signature"}
        if set(card) != fields or not isinstance(card.get("signature"), str) or not re.fullmatch(r"[0-9a-f]{64}", card["signature"]):
            raise Forbidden("invalid approval card")
        payload = {k: v for k, v in card.items() if k != "signature"}
        if not hmac.compare_digest(self._mac(payload), card["signature"]):
            raise Forbidden("invalid approval signature")
        with self.store.transaction(timeout=timeout) as conn:
            if receipt:
                old = conn.execute("SELECT * FROM inbox WHERE id=?", (receipt,)).fetchone()
                if old:
                    if old["digest"] != digest:
                        raise Conflict("callback nonce reused with different content")
                    return json.loads(old["result"])
            approval = self.store.one(conn, "approvals", card["approval_id"])
            item = self.store.one(conn, "epics" if approval["action"] == "l0" else "work_items", approval["item"])
            if (approval["item"] != card["item_id"] or approval["subject_digest"] != card["subject_digest"]
                    or approval["expected_version"] != card["expected_state_version"]
                    or approval["expires"] != card["expires_at"] or approval["action"] != card["action"]):
                raise Forbidden("approval binding mismatch")
            if approval["approved_by"]:
                result = {"approved": True, "duplicate": True}
                if receipt:
                    conn.execute("INSERT INTO inbox VALUES(?,?,?)", (receipt, digest, encode(result)))
                return result
            if approval["expires"] <= self.store.clock() or item["version"] != approval["expected_version"]:
                raise Conflict("expired or stale approval")
            self._check_target(item, approval["action"])
            conn.execute("UPDATE approvals SET approved_by=? WHERE id=?", (actor, approval["id"]))
            if approval["action"] == "l0":
                conn.execute("UPDATE epics SET state='ACTIVE',version=version+1 WHERE id=?", (item["id"],))
                for row in conn.execute("SELECT id FROM work_items WHERE epic_id=? AND state='L0_PENDING_APPROVAL'", (item["id"],)).fetchall():
                    self.store._ready(conn, row["id"])
            elif approval["action"] == "resume" and item["state"] in {"BLOCKED_ENV", "FLAKY_BLOCKED"}:
                conn.execute("""UPDATE work_items SET state='MERGED_PENDING_QA',version=version+1,
                    blocked_reason=NULL,env_round=0,flaky_round=0,flaky_observed=NULL
                    WHERE id=? OR parent_id=?""", (item["id"], item["id"]))
            elif approval["action"] == "resume":
                conn.execute("""UPDATE attempts SET state='canceled',version=version+1 WHERE state IN
                    ('submitted','working','input-required') AND work_item_id IN
                    (SELECT id FROM work_items WHERE id=? OR parent_id=?)""", (item["id"], item["id"]))
                conn.execute("""UPDATE work_items SET state='REWORK',version=version+1,blocked_reason=NULL,
                    dispute_generation=dispute_generation+1,dispute_round=0,negotiation_round=0,rework_round=0 WHERE id=? OR parent_id=?""", (item["id"], item["id"]))
                children = conn.execute("SELECT id FROM work_items WHERE parent_id=?", (item["id"],)).fetchall()
                for leaf in children or [item]:
                    self.store._ready(conn, leaf["id"], ready_state="REWORK")
            # breaking_contract grants only the exact signed diff at this item version.
            self.store.emit(conn, "approval.accepted", {("epic_id" if approval["action"] == "l0" else "item"): item["id"], "approved_by": actor, "approval_id": approval["id"],
                "action": approval["action"], "expected_state_version": approval["expected_version"]})
            result = {"approved": True, "duplicate": False}
            if receipt:
                conn.execute("INSERT INTO inbox VALUES(?,?,?)", (receipt, digest, encode(result)))
            return result


    def target_owner(self, approval_id):
        with self.store.connect() as conn:
            approval = self.store.one(conn, "approvals", approval_id)
            if approval["action"] == "l0":
                return None
            return self.store.one(conn, "work_items", approval["item"])["owner"]

    @staticmethod
    def _check_target(item, action):
        if action == "resume" and item.get("parent_id"):
            raise Conflict("resume must target the aggregate parent work item")
        if action == "resume" and item["state"] not in {"BLOCKED", "BLOCKED_ENV", "FLAKY_BLOCKED"} and not (
                item.get("blocked_reason") == "ownership_dispute" and item["state"] in {"READY", "ACTIVE", "REWORK", "WAITING", "WAITING_WORKER"}):
            raise Conflict("resume approval requires a blocked item")
        if action == "l0" and item["state"] != "L0_PENDING_APPROVAL":
            raise Conflict("L0 approval requires pending skeleton")
        if action == "breaking_contract" and (item["kind"] != "CONTRACT_PATCH" or not item["change_set_id"] or item["state"] != "GATE_PENDING"):
            raise Conflict("breaking approval requires submitted contract change set")
