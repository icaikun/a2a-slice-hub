"""Three gates followed by durable journal + Git CAS. No test runs under SQLite locks."""
import json
import hashlib
from dataclasses import asdict
from .store import encode

from .errors import Conflict, GateRejected, InfrastructureError
from .paths import enforce
from .review import contract_subject, check_contracts, context_for, RejectUnconfiguredReviewer


class Gatekeeper:
    def __init__(self, store, git, runner, reviewer=None):
        self.store, self.git, self.runner = store, git, runner
        self.reviewer = reviewer or RejectUnconfiguredReviewer()

    def check_paths(self, base, source, allowed, item=None, *, paths_only=False):
        before, after = self.git.tree(base), self.git.tree(source)
        paths = set()
        for _, names in self.git.changes(base, source):
            for name in names:  # Both endpoints of a rename/copy must belong to the slice.
                enforce(name, allowed)
                paths.add(name)
                for tree in (before, after):
                    if name in tree and tree[name][0] not in {"100644", "100755"}:
                        raise GateRejected("symlink/submodule mode is forbidden")
        # Checkout itself must not materialize a malicious inherited link.
        if any(entry[0] not in {"100644", "100755"} for entry in after.values()):
            raise GateRejected("candidate repository contains unsupported link modes")
        if not paths_only:
            self._contracts(base, source, paths, item)
        return paths

    def _contracts(self, base, source, paths, item):
        grant = None
        if item and item["change_set_id"]:
            subject = contract_subject(self.git, base, source, paths, item["change_set_id"])
            digest = hashlib.sha256(encode(subject).encode()).hexdigest()
            with self.store.connect() as conn:
                grant = conn.execute("""SELECT id FROM approvals WHERE item=? AND action='breaking_contract'
                    AND subject_digest=? AND expected_version=? AND approved_by IS NOT NULL AND expires>?""",
                    (item["id"], digest, item["version"], self.store.clock())).fetchone()
        check_contracts(self.git, base, source, paths, kind=item["kind"] if item else "CONTRACT_PATCH", breaking=grant is not None)
        return grant["id"] if grant else None

    def merge(self, branch, attempt_ids, *, request_id=None, request_digest=None):
        if request_id:
            with self.store.connect() as conn:
                old = conn.execute("SELECT * FROM inbox WHERE id=?", (request_id,)).fetchone()
                intent = conn.execute("SELECT * FROM merge_intents WHERE request_id=?", (request_id,)).fetchone()
            if old:
                if old["digest"] != request_digest:
                    raise Conflict("message ID reused with different content")
                return self._replay_result(branch, json.loads(old["result"]))
            if intent:
                if intent["request_digest"] != request_digest:
                    raise Conflict("message ID reused with different content")
                if self.store.finish_merge(intent["id"], self.git.publish) != "COMMITTED":
                    raise Conflict("stale integration; use a new merge request")
                with self.store.connect() as conn:
                    result = json.loads(conn.execute("SELECT result FROM inbox WHERE id=?", (request_id,)).fetchone()[0])
                return self._replay_result(branch, result)
        validated = {}
        try:
            return self._merge(branch, attempt_ids, validated, request_id, request_digest)
        except InfrastructureError:
            raise
        except GateRejected as exc:
            # CAS each captured version; never rewrite a newer submission or a frozen journal.
            for key, version in validated.items():
                try:
                    self.store.reject(key, version, str(exc))
                except Conflict:
                    pass
            raise

    def _replay_result(self, branch, result):
        if not isinstance(result, dict) or "candidate_sha" not in result:
            raise Conflict("message ID does not belong to a merge result")
        if not self.git.ancestor(result["candidate_sha"], self.git.ref(branch)):
            raise Conflict("branch head rewound outside Hub; candidate no longer in branch ancestry")
        return result

    def _merge(self, branch, attempt_ids, validated, request_id=None, request_digest=None):
        if not attempt_ids or len(set(attempt_ids)) != len(attempt_ids):
            raise GateRejected("empty or duplicate attempts")
        base = self.git.ref(branch)
        with self.store.connect() as conn:
            branch_epic = conn.execute("SELECT * FROM epics WHERE branch=?", (branch,)).fetchone()
        if branch_epic and branch_epic["state"] != "ACTIVE":
            raise Conflict("epic branch is not approved or is sealed")
        regression = self.store.accepted_slices()
        item_rows = []
        items, sources, slices, allowed_union = {}, [], set(), set()
        submissions = []
        for key in attempt_ids:
            a = self.store.get_attempt(key)
            row = self.store.get_item(a["work_item_id"])
            if a["state"] != "completed" or a["epoch"] != row["epoch"] or row["state"] != "GATE_PENDING":
                raise Conflict("attempt is not the current completed submission")
            if row["id"] in validated:
                raise GateRejected("multiple submissions for one work item")
            validated[row["id"]] = row["version"]
            submissions.append(key)
        for key in submissions:
            attempt = self.store.get_attempt(key)
            item = self.store.get_item(attempt["work_item_id"])
            if attempt["state"] != "completed" or attempt["epoch"] != item["epoch"] or item["state"] != "GATE_PENDING":
                raise Conflict("attempt is not the current completed submission")
            if item["id"] in items:
                raise GateRejected("multiple submissions for one work item")
            validated[item["id"]] = item["version"]
            if item.get("epic_id"):
                epic = self.store.get_epic(item["epic_id"])
                if epic["branch"] != branch or epic["state"] != "ACTIVE":
                    raise Conflict("wrong epic branch or epic not active")
            if branch_epic and item.get("epic_id") != branch_epic["id"]:
                raise Conflict("work item is not bound to this epic")
            item_rows.append(item)
            source, origin = attempt["source_sha"], attempt["base_sha"]
            if not self.git.ancestor(origin, source) or not self.git.ancestor(origin, base):
                raise GateRejected("submission does not descend from an integration baseline")
            if item["integration_sha"] and not self.git.ancestor(item["integration_sha"], origin):
                raise GateRejected("submission baseline predates required integration snapshot")
            with self.store.connect() as conn:
                dependencies = self.store._dependencies(conn, item["id"])
            if any(d["state"] not in {"ACCEPTED", "CLOSED"} or (d["integration_sha"] and
                   (not self.git.ancestor(d["integration_sha"], origin) or
                    not self.git.ancestor(d["integration_sha"], base))) for d in dependencies):
                raise GateRejected("submission baseline is missing an accepted dependency snapshot")
            allowed = json.loads(item["allowed_paths"])
            self.check_paths(origin, source, allowed, item)
            items[item["id"]] = item["version"]
            sources.append(source)
            slices.add(item["slice_id"])
            allowed_union.update(allowed)
        candidate = self.git.candidate(base, sources)
        paths = self.check_paths(base, candidate, sorted(allowed_union), paths_only=True)
        from .paths import inside
        approval_grants = []
        for item in item_rows:
            owned = {p for p in paths if any(inside(p, a) for a in json.loads(item["allowed_paths"]))}
            grant = self._contracts(base, candidate, owned, item)
            if grant:
                approval_grants.append(grant)
        evidence = self.runner.run(self.git, base, candidate, slices | regression)
        verdict = self.reviewer.review(context_for(self.git, base, candidate))
        if not verdict.approved:
            raise GateRejected("semantic review rejected: " + verdict.reason)
        intent = self.store.prepare_merge(branch, base, candidate, items,
            evidence={"tests": evidence, "review": asdict(verdict), "regression_slices": sorted(regression), "approval_ids": approval_grants},
            regression_snapshot=regression, approval_grants=approval_grants, request_id=request_id, request_digest=request_digest)
        state = self.store.finish_merge(intent, self.git.publish)
        if state != "COMMITTED":
            raise Conflict("integration branch moved; candidate must be regenerated and retested")
        return {"candidate_sha": candidate, "base_sha": base, "intent": intent, "tests": evidence}

    def recover(self):
        with self.store.connect() as conn:
            pending = [r["id"] for r in conn.execute("SELECT id FROM merge_intents WHERE state='PREPARED'")]
        return {key: self.store.finish_merge(key, self.git.publish) for key in pending}
