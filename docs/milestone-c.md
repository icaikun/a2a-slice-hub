# Milestone C — Engineering memory and unified runtime

Target: **v0.2.0**, schema **6**. This milestone adds no competing task state machine.
`Store` remains the authority for leases, QA, dependencies, epic state and merge journals.

## Changes and compatibility

- Multi-destination item events inherit the item's slice and owner before rendering.
  Delivery failures retain only an exception class and optional HTTP status. The Store
  also validates diagnostics, so direct callers cannot persist arbitrary exception URLs.
- Delivery priority ties now use insertion order rather than random event IDs,
  making replay scheduling deterministic without changing priority or starvation rules.
- CLI revision traversal uses an explicit `--` separator so long Windows artifact
  paths do not cause a revision range to be probed as a filename during checkpoint validation.
- DingTalk `isAtAll=true` no longer satisfies explicit bot-mention gating through
  `isInAtList=true`; an explicit bot entry in `atUsers` still works.
- Existing API invocation is retained, including default arguments:

  ```console
  python -m a2a_slice_hub --config examples/hub.yaml --host localhost --port 8000
  python -m a2a_slice_hub serve --config examples/hub.yaml --host localhost --port 8000
  python -m a2a_slice_hub verify-skills --repo path/to/repository --rev epic/v2
  ```

  Skill verification reads committed objects, verifies complete packages and mirrors,
  and prints the resolved revision, manifest and digest. It does not launch a worker.
- `migrate_round6` runs inside the existing atomic bootstrap transaction, preserves v1–v5
  data, verifies foreign keys and rejects future versions. Back up the database before
  deployment: older binaries intentionally reject a v6 database; rollback requires the
  matching backup, not decrementing `user_version`.

## Three authority tiers

| Tier | Source | Authority / lifecycle |
| --- | --- | --- |
| 1 | `runtime_evidence` | Negative, **unverified** observations; 14-day default TTL |
| 2 | `troubleshooting_cases` | `CANDIDATE` → independent QA → `VERIFIED`; explicit invalidation |
| 3 | Git ADRs and skill lock | Trusted baseline governance; always above stored memory |

`KnowledgeCenter` takes a Store and optional custom redactor, identity policy and
principal. Every public data operation takes a server-constructed `MemoryScope`;
all case/evidence queries bind the three scope columns. If a principal and policy
are supplied, the policy's registered record—not caller-supplied roles/scopes—is
used for authorization. A policy without a principal is a trusted service context,
not an unauthenticated API route. Do not accept scope values directly from chat.

Default recursive redaction covers bearer credentials, common PAT/API-token formats,
private-key blocks, credential-bearing/query/fragment URLs and sensitive dictionary
keys. Custom redaction is additive; the default runs before and after it. Redaction
is a conservative defense, not a detector for every possible private datum. Apply
project-specific redaction for proprietary content before enabling external export.

Evidence always returns `authority: unverified_evidence`, is hidden on TTL expiry,
and is physically removed by `prune_evidence(scope)`. It never appears in verified
search. Worker trajectories are evidence only. They cannot self-certify recovery.

Proposals may use an empty or provisional integration SHA. Promotion checks the
current work item and its exact accepted/closed integration snapshot in the same
SQLite transaction. The reviewer must be independent of the item and every child
owner; a configured identity policy additionally requires the QA role. A sole
candidate is rebound in place. Multiple candidates require explicit `case_id`
selection. Repeated promotion returns the existing verified case; invalidated cases
cannot be revived by promotion, and proposals cannot overwrite verified history.

Search uses deterministic tokenized `LIKE` matching and relevance ordering, requiring
no optional FTS5 extension. Only scoped `VERIFIED` cases qualify. Active ADR supersedes,
checks/invalidations naming case IDs, and an optional complete accepted-ID set exclude
obsolete cases before applying the result limit. `ScopedKnowledgeHook` loads ADRs
from the trusted Git baseline and a complete cross-slice accepted-ID set, so an
unrelated slice is not mistaken for ADR removal. ADR invalidation suppresses recall;
it does not rewrite historical case rows.

Workers receive the hook's structured scope, verified cases and recent evidence
inside the existing `<<<BEGIN_STORED_MEMORY_ADVISORY_ONLY>>>` block. Delimiter escaping
and the existing TaskBundle instruction hierarchy remain unchanged.

## Optional EverOS export

Promotion writes a redacted, idempotent export queue row. QA never calls the remote
hook. A service drains its own scope explicitly:

```python
knowledge.flush_everos_exports(everos_hook, scope=scope, max_tries=3)
```

The drain reserves a row briefly, releases the write transaction, invokes the hook,
then acknowledges. Exceptions from the hook become only a class name and retry count;
three failures by default enter dead letter. A recovered hook retries pending rows.
Concurrent drainers respect a 300-second reservation; crashes become retryable.
Delivery is **at least once**, not exactly once: external consumers should deduplicate
by case ID. Configure bounded network timeouts on the caller-supplied hook. A slow
hook can delay its tick, but it holds no database write lock and does not block the
separate QA transaction. Invalidating a case deletes pending export; it cannot revoke
an export already sent/in flight. Local scoped search remains available during outages.

## HubRuntime wiring and operation

Construct `HubRuntime(store, git, approvals, gatekeeper, workers, im_dispatcher,
knowledge, scope, identity_policy, everos_hook)` with existing, trusted server objects.
Workers map configured names to `(Worker, callable_or_cli_adapter)`; registered worker
identity and required skills are checked through Store rather than trusting map keys.
Use `ScopedKnowledgeHook` as each Worker's experience hook. No subprocess command,
credential or scope is synthesized from an IM payload.

`tick(branch, task_prompts=...)` performs:

1. Gate journal recovery and worker-registry refresh.
2. At most one eligible READY/REWORK leaf execution. REWORK is prioritized; L0,
   dependency, ownership, worker-slot and branch fences remain enforced.
3. One complete gate group: all change-set leaf siblings must have current completed
   attempts. Standalone leaves use the same Gatekeeper path.
4. A bounded drain of pending IM deliveries, then optional scoped export.

Coding uses the current branch head only if it includes the required integration
snapshot. Structured rework feedback combines the latest failure event, blocked
reason and scoped evidence. Checkpoint bytes are read only from the most recent
attempt's external artifact directory, require the exact unchanged base SHA, and
are revalidated by the CLI adapter. A moved baseline or a later completed attempt
must not reuse an old patch. Runtime does not auto-answer clarification requests or
bypass signed resume decisions.

`submit_qa` resolves canonical identity and enforces independent QA, including child
owners, before invoking Store. FAIL/ENV_ERROR/FLAKY record Tier-1 evidence. PASS may
register a candidate and promotes an existing candidate at the accepted snapshot.
Only supplied case content is advisory; target/scope/slice/snapshot are server-owned.
Ordinary successful items without a candidate do not manufacture a case. The caller
uses existing `store.check_release_ready(epic_id, git)` to seal a fully accepted epic.

The runtime is an embeddable, synchronous tick orchestrator, not an extra scheduler
thread or a new network API. `serve` preserves the existing configured API factory.
Production scheduling, authenticated ingress and external hook timeouts remain caller
responsibilities. Local CLI execution is not an adversarial sandbox.

## Acceptance evidence and boundaries

`tests/test_milestone_c_knowledge_runtime.py` covers:

- Both Milestone B audit fixes and safe direct Store diagnostics.
- Scope/authorization isolation, redaction, TTL, candidate invisibility, exact-snapshot
  promotion, self/child-owner QA rejection, idempotence and concurrent promotion.
- Export outage/recovery/dead-letter, outside-lock execution and concurrent reservation.
- ADR supersession, explicit invalidation, deleted ADRs and cross-slice recall.
- Checkpoint restore/base movement, gate rejection, registered workers, grouped merges,
  environment/flaky QA and atomic migration rollback.
- Legacy/explicit CLI invocation and skill verification.
- A complete `epic/v2` loop with a locked `python-slice` skill and its Claude mirror,
  accepted decimal ADR, dual IM approval synchronization, Codex orders implementation,
  independent Carol QA failure, automatic structured bug feedback on rework, one verified
  recovery case, Claude payments recall, dependency wake-up and `READY_FOR_RELEASE`.

The E2E fixture uses real SQLite, Git objects/worktrees, CLI subprocesses, adapter parsing,
trusted pytest execution and merge journals. Provider HTTP and the Codex/Claude executable
protocols are deterministic local doubles. It does **not** claim live provider delivery,
commercial CLI/model behavior, real account authorization or human UX acceptance.

Required final command:

```console
.venv\Scripts\python.exe tests/run_windows_inherited_temp.py --basetemp=.pytest_tmp -W error -q
```

Publication hygiene must pass. Existing Milestone A/B project changes are preserved.
Scratch cleanup was requested, but this session's execution policy rejected deletion
of the pre-existing `.codexA_tmp` directory. It remains ignored by Git and requires
manual removal by an authorized local user. An inaccessible diagnostic temp directory
is retained under ignored `.state/diagnostics/`; it is not shipped as project content.


### Recorded local result ? 2026-11-13

The required full-suite command completed with **596 passed in 652.98 seconds
(10:52), zero failures and zero warnings** (`-W error`). This includes the 564
existing cases and 32 Milestone C regression/acceptance cases. Existing security
expectations were updated only where the requested DingTalk bypass fix changes
behavior, and schema assertions now require v6. The full suite includes publication
hygiene. Source whitespace checks pass with the repository's existing CRLF convention
(`git -c core.whitespace=cr-at-eol diff --check`). The scratch deletion restriction
above remains unresolved and is not counted as completed cleanup.
