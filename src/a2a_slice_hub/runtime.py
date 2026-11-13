"""Single control plane: orchestrate existing Store/Worker/Gatekeeper transitions."""
import json
from pathlib import Path

from .errors import Conflict, Forbidden, GateRejected, NotFound
from .knowledge import redact_secrets
from .policy import safe_identifier


class HubRuntime:
    def __init__(self, store, git, approvals=None, gatekeeper=None, workers=None,
                 im_dispatcher=None, knowledge=None, scope=None, identity_policy=None, everos_hook=None):
        self.store, self.git, self.approvals = store, git, approvals
        self.gatekeeper, self.workers = gatekeeper, dict(workers or {})
        self.im_dispatcher, self.knowledge, self.scope = im_dispatcher, knowledge, scope
        self.identity_policy, self.everos_hook = identity_policy, everos_hook
        if knowledge is not None:
            if Path(knowledge.store.database).resolve() != Path(store.database).resolve():
                raise ValueError("runtime knowledge must share its transactional database")
            knowledge._scope(scope)

    def recover(self):
        result = self.gatekeeper.recover() if self.gatekeeper else None
        if self.store.registry is not None:
            self.store.refresh_workers()
        return result

    def _evidence(self, item_id, kind, summary, *, attempt_id=None, payload=None):
        if self.knowledge is not None:
            return self.knowledge.record_evidence(self.scope, item_id=item_id, attempt_id=attempt_id,
                kind=kind, summary=summary, payload=payload)

    def latest_rework_feedback(self, item_id):
        item = self.store.get_item(item_id)
        feedback = {"blocked_reason": item["blocked_reason"]}
        with self.store.connect() as conn:
            # Outbox rowid provides stable event order even with an injected/frozen clock.
            rows = conn.execute("SELECT topic,payload FROM outbox WHERE topic IN ('qa.result','gate.rejected') ORDER BY rowid DESC").fetchall()
        for row in rows:
            payload = json.loads(row["payload"])
            if payload.get("item") != item_id:
                continue
            if row["topic"] == "qa.result" and payload.get("passed") is not False:
                continue
            feedback.update(event=row["topic"], payload=payload)
            if payload.get("report") is not None:
                feedback["report"] = payload["report"]
            break
        if self.knowledge:
            feedback["recent_unverified_evidence"] = self.knowledge.list_evidence(self.scope, item_id=item_id)
        return redact_secrets(feedback)

    def latest_checkpoint(self, item_id, base_sha):
        with self.store.connect() as conn:
            rows = conn.execute("SELECT id,artifact FROM attempts WHERE work_item_id=? ORDER BY epoch DESC,rowid DESC LIMIT 1", (item_id,)).fetchall()
        for row in rows:
            artifact = json.loads(row["artifact"])
            if not isinstance(artifact, dict) or artifact.get("checkpoint_saved") is not True:
                continue
            if artifact.get("checkpoint_base_sha") != base_sha:
                return None, None
            safe_identifier(row["id"])
            root = self.git.scratch.resolve()
            path = root / "attempts" / row["id"] / "checkpoint.patch"
            components = (path, path.parent, path.parent.parent)
            if (any(p.is_symlink() or getattr(p, "is_junction", lambda: False)() for p in components)
                    or not path.resolve().is_relative_to(root) or not path.is_file()):
                return None, None
            return path.read_bytes(), base_sha
        return None, None

    def _leaves(self, branch, states=None):
        with self.store.connect() as conn:
            epic = conn.execute("SELECT id,state FROM epics WHERE branch=?", (branch,)).fetchone()
            if epic and epic["state"] != "ACTIVE":
                return []
            rows = conn.execute("""SELECT w.* FROM work_items w LEFT JOIN epics e ON w.epic_id=e.id
                WHERE NOT EXISTS (SELECT 1 FROM work_items c WHERE c.parent_id=w.id)
                AND ((w.epic_id IS NULL AND ? IS NULL) OR (e.branch=? AND e.state='ACTIVE'))
                ORDER BY CASE w.state WHEN 'REWORK' THEN 0 ELSE 1 END,w.rowid""",
                (epic["id"] if epic else None, branch)).fetchall()
        return [dict(r) for r in rows if states is None or r["state"] in states]

    def dispatch_coding_once(self, branch, *, task_prompts=None):
        for item in self._leaves(branch, {"READY", "REWORK"}):
            selected = None
            with self.store.connect() as conn:
                try:
                    self.store._eligible(conn, item)
                except Conflict:
                    continue
                for worker, adapter in self.workers.values():
                    try:
                        self.store.authorize_worker(conn, item, worker.worker_id)
                    except Forbidden:
                        continue
                    if conn.execute("SELECT 1 FROM attempts WHERE worker_id=? AND state IN ('submitted','working') AND expires>?",
                                    (worker.worker_id, self.store.clock())).fetchone():
                        continue
                    selected = worker, adapter
                    break
            if selected is None:
                continue
            worker, adapter = selected
            attempt = None
            try:
                base = self.git.ref(branch)
                if item["integration_sha"] and not self.git.ancestor(item["integration_sha"], base):
                    raise Conflict("branch is missing required integration snapshot")
                feedback = self.latest_rework_feedback(item["id"]) if item["state"] == "REWORK" else None
                patch, checkpoint_base = self.latest_checkpoint(item["id"], base)
                attempt = self.store.claim(item["id"], worker.worker_id, item["version"])
                result = worker.execute(attempt["id"], base, (task_prompts or {}).get(item["id"], item["slice_id"]), adapter,
                    rework_feedback=feedback, checkpoint_patch=patch, checkpoint_base_sha=checkpoint_base)
                if result["state"] == "failed":
                    self._evidence(item["id"], "cli_failure", "Worker reported failure", attempt_id=attempt["id"],
                                   payload=json.loads(result["artifact"]))
                return {"item_id": item["id"], "attempt_id": attempt["id"], "state": result["state"]}
            except Exception as exc:
                error = type(exc).__name__
                self._evidence(item["id"], "cli_failure", error, attempt_id=attempt["id"] if attempt else None)
                return {"item_id": item["id"], "error": error}
        return None

    def gate_once(self, branch):
        if self.gatekeeper is None:
            return None
        leaves = self._leaves(branch)
        for item in leaves:
            if item["state"] != "GATE_PENDING":
                continue
            group = [w for w in leaves if w["change_set_id"] == item["change_set_id"]] if item["change_set_id"] else [item]
            if any(w["state"] != "GATE_PENDING" for w in group):
                continue
            if item["change_set_id"]:
                with self.store.connect() as conn:
                    siblings = {r[0] for r in conn.execute("SELECT w.id FROM work_items w WHERE change_set_id=? "
                        "AND NOT EXISTS (SELECT 1 FROM work_items c WHERE c.parent_id=w.id)", (item["change_set_id"],))}
                if siblings != {w["id"] for w in group}:
                    continue  # Never partially merge a change set across branch bindings.
            attempts = []
            with self.store.connect() as conn:
                for work in group:
                    row = conn.execute("SELECT id FROM attempts WHERE work_item_id=? AND epoch=? AND state='completed'",
                                       (work["id"], work["epoch"])).fetchone()
                    if row:
                        attempts.append(row["id"])
            if len(attempts) != len(group):
                continue
            try:
                result = self.gatekeeper.merge(branch, attempts)
                return {"item_ids": [w["id"] for w in group], "result": result}
            except Exception as exc:
                error = type(exc).__name__
                for work in group:
                    self._evidence(work["id"], "gate_rejected" if isinstance(exc, GateRejected) else "env_error", error)
                return {"item_ids": [w["id"] for w in group], "error": error}
        return None

    def submit_qa(self, item_id, expected_version, integration_sha, outcome, qa_actor, *, report=None, candidate_case=None):
        item = self.store.get_item(item_id)
        with self.store.connect() as conn:
            owners = {item["owner"]} | {r[0] for r in conn.execute("""WITH RECURSIVE children AS (
                SELECT id,owner FROM work_items WHERE parent_id=? UNION ALL
                SELECT w.id,w.owner FROM work_items w JOIN children c ON w.parent_id=c.id)
                SELECT owner FROM children""", (item_id,))}
        if self.identity_policy:
            qa_actor = self.identity_policy.authorize_independent_qa(qa_actor, owners=owners)
        elif not isinstance(qa_actor, str) or not qa_actor.strip() or qa_actor in owners:
            raise Forbidden("independent QA reviewer required")
        with self.store.transaction() as conn:
            result = self.store.qa(item_id, expected_version, integration_sha, None, qa_actor, report, outcome=outcome, _conn=conn)
            if outcome != "PASS":
                if self.knowledge:
                    self.knowledge.record_evidence(self.scope, item_id=item_id,
                        kind="qa_bug_report" if outcome == "FAIL" else "env_error", summary="QA " + outcome,
                        payload={"outcome": outcome, "report": report}, _conn=conn)
            elif self.knowledge and result["state"] in {"ACCEPTED", "CLOSED"}:
                if candidate_case is not None:
                    proposal = dict(candidate_case)
                    # Target/scope/snapshot are server-owned, never copied from candidate text.
                    for key in ("scope", "item_id", "integration_sha", "slice_id", "_conn"):
                        proposal.pop(key, None)
                    self.knowledge.propose_case(self.scope, item_id=item_id, slice_id=item["slice_id"], _conn=conn, **proposal)
                try:
                    self.knowledge.promote_verified_case(self.scope, item_id=item_id, integration_sha=integration_sha,
                                                          qa_reviewer=qa_actor, _conn=conn)
                except NotFound:
                    pass  # Ordinary successful work need not produce a troubleshooting case.
            return result

    def tick(self, branch, *, task_prompts=None):
        recovery = self.recover()
        coding = self.dispatch_coding_once(branch, task_prompts=task_prompts)
        gate = self.gate_once(branch)
        delivered = 0
        if self.im_dispatcher:
            # Bound work so a continuously growing outbox cannot starve coding.
            while delivered < 100 and self.im_dispatcher.once():
                delivered += 1
        exports = None
        if self.knowledge and self.everos_hook:
            exports = self.knowledge.flush_everos_exports(self.everos_hook, scope=self.scope)
        return {"recovery": recovery, "coding": coding, "gate": gate, "im_batches": delivered, "exports": exports}
