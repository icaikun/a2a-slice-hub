"""Single-host SQLite coordinator. Never perform network or test work in a transaction."""
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import time
from uuid import uuid4

from .errors import Conflict, Forbidden, NotFound
from .workflow import Workflow

TERMINAL = {"completed", "failed", "canceled", "rejected"}
ATTEMPT_EDGES = {
    "submitted": {"working", "failed", "canceled", "rejected"},
    "working": {"input-required", "completed", "failed", "canceled"},
    "input-required": {"working", "failed", "canceled"},
}
SCHEMA = """
CREATE TABLE IF NOT EXISTS work_items (
 id TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('DEV','CONTRACT_PATCH','BUG')),
 slice_id TEXT NOT NULL, owner TEXT NOT NULL, allowed_paths TEXT NOT NULL,
 parent_id TEXT REFERENCES work_items(id), change_set_id TEXT,
 state TEXT NOT NULL DEFAULT 'READY' CHECK(state IN
 ('READY','ACTIVE','WAITING','WAITING_WORKER','L0_PENDING_APPROVAL','BLOCKED_ENV','FLAKY_BLOCKED','REWORK','GATE_PENDING','GATING','MERGED_PENDING_QA','ACCEPTED','CLOSED','BLOCKED')),
 version INTEGER NOT NULL DEFAULT 0,
 epoch INTEGER NOT NULL DEFAULT 0, dispute_round INTEGER NOT NULL DEFAULT 0,
 rework_round INTEGER NOT NULL DEFAULT 0, negotiation_round INTEGER NOT NULL DEFAULT 0,
 blocked_reason TEXT, deadline REAL NOT NULL, integration_sha TEXT,
 epic_id TEXT REFERENCES epics(id), dependency_key TEXT, dependency_request TEXT,
 env_round INTEGER NOT NULL DEFAULT 0, flaky_round INTEGER NOT NULL DEFAULT 0, flaky_observed TEXT
);
CREATE TABLE IF NOT EXISTS attempts (
 id TEXT PRIMARY KEY, work_item_id TEXT NOT NULL REFERENCES work_items(id),
 worker_id TEXT NOT NULL, epoch INTEGER NOT NULL, state TEXT NOT NULL CHECK(state IN
 ('submitted','working','input-required','completed','failed','canceled','rejected')),
 expires REAL NOT NULL, version INTEGER NOT NULL DEFAULT 0,
 base_sha TEXT, source_sha TEXT, artifact TEXT NOT NULL DEFAULT '{}'
);
CREATE UNIQUE INDEX IF NOT EXISTS one_live_attempt ON attempts(work_item_id)
 WHERE state IN ('submitted','working','input-required');
CREATE UNIQUE INDEX IF NOT EXISTS one_worker_slot ON attempts(worker_id)
 WHERE state IN ('submitted','working');
CREATE TABLE IF NOT EXISTS dependencies (
 item TEXT NOT NULL REFERENCES work_items(id), needs TEXT NOT NULL REFERENCES work_items(id),
 PRIMARY KEY(item,needs), CHECK(item <> needs)
);
CREATE TABLE IF NOT EXISTS approvals (
 id TEXT PRIMARY KEY, item TEXT NOT NULL,
 subject_digest TEXT NOT NULL, expected_version INTEGER NOT NULL, expires REAL NOT NULL,
 action TEXT NOT NULL, approved_by TEXT
);
CREATE TABLE IF NOT EXISTS outbox (
 id TEXT PRIMARY KEY, topic TEXT NOT NULL, payload TEXT NOT NULL,
 available REAL NOT NULL, claimed_until REAL NOT NULL DEFAULT 0,
 claim TEXT, tries INTEGER NOT NULL DEFAULT 0, delivered INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS inbox (
 id TEXT PRIMARY KEY, digest TEXT NOT NULL, result TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS merge_intents (
 id TEXT PRIMARY KEY, branch TEXT NOT NULL, base_sha TEXT NOT NULL,
 candidate_sha TEXT NOT NULL, items TEXT NOT NULL, evidence_json TEXT NOT NULL DEFAULT '{}', state TEXT NOT NULL DEFAULT 'PREPARED'
);
CREATE UNIQUE INDEX IF NOT EXISTS one_branch_intent ON merge_intents(branch)
 WHERE state = 'PREPARED';
"""


def uid():
    return uuid4().hex


def strict_integer(value, minimum=0):
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ValueError("expected a strict integer")
    return value


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


class Store(Workflow):
    def __init__(self, database, clock=time.time, registry=None):
        self.registry = registry
        self.database = str(Path(database))
        self.clock = clock
        Path(database).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            deadline = time.monotonic() + 30
            while True:
                try:
                    conn.execute("PRAGMA journal_mode=WAL")
                    break
                except sqlite3.OperationalError as exc:
                    # WAL's initial journal upgrade can bypass SQLite's busy timeout.
                    if (getattr(exc, "sqlite_errorcode", 0) & 255) not in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED} or time.monotonic() >= deadline:
                        raise
                    time.sleep(0.05)
            conn.execute("PRAGMA foreign_keys=OFF")
            try:
                conn.execute("BEGIN IMMEDIATE")
                if conn.execute("PRAGMA user_version").fetchone()[0] > 4:
                    raise Conflict("database schema is newer than this Hub")
                self.migrate(conn, SCHEMA)
                self.migrate_round3(conn)
                self.migrate_round4(conn)
                if conn.execute("PRAGMA foreign_key_check").fetchall():
                    raise Conflict("migration foreign-key check failed")
                conn.execute("PRAGMA user_version=4")
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            finally:
                conn.execute("PRAGMA foreign_keys=ON")

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.database, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=FULL")
        try:
            yield conn
        finally:
            conn.close()  # Connection context manager alone does NOT close on Windows.

    @contextmanager
    def transaction(self):
        with self.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                yield conn
                conn.commit()
            except sqlite3.IntegrityError as exc:
                conn.rollback()
                raise Conflict("constraint conflict") from exc
            except sqlite3.OperationalError as exc:
                conn.rollback()
                raise Conflict("database temporarily unavailable; retry safely") from exc
            except BaseException:
                conn.rollback()
                raise

    @staticmethod
    def one(conn, table, key):
        # Table names below are constants controlled by this module's callers, not HTTP input.
        if table not in {"work_items", "attempts", "approvals", "merge_intents", "epics", "outbox"}:
            raise ValueError("invalid table")
        row = conn.execute(f"SELECT * FROM {table} WHERE id=?", (key,)).fetchone()
        if row is None:
            raise NotFound("record not found")
        return dict(row)

    def get_item(self, key):
        with self.connect() as conn:
            return self.one(conn, "work_items", key)

    def get_attempt(self, key):
        with self.connect() as conn:
            return self.one(conn, "attempts", key)

    def emit(self, conn, topic, payload):
        key = uid()
        conn.execute("INSERT INTO outbox(id,topic,payload,available,priority) VALUES(?,?,?,?,?)",
                     (key, topic, encode(payload), self.clock(), 10 if topic in {"item.escalated", "approval.requested", "worker.offline"} else 0))
        return key

    def create_item(self, kind, slice_id, owner, allowed_paths, *, parent_id=None,
                    change_set_id=None, deadline=None, epic_id=None):
        from .paths import safe_path
        if kind not in {"DEV", "CONTRACT_PATCH", "BUG"} or not owner or not slice_id:
            raise ValueError("invalid work item")
        paths = [safe_path(p) for p in allowed_paths]
        if not paths:
            raise ValueError("empty ownership boundary")
        if self.registry:
            spec = self.registry.get(slice_id)
            if owner != spec.owner_user_id:
                raise Forbidden("owner must match protected slice registry")
            from .paths import inside
            grants = self.registry.paths(slice_id, kind)
            if any(not any(inside(p, g) for g in grants) for p in paths):
                raise Forbidden("paths exceed protected slice registry")
        key = uid()
        with self.transaction() as conn:
            if epic_id:
                epic = self.one(conn, "epics", epic_id)
                if epic["state"] == "READY_FOR_RELEASE":
                    raise Conflict("release epic is sealed")
            if parent_id:
                parent = self.one(conn, "work_items", parent_id)
                if parent["kind"] != "BUG" or parent["parent_id"] or not change_set_id or parent["change_set_id"] != change_set_id:
                    raise Conflict("child requires a BUG parent and identical change_set_id")
                if parent["state"] not in {"READY", "REWORK"}:
                    raise Conflict("parent cannot accept children")
                if parent["epic_id"] != epic_id:
                    raise Conflict("child must share parent epic")
            conn.execute("""INSERT INTO work_items
                (id,kind,slice_id,owner,allowed_paths,parent_id,change_set_id,deadline,epic_id)
                VALUES(?,?,?,?,?,?,?,?,?)""", (key, kind, slice_id, owner, encode(paths),
                parent_id, change_set_id, self.clock() + 86400 if deadline is None else deadline, epic_id))
            self._ready(conn, key)
            self.emit(conn, "item.created", {"item": key, "slice_id": slice_id, "owner": owner})
        return self.get_item(key)

    def add_dependency(self, item, needs):
        with self.transaction() as conn:
            current = self.one(conn, "work_items", item)
            if current["state"] not in {"READY", "REWORK", "WAITING", "WAITING_WORKER", "L0_PENDING_APPROVAL"}:
                raise Conflict("dependencies cannot change after dispatch")
            children = conn.execute("SELECT * FROM work_items WHERE parent_id=?", (item,)).fetchall()
            if any(c["state"] not in {"READY", "REWORK", "WAITING", "WAITING_WORKER", "L0_PENDING_APPROVAL"} for c in children):
                raise Conflict("aggregate dependencies cannot change after child dispatch")
            self.one(conn, "work_items", needs)
            cycle = self._dependency_cycle(conn, item, needs)
            if cycle:
                raise Conflict("dependency cycle; use an atomic change set instead")
            conn.execute("INSERT INTO dependencies VALUES(?,?)", (item, needs))
            self._ready(conn, item, ready_state="REWORK" if current["state"] == "REWORK" else "READY")
            # Dependency mutation invalidates approvals even if dispatchability is unchanged.
            conn.execute("UPDATE work_items SET version=MAX(version,?) WHERE id=?", (current["version"] + 1, item))
            for child in children:
                self._ready(conn, child["id"], ready_state="REWORK" if child["state"] == "REWORK" else "READY")
                conn.execute("UPDATE work_items SET version=MAX(version,?) WHERE id=?", (child["version"] + 1, child["id"]))

    def _eligible(self, conn, item):
        if item.get("epic_id") and self.one(conn, "epics", item["epic_id"])["state"] != "ACTIVE":
            raise Conflict("epic not approved or sealed")
        if item["deadline"] <= self.clock():
            raise Conflict("deadline expired")
        if item["parent_id"]:
            parent = self.one(conn, "work_items", item["parent_id"])
            if parent["state"] == "BLOCKED":
                raise Conflict("parent ownership dispute is blocked")
        if conn.execute("SELECT 1 FROM work_items WHERE parent_id=?", (item["id"],)).fetchone():
            raise Conflict("aggregate parent cannot execute a development attempt")
        unmet = any(d["state"] not in {"ACCEPTED", "CLOSED"} for d in self._dependencies(conn, item["id"]))
        if unmet:
            raise Conflict("dependency is not accepted")

    def claim(self, item_id, worker_id, expected_version, ttl=60, *, request_id=None, request_digest=None):
        strict_integer(ttl, 1)
        if not 1 <= ttl <= 3600:
            raise ValueError("invalid lease duration")
        strict_integer(expected_version)
        if self.registry:
            self.refresh_workers()
        with self.transaction() as conn:
            if request_id:
                self._check_merge_receipt(conn, request_id)
                old = conn.execute("SELECT * FROM inbox WHERE id=?", (request_id,)).fetchone()
                if old:
                    if old["digest"] != request_digest:
                        raise Conflict("message ID reused with different content")
                    return json.loads(old["result"]) if old["result"].startswith("{") else self.one(conn, "attempts", old["result"])
            item = self.one(conn, "work_items", item_id)
            self.authorize_worker(conn, item, worker_id)
            if item["version"] != expected_version:
                raise Conflict("stale work item version")
            self._eligible(conn, item)
            abandoned = conn.execute("""SELECT id,work_item_id FROM attempts WHERE worker_id=?
                AND work_item_id<>? AND expires<=? AND state IN ('submitted','working')""",
                (worker_id, item_id, self.clock())).fetchall()
            for old in abandoned:
                conn.execute("UPDATE attempts SET state='failed',version=version+1 WHERE id=?", (old["id"],))
                conn.execute("UPDATE work_items SET state='REWORK',version=version+1 WHERE id=? AND state='ACTIVE'",
                             (old["work_item_id"],))
                self.emit(conn, "attempt.expired", {"item": old["work_item_id"], "attempt": old["id"]})
            active = conn.execute("""SELECT * FROM attempts WHERE work_item_id=? AND state IN
                ('submitted','working','input-required')""", (item_id,)).fetchone()
            if active:
                if active["expires"] > self.clock():
                    raise Conflict("attempt still leased")
                conn.execute("UPDATE attempts SET state='failed',version=version+1 WHERE id=?", (active["id"],))
            elif item["state"] not in {"READY", "REWORK"}:
                raise Conflict("work item cannot be dispatched")
            key, epoch = uid(), item["epoch"] + 1
            conn.execute("""INSERT INTO attempts(id,work_item_id,worker_id,epoch,state,expires)
                VALUES(?,?,?,?,'submitted',?)""", (key, item_id, worker_id, epoch, self.clock() + ttl))
            conn.execute("UPDATE work_items SET state='ACTIVE',epoch=?,version=version+1 WHERE id=?", (epoch, item_id))
            self.emit(conn, "attempt.submitted", {"item": item_id, "attempt": key})
            result = self.one(conn, "attempts", key)
            if request_id:
                conn.execute("INSERT INTO inbox VALUES(?,?,?)", (request_id, request_digest, encode(result)))
            return result

    def _fenced(self, conn, key, worker, epoch):
        strict_integer(epoch, 1)
        attempt = self.one(conn, "attempts", key)
        item = self.one(conn, "work_items", attempt["work_item_id"])
        if attempt["worker_id"] != worker:
            raise Forbidden("wrong worker")
        if attempt["epoch"] != epoch or item["epoch"] != epoch or attempt["expires"] <= self.clock() or item["deadline"] <= self.clock():
            raise Conflict("stale or expired lease")
        if item["state"] in {"BLOCKED", "BLOCKED_ENV", "FLAKY_BLOCKED"}:
            raise Conflict("work item blocked")
        if item["parent_id"] and self.one(conn, "work_items", item["parent_id"])["state"] == "BLOCKED":
            raise Conflict("parent blocked")
        return attempt, item

    def heartbeat(self, key, worker, epoch, ttl=60):
        strict_integer(ttl, 1)
        if not 1 <= ttl <= 3600:
            raise ValueError("invalid lease duration")
        with self.transaction() as conn:
            attempt, _ = self._fenced(conn, key, worker, epoch)
            if attempt["state"] in TERMINAL:
                raise Conflict("terminal attempt")
            conn.execute("UPDATE attempts SET expires=? WHERE id=?", (self.clock() + ttl, key))

    def transition(self, key, worker, epoch, expected_version, state, *, base_sha=None, source_sha=None, artifact=None, request_id=None, request_digest=None):
        from .git import checked_sha
        strict_integer(epoch, 1)
        strict_integer(expected_version)
        with self.transaction() as conn:
            if request_id:
                self._check_merge_receipt(conn, request_id)
                prior = conn.execute("SELECT * FROM inbox WHERE id=?", (request_id,)).fetchone()
                if prior:
                    if prior["digest"] != request_digest:
                        raise Conflict("message ID reused with different content")
                    return json.loads(prior["result"]) if prior["result"].startswith("{") else self.one(conn, "attempts", prior["result"])
            attempt, item = self._fenced(conn, key, worker, epoch)
            if attempt["version"] != expected_version or state not in ATTEMPT_EDGES.get(attempt["state"], set()):
                raise Conflict("invalid attempt transition or stale version")
            if state == "working":
                self._eligible(conn, item)
            if state == "completed":
                checked_sha(base_sha)
                checked_sha(source_sha)
            next_state = {"completed": "GATE_PENDING", "failed": "REWORK", "canceled": "REWORK",
                          "rejected": "REWORK", "input-required": "WAITING", "working": "ACTIVE"}[state]
            reason = None
            if state in {"failed", "canceled", "rejected"} and any(
                    d["state"] not in {"ACCEPTED", "CLOSED"} for d in self._dependencies(conn, item["id"])):
                next_state, reason = "WAITING", "dependency"
            rounds = item["negotiation_round"] + (state == "input-required")
            if rounds >= 3 and state == "input-required":
                next_state = "BLOCKED"
                state = "failed"
            conn.execute("""UPDATE attempts SET state=?,version=version+1,base_sha=?,source_sha=?,artifact=? WHERE id=?""",
                         (state, base_sha, source_sha, encode(artifact or {}), key))
            conn.execute("""UPDATE work_items SET state=?,version=version+1,negotiation_round=?,
                blocked_reason=? WHERE id=?""", (next_state, rounds,
                "negotiation_limit" if next_state == "BLOCKED" else reason, item["id"]))
            if state == "input-required" and self.registry:
                self._route_dependency(conn, item, artifact)
            self.emit(conn, "attempt." + state, {"item": item["id"], "attempt": key})
            result = self.one(conn, "attempts", key)
            if request_id:
                conn.execute("INSERT INTO inbox VALUES(?,?,?)", (request_id, request_digest, encode(result)))
            return result

    def reject(self, item_id, expected_version, reason):
        strict_integer(expected_version)
        with self.transaction() as conn:
            item = self.one(conn, "work_items", item_id)
            if item["version"] != expected_version or item["state"] != "GATE_PENDING":
                raise Conflict("cannot reject this version")
            rounds = item["rework_round"] + 1
            conn.execute("UPDATE work_items SET state=?,version=version+1,rework_round=?,blocked_reason=? WHERE id=?",
                         ("BLOCKED" if rounds >= 3 else "REWORK", rounds, reason, item_id))
            self.emit(conn, "gate.rejected", {"item": item_id, "reason": reason})

    def dispute(self, item_id, worker, evidence):
        if not evidence:
            raise ValueError("evidence required")
        with self.transaction() as conn:
            item = self.one(conn, "work_items", item_id)
            self.authorize_worker(conn, item, worker)
            target = self.one(conn, "work_items", item["parent_id"] or item_id)
            if conn.execute("SELECT 1 FROM work_items WHERE parent_id=?", (item_id,)).fetchone():
                raise Conflict("aggregate work must be disputed by a responsible child module")
            generation = target["dispute_generation"]
            # A changing item version is NOT a new arbitration round.
            if conn.execute("SELECT 1 FROM disputes WHERE item=? AND generation=?", (item_id, generation)).fetchone():
                raise Conflict("this item already disputed in this arbitration round")
            frozen = {"ACCEPTED", "CLOSED", "GATING", "GATE_PENDING", "MERGED_PENDING_QA", "BLOCKED", "BLOCKED_ENV", "FLAKY_BLOCKED"}
            if target["state"] in frozen or item["state"] in frozen or item["deadline"] <= self.clock():
                raise Conflict("cannot dispute frozen, blocked or terminal work")
            lease = conn.execute("SELECT * FROM attempts WHERE work_item_id=? AND state IN ('submitted','working','input-required')", (item_id,)).fetchone()
            if lease and (lease["worker_id"] != worker or lease["expires"] <= self.clock()):
                raise Conflict("only the current live lease holder may dispute active work")
            # Multiple children of ONE module cannot impersonate independent responsibility domains.
            if conn.execute("SELECT 1 FROM disputes WHERE target=? AND generation=? AND slice_id=?", (target["id"], generation, item["slice_id"])).fetchone():
                raise Conflict("this responsibility module already disputed this round")
            if conn.execute("SELECT 1 FROM disputes WHERE target=? AND generation=? AND owner=?",
                            (target["id"], generation, item["owner"])).fetchone():
                raise Conflict("this owner already disputed in this arbitration round")
            conn.execute("INSERT INTO disputes VALUES(?,?,?,?,?,?,?,?,?)", (item_id, target["id"], generation,
                item["slice_id"], item["version"], item["rework_round"], worker, evidence, item["owner"]))
            n = conn.execute("SELECT count(*) FROM disputes WHERE target=? AND generation=?",
                             (target["id"], generation)).fetchone()[0]
            conn.execute("UPDATE work_items SET dispute_round=?,version=version+1,state=?,blocked_reason=? WHERE id=?",
                         (n, "BLOCKED" if n >= 2 else target["state"], "ownership_dispute", target["id"]))
            if n >= 2:
                conn.execute("""UPDATE attempts SET state='failed',version=version+1 WHERE state IN
                    ('submitted','working','input-required') AND work_item_id IN
                    (SELECT id FROM work_items WHERE id=? OR parent_id=?)""", (target["id"], target["id"]))
                conn.execute("""UPDATE work_items SET state='BLOCKED',version=version+1,
                    blocked_reason='ownership_dispute' WHERE parent_id=?""", (target["id"],))
            self.emit(conn, "ownership.dispute", {"item": target["id"], "child": item_id,
                "slice_id": item["slice_id"], "owner": item["owner"], "round": n, "evidence": evidence})

    def qa(self, item_id, expected_version, integration_sha, passed, actor, report=None, *, outcome=None, request_id=None, request_digest=None):
        outcome = outcome or ("PASS" if passed else "FAIL")
        if outcome not in {"PASS", "FAIL", "ENV_ERROR", "FLAKY"}:
            raise ValueError("invalid QA outcome")
        if passed is not None and passed != (outcome == "PASS"):
            raise ValueError("contradictory QA result")
        strict_integer(expected_version)
        passed = outcome == "PASS"
        # actor is supplied by the authenticated QA route, never by a developer's body.
        with self.transaction() as conn:
            if request_id:
                self._check_merge_receipt(conn, request_id)
                prior = conn.execute("SELECT * FROM inbox WHERE id=?", (request_id,)).fetchone()
                if prior:
                    if prior["digest"] != request_digest:
                        raise Conflict("message ID reused with different content")
                    return json.loads(prior["result"])
            item = self.one(conn, "work_items", item_id)
            if item["parent_id"]:
                raise Conflict("change-set children require aggregate parent QA")
            if actor == item["owner"]:
                raise Forbidden("developer cannot QA their own work")
            if item["state"] != "MERGED_PENDING_QA" or item["version"] != expected_version or item["integration_sha"] != integration_sha:
                raise Conflict("QA must attest the exact pending snapshot and version")
            children = conn.execute("SELECT * FROM work_items WHERE parent_id=?", (item_id,)).fetchall()
            if children and any(c["integration_sha"] != integration_sha or c["state"] not in {"MERGED_PENDING_QA", "ACCEPTED", "CLOSED"} for c in children):
                raise Conflict("parent requires all fixes in one snapshot")
            if any(c["owner"] == actor for c in children):
                raise Forbidden("QA must be independent of every child owner")
            if outcome in {"ENV_ERROR", "FLAKY"} or item["flaky_round"]:
                if self._qa_exception(conn, item, outcome, report):
                    result = self.one(conn, "work_items", item_id)
                    if request_id:
                        conn.execute("INSERT INTO inbox VALUES(?,?,?)", (request_id, request_digest, encode(result)))
                    return result
            if not passed:
                from .models import BugReport
                BugReport.model_validate(report)
                if report["target"]["integration_sha"] != integration_sha:
                    raise Conflict("bug report snapshot mismatch")
            rounds = item["rework_round"] + (not passed)
            state = ("CLOSED" if item["kind"] == "BUG" else "ACCEPTED") if passed else ("BLOCKED" if rounds >= 3 else "REWORK")
            conn.execute("UPDATE work_items SET state=?,version=version+1,rework_round=?,blocked_reason=? WHERE id=?",
                         (state, rounds, None if passed else "qa_failed", item_id))
            if children and passed:
                if any(c["owner"] == actor for c in children):
                    raise Forbidden("QA must be independent of every child owner")
                conn.execute("""UPDATE work_items SET state=CASE WHEN kind='BUG' THEN 'CLOSED' ELSE 'ACCEPTED' END,
                    version=version+1,blocked_reason=NULL WHERE parent_id=?""", (item_id,))
            if children and not passed:
                conn.execute("UPDATE work_items SET state=?,version=version+1 WHERE parent_id=?",
                             ("BLOCKED" if rounds >= 3 else "REWORK", item_id))
            if passed:
                if conn.execute("SELECT 1 FROM merge_intents WHERE state='PREPARED'").fetchone():
                    raise Conflict("regression promotion must wait for prepared merge recovery")
                self._accepted(conn, [item, *children], integration_sha)
            self.emit(conn, "qa.result", {"item": item_id, "sha": integration_sha, "passed": passed, "outcome": outcome, "report": report})
            result = self.one(conn, "work_items", item_id)
            if request_id:
                conn.execute("INSERT INTO inbox VALUES(?,?,?)", (request_id, request_digest, encode(result)))
            return result

    @staticmethod
    def _check_merge_receipt(conn, request_id):
        if conn.execute("SELECT 1 FROM merge_intents WHERE request_id=?", (request_id,)).fetchone():
            raise Conflict("message ID is reserved by a merge request")

    def prepare_merge(self, branch, base, candidate, items, evidence=None, regression_snapshot=None, approval_grants=(), request_id=None, request_digest=None):
        """Freeze validated item versions before touching the Git ref; journal survives crashes."""
        key = uid()
        with self.transaction() as conn:
            if request_id and conn.execute("SELECT 1 FROM inbox WHERE id=?", (request_id,)).fetchone():
                raise Conflict("message ID already completed; retry or choose a new message ID")
            if regression_snapshot is not None and self.accepted_slices(conn) != set(regression_snapshot):
                raise Conflict("accepted regression set changed; rerun gate")
            for grant_id in approval_grants:
                approval = self.one(conn, "approvals", grant_id)
                if (not approval["approved_by"] or approval["action"] != "breaking_contract"
                        or approval["expires"] <= self.clock()
                        or items.get(approval["item"]) != approval["expected_version"]):
                    raise Conflict("breaking approval expired or changed during validation")
            rows = [self.one(conn, "work_items", i) for i in items]
            if not rows:
                raise ValueError("empty merge")
            parents = set()
            for row in rows:
                if row["state"] != "GATE_PENDING" or row["version"] != items[row["id"]]:
                    raise Conflict("item changed during validation")
                if row["epic_id"]:
                    epic = self.one(conn, "epics", row["epic_id"])
                    if epic["state"] != "ACTIVE" or epic["branch"] != branch:
                        raise Conflict("epic no longer active on this branch")
                if row["deadline"] <= self.clock():
                    raise Conflict("deadline expired")
                if row["parent_id"]:
                    parents.add(row["parent_id"])
                if row["change_set_id"]:
                    members = conn.execute("""SELECT id FROM work_items WHERE change_set_id=? AND NOT EXISTS
                        (SELECT 1 FROM work_items c WHERE c.parent_id=work_items.id)""", (row["change_set_id"],)).fetchall()
                    if any(m["id"] not in items for m in members):
                        raise Conflict("partial change set forbidden")
            for parent in parents:
                p = self.one(conn, "work_items", parent)
                if p["state"] not in {"READY", "REWORK"}:
                    raise Conflict("parent not mergeable")
                if any(r["epic_id"] != p["epic_id"] for r in rows if r["parent_id"] == parent):
                    raise Conflict("child must share parent epic")
                if p["epic_id"]:
                    epic = self.one(conn, "epics", p["epic_id"])
                    if epic["state"] != "ACTIVE" or epic["branch"] != branch:
                        raise Conflict("parent epic not active on this branch")
            ids = sorted(set(items) | parents)
            conn.execute("INSERT INTO merge_intents(id,branch,base_sha,candidate_sha,items,evidence_json) VALUES(?,?,?,?,?,?)",
                         (key, branch, base, candidate, encode(ids), encode(evidence or {})))
            if request_id:
                conn.execute("UPDATE merge_intents SET request_id=?,request_digest=? WHERE id=?", (request_id, request_digest, key))
            for item_id in ids:
                conn.execute("UPDATE work_items SET state='GATING',version=version+1 WHERE id=?", (item_id,))
        return key

    def finish_merge(self, intent_id, publish):
        """Only the bounded local Git ref operation is inside this short transaction.

        PREPARED is committed beforehand. A crash after ref CAS rolls back this transaction,
        leaving a recoverable intent rather than silently accepting unrecorded work.
        """
        with self.transaction() as conn:
            intent = self.one(conn, "merge_intents", intent_id)
            if intent["state"] != "PREPARED":
                return intent["state"]
            outcome = publish(intent)  # returns True for published/in ancestry; False for stale base
            state = "COMMITTED" if outcome else "STALE"
            for key in json.loads(intent["items"]):
                conn.execute("UPDATE work_items SET state=?,integration_sha=?,version=version+1,env_round=0,flaky_round=0 WHERE id=?",
                    ("MERGED_PENDING_QA" if outcome else "REWORK", intent["candidate_sha"] if outcome else None, key))
            conn.execute("UPDATE merge_intents SET state=? WHERE id=?", (state, intent_id))
            if outcome and intent["request_id"]:
                result = {"candidate_sha": intent["candidate_sha"], "base_sha": intent["base_sha"], "intent": intent_id,
                          "tests": json.loads(intent["evidence_json"]).get("tests", {})}
                conn.execute("INSERT INTO inbox VALUES(?,?,?)", (intent["request_id"], intent["request_digest"], encode(result)))
            self.emit(conn, "merge." + state.lower(), {"intent": intent_id, "candidate": intent["candidate_sha"]})
            return state

    def claim_outbox(self, ttl=30, max_tries=5):
        if ttl <= 0 or max_tries < 1:
            raise ValueError("invalid outbox retry policy")
        with self.transaction() as conn:
            # A worker crash must not bypass the retry cap after its lease expires.
            conn.execute("""UPDATE outbox SET dead_letter=1,last_error=COALESCE(last_error,'delivery lease expired'),
                claim=NULL,claimed_until=0 WHERE delivered=0 AND dead_letter=0 AND tries>=? AND claimed_until<=?""",
                (max_tries, self.clock()))
            row = conn.execute("""SELECT * FROM outbox WHERE delivered=0 AND dead_letter=0 AND available<=?
                AND claimed_until<=? ORDER BY CASE WHEN priority>=10 OR (? - available)>=300 THEN 10
                ELSE priority END DESC,available ASC,id ASC LIMIT 1""", (self.clock(), self.clock(), self.clock())).fetchone()
            if row is None:
                return None
            token = uid()
            conn.execute("UPDATE outbox SET claim=?,claimed_until=?,tries=tries+1 WHERE id=?",
                         (token, self.clock() + ttl, row["id"]))
            return dict(row) | {"claim": token, "tries": row["tries"] + 1}

    def ack_outbox(self, key, claim, success, error=None, max_tries=5):
        with self.transaction() as conn:
            row = self.one(conn, "outbox", key)
            delay = 0 if success else min(300, 5 * (2 ** min(max(row["tries"] - 1, 0), 6)))
            changed = conn.execute("""UPDATE outbox SET delivered=?,claim=NULL,claimed_until=0,available=?,
                dead_letter=?,last_error=? WHERE id=? AND claim=? AND delivered=0 AND dead_letter=0 AND claimed_until>?""",
                (int(success), self.clock() + delay, int(not success and row["tries"] >= max_tries),
                 None if success else (error or "delivery failed")[:1000], key, claim, self.clock())).rowcount
            if not changed:
                raise Conflict("stale outbox delivery claim")

    def dead_letters(self, limit=100):
        if not 1 <= limit <= 1000:
            raise ValueError("invalid dead letter limit")
        with self.connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM outbox WHERE dead_letter=1 ORDER BY available,id LIMIT ?", (limit,))]

    def replay_dead_letter(self, key, actor, max_replays: int = 3):
        strict_integer(max_replays, 1)
        if max_replays > 10:
            raise ValueError("replay budget exceeds 10")
        if not isinstance(actor, str) or not actor.strip():
            raise ValueError("audit actor required")
        with self.transaction() as conn:
            row = self.one(conn, "outbox", key)
            if row["dead_letter"] != 1 or row["delivered"] != 0:
                raise Conflict("only undelivered dead letters can be replayed")
            if row["replay_count"] >= max_replays:
                # No new outbox event on rejection: repeated rejected requests cannot create storms.
                raise Conflict("dead-letter replay budget exhausted")
            conn.execute("""UPDATE outbox SET dead_letter=0,tries=0,available=?,claim=NULL,claimed_until=0,
                replay_count=replay_count+1 WHERE id=?""", (self.clock(), key))
            self.emit(conn, "outbox.replayed", {"event_id": key, "actor": actor, "replay_count": row["replay_count"] + 1})
            return self.one(conn, "outbox", key)

    def prune_history(self, *, delivered_before, limit=100):
        strict_integer(limit, 1)
        if limit > 1000:
            raise ValueError("prune limit exceeds 1000")
        with self.transaction() as conn:
            return conn.execute("""DELETE FROM outbox WHERE id IN (SELECT id FROM outbox
                WHERE delivered=1 AND dead_letter=0 AND available<? ORDER BY available,id LIMIT ?)""",
                (delivered_before, limit)).rowcount
