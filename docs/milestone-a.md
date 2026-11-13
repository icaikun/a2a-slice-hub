# Milestone A integration contracts

## Governance

`IdentityPolicy(persons, bindings, contacts=...)` is server-owned configuration.
Bind every channel or worker account to its canonical `person_id`; contact
pairing/allowlisting does not confer any role. Approval owners and QA owners must
be canonical person IDs, not channel handles. A supplied `PersonRecord` is
looked up in the policy; its caller-supplied roles are not trusted. `MemoryScope`
values must be constructed from authenticated project configuration, not client
payloads. These primitives do not themselves install API authentication or a
project-scope storage filter.

`load_adrs(git, base_sha, ...)` reads only Git objects. Callers must supply their
trusted baseline, never a candidate SHA. Frontmatter-free documents are ignored;
malformed frontmatter fails closed. `effective_from` accepts an ISO date/timestamp
(compared to the baseline commit timestamp, UTC when no timezone is supplied), or
an ancestor commit ID. Effective accepted ADRs supersede older IDs before scope
filtering; a future ADR does not prematurely remove its predecessor.

Skills locks use the following complete-package structure (digests are lowercase
SHA-256 strings, not the placeholder below):

```json
{
  "skills": [
    {
      "name": "example",
      "files": {
        "SKILL.md": "<sha256>",
        "scripts/helper.py": "<sha256>",
        "references/guide.md": "<sha256>"
      },
      "claude_mirror": true
    }
  ]
}
```

A mapping from skill names to entries is also accepted. Every package file must
be listed; additional packages/files fail verification. Present Claude mirrors
are checked even without the flag. Mirrors require equal content and Git modes.
No lock means an empty verified manifest, not implicit trust in unlocked skills.

## Worker and adapter integration

One-argument `coding(directory)` callbacks retain their previous semantics.
Two-argument callbacks receive `ExecutionContext`, including a cancellation event,
the external `TaskBundle`, and recalled experience as advisory data. CLI adapters
add stricter governance checks, automatic commits and zero-net-change rejection.
The worker creates submission refs only after a successful adapter result.

Configure Git scratch **outside Git worktrees**. Artifacts are under
`attempts/<attempt_id>/`; no task, output schema, events, or checkpoint is written
into the execution worktree. A genuine bare repository is also a valid external
artifact location because Git discovery stops there and it has no worktree.

Adapters accept `executable`, `extra_args`, `process_spawner`, `timeout_seconds`,
`allowed_env_keys`, `env`, and `allow_empty`. Inherited secret/token/approval-key/encrypt-key/password/private-key variables,
`HUB_*` variables (except `HUB_DOWNLOAD_WRAPPER`), and HTTP/HTTPS/ALL proxy
variables are removed unless explicitly allowlisted. `HUB_DOWNLOAD_WRAPPER` can advertise the separate download wrapper;
AI CLI proxy settings are not injected automatically.

Windows cancellation uses a new process group, a Windows Job Object and taskkill;
the Job Object also handles restricted environments where taskkill is denied.
POSIX cancellation kills the process group. Artifacts and process containment
are not an OS security sandbox: run untrusted CLI tools under a suitable account
or container and protect the Hub's repository/control plane separately.

On `input-required`, a validated binary patch includes both uncommitted changes
and any changes already committed by the CLI. The worktree is reset to base.
To resume, the server reads the previous external `checkpoint.patch` and passes
`checkpoint_patch` plus `checkpoint_base_sha` into `Worker.execute`. A patch is
first applied and checked in a disposable validation worktree. Do not accept an
arbitrary client-supplied checkpoint or baseline. Unignored foreign untracked
files remain scope violations. Gitignored transient files are excluded from
checkpoints and removed with `git clean -fdX` after checking filesystem links.
`Worker.execute(..., rework_feedback=...)` forwards feedback into the task bundle.

Legacy `record_recovery=True` still calls the existing raw trajectory hook. This
is advisory recording, not verified knowledge promotion. The separate
`record_verified_recovery(..., qa_verified=True)` server-side hook passes
`verified_only=True`; future KnowledgeStore integration must invoke it only after
independent QA, not from developer-controlled task output.

## Verification boundary

Acceptance tests exercise both adapters with real Git and real local Python
subprocesses, including a spawned grandchild. They do not authenticate to, bill,
or validate a live Codex/Claude service. The Windows execution path is exercised
by the inherited-temp test runner; POSIX process-group behavior requires a POSIX
run of the same tests before claiming runtime acceptance on that platform.
