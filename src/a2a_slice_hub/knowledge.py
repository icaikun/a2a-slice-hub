"""Scoped, redacted evidence and QA-verified cases; Git ADRs remain authoritative.

Search deliberately uses deterministic tokenized LIKE instead of requiring FTS5.
External export is optional, at-least-once, and never holds a SQLite write lock.
"""
from contextlib import nullcontext
from dataclasses import asdict
import json
import math
import re
from uuid import uuid4

from .errors import Conflict, Forbidden, NotFound
from .policy import MemoryScope

SCHEMA_V6 = """
CREATE TABLE IF NOT EXISTS runtime_evidence (
 id TEXT PRIMARY KEY, org_id TEXT NOT NULL, project_id TEXT NOT NULL, repo_id TEXT NOT NULL, visibility TEXT NOT NULL DEFAULT 'project',
 item_id TEXT, attempt_id TEXT, kind TEXT NOT NULL, environment_json TEXT NOT NULL DEFAULT '{}',
 summary TEXT NOT NULL, payload_json TEXT NOT NULL DEFAULT '{}', created_at REAL NOT NULL, expires_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS evidence_scope ON runtime_evidence(org_id, project_id, repo_id, expires_at);
CREATE TABLE IF NOT EXISTS troubleshooting_cases (
 case_id TEXT PRIMARY KEY, org_id TEXT NOT NULL, project_id TEXT NOT NULL, repo_id TEXT NOT NULL, visibility TEXT NOT NULL DEFAULT 'project',
 item_id TEXT NOT NULL, integration_sha TEXT NOT NULL, fix_sha TEXT, slice_id TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('CANDIDATE','VERIFIED','INVALIDATED')),
 failure_signature TEXT NOT NULL, root_cause TEXT NOT NULL, resolution_steps TEXT NOT NULL,
 falsified_approaches_json TEXT NOT NULL DEFAULT '[]', environment_json TEXT NOT NULL DEFAULT '{}',
 linked_adr_ids_json TEXT NOT NULL DEFAULT '[]', qa_reviewer TEXT, verified_at REAL, invalidated_at REAL,
 invalidation_reason TEXT, superseded_by_adr TEXT, created_at REAL NOT NULL,
 UNIQUE(org_id, project_id, repo_id, visibility, item_id, integration_sha)
);
CREATE INDEX IF NOT EXISTS cases_scope ON troubleshooting_cases(org_id, project_id, repo_id, status);
CREATE TABLE IF NOT EXISTS everos_export_queue (
 case_id TEXT PRIMARY KEY REFERENCES troubleshooting_cases(case_id) ON DELETE CASCADE,
 payload_json TEXT NOT NULL, available REAL NOT NULL, tries INTEGER NOT NULL DEFAULT 0,
 dead_letter INTEGER NOT NULL DEFAULT 0, last_error TEXT, exported_at REAL
);
"""
_SCOPE = "org_id=? AND project_id=? AND repo_id=? AND visibility=?"
_KINDS = {"cli_failure", "gate_rejected", "qa_bug_report", "env_error"}


def redact_secrets(value):
    """Conservative recursive default; no raw credentials cross storage boundaries."""
    if isinstance(value, dict):
        return {redact_secrets(str(k)): ("[REDACTED]" if re.search(
            r"(?i)(?:token|password|secret|authorization|api[_-]?key)", str(k)) else redact_secrets(v))
            for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [redact_secrets(v) for v in value]
    if not isinstance(value, str):
        return value
    value = re.sub(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----.*?(?:-----END (?:[A-Z0-9]+ )*PRIVATE KEY-----|\Z)",
                   "[REDACTED PRIVATE KEY]", value, flags=re.S)
    # Redact whole URLs with userinfo/query/fragment: signed links have many vendor-specific keys.
    value = re.sub(r"https?://[^\s<>\"']+", lambda m: "[REDACTED URL]" if any(
        c in m[0] for c in ("@", "?", "#")) else m[0], value, flags=re.I)
    value = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [REDACTED]", value)
    return re.sub(r"\b(?:github_pat_|gh[pousr]_|sk-|xox[a-z]*-)[A-Za-z0-9_-]+", "[REDACTED]", value)


def _decode(row):
    result = dict(row)
    for key in tuple(result):
        if key.endswith("_json"):
            result[key[:-5]] = json.loads(result.pop(key))
    return result


def _strings(value):
    if isinstance(value, str):
        return {value}
    if isinstance(value, dict):
        return set(value) | {s for v in value.values() for s in _strings(v)}
    if isinstance(value, (list, tuple, set, frozenset)):
        return {s for v in value for s in _strings(v)}
    return set()


class KnowledgeCenter:
    def __init__(self, store, *, redact=None, identity_policy=None, principal=None):
        self.store, self.redact = store, redact
        self.identity_policy, self.principal = identity_policy, principal
        if principal is not None and identity_policy is None:
            raise ValueError("principal requires identity policy")

    def _scope(self, scope: MemoryScope):
        if not isinstance(scope, MemoryScope):
            raise Forbidden("server-constructed MemoryScope required")
        if self.identity_policy is not None and self.principal is not None:
            if scope not in self.identity_policy._person(self.principal).scopes:
                raise Forbidden("memory scope is not authorized")
        return scope.org_id, scope.project_id, scope.repo_id, scope.visibility

    def _clean(self, value):
        value = redact_secrets(value)
        return redact_secrets(self.redact(value)) if self.redact else value

    def _json(self, value):
        return json.dumps(self._clean(value), ensure_ascii=True, sort_keys=True, allow_nan=False)

    def record_evidence(self, scope: MemoryScope, *, kind, summary, item_id=None, attempt_id=None,
                        environment=None, payload=None, ttl_seconds=14 * 86400, _conn=None):
        params = self._scope(scope)
        if kind not in _KINDS:
            raise ValueError("unsupported evidence kind")
        if isinstance(ttl_seconds, bool) or not math.isfinite(ttl_seconds) or ttl_seconds <= 0:
            raise ValueError("positive finite evidence TTL required")
        now, key = self.store.clock(), uuid4().hex
        with (self.store.transaction() if _conn is None else nullcontext(_conn)) as conn:
            conn.execute("INSERT INTO runtime_evidence(id,org_id,project_id,repo_id,visibility,item_id,attempt_id,kind,"
                "environment_json,summary,payload_json,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (key, *params, self._clean(item_id), self._clean(attempt_id), kind,
                 self._json(environment or {}), self._clean(summary), self._json(payload or {}), now, now + ttl_seconds))
            row = conn.execute("SELECT * FROM runtime_evidence WHERE " + _SCOPE + " AND id=?", (*params, key)).fetchone()
        return _decode(row) | {"authority": "unverified_evidence"}

    def list_evidence(self, scope: MemoryScope, *, item_id=None, limit=20):
        params = self._scope(scope)
        clause, extra = (" AND item_id=?", (item_id,)) if item_id is not None else ("", ())
        with self.store.connect() as conn:
            rows = conn.execute("SELECT * FROM runtime_evidence WHERE " + _SCOPE +
                " AND expires_at>?" + clause + " ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (*params, self.store.clock(), *extra, self._limit(limit))).fetchall()
        return [_decode(r) | {"authority": "unverified_evidence"} for r in rows]

    @staticmethod
    def _limit(limit):
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise ValueError("positive integer limit required")
        return limit

    def prune_evidence(self, scope: MemoryScope):
        params = self._scope(scope)
        with self.store.transaction() as conn:
            return conn.execute("DELETE FROM runtime_evidence WHERE " + _SCOPE + " AND expires_at<=?",
                                (*params, self.store.clock())).rowcount

    def propose_case(self, scope: MemoryScope, *, item_id, integration_sha=None, fix_sha=None, slice_id,
                     failure_signature, root_cause, resolution_steps, falsified_approaches=(),
                     environment=None, linked_adr_ids=(), _conn=None):
        params = self._scope(scope)
        sha = integration_sha or ""
        with (self.store.transaction() if _conn is None else nullcontext(_conn)) as conn:
            old = conn.execute("SELECT * FROM troubleshooting_cases WHERE " + _SCOPE +
                               " AND item_id=? AND integration_sha=?", (*params, item_id, sha)).fetchone()
            if old and old["status"] != "CANDIDATE":
                raise Conflict("verified or invalidated case cannot be overwritten by a proposal")
            key = old["case_id"] if old else uuid4().hex
            values = (self._clean(fix_sha), slice_id, self._clean(failure_signature), self._clean(root_cause),
                      self._clean(resolution_steps), self._json(falsified_approaches), self._json(environment or {}),
                      self._json(linked_adr_ids))
            if old:
                conn.execute("UPDATE troubleshooting_cases SET fix_sha=?,slice_id=?,failure_signature=?,root_cause=?,"
                    "resolution_steps=?,falsified_approaches_json=?,environment_json=?,linked_adr_ids_json=? WHERE " +
                    _SCOPE + " AND case_id=?", (*values, *params, key))
            else:
                conn.execute("INSERT INTO troubleshooting_cases(case_id,org_id,project_id,repo_id,visibility,item_id,"
                    "integration_sha,status,fix_sha,slice_id,failure_signature,root_cause,resolution_steps,"
                    "falsified_approaches_json,environment_json,linked_adr_ids_json,created_at) "
                    "VALUES(?,?,?,?,?,?,?,'CANDIDATE',?,?,?,?,?,?,?,?,?)", (key, *params, item_id, sha, *values, self.store.clock()))
            return _decode(conn.execute("SELECT * FROM troubleshooting_cases WHERE " + _SCOPE +
                                       " AND case_id=?", (*params, key)).fetchone())

    def promote_verified_case(self, scope: MemoryScope, *, item_id, integration_sha, qa_reviewer,
                              case_id=None, enqueue_everos=True, _conn=None):
        params = self._scope(scope)
        with (self.store.transaction() if _conn is None else nullcontext(_conn)) as conn:
            item = conn.execute("SELECT * FROM work_items WHERE id=?", (item_id,)).fetchone()
            if not item or not integration_sha or item["state"] not in {"ACCEPTED", "CLOSED"} or item["integration_sha"] != integration_sha:
                raise Conflict("item has not passed Gatekeeper and independent QA at this snapshot")
            owners = {item["owner"]} | {r[0] for r in conn.execute("""WITH RECURSIVE children AS (
                SELECT id,owner FROM work_items WHERE parent_id=? UNION ALL
                SELECT w.id,w.owner FROM work_items w JOIN children c ON w.parent_id=c.id)
                SELECT owner FROM children""", (item_id,))}
            if not isinstance(qa_reviewer, str) or not qa_reviewer.strip() or qa_reviewer in owners:
                raise Forbidden("independent QA reviewer required")
            if self.identity_policy is not None:
                qa_reviewer = self.identity_policy.authorize_independent_qa(qa_reviewer, owners=owners)
            existing = conn.execute("SELECT * FROM troubleshooting_cases WHERE " + _SCOPE +
                " AND item_id=? AND integration_sha=?", (*params, item_id, integration_sha)).fetchone()
            if existing and existing["status"] == "INVALIDATED":
                raise Conflict("invalidated case cannot be promoted")
            if existing and existing["status"] == "VERIFIED":
                row = existing
            else:
                clause, extra = (" AND case_id=?", (case_id,)) if case_id else ("", ())
                candidates = conn.execute("SELECT * FROM troubleshooting_cases WHERE " + _SCOPE +
                    " AND item_id=? AND status='CANDIDATE'" + clause + " ORDER BY rowid", (*params, item_id, *extra)).fetchall()
                if not candidates:
                    raise NotFound("candidate case not found")
                matching = [r for r in candidates if r["integration_sha"] == integration_sha]
                candidates = matching or candidates
                if len(candidates) != 1:
                    raise Conflict("ambiguous candidates; specify case_id")
                row = candidates[0]
                conn.execute("UPDATE troubleshooting_cases SET status='VERIFIED',integration_sha=?,qa_reviewer=?,verified_at=? WHERE " +
                    _SCOPE + " AND case_id=?", (integration_sha, qa_reviewer, self.store.clock(), *params, row["case_id"]))
                row = conn.execute("SELECT * FROM troubleshooting_cases WHERE " + _SCOPE + " AND case_id=?", (*params, row["case_id"])).fetchone()
            result = _decode(row)
            if enqueue_everos:
                payload = {"scope": asdict(scope), "verified_case": result, "authority": "qa_verified_case"}
                conn.execute("INSERT OR IGNORE INTO everos_export_queue(case_id,payload_json,available) VALUES(?,?,?)",
                             (result["case_id"], self._json(payload), self.store.clock()))
            return result

    def invalidate_case(self, scope: MemoryScope, case_id, reason, *, superseded_by_adr=None):
        params = self._scope(scope)
        with self.store.transaction() as conn:
            changed = conn.execute("UPDATE troubleshooting_cases SET status='INVALIDATED',invalidated_at=?,"
                "invalidation_reason=?,superseded_by_adr=? WHERE " + _SCOPE + " AND case_id=?",
                (self.store.clock(), self._clean(reason), self._clean(superseded_by_adr), *params, case_id)).rowcount
            if not changed:
                raise NotFound("case not found")
            conn.execute("DELETE FROM everos_export_queue WHERE case_id IN (SELECT case_id FROM troubleshooting_cases WHERE " +
                         _SCOPE + " AND case_id=?)", (*params, case_id))
            return _decode(conn.execute("SELECT * FROM troubleshooting_cases WHERE " + _SCOPE + " AND case_id=?", (*params, case_id)).fetchone())

    def search_cases(self, scope: MemoryScope, query, *, active_adrs=None, limit=5):
        params = self._scope(scope)
        limit = self._limit(limit)
        # Wildcards/punctuation are not executable search syntax.
        tokens = sorted(set(re.findall(r"[^\W_]+", query.casefold(), re.UNICODE)))
        conditions, args = [], []
        for token in tokens:
            conditions.append("lower(failure_signature || ' ' || root_cause || ' ' || resolution_steps) LIKE ?")
            args.append("%" + token + "%")
        with self.store.connect() as conn:
            rows = conn.execute("SELECT * FROM troubleshooting_cases WHERE " + _SCOPE + " AND status='VERIFIED'" +
                (" AND (" + " OR ".join(conditions) + ")" if conditions else "") +
                " ORDER BY verified_at DESC, case_id", (*params, *args)).fetchall()
        if tokens:
            rows.sort(key=lambda r: (-sum(token in (r["failure_signature"] + " " + r["root_cause"] + " " +
                r["resolution_steps"]).casefold() for token in tokens), -r["verified_at"], r["case_id"]))
        superseded, invalidated, accepted = set(), set(), None
        if active_adrs is not None:
            if isinstance(active_adrs, dict):
                records = active_adrs.get("active_adrs", active_adrs.get("adrs", ()))
                superseded |= _strings(active_adrs.get("superseded", ()))
                invalidated |= _strings(active_adrs.get("invalidations", ()))
                if "all_accepted_adr_ids" in active_adrs:
                    accepted = _strings(active_adrs["all_accepted_adr_ids"])
            else:
                records = active_adrs
            for adr in records:
                if adr.get("status", "accepted").casefold() == "accepted":
                    superseded |= _strings(adr.get("supersedes", ()))
                    invalidated |= _strings(adr.get("checks", ())) | _strings(adr.get("invalidations", ()))
        results = []
        for row in rows:
            case = _decode(row)
            linked = set(case["linked_adr_ids"])
            if linked & superseded or (accepted is not None and linked - accepted) or case["case_id"] in invalidated:
                continue
            results.append(case)
            if len(results) == limit:
                break
        return results

    def flush_everos_exports(self, everos_hook, max_tries=3, *, scope: MemoryScope):
        """Service calls must explicitly name their scope, just like interactive reads."""
        params = self._scope(scope)
        self._limit(max_tries)
        result = {"exported": 0, "failed": 0}
        with self.store.connect() as conn:
            keys = [r[0] for r in conn.execute("SELECT case_id FROM everos_export_queue WHERE exported_at IS NULL "
                "AND dead_letter=0 AND available<=? AND case_id IN (SELECT case_id FROM troubleshooting_cases WHERE " +
                _SCOPE + " AND status='VERIFIED') ORDER BY available,case_id", (self.store.clock(), *params))]
        for key in keys:
            with self.store.transaction() as conn:
                row = conn.execute("SELECT * FROM everos_export_queue WHERE case_id=? AND exported_at IS NULL AND dead_letter=0 "
                    "AND available<=? AND case_id IN (SELECT case_id FROM troubleshooting_cases WHERE " + _SCOPE +
                    " AND status='VERIFIED')", (key, self.store.clock(), *params)).fetchone()
                if row is None:
                    continue
                # A bounded reservation prevents simultaneous drainers; crashes are retryable.
                reservation = self.store.clock() + 300
                conn.execute("UPDATE everos_export_queue SET available=? WHERE case_id=? AND case_id IN "
                    "(SELECT case_id FROM troubleshooting_cases WHERE " + _SCOPE + ")", (reservation, key, *params))
            try:
                everos_hook.record_trajectory(messages=[{"role": "assistant", "content": row["payload_json"]}])
            except Exception as exc:
                error = type(exc).__name__
            else:
                error = None
            with self.store.transaction() as conn:
                # Recheck scope/status: invalidation while HTTP is in flight must not resurrect a queue row.
                where = "case_id=? AND available=? AND case_id IN (SELECT case_id FROM troubleshooting_cases WHERE " + _SCOPE + " AND status='VERIFIED')"
                if error:
                    changed = conn.execute("UPDATE everos_export_queue SET tries=tries+1,dead_letter=(tries+1>=?),last_error=?,available=? WHERE " + where,
                        (max_tries, error, self.store.clock(), key, reservation, *params)).rowcount
                    result["failed"] += changed
                else:
                    changed = conn.execute("UPDATE everos_export_queue SET exported_at=?,last_error=NULL WHERE " + where,
                                 (self.store.clock(), key, reservation, *params)).rowcount
                    result["exported"] += changed
        return result


class ScopedKnowledgeHook:
    def __init__(self, knowledge_center, scope: MemoryScope, *, git=None, base_sha_provider=None, slice_id=None, paths=()):
        knowledge_center._scope(scope)
        self.knowledge, self.scope = knowledge_center, scope
        self.git, self.base_sha_provider = git, base_sha_provider
        self.slice_id, self.paths = slice_id, paths

    def recall_experience(self, *, task, base_sha=None, slice_id=None, paths=None):
        adrs = None
        if self.git is not None and (base_sha is not None or self.base_sha_provider is not None):
            from .context import load_adrs
            base = base_sha if base_sha is not None else self.base_sha_provider()
            active = load_adrs(self.git, base,
                slice_id=self.slice_id if slice_id is None else slice_id,
                paths=self.paths if paths is None else paths)
            # Global accepted IDs are needed to distinguish unrelated scope from removal.
            # Enumerate ADR scopes, not just this worker's paths, for a complete accepted set.
            all_active = load_adrs(self.git, base, all_scopes=True)
            adrs = {"active_adrs": active, "all_accepted_adr_ids": [a["id"] for a in all_active]}
        return {"scope": asdict(self.scope),
                "verified_cases": self.knowledge.search_cases(self.scope, task, active_adrs=adrs),
                "recent_unverified_evidence": self.knowledge.list_evidence(self.scope)}

    def record_trajectory(self, *, messages):
        return self.knowledge.record_evidence(self.scope, kind="cli_failure", summary="Unverified worker trajectory",
                                               payload={"messages": messages})
