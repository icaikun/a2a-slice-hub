# Round 1 implementation report

Date: 2026-11-13. Status: **implementation written; runtime verification BLOCKED**.
This is round 1 only, not five-round convergence, independent acceptance, or a
production/open-source release-readiness certification.

## 1. Decisions on the four challenges

### 1. SQLite instead of Redis + PostgreSQL: accept, with a local-disk boundary

A single SQLite WAL database holds work items, attempts, dependency edges, approvals,
inbox deduplication, outbox deliveries and merge intents. Mutations use BEGIN
IMMEDIATE, foreign keys, explicit rollback and explicit connection close. Unique
partial indexes fence one live attempt per item, one execution slot per worker,
and one PREPARED merge intent per branch. Database-busy failures become retryable
conflicts rather than silently weakening consistency.

Do not hold a write transaction while testing, calling a model, or sending IM.
The deliberate exception is bounded local Git publication/reconciliation; it is
protected by a previously committed intent. This is a single-host coordinator,
not a shared network-filesystem database or an HA control plane.

Counterexample to the stronger claim that one database solves all atomicity:

1. Tests pass for candidate C.
2. Git ref CAS succeeds from B to C.
3. The process dies before SQLite records MERGED_PENDING_QA.
4. Without a journal, Git and business state disagree permanently.

The committed PREPARED intent and recovery path address this window. Tests include
both an injected exception and a spawned process that exits immediately after Git
publication, without running Python cleanup.

### 2. Unified work_items + attempts: accept, repair parent/child semantics

DEV, CONTRACT_PATCH and BUG share one work-item engine. Terminal attempts cannot
resume. Rework creates a new attempt ID and increments the lease epoch. Same-item
business context survives across those attempts. Expired execution slots are reaped
transactionally when another item is assigned to the same worker.

An aggregate BUG has no developer execution attempt. Its one-level children share a
change_set_id and enter a single candidate. Partial change-set publication is
rejected. An ownership dispute at the parent fences children; a late completed task
cannot escape the blockade. QA is independent and bound to the exact integration
SHA and item version. Aggregate QA is performed on the parent only, so a child
cannot be accepted early and then revived by a later parent failure.

FIX_SUBMITTED is an unnecessary parallel state: use MERGED_PENDING_QA. CLOSED is a
BUG business state, not an A2A attempt state. Nested aggregate BUGs are rejected
rather than pretending their cascades are specified. See state_transitions.md.

### 3. Real Git gates: accept; reject the claim that a worktree is a sandbox

The implementation invokes Git, not fake SHA strings. Tests create bare repositories,
commit source branches and develop inside detached worktrees. Gate 1 checks both
rename endpoints, protected names and path components, case collisions, file modes,
and conservative contract compatibility. Symlink fixtures use real Git tree/index
entries, avoiding Windows symlink privilege assumptions.

Gate 2 constructs an immutable candidate from the current base and submitted source
commits. Protected suites/configuration are copied from the trusted base. Required
slice tests plus accepted regressions must have exactly the expected collected and
passing node IDs; skipped/xfail/zero collection cannot pass. The final ref update is
an expected-old-value CAS. Candidate/submission refs retain Git objects for recovery.

Gate 3 extracts changed Python symbols and caller source snippets, invokes a supplied
SemanticReviewer, and defaults to rejection if no reviewer exists. Reflection,
dynamic dispatch, other languages and incomplete call graphs are not claimed solved.

A same-identity subprocess is not safe for deliberately malicious code. The local
runner therefore defaults to refusal; explicit trusted-local opt-in is required.
A separately isolated production runner remains a release blocker, not an implied
property of git worktree or pytest evidence.

### 4. IM + A2A + experience: accept the interfaces; narrow compatibility claims

Implemented: authenticated FastAPI routes, agent card, custom /a2a/tasks endpoints,
a constrained A2A 0.3 JSON-RPC subset, message-ID deduplication in the dispatch
transaction, HMAC approval cards, leased outbox delivery, and a Feishu outbound
adapter requiring three distinct application identities. Bot-originated events are
ignored. Worker completion, merge authority, QA authority and human approval are
separate server-side principals.

A2A paths alone do not prove standard compliance. message/send, tasks/get and
tasks/cancel are present with documented restrictions; streaming, push subscription,
full task continuation and official conformance testing are not implemented.
External protocol documentation lookup did not provide usable verification evidence
in this session; protocol conformance must be checked before making a release claim.

Feishu application provisioning, live long connections, callback-signature validation
and human identity mapping are not complete. Incoming events require an authenticated
gateway and never execute arbitrary chat commands. Approval APIs require a real
configured approver principal. Three live bots have not been exercised.

ExperienceHook and EverOSHook preserve recall_agent_experience / record_trajectory
semantics. There is no implicit upload; the caller supplies authentication and
redaction. Hook failure does not mutate task state. Worker invokes recall before
coding and records an explicitly supplied recovery trajectory only when requested.

## 2. Additional blueprint changes

- **Split approvals from outbox.** Approval replay state and delivery retries are
  different lifecycles; putting both into one overloaded row is not simpler.
- **Add inbox and merge journal.** Durable message idempotency and Git/DB recovery
  are needed even after removing external middleware.
- **Freeze dependencies after dispatch.** Otherwise an edge can appear after tests
  finish without invalidating item state. Edge creation also bumps the item version.
- **Pin test IDs, not merely counts.** Enough unrelated tests must not substitute
  for the required acceptance tests.
- **Fail closed on unknown schema compatibility.** Annotation-only differences may
  pass. Unknown semantic changes require reviewed baseline promotion; a complete
  producer/consumer compatibility solver is not invented here.
- **Keep resume approval narrow.** It does not silently authorize protected writes,
  transfer slice ownership, or approve a contract migration.
- **No exactly-once IM claim.** Provider acceptance followed by a network failure can
  cause a replay. Stable delivery IDs and fenced acknowledgments support at-least-once.
- **No measured latency promise.** Approval transactions are short, but a 100 ms
  latency SLO and provider 3 s acknowledgment budget have not been benchmarked.
- **Retain original blueprint.** Its historical diagrams are not silently rewritten
  as proof of implementation. This report and state table describe the actual v0.1.

## 3. Modules and verification

| Module | Responsibility |
|---|---|
| store.py | WAL transactions, lifecycle, lease/slot fencing, DAG, QA, inbox/outbox, merge journal |
| git.py / paths.py | Real Git subprocesses, worktrees, commit candidates, path policy, ref CAS |
| gatekeeper.py | Three gates, full change-set preparation, recovery |
| runner.py | Base-owned pytest selection and evidence checks; opt-in local execution |
| review.py | Conservative schema checks, AST snippets, semantic reviewer protocol |
| api.py / models.py | Role-authenticated Hub and constrained A2A transport |
| approvals.py | HMAC subject/version/expiry binding and transactional approval |
| im.py | Three-identity Feishu transport and retryable outbox dispatcher |
| worker.py | Real worktree execution, lease renewal, release-on-wait, cleanup |
| experience.py | Explicit experience hooks and EverOS-compatible invocation |
| config.py / __main__.py | Environment-backed configuration and API entrypoint |

Added tests cover lease races, DAG races, separate-process claiming, deadline and
ownership fencing, duplicate/expired/tampered approvals, IM echo suppression and
retries, real Git rename/symlink/protected-path checks, test failures/skips/xfails,
concurrent candidate CAS, aggregate fixes, independent QA, worker cleanup, and
publication hygiene. External IM/model responses are explicit doubles; Git and
SQLite test code uses actual implementations.

### Requested pytest run

Command attempted from the workspace:

~~~powershell
.\.venv\Scripts\pytest.exe -q
~~~

Result: **NOT STARTED / BLOCKED**, not passed, failed-test count, or collected-test
count. The execution service returned:

~~~text
windows unelevated restricted-token sandbox cannot enforce split writable root sets directly; refusing to run unsandboxed
~~~

The alternative subprocess API also returned EPERM. No elevation or execution-policy
bypass was attempted. No package installation or network download was needed.

Limited file-only checks were possible: SQLite DDL execution and SQL compilation
using the available Node SQLite engine, balanced lexical delimiters, and a bounded
private-data-pattern scan. See verification_round1.json for the measured counts.
These are **not** Python compilation, pytest collection, Git integration execution,
Windows file-lock verification, a secret-scanner certification, or live IM testing.

## 4. Round 2 challenges for Antigravity

1. **Restore the test execution path first.** Run the exact suite on Windows and
   Linux. Return full failures, not an assertion that source review implies green.
   Audit subprocess termination, multiprocessing spawn, worktree locks and cleanup.
2. **Define the hostile-code threat model.** If workers can intentionally attack the
   Hub, what isolates candidate execution and test evidence from Git, SQLite, tokens
   and the host filesystem? A container name alone is not proof.
3. **Attack the merge journal.** Examine crashes before/after intent creation, CAS and
   DB commit, plus disk-full, ref-lock timeout, manual force-push and garbage collection.
   Is the single authorized ref-publisher assumption acceptable? Which artifacts must
   be persisted to audit tests and model review after a restart?
4. **Specify contract directionality.** Request versus response schemas, optional
   fields, enum widening and additionalProperties have different compatibility rules.
   Define the supported subset and hash-bound migration approval path before replacing
   conservative rejection with permissive inference.
5. **Audit aggregate QA and ownership.** Is one-level aggregation sufficient? Should
   persistent slice ownership live in a separate registry rather than per-item trusted
   assignment? Prove parent dispute and final QA remain atomic under regrouping.
6. **Prove the Feishu/A2A edge.** Provide live callback-signature and human-principal
   verification, tenant/bot credential rotation, deduplication-window behavior, and
   official A2A conformance evidence. Do not relabel the REST extension as a standard.
7. **Finish operational orchestration.** Define durable dispatcher supervision,
   dead-letter queues, environment/flaky retry budgets, QA sandbox provisioning,
   contract registry promotion, PENDING==0 release closure and schema migrations.
8. **Challenge conservative portability rules.** Test directory case aliases, Unicode
   normalization, NTFS short-name aliases, malicious Git object metadata and large
   repositories. Do not weaken path checks merely to make a fixture pass.

Round 2 is a challenge brief, not an automatically launched peer review. At least
five rounds and both parties' explicit zero-objection acceptance still remain the
collaboration rule. No claim of that convergence is made here.
