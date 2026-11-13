"""Transactional worker, dependency, epic and QA lifecycle extensions."""
import hashlib
import json
from .errors import Conflict, Forbidden
from .paths import inside

EXTRA_SCHEMA = """
CREATE TABLE IF NOT EXISTS epics (
 id TEXT PRIMARY KEY, branch TEXT NOT NULL UNIQUE, skeleton_sha TEXT NOT NULL,
 state TEXT NOT NULL DEFAULT 'L0_PENDING_APPROVAL', version INTEGER NOT NULL DEFAULT 0,
 release_sha TEXT
);
CREATE TABLE IF NOT EXISTS workers (
 worker_id TEXT PRIMARY KEY, owner_user_id TEXT NOT NULL, endpoint_url TEXT NOT NULL,
 skills TEXT NOT NULL, expires REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS accepted_slices (
 slice_id TEXT PRIMARY KEY, integration_sha TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS dependency_dedup ON work_items(dependency_key)
 WHERE dependency_key IS NOT NULL AND state NOT IN ('ACCEPTED','CLOSED');
"""


class Workflow:
    @staticmethod
    def migrate(conn, schema):
        """Atomic v1 -> v2 rebuild retains CHECK constraints, references and journal data."""
        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE name='work_items'").fetchone()
        if exists and "epic_id" not in {r[1] for r in conn.execute("PRAGMA table_info(work_items)")}:
            for table in ("work_items", "approvals"):
                start = schema.index("CREATE TABLE IF NOT EXISTS " + table + " (")
                statement = schema[start:schema.index(";", start)]
                conn.execute(statement.replace(table + " (", table + "_v2 (", 1))
                names_sql = ",".join(r[1] for r in conn.execute("PRAGMA table_info(" + table + ")"))
                conn.execute(f"INSERT INTO {table}_v2({names_sql}) SELECT {names_sql} FROM {table}")
                conn.execute("DROP TABLE " + table)
                conn.execute(f"ALTER TABLE {table}_v2 RENAME TO {table}")
            conn.execute("ALTER TABLE merge_intents ADD COLUMN evidence_json TEXT NOT NULL DEFAULT '{}'")
        # executescript implicitly commits; all bootstrap/migration DDL uses the caller's lock.
        for statement in (schema + EXTRA_SCHEMA).split(";"):
            if statement.strip():
                conn.execute(statement)
        conn.execute("""INSERT OR IGNORE INTO accepted_slices SELECT slice_id,integration_sha FROM work_items w
            WHERE state IN ('ACCEPTED','CLOSED') AND integration_sha IS NOT NULL
            AND NOT EXISTS (SELECT 1 FROM work_items c WHERE c.parent_id=w.id)""")

    @staticmethod
    def migrate_round3(conn):
        for table, column, ddl in (
            ("outbox", "dead_letter", "INTEGER NOT NULL DEFAULT 0"),
            ("outbox", "last_error", "TEXT"),
            ("merge_intents", "request_id", "TEXT"),
            ("merge_intents", "request_digest", "TEXT"),
            ("work_items", "dispute_generation", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if column not in {r[1] for r in conn.execute("PRAGMA table_info(" + table + ")")}:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        conn.execute("""CREATE TABLE IF NOT EXISTS disputes (
            item TEXT NOT NULL REFERENCES work_items(id), target TEXT NOT NULL REFERENCES work_items(id),
            generation INTEGER NOT NULL, slice_id TEXT NOT NULL, item_version INTEGER NOT NULL,
            rework_round INTEGER NOT NULL, worker TEXT NOT NULL, evidence TEXT NOT NULL,
            PRIMARY KEY(item,generation), UNIQUE(target,generation,slice_id))""")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS merge_request ON merge_intents(request_id) WHERE request_id IS NOT NULL")

    @staticmethod
    def migrate_round4(conn):
        for column in ("priority", "replay_count"):
            if column not in {r[1] for r in conn.execute("PRAGMA table_info(outbox)")}:
                conn.execute(f"ALTER TABLE outbox ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0")
        conn.execute("UPDATE outbox SET priority=10 WHERE topic IN ('item.escalated','approval.requested','worker.offline')")
        if "owner" not in {r[1] for r in conn.execute("PRAGMA table_info(disputes)")}:
            conn.execute("""CREATE TABLE disputes_v4 (
                item TEXT NOT NULL REFERENCES work_items(id), target TEXT NOT NULL REFERENCES work_items(id),
                generation INTEGER NOT NULL, slice_id TEXT NOT NULL, item_version INTEGER NOT NULL,
                rework_round INTEGER NOT NULL, worker TEXT NOT NULL, evidence TEXT NOT NULL, owner TEXT NOT NULL,
                PRIMARY KEY(item,generation), UNIQUE(target,generation,slice_id), UNIQUE(target,generation,owner))""")
            # Conflicting legacy human votes fail the entire migration, never silently erase audit evidence.
            conn.execute("INSERT INTO disputes_v4 SELECT d.*, w.owner FROM disputes d JOIN work_items w ON w.id=d.item")
            conn.execute("DROP TABLE disputes")
            conn.execute("ALTER TABLE disputes_v4 RENAME TO disputes")
        conn.execute("DROP TRIGGER IF EXISTS blocked_escalation")
        conn.execute("""CREATE TRIGGER blocked_escalation AFTER UPDATE OF state ON work_items
            WHEN NEW.state IN ('BLOCKED','BLOCKED_ENV','FLAKY_BLOCKED') AND OLD.state != NEW.state
            BEGIN INSERT INTO outbox(id,topic,payload,available,priority) VALUES(
                lower(hex(randomblob(16))), 'item.escalated',
                json_object('item',NEW.id,'state',NEW.state,'version',NEW.version,'reason',NEW.blocked_reason,
                            'slice_id',NEW.slice_id,'owner',NEW.owner,'mention_user_id','coordinator'),0,10); END""")
        conn.execute("CREATE INDEX IF NOT EXISTS outbox_dispatch ON outbox(priority DESC,available,id) WHERE delivered=0 AND dead_letter=0")

    def authorize_worker(self, conn, item, worker_id):
        if self.registry is None:  # Explicit legacy in-process Store; production factory loads registry.
            if item["owner"] != worker_id:
                raise Forbidden("worker does not own this slice")
            return
        spec = self.registry.get(item["slice_id"])
        worker = conn.execute("SELECT * FROM workers WHERE worker_id=?", (worker_id,)).fetchone()
        if (not worker or worker["expires"] <= self.clock() or worker["owner_user_id"] != spec.owner_user_id
                or not set(spec.required_skills) <= set(json.loads(worker["skills"]))):
            raise Forbidden("worker is offline, lacks skills, or belongs to another owner")

    def _online(self, conn, slice_id):
        if self.registry is None:
            return True
        spec = self.registry.get(slice_id)
        return any(set(spec.required_skills) <= set(json.loads(w["skills"])) for w in conn.execute(
            "SELECT skills FROM workers WHERE owner_user_id=? AND expires>?", (spec.owner_user_id, self.clock())))

    @staticmethod
    def _dependency_cycle(conn, item, needs):
        # QA accepts atomic change sets as a unit: even a one-way intra-set edge deadlocks.
        return conn.execute("""WITH RECURSIVE units(id,unit) AS (
                SELECT id,COALESCE('set:' || change_set_id,'item:' || id) FROM work_items),
            edges(item,needs) AS (
                SELECT a.unit,b.unit FROM dependencies d JOIN units a ON a.id=d.item
                JOIN units b ON b.id=d.needs),
            reachable(id) AS (SELECT unit FROM units WHERE id=? UNION
                SELECT e.needs FROM edges e JOIN reachable r ON e.item=r.id)
            SELECT 1 FROM reachable WHERE id=(SELECT unit FROM units WHERE id=?)""", (needs, item)).fetchone()

    @staticmethod
    def _dependencies(conn, key):
        # Aggregate dependencies constrain every executable leaf, including after resume.
        return conn.execute("""SELECT w.state,w.integration_sha FROM dependencies d
            JOIN work_items w ON w.id=d.needs WHERE d.item=? OR d.item=
            (SELECT parent_id FROM work_items WHERE id=?)""", (key, key)).fetchall()

    def _ready(self, conn, key, integration_sha=None, *, ready_state="READY"):
        item = self.one(conn, "work_items", key)
        state, reason = ready_state, None
        if item["epic_id"] and self.one(conn, "epics", item["epic_id"])["state"] == "L0_PENDING_APPROVAL":
            state, reason = "L0_PENDING_APPROVAL", "l0_approval"
        elif any(d["state"] not in {"ACCEPTED", "CLOSED"} for d in self._dependencies(conn, key)):
            state, reason = "WAITING", "dependency"
        elif not self._online(conn, item["slice_id"]):
            state, reason = "WAITING_WORKER", "offline_owner"
            if item["state"] != state:
                self.emit(conn, "worker.offline", {"item": key, "owner_user_id": item["owner"], "mention_user_id": item["owner"]})
        integration_sha = self._dependency_snapshot(conn, item, integration_sha)
        if item["state"] != state or item["blocked_reason"] != reason or integration_sha != item["integration_sha"]:
            conn.execute("""UPDATE work_items SET state=?,blocked_reason=?,version=version+1,
                integration_sha=COALESCE(?,integration_sha) WHERE id=?""", (state, reason, integration_sha, key))

    @staticmethod
    def _dependency_snapshot(conn, item, fallback=None):
        """Commit order, not QA arrival order; ancestry is enforced separately by Git."""
        snapshots = {r["integration_sha"] for r in Workflow._dependencies(conn, item["id"])
                     if r["state"] in {"ACCEPTED", "CLOSED"} and r["integration_sha"]}
        snapshots.update(s for s in (item["integration_sha"], fallback) if s)
        if not snapshots:
            return None
        placeholders = ",".join("?" for _ in snapshots)
        newest = conn.execute(f"""SELECT candidate_sha FROM merge_intents WHERE state='COMMITTED'
            AND candidate_sha IN ({placeholders}) ORDER BY rowid DESC LIMIT 1""", tuple(snapshots)).fetchone()
        return newest[0] if newest else fallback or item["integration_sha"] or sorted(snapshots)[-1]

    def register_worker(self, worker_id, owner_user_id, endpoint_url, skills, ttl=60):
        from .models import WorkerRegistration
        from .store import encode
        WorkerRegistration(endpoint_url=endpoint_url, skills=skills, ttl=ttl)
        if self.registry is None or not owner_user_id:
            raise Forbidden("authenticated owner binding and slice registry required")
        with self.transaction() as conn:
            old = conn.execute("SELECT owner_user_id FROM workers WHERE worker_id=?", (worker_id,)).fetchone()
            if old and old[0] != owner_user_id:
                raise Forbidden("worker identity cannot be rebound")
            conn.execute("""INSERT INTO workers VALUES(?,?,?,?,?) ON CONFLICT(worker_id) DO UPDATE SET
                endpoint_url=excluded.endpoint_url,skills=excluded.skills,expires=excluded.expires""",
                (worker_id, owner_user_id, endpoint_url, encode(skills), self.clock() + ttl))
            for row in conn.execute("SELECT id FROM work_items WHERE state='WAITING_WORKER'").fetchall():
                self._ready(conn, row["id"])
            eligible = [s for s, v in self.registry.slices.items() if v.owner_user_id == owner_user_id and set(v.required_skills) <= set(skills)]
        return {"worker_id": worker_id, "owner_user_id": owner_user_id, "eligible_slices": eligible}

    def refresh_workers(self):
        with self.transaction() as conn:
            for item in conn.execute("SELECT * FROM work_items WHERE state IN ('READY','REWORK','WAITING_WORKER')").fetchall():
                if item["state"] == "WAITING_WORKER" or not self._online(conn, item["slice_id"]):
                    self._ready(conn, item["id"])

    def _route_dependency(self, conn, item, artifact):
        from .models import DependencyRequest
        from .slices import contract_path
        from .store import encode, uid
        request = DependencyRequest.model_validate((artifact or {}).get("dependency_request"))
        path = contract_path(request.contract)
        consumer = self.registry.get(item["slice_id"])
        if path not in {contract_path(c) for c in consumer.consumes_contracts}:
            raise Forbidden("slice does not consume this contract")
        provider = self.registry.provider(request.contract)
        digest = hashlib.sha256(encode({"path": path, "change": request.expected_change, "epic": item["epic_id"]}).encode()).hexdigest()
        patch = conn.execute("SELECT * FROM work_items WHERE dependency_key=? AND state NOT IN ('ACCEPTED','CLOSED')", (digest,)).fetchone()
        if patch:
            key = patch["id"]
            conn.execute("UPDATE work_items SET deadline=MAX(deadline,?) WHERE id=?", (item["deadline"], key))
        else:
            key = uid()
            conn.execute("""INSERT INTO work_items(id,kind,slice_id,owner,allowed_paths,deadline,epic_id,dependency_key,dependency_request)
                VALUES(?,'CONTRACT_PATCH',?,?,?,?,?,?,?)""", (key, provider.slice_id, provider.owner_user_id,
                encode([path, *provider.owned_paths, "tests/unit/" + provider.slice_id]), item["deadline"], item["epic_id"], digest, encode(request.model_dump())))
            self._ready(conn, key)
            self.emit(conn, "contract.patch_requested", {"item": key, "request": request.model_dump(), "requester_slice": item["slice_id"], "requester_owner": item["owner"]})
        if self._dependency_cycle(conn, item["id"], key):
            raise Conflict("dependency cycle; atomic change set required")
        conn.execute("INSERT OR IGNORE INTO dependencies VALUES(?,?)", (item["id"], key))
        conn.execute("UPDATE work_items SET blocked_reason='dependency' WHERE id=?", (item["id"],))
        self.emit(conn, "dependency.linked", {"item": item["id"], "needs": key})

    def _accepted(self, conn, items, sha):
        for item in items:
            if conn.execute("SELECT 1 FROM work_items WHERE parent_id=?", (item["id"],)).fetchone():
                continue
            conn.execute("INSERT INTO accepted_slices VALUES(?,?) ON CONFLICT(slice_id) DO UPDATE SET integration_sha=excluded.integration_sha", (item["slice_id"], sha))
        ids = [i["id"] for i in items]
        for key in ids:
            for row in conn.execute("""SELECT DISTINCT w.id FROM dependencies d JOIN work_items w
                    ON w.id=d.item OR w.parent_id=d.item WHERE d.needs=?""", (key,)).fetchall():
                waiting = self.one(conn, "work_items", row["id"])
                if waiting["state"] != "WAITING":
                    continue
                unmet = any(d["state"] not in {"ACCEPTED", "CLOSED"}
                            for d in self._dependencies(conn, waiting["id"]))
                if unmet:
                    continue
                conn.execute("UPDATE attempts SET state='canceled',version=version+1 WHERE work_item_id=? AND state='input-required'", (waiting["id"],))
                snapshot = self._dependency_snapshot(conn, waiting, sha)
                self._ready(conn, waiting["id"], snapshot)
                self.emit(conn, "dependency.ready", {"item": waiting["id"], "integration_sha": snapshot})

    def accepted_slices(self, conn=None):
        if conn is None:
            with self.connect() as connection:
                return self.accepted_slices(connection)
        return {r[0] for r in conn.execute("SELECT slice_id FROM accepted_slices")}

    def create_epic(self, id, branch, skeleton_sha):
        from .git import checked_sha
        checked_sha(skeleton_sha)
        with self.transaction() as conn:
            conn.execute("INSERT INTO epics(id,branch,skeleton_sha) VALUES(?,?,?)", (id, branch, skeleton_sha))
            self.emit(conn, "epic.l0_pending", {"epic_id": id})
        return self.get_epic(id)

    def get_epic(self, key):
        with self.connect() as conn:
            return self.one(conn, "epics", key)

    def check_release_ready(self, key, git):
        epic = self.get_epic(key)
        sha = git.ref(epic["branch"])
        manifest = json.loads(git.blob(sha, ".hub/tests.json"))
        if not isinstance(manifest, dict) or manifest.get("pending") != []:
            raise Conflict("release requires explicit PENDING == 0")
        with self.transaction() as conn:
            current = self.one(conn, "epics", key)
            if current["state"] != "ACTIVE" or current["version"] != epic["version"]:
                raise Conflict("epic not active")
            items = conn.execute("SELECT * FROM work_items WHERE epic_id=?", (key,)).fetchall()
            if not items or any(i["state"] not in {"ACCEPTED", "CLOSED"} for i in items):
                raise Conflict("all epic work must be accepted or closed")
            if conn.execute("SELECT 1 FROM merge_intents WHERE branch=? AND state='PREPARED'", (epic["branch"],)).fetchone():
                raise Conflict("merge is in flight")
            if not any(i["integration_sha"] == sha for i in items) or any(
                    not i["integration_sha"] or not git.ancestor(i["integration_sha"], sha) for i in items):
                raise Conflict("release head must be QA-attested and include all accepted snapshots")
            if git.ref(epic["branch"]) != sha:
                raise Conflict("integration moved; retry release check")
            conn.execute("UPDATE epics SET state='READY_FOR_RELEASE',release_sha=?,version=version+1 WHERE id=?", (sha, key))
            self.emit(conn, "epic.release_ready", {"epic_id": key, "integration_sha": sha})
        return self.get_epic(key)

    def _qa_exception(self, conn, item, outcome, report):
        if outcome == "ENV_ERROR":
            n = item["env_round"] + 1
            state = "BLOCKED_ENV" if n > 2 else "MERGED_PENDING_QA"
            conn.execute("UPDATE work_items SET env_round=?,state=?,version=version+1,blocked_reason='qa_env_error' WHERE id=?", (n, state, item["id"]))
        elif item["flaky_round"]:
            if outcome == item["flaky_observed"]:
                return False  # One consistent rerun: normal PASS/FAIL handling below.
            conn.execute("UPDATE work_items SET state='FLAKY_BLOCKED',version=version+1,blocked_reason='qa_flaky' WHERE id=?", (item["id"],))
        else:
            observed = (report or {}).get("observed")
            if observed not in {"PASS", "FAIL"}:
                raise ValueError("FLAKY requires observed PASS or FAIL")
            conn.execute("UPDATE work_items SET flaky_round=1,flaky_observed=?,version=version+1,blocked_reason='qa_flaky_rerun' WHERE id=?", (observed, item["id"]))
        self.emit(conn, "qa." + outcome.lower(), {"item": item["id"], "report": report})
        return True
