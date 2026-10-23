"""Versioned contracts. Source text never controls policy or execution commands."""
from __future__ import annotations

from enum import StrEnum
from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


def canonical_hash(value: Any) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class ImmutableContract(Contract):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ExecutionStatus(StrEnum):
    QUEUED = "QUEUED"
    PREFLIGHT = "PREFLIGHT"
    TESTING = "TESTING"
    DIAGNOSING = "DIAGNOSING"
    REPAIRING = "REPAIRING"
    COMPLETED = "COMPLETED"
    BLOCKED = "BLOCKED"
    CANCELLED = "CANCELLED"
    ERROR = "ERROR"


class Readiness(StrEnum):
    READY_FOR_QA = "READY_FOR_QA"
    CHANGES_REQUIRED = "CHANGES_REQUIRED"
    BLOCKED = "BLOCKED"
    INCONCLUSIVE = "INCONCLUSIVE"
    STALE = "STALE"


class FailureClass(StrEnum):
    CODE_REGRESSION = "CODE_REGRESSION"
    ENVIRONMENT_BLOCKED = "ENVIRONMENT_BLOCKED"
    SPEC_AMBIGUOUS = "SPEC_AMBIGUOUS"
    TEST_FLAKY = "TEST_FLAKY"
    UNKNOWN = "UNKNOWN"


class RequirementOutcome(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"
    NOT_VERIFIED = "NOT_VERIFIED"


class Principal(ImmutableContract):
    name: str
    roles: frozenset[str] = frozenset({"engineer"})
    repos: frozenset[str] = frozenset({"org/service"})


class Requirement(ImmutableContract):
    id: str
    description: str
    test_ids: tuple[str, ...]


class AcceptanceTest(ImmutableContract):
    id: str
    node_id: str
    expected_behavior: str


class SpecDocument(ImmutableContract):
    schema_version: Literal["1.0"] = "1.0"
    spec_id: str
    status: str = "APPROVED"
    intent: str
    requirements: tuple[Requirement, ...]
    acceptance_tests: tuple[AcceptanceTest, ...]
    constraints: tuple[str, ...] = ()
    unresolved_questions: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()
    work_items: tuple[str, ...] = ()


class Prerequisite(ImmutableContract):
    name: str
    available: bool
    owner_role: str = "environment_owner"
    remediation: str = "Provision the declared sandbox prerequisite and retry."


class EnvironmentManifest(ImmutableContract):
    id: str
    prerequisites: tuple[Prerequisite, ...] = ()
    @property
    def fingerprint(self) -> str:
        return canonical_hash(self)


class TestProfile(ImmutableContract):
    __test__ = False
    id: str = "pytest-default-v1"
    test_paths: tuple[str, ...] = ("tests",)
    trusted: bool = False
    backend: Literal["local_trusted", "docker"] = "docker"
    timeout_seconds: int = Field(default=60, ge=1, le=1200, strict=True)
    docker_image: str | None = None
    @property
    def fingerprint(self) -> str:
        return canonical_hash(self)


class CreateRunRequest(Contract):
    schema_version: Literal["1.0"] = "1.0"
    repo: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    pr_number: int = Field(gt=0, strict=True)
    spec_id: str
    spec_uri: str
    spec_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    test_profile_id: str
    environment_manifest_id: str
    max_debug_iterations: int = Field(default=2, ge=0, le=2, strict=True)
    max_test_executions: int = Field(default=4, ge=1, le=4, strict=True)
    max_runtime_seconds: int = Field(default=1200, ge=1, le=1200, strict=True)
    max_llm_cost_usd: float = Field(default=2.0, ge=0, le=2.0)
    correlation_id: str = Field(min_length=1, max_length=128)
    parent_run_id: str | None = None


class ResolvedContext(ImmutableContract):
    repo: str
    pr_number: int
    base_sha: str
    head_sha: str
    snapshot_root: Path
    base_snapshot_root: Path
    spec: SpecDocument
    spec_uri: str
    spec_hash: str
    profile: TestProfile
    environment: EnvironmentManifest
    policy_version: str = "pilot-v1"
    prompt_version: str = "frozen-v1"
    allowed_patch_paths: tuple[str, ...] = ("src/",)
    @property
    def identity(self) -> dict[str, Any]:
        return dict(repo=self.repo, pr_number=self.pr_number, base_sha=self.base_sha,
                    pr_head_sha=self.head_sha, spec_hash=self.spec_hash,
                    test_profile_hash=self.profile.fingerprint,
                    environment_fingerprint=self.environment.fingerprint,
                    policy_version=self.policy_version, prompt_version=self.prompt_version)
    @property
    def identity_hash(self) -> str:
        return canonical_hash(self.identity)


def idempotency_key(context: ResolvedContext, trigger_identity: str) -> str:
    return canonical_hash({**context.identity, "trigger_identity": trigger_identity})


class TestCase(Contract):
    __test__ = False
    node_id: str
    outcome: str
    evidence_id: str


class TestExecution(Contract):
    __test__ = False
    exit_code: int
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    cases: list[TestCase] = Field(default_factory=list)
    junit_path: Path | None = None
    log_path: Path | None = None
    command: list[str] = Field(default_factory=list)
    timed_out: bool = False
    cancelled: bool = False
    trusted: bool = True
    blocked_reason: str | None = None
    duration_seconds: float = 0.0


class RequirementResult(Contract):
    id: str
    test_ids: list[str]
    result: RequirementOutcome
    evidence_ids: list[str] = Field(default_factory=list)
    reason: str = ""


class RootCause(Contract):
    failure_class: FailureClass
    summary: str
    observations: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    reproduction: list[str] = Field(default_factory=list)
    owner_role: str = "engineer"
    next_action: str = "NEEDS_ENGINEER"
    confidence: float = Field(default=0.0, ge=0, le=1)


class TestTotals(Contract):
    passed: int = Field(default=0, ge=0)
    failed: int = Field(default=0, ge=0)
    skipped: int = Field(default=0, ge=0)


class Metrics(Contract):
    debug_iterations: int = 0
    test_executions: int = 0
    runtime_seconds: float = 0.0
    model_spend_usd: float = 0.0
    model_reserved_usd: float = 0.0
    queue_seconds: float = 0.0
    phase_elapsed_seconds: dict[str, float] = Field(default_factory=dict)
    ci_cost_usd: float | None = None


class QAReview(Contract):
    status: Literal["PENDING", "APPROVED", "REJECTED"] = "PENDING"
    reviewer: str | None = None
    timestamp: str | None = None
    identity_hash: str | None = None
    artifact_manifest_hash: str | None = None


class Verification(Contract):
    schema_version: Literal["1.0"] = "1.0"
    run_id: str
    repo: str
    pr_number: int
    base_sha: str
    reviewed_head_sha: str
    spec_id: str
    spec_hash: str
    test_profile_hash: str
    environment_fingerprint: str
    policy_version: str
    identity_hash: str
    execution_status: ExecutionStatus = ExecutionStatus.QUEUED
    readiness: Readiness = Readiness.INCONCLUSIVE
    reasons: list[str] = Field(default_factory=list)
    tests: TestTotals = Field(default_factory=TestTotals)
    requirements: list[RequirementResult] = Field(default_factory=list)
    candidate_patch: dict[str, Any] | None = None
    unresolved_findings: list[RootCause] = Field(default_factory=list)
    artifact_manifest_uri: str
    artifact_manifest_hash: str = ""
    metrics: Metrics = Field(default_factory=Metrics)
    qa_review: QAReview = Field(default_factory=QAReview)
    human_approval_required: Literal[True] = True
    lifecycle: list[dict[str, Any]] = Field(default_factory=list)
    @model_validator(mode="after")
    def ready_has_evidence(self):
        if self.readiness == Readiness.READY_FOR_QA:
            if self.execution_status != ExecutionStatus.COMPLETED or self.tests.failed:
                raise ValueError("READY_FOR_QA requires completed execution without failed tests")
            if not self.requirements or any(r.result != RequirementOutcome.PASS for r in self.requirements):
                raise ValueError("READY_FOR_QA requires every applicable requirement to pass")
            if self.unresolved_findings or self.candidate_patch:
                raise ValueError("Original PR cannot be ready with unresolved findings or a candidate patch")
        return self


class ReviewRequest(Contract):
    decision: Literal["APPROVED", "REJECTED"]
    identity_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    artifact_manifest_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    comment: str = ""


class FeedbackRequest(Contract):
    kind: Literal["CODE_AGENT_ACK", "CODE_AGENT_REJECT", "RCA_CORRECTION", "PATCH_ACCEPTED", "PATCH_REJECTED"]
    comment: str = ""
    identity_hash: str = Field(pattern=r"^[a-f0-9]{64}$")

