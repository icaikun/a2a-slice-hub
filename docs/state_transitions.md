# State and authority table — v0.1.0 (current specification)

## Attempts: one execution, one immutable terminal result

| From | Allowed destinations | Authority / conditions |
|---|---|---|
| submitted | working, failed, canceled, rejected | Owning worker, current epoch, unexpired lease, version CAS |
| working | input-required, completed, failed, canceled | Same fences; completed requires full base/source object IDs |
| input-required | working, failed, canceled | Same fences; working must acquire the worker's database slot |
| completed / failed / canceled / rejected | none | Terminal attempts never revive |

Expired live attempts can only be retired as failed by a new dispatch transaction.
That transaction increments the work item's epoch and creates a new attempt. A late
worker's update and heartbeat are fenced. A worker's local slot and database slot
are released on input-required. Three negotiation rounds block work and terminate
the attempt. Time spent blocked does not preserve an executable worker slot.

## Work items: DEV, CONTRACT_PATCH, BUG share one lifecycle

| Event | From | To |
|---|---|---|
| dispatch | READY / REWORK | ACTIVE |
| worker waiting | ACTIVE | WAITING |
| worker resume | WAITING | ACTIVE |
| successful execution | ACTIVE | GATE_PENDING |
| failed/canceled/rejected attempt | ACTIVE / WAITING | WAITING with dependency reason if any dependency is unmet; otherwise REWORK |
| candidate gate rejection | GATE_PENDING | REWORK; BLOCKED on third shared rework round |
| durable merge intent | GATE_PENDING | GATING |
| successful or recovered Git publication | GATING | MERGED_PENDING_QA |
| stale integration ref | GATING | REWORK |
| independent QA pass | MERGED_PENDING_QA | ACCEPTED for DEV/CONTRACT_PATCH; CLOSED for BUG |
| independent QA failure | MERGED_PENDING_QA | REWORK; BLOCKED on third failed round |
| ownership dispute | non-frozen leaf, responsible owner/live lease holder | one per item, module AND human owner per generation; BLOCKED at two independent owner/module votes |
| signed, authenticated human resume | BLOCKED / ownership-disputed | parent REWORK; leaves recompute L0/dependency/worker gates, otherwise REWORK |

A resume card authorizes resuming the same ownership, not reassignment, protected
path access, or a contract migration. It opens a new dispute_generation, resets dispute_round/negotiation_round/rework_round and cancels live attempts while
retaining prior attempts and outbox audit records. Outbox is not an immutable audit
ledger; a separately hardened audit facility is future work.

FIX_SUBMITTED is intentionally not a second BUG-only state: it maps to
MERGED_PENDING_QA. CLOSED is the business terminal state of a BUG, not an A2A task
state. ACCEPTED and CLOSED do not revive; a regression requires a new business item.

## Aggregate BUGs

An aggregate is a one-level BUG parent with child repair work items sharing its
change_set_id and Epic binding. It has no development attempt of its own. The parent
Epic must remain ACTIVE on the target branch at merge preparation. All leaf members of a
change set must enter the same candidate; partial publication is forbidden. Merge
preparation freezes parent and children together. A parent ownership dispute blocks
and fences its children. Parent QA binds the same integration SHA for every child;
passing parent QA accepts/closes the children together. Individual child QA is rejected
to prevent accepting one child and later reviving it when parent QA fails. Nested aggregate BUGs are
rejected in v0.1 rather than left with undefined cascade behavior.

Coordinator-added dependency edges require an undispatched item (READY/REWORK/WAITING, WAITING_WORKER or L0_PENDING_APPROVAL). Structured worker input-required routing adds edges transactionally while moving the active item to WAITING. Adding an
edge always bumps its version and recomputes dispatchability. Accepted dependencies
pin their snapshot immediately; unmet dependencies yield WAITING unless L0 takes precedence. Reciprocal edges serialize under BEGIN IMMEDIATE and cannot
both commit. A group is an atomic merge unit, not an exemption from DAG safety; do
not model intra-group mutual coordination as mutually blocking execution edges.

## Roles

- coordinator: create work items/Epics, add dependencies, issue L0/resume/exact-diff Breaking cards, refresh workers, check release readiness.
- worker: register authenticated user/machine heartbeat, claim authorized Slice work, renew lease, update own attempt, request structured dependencies, submit ownership evidence.
- gatekeeper: publish candidates via the three gates and durable journal.
- qa: attest a fixed pending snapshot; may not QA any item it owns.
- approver: consume a valid signed card as an authenticated human principal.
- im_gateway: forward already authenticated IM events; no automatic command execution.

Python domain objects are trusted internal APIs; authorization is enforced at HTTP
entry points. Do not give untrusted plugins an in-process Store reference.


## Epic, worker and QA exception gates

| Event | From | To / condition |
|---|---|---|
| create item under unapproved Epic | new | L0_PENDING_APPROVAL |
| signed L0 approval | L0_PENDING_APPROVAL | READY, WAITING, or WAITING_WORKER according to dependencies/owner |
| no eligible online owner | READY / REWORK / creation | WAITING_WORKER + transactional worker.offline mention |
| authenticated eligible heartbeat | WAITING_WORKER | READY if L0/dependencies also satisfied |
| structured input-required | ACTIVE | WAITING + created/reused CONTRACT_PATCH dependency |
| all providers independently accepted | WAITING | old attempt canceled; new attempt may claim READY at required integration_sha |
| infra failure before intent | GATE_PENDING | unchanged; safe retry, no developer penalty |
| uncertain publication after intent | GATING | recovery required; do not redispatch frozen work |
| QA ENV_ERROR | MERGED_PENDING_QA | retry twice, then BLOCKED_ENV; developer budget unchanged |
| first QA FLAKY with observed result | MERGED_PENDING_QA | one controlled rerun |
| inconsistent/repeated FLAKY | MERGED_PENDING_QA | FLAKY_BLOCKED; developer budget unchanged |
| signed environment/fuse recovery | BLOCKED_ENV / FLAKY_BLOCKED | same snapshot MERGED_PENDING_QA; reset only QA exception counters |
| independent QA acceptance | MERGED_PENDING_QA | ACCEPTED/CLOSED; promote leaf Slice to persistent regression set |

An Epic has `L0_PENDING_APPROVAL -> ACTIVE -> READY_FOR_RELEASE`. The last transition
requires all work accepted/closed, no prepared intent, an explicit empty manifest
`pending` list, and a current QA-attested head containing every accepted snapshot.
This seals work admission; it is not deployment. Breaking approvals bind the exact
contract diff and work version and are rechecked inside merge preparation after tests
and review, including expiration during a long-running gate.


## Arbitration, receipts and escalation

- Duplicate disputes are Conflict, not new rounds. Versions/heartbeats/claims do not
  reset arbitration generation. Two votes require both distinct slice IDs AND distinct human owners.
  A signed aggregate-parent resume opens a new generation, cancels live affected leases
  and resets the current vote count; historical dispute rows remain for audit.
  Individual child resume cards are rejected. Votes never accumulate across generations.
- Entering BLOCKED, BLOCKED_ENV or FLAKY_BLOCKED emits item.escalated in the same
  SQLite transaction, including aggregate child updates. The outbox trigger uses
  available=0 to make escalation immediately dispatchable under any clock.
- All successful QA outcomes record their idempotency receipt and original response
  snapshot in the state mutation transaction, including exceptional outcomes.
- DLQ records stop dispatch after the attempt cap. Expired delivery leases count
  toward that cap; an external send may still have succeeded before a crash.


## Transport, dependency and queue invariants

- Reusing a non-accepted CONTRACT_PATCH extends its deadline to the maximum consumer
  deadline. An expired unleased patch becomes claimable again when a live consumer joins;
  dispatch still checks worker identity, epoch, parent, dependencies and item state.
- Failed/canceled/rejected consumers with unmet dependencies remain WAITING. Final
  dependency QA acceptance pins integration_sha and readies each eligible consumer (or
  WAITING_WORKER if offline), independently of another consumer's expired deadline.
- Urgent item.escalated, approval.requested and worker.offline events have priority 10.
  After 300 seconds available, normal events gain effective priority 10. Claim order
  is effective priority DESC, available ASC, id ASC; priority does not bypass leases,
  availability delays, the retry limit or dead-letter status.
- tasks/cancel retries by the owning worker return the existing canceled task, including
  after lease expiry. No terminal state is changed. Other terminal outcomes are not cancelable.
- REST and A2A require actual integer versions/epochs, not booleans/coerced strings/floats.
  A2A envelopes are capped at 1MB; invalid JSON is -32700, nonobject/batch envelopes -32600.
  bug-report/v1 and qa-verdict/v1 share the same task/context and independent-QA handler.

## Signed resume and dependency snapshots

General signed resume resets the arbitration generation/counters for the parent and
all children while retaining historical disputes. An aggregate parent stays REWORK;
each leaf (or a standalone item) evaluates these gates in order:

1. Epic L0_PENDING_APPROVAL -> L0_PENDING_APPROVAL / l0_approval.
2. Any dependency not ACCEPTED/CLOSED -> WAITING / dependency.
3. No eligible online worker -> WAITING_WORKER / offline_owner + worker.offline.
4. Otherwise -> REWORK, no blocked reason.

Environment/flaky recovery remains separate: same snapshot MERGED_PENDING_QA, only
QA exception counters reset. Individual child resume is forbidden. Historical votes
never count toward the new generation; two distinct human owners AND two distinct
slices must dispute again in that generation to escalate.

When all dependencies of a WAITING leaf pass QA, cancel its input-required attempt,
select the latest COMMITTED merge_intents.rowid among all dependency snapshots and
its existing pin (fallback: accepting SHA), recompute readiness, and emit
dependency.ready with that snapshot. L0/worker readiness also recomputes pins, so
acceptance while L0 is pending cannot lose the dependency baseline. Gatekeeper checks
all dependency states and each SHA's ancestry against both origin and target head;
latest-intent order alone is not an ancestry proof across branches.

Manual DLQ replay defaults to three per event (trusted Store override: integer 1..10).
Exhaustion is Conflict; it neither resets retries nor emits another audit event.
Successful single-event reset, stable ID, replay_count and actor audit are atomic.
Only coordinator may replay via HTTP. No automatic/bulk replay or exactly-once claim.

A2A text claims allow only worker/coordinator; standalone gatekeeper may send
only its authorized gate command, never claim work. Development transitions and
dependency requests are worker-only; QA/bug verdicts QA-only; gate-merge allows
coordinator/gatekeeper. Approver/IM gateway cannot enter message/send. See README
for method/role matrix and exact transport boundaries.

## Aggregate dependency inheritance

Dependencies attached to a non-executable parent constrain each child. Adding an edge
recomputes every undispatched child's readiness/version; it is rejected if any child
is already dispatched/frozen. Resume leaves inherit parent edges; provider QA wakes
those WAITING children and pins their snapshots. Gatekeeper also reads inherited
edges, including when legacy scalar pins or states are stale. DAG detection collapses
all members of a change_set_id to one acceptance unit: even a one-way intra-group
edge is a deadlock and is rejected, as are cycles spanning groups.
