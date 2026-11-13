# Security and trust boundaries — v0.1.0

This is the authoritative current specification; historical round reports preserve
prior evidence, not alternative current policies. Read alongside README and
`docs/state_transitions.md`.

## Trusted control plane and authority

The coordinator, protected baseline/registry, local SQLite database, publisher Git
repository, API credentials, semantic reviewer and independent QA identity are trusted.
Python Store/domain objects are internal trusted APIs, not plugin security boundaries.
Credentials map to server-side roles and human owners; bodies cannot choose identity.
Registry-backed deployments require QA owner_user_id; aliases are not human independence.
Worker heartbeat skills are assertions, not remote attestation; endpoints are stored,
not fetched. Backup ownership is informational, not silent takeover permission.

A2A claim permits worker/coordinator only; development transitions/dependency requests
worker only; QA verdicts QA only; gate-merge coordinator/gatekeeper only. A standalone
gatekeeper cannot claim. REST merge requires gatekeeper. Approver and IM gateway cannot
send development commands. QA must be independent of every aggregate child's owner. Aggregate children share the
parent Epic; merge preparation rechecks its active state and branch.

Signed cards bind item, action, exact version, subject digest, expiration and approval ID.
A valid HMAC alone does not establish human authority. L0 binds skeleton/branch; Breaking
binds the exact before/after contract blob hashes and change-set. Grants are rechecked
inside merge preparation after tests/review. Resume does not reassign ownership, grant
protected-path rights, or approve contract migration. Resume opens a new arbitration
generation and recomputes leaf L0/dependency/online-worker gates; it cannot bypass them.
Escalation requires two distinct slices AND two distinct human owners in one generation.
Historical disputes remain; child-only resume and cross-generation vote accumulation fail.

## Candidate gates and contract evolution

Worker diffs stay inside slice-derived allowed paths, including both rename endpoints.
Protected tests/configuration/registry/lockfiles cannot be changed by submissions.
Symlinks and submodules are rejected, including inherited candidate tree entries.
New owned unit tests do not become authoritative merely by submission: trusted baseline
manifest IDs must be collected and pass with no skip/xfail. Accepted-slice promotion is
serialized with PREPARED intents; a changed regression set requires a gate retry.

Only CONTRACT_PATCH can edit contracts. `schema_compatible` enforces a **bounded
producer-additive contract evolution policy**, **not general bidirectional JSON Schema
entailment**. Existing fields and constraints remain unchanged; original objects must
be open and new fields optional. Positive Habitable Additive Profile permits only
positive type/properties/required/items/additionalProperties, nonempty primitive/object enum, const,
default, format, annotations and acyclic local $defs/definitions references. Every new
schema and referenced schema must have a constructive inhabitant. Boolean schemas,
unknown keywords, negation/combinators/conditionals, regex, numeric/length/cardinality
constraints and recursion require explicit signed breaking_contract approval. A valid
but unproven combination also fails closed. Format/default/annotations are metadata,
not format assertion or default materialization. A formerly unknown field with an
incompatible existing value can fail the new schema; provider implementation plus
independent QA remains necessary. This policy does not prove general schema inclusion.

Each accepted/closed dependency snapshot must be an ancestor of BOTH submitted origin
and target branch head. A newest committed scalar pin is convenience, not proof of all
cross-branch dependencies. Unknown/unaccepted dependencies reject publication. Parent edges constrain all children
and are checked at dispatch, wakeup and merge. Atomic change sets form single DAG
acceptance units; intra-set and transitive group cycles fail closed. Parent edge
mutations cannot invalidate an already dispatched child. Task reads check role as well
as machine name; JSON contract equality distinguishes booleans from numbers.

## Execution isolation — mandatory external prerequisite

`allow_local_execution=False` is the default. The local pytest runner, trusted import
precedence and a Git worktree are NOT hostile-Python sandboxes. Never opt in for
untrusted code without externally validated OS/container isolation. Provide a disposable
VM/container or restricted account with no Hub credentials, database, publisher Git,
host sockets, home directory or unrelated writable host mounts. Mount pinned candidate
and trusted tests only; use private scratch. Disable network by default, restrict any
necessary egress, cap CPU/memory/processes/time/output, kill the entire process tree on
timeout, destroy the environment, and validate returned evidence before publication.
This repository does not provision or certify that runner isolation.

Git uses no shell, strips environment overrides, disables global/system config, hooks,
submodule recursion and ordinary local-file transport. Bare-repository config is trusted;
these flags do not prevent resource exhaustion or malicious object administration.
Semantic AST caller extraction is advisory, not a complete call graph or proof.

## SQLite, Git and recovery

Single-host local-disk SQLite WAL v4 is supported; shared NFS/SMB or multi-Hub HA is not.
Bootstrap and v1-to-v4 migration run in one BEGIN IMMEDIATE transaction with foreign
keys/journal retention. Back up first. Duplicate legacy human votes fail migration
atomically: stop writers, back up the database and audit evidence, independently resolve
the identity/generation conflict, then retry on a copy before promotion. Do not silently
delete votes to force migration. No automatic historical reconciliation tool is supplied.

SQLite does not make Git publication part of its ACID transaction. PREPARED merge intent
freezes item versions; only bounded local ref CAS/reconciliation runs under the SQL write
lock, never tests, review or network delivery. Recover before accepting new merges;
unresolved intents block publication. Do not delete them to bypass recovery. Successful
replays recheck candidate ancestry, detecting observed rewinds but not malicious external
writers racing after the check. Require single-publisher Git ref ACLs/no force-push and
retain in-flight candidate objects. External destructive GC is unsupported.

Release readiness seals a QA-attested head containing all accepted snapshots, requires
all items accepted/closed and manifest pending=[]; it is not a deployment operation.
Environment/flaky signed recovery restores pending QA on the same SHA and resets only
QA exception counters, not developer budgets.

## IM, receipts, queues and retention

Feishu plaintext callbacks verify raw-body SHA-256 signature, timestamp window,
nonce/content receipt and registered human mapping. Approval, receipt and outbox commit
atomically. Encrypted envelopes are rejected. The legacy gateway path is read-only.
DingTalk requires a separate bearer-authenticated gateway, fresh timestamp HMAC and
allowlisted staff identity. That HMAC is NOT a body signature: a compromised gateway can
substitute the first body. Prefer nonblank msgId receipts; fallback is timestamp/sign;
same ID with changed bytes conflicts. No DingTalk/chat callback executes approvals or
arbitrary commands. Operators must validate TLS, actual tenant identities and credentials.

Outbox is at-least-once: a send accepted before ack crash may repeat. Reuse stable event
IDs and provider reconciliation; no exactly-once promise. Urgent topics have priority 10;
normal events available for 300 seconds gain effective priority 10, with oldest-first
ordering. Availability, lease, tries and DLQ fences still apply. Expired claims count
against the default five-attempt delivery cap. Errors retain exception class/status,
not provider response bodies, webhook URLs or exception messages containing secrets.

Coordinator-only single-event replay preserves ID and atomically audits actor/count.
HTTP caps each event at three replays. Trusted Store accepts max_replays integer 1..10.
Exhaustion rejects without another outbox event (no rejection storm); there is no bulk or
automatic replay. These limits are not a tenant-wide rate limiter or delivery SLA.
DLQ inspection can expose event content; restrict credentials. Audit outbox is not an
immutable external ledger. Export audit records before trusted bounded pruning (at most
1000 successfully delivered non-DLQ rows/call). Inbox/intents/disputes remain retained;
no automatic receipt expiry or storage bound is promised. Legacy ID-only inbox receipts
return current attempts, not reconstructed historical snapshots.

## Operations and reporting

Provide credential rotation, recovery/heartbeat/QA/outbox scheduler loops, monitored
disk capacity, backups and tested restore. PostgreSQL and remote isolated runners are
future scale-out options, not implemented alternative storage/execution backends.
Never log bearer values, approval keys, private paths/addresses, provider bodies or raw
experience trajectories. Experience export requires explicit opt-in plus redaction.
Keep local failure traces private until reviewed. Arrange a private maintainer channel
before sharing sensitive evidence; do not put credentials in public issues. Local
regression results do not constitute sandbox, live tenant or production acceptance.
