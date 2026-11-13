"""Strict public request types; identities/roles are never read from request bodies."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Target(StrictModel):
    integration_sha: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    contracts: dict[str, str]
    seed: int


class Reproduction(StrictModel):
    command: list[str] = Field(min_length=1)
    prerequisites: str
    expected: str = Field(min_length=1)
    actual: str = Field(min_length=1)


class Evidence(StrictModel):
    trace_id: str
    log_excerpt: str
    violated_contract: str


class BugReport(StrictModel):
    schema_version: Literal["bug-report/v1"]
    target: Target
    reproduction: Reproduction
    evidence: Evidence
    suspected_slices: list[str] = Field(min_length=1)


class ItemCreate(StrictModel):
    kind: Literal["DEV", "CONTRACT_PATCH", "BUG"]
    slice_id: str = Field(min_length=1, max_length=128)
    owner: str = Field(min_length=1, max_length=128)
    allowed_paths: list[str] = Field(min_length=1)
    parent_id: str | None = None
    change_set_id: str | None = None
    deadline: float | None = None
    epic_id: str | None = None


class Claim(StrictModel):
    item_id: str
    expected_version: int = Field(ge=0, strict=True)
    ttl: int = Field(default=60, ge=1, le=3600)


class Transition(StrictModel):
    epoch: int = Field(ge=1, strict=True)
    expected_version: int = Field(ge=0, strict=True)
    state: Literal["working", "input-required", "completed", "failed", "canceled", "rejected"]
    base_sha: str | None = None
    source_sha: str | None = None
    artifact: dict = Field(default_factory=dict)


class Heartbeat(StrictModel):
    epoch: int = Field(ge=1, strict=True)
    ttl: int = Field(default=60, ge=1, le=3600)


class QAResult(StrictModel):
    expected_version: int = Field(ge=0, strict=True)
    integration_sha: str
    passed: bool | None = None
    outcome: Literal["PASS", "FAIL", "ENV_ERROR", "FLAKY"] | None = None
    report: dict | None = None


class MergeRequest(StrictModel):
    branch: str
    attempt_ids: list[str] = Field(min_length=1)


class Dependency(StrictModel):
    needs: str


class Dispute(StrictModel):
    evidence: str = Field(min_length=1, max_length=8000)


class ApprovalCreate(StrictModel):
    action: Literal["resume", "l0", "breaking_contract"] = "resume"
    item_id: str
    subject: dict
    ttl: int = Field(default=300, ge=1, le=3600)


class ApprovalUse(StrictModel):
    card: dict


class WorkerRegistration(StrictModel):
    endpoint_url: str = Field(pattern=r"^https?://[^\s]+$", max_length=2048)
    skills: list[str] = Field(default_factory=list, max_length=128)
    ttl: int = Field(default=60, ge=1, le=3600)


class DependencyRequest(StrictModel):
    schema_version: Literal["dependency-request/v1"]
    contract: str = Field(min_length=1, max_length=256)
    expected_change: dict


class EpicCreate(StrictModel):
    id: str = Field(min_length=1, max_length=128)
    branch: str
    skeleton_sha: str


class TaskStatus(StrictModel):
    schema_version: Literal["task-status/v1"]
    state: Literal["working"]


class DevSubmission(StrictModel):
    schema_version: Literal["dev-submission/v1"]
    state: Literal["completed"] = "completed"
    base_sha: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    source_sha: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    artifact: dict = Field(default_factory=dict)


class QAVerdict(StrictModel):
    schema_version: Literal["qa-verdict/v1"]
    integration_sha: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    outcome: Literal["PASS", "FAIL", "ENV_ERROR", "FLAKY"]
    report: dict | None = None


class GateMerge(MergeRequest):
    schema_version: Literal["gate-merge/v1"]
