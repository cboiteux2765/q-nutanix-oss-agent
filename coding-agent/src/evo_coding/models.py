"""Versioned data contracts for the Coding Agent.

The handoff schema is PROVISIONAL (schema_version "0.1"): the team finalizes the
shared handoff format at the 10/30 design milestone. Every field the PRD lists as
a minimum shared field is present; coding-specific fields (scope, environment,
budget_limits) are additions that should be folded into the shared schema.
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION = "0.1"

Sha1 = Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Identifier = Annotated[str, Field(pattern=r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")]
RunId = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{2,127}$")]
Repository = Annotated[str, Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RunState(StrEnum):
    """Workflow states from the SRS state machine, plus exceptional states."""

    QUEUED = "QUEUED"
    SPEC_DRAFT = "SPEC_DRAFT"
    SPEC_APPROVED = "SPEC_APPROVED"
    CODING = "CODING"
    TESTING = "TESTING"
    READY_FOR_QUALITY_REVIEW = "READY_FOR_QUALITY_REVIEW"
    QA_REVIEW = "QA_REVIEW"
    AWAITING_HUMAN_MERGE = "AWAITING_HUMAN_MERGE"
    CLOSED = "CLOSED"
    BLOCKED = "BLOCKED"
    CHANGES_REQUIRED = "CHANGES_REQUIRED"
    INCONCLUSIVE = "INCONCLUSIVE"
    STALE = "STALE"
    CANCELLED = "CANCELLED"


class Owner(StrEnum):
    SPEC_AGENT = "spec_agent"
    SPEC_REVIEWER = "spec_reviewer"
    CODING_AGENT = "coding_agent"
    TEST_AGENT = "test_agent"
    QA_AGENT = "qa_agent"
    MAINTAINER = "maintainer"
    ENVIRONMENT_OWNER = "environment_owner"
    HUMAN_REVIEWER = "human_reviewer"
    ORCHESTRATOR = "orchestrator"


class RiskTier(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


# ---------------------------------------------------------------- handoff (input)


class Requirement(Contract):
    id: Identifier
    text: str = Field(min_length=1)


class AcceptanceCriterion(Contract):
    id: Identifier
    requirement_ids: list[Identifier] = Field(min_length=1)
    description: str = Field(min_length=1)
    test_hints: list[str] = Field(
        default_factory=list,
        description="pytest node IDs expected to verify this criterion, e.g. tests/test_x.py::test_y",
    )


class Approval(Contract):
    approved_by: str = Field(min_length=1)
    approved_at: datetime
    spec_hash: Sha256


class ScopePolicy(Contract):
    allowed_paths: list[str] = Field(min_length=1, description="Glob patterns the implementer may change")
    forbidden_paths: list[str] = Field(default_factory=list)
    test_paths: list[str] = Field(
        default_factory=lambda: ["tests/**", "**/test_*.py", "**/*_test.py", "**/conftest.py"]
    )
    may_add_tests: bool = True
    may_modify_tests: bool = False
    max_changed_files: int = Field(default=20, ge=1)
    max_changed_lines: int = Field(default=1000, ge=1)


class Prerequisite(Contract):
    name: str = Field(min_length=1)
    check_command: str = Field(min_length=1)
    owner: Owner = Owner.ENVIRONMENT_OWNER
    remediation: str = ""


class EnvironmentSpec(Contract):
    setup_commands: list[str] = Field(default_factory=list)
    test_command: str = "python -m pytest -q"
    test_framework: Literal["pytest"] = "pytest"
    prerequisites: list[Prerequisite] = Field(default_factory=list)
    baseline_must_pass: bool = True
    test_profile_id: str = "pytest-default-v1"
    environment_manifest_id: str = "pilot-env-v1"


class BudgetLimits(Contract):
    """Per-run ceilings requested by the handoff. They can only lower the operator caps."""

    max_usd: float | None = Field(default=None, ge=0)
    max_seconds: int | None = Field(default=None, ge=1)
    max_steps: int | None = Field(default=None, ge=1)
    max_revisions: int | None = Field(default=None, ge=0)


class BudgetUsed(Contract):
    usd: float = 0.0
    seconds: float = 0.0
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    reasoning_tokens: int = 0
    steps: int = 0


class ArtifactRef(Contract):
    name: str
    uri: str
    sha256: Sha256 | None = None
    media_type: str | None = None


class Handoff(Contract):
    """Spec Agent -> Coding Agent handoff (handoff.json)."""

    schema_version: Literal["0.1"] = SCHEMA_VERSION
    run_id: RunId
    repository: Repository
    issue_id: int = Field(gt=0)
    issue_title: str = Field(min_length=1)
    pr_number: int | None = Field(default=None, gt=0)
    source_commit: Sha1
    base_branch: str = "main"
    pr_head_sha: Sha1 | None = None
    spec_version: int = Field(ge=1)
    spec_hash: Sha256
    spec_uri: str = Field(min_length=1, description="Path relative to the handoff file, or absolute path")
    requirement_ids: list[Identifier] = Field(min_length=1)
    requirements: list[Requirement] = Field(min_length=1)
    acceptance_criteria: list[AcceptanceCriterion] = Field(min_length=1)
    status: RunState
    approval: Approval | None = None
    evidence_refs: list[str] = Field(default_factory=list)
    artifacts: list[ArtifactRef] = Field(default_factory=list)
    risk_tier: RiskTier = RiskTier.LOW
    unresolved_items: list[str] = Field(default_factory=list)
    next_owner: Owner
    timestamp: datetime
    budget_used: BudgetUsed = Field(default_factory=BudgetUsed)
    scope: ScopePolicy
    environment: EnvironmentSpec = Field(default_factory=EnvironmentSpec)
    budget_limits: BudgetLimits = Field(default_factory=BudgetLimits)


# ---------------------------------------------------------------- revision feedback (input)


class FailureDetail(Contract):
    test_id: str | None = None
    message: str = Field(min_length=1)
    details: str = ""


class RevisionRequest(Contract):
    """Test/QA/human -> Coding Agent request to fix an already-open PR."""

    schema_version: Literal["0.1"] = SCHEMA_VERSION
    run_id: RunId
    source: Literal["test_agent", "qa_agent", "human"]
    verdict: Literal["CHANGES_REQUIRED"] = "CHANGES_REQUIRED"
    tested_head_sha: Sha1
    failures: list[FailureDetail] = Field(default_factory=list)
    findings: list[str] = Field(default_factory=list)
    requested_by: str = Field(min_length=1)
    timestamp: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def _has_content(self) -> RevisionRequest:
        if not self.failures and not self.findings:
            raise ValueError("a revision request needs at least one failure or finding")
        return self

    @classmethod
    def from_test_agent_verification(cls, verification: dict[str, Any], run_id: str) -> RevisionRequest:
        """Adapt the Test Agent prototype's verification.json (schema 1.0) into a revision request."""
        if verification.get("readiness") != "CHANGES_REQUIRED":
            raise ValueError(f"verification readiness is {verification.get('readiness')!r}, not CHANGES_REQUIRED")
        failures: list[FailureDetail] = []
        for req in verification.get("requirements", []):
            if req.get("result") not in (None, "PASS", "VERIFIED"):
                failures.append(
                    FailureDetail(
                        test_id=",".join(req.get("test_ids", [])) or None,
                        message=f"{req.get('id')}: {req.get('result')}",
                        details=req.get("reason") or "",
                    )
                )
        findings = [
            f"{f.get('failure_class', 'UNKNOWN')}: {f.get('summary', '')} (next: {f.get('next_action', '')})"
            for f in verification.get("unresolved_findings", [])
        ]
        findings += [str(r) for r in verification.get("reasons", [])]
        return cls(
            run_id=run_id,
            source="test_agent",
            tested_head_sha=verification["reviewed_head_sha"],
            failures=failures,
            findings=findings or ["Test Agent reported CHANGES_REQUIRED"],
            requested_by=f"test-agent:{verification.get('run_id', 'unknown')}",
        )


# ---------------------------------------------------------------- coding result (output)


class ChangeType(StrEnum):
    ADDED = "A"
    MODIFIED = "M"
    DELETED = "D"


class ChangedFile(Contract):
    path: str
    change: ChangeType
    additions: int = 0
    deletions: int = 0
    is_test: bool = False


class CoverageStatus(StrEnum):
    IMPLEMENTED = "IMPLEMENTED"
    PARTIAL = "PARTIAL"
    NOT_ADDRESSED = "NOT_ADDRESSED"
    FAILING = "FAILING"


class LocalTestEvidence(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    NOT_FOUND = "NOT_FOUND"
    NO_HINTS = "NO_HINTS"


class CriterionCoverage(Contract):
    criterion_id: str
    status: CoverageStatus
    implementer_claim: str | None = None
    implementer_notes: str = ""
    local_test_ids: list[str] = Field(default_factory=list)
    local_test_evidence: LocalTestEvidence = LocalTestEvidence.NO_HINTS


class TestRunSummary(Contract):
    __test__ = False  # not a pytest test class

    label: str
    command: str
    commit: str | None = None
    returncode: int
    timed_out: bool = False
    passed: int = 0
    failed: int = 0
    errors: int = 0
    skipped: int = 0
    duration_seconds: float = 0.0
    failing_tests: list[str] = Field(default_factory=list)
    junit_artifact: str | None = None
    log_artifact: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.returncode == 0 and not self.timed_out and self.failed == 0 and self.errors == 0


class Blocker(Contract):
    code: str
    message: str
    owner: Owner
    details: dict[str, Any] = Field(default_factory=dict)


class ClarificationRequest(Contract):
    question: str
    ref: str | None = None
    owner: Owner = Owner.SPEC_REVIEWER


class Transition(Contract):
    from_state: RunState | None
    to_state: RunState
    at: datetime
    reason: str = ""


class ImplementerInfo(Contract):
    name: str
    version: str
    model: str | None = None
    prompt_version: str | None = None
    exit_status: str = ""
    steps: int = 0


class CodingResult(Contract):
    """Coding Agent output (coding_result.json): shared handoff fields + coding-phase fields."""

    schema_version: Literal["0.1"] = SCHEMA_VERSION
    phase: Literal["coding"] = "coding"
    run_id: RunId
    repository: Repository
    issue_id: int
    pr_number: int | None = None
    pr_url: str | None = None
    source_commit: Sha1
    pr_head_sha: Sha1 | None = None
    previous_head_sha: Sha1 | None = None
    base_branch: str
    branch: str | None = None
    revision: int = Field(default=0, ge=0)
    spec_version: int
    spec_hash: Sha256
    requirement_ids: list[str]
    acceptance_criteria: list[AcceptanceCriterion]
    status: RunState
    evidence_refs: list[str] = Field(default_factory=list)
    artifacts: list[ArtifactRef] = Field(default_factory=list)
    risk_tier: RiskTier
    unresolved_items: list[str] = Field(default_factory=list)
    next_owner: Owner
    timestamp: datetime = Field(default_factory=utc_now)
    budget_used: BudgetUsed = Field(default_factory=BudgetUsed)
    input_hash: Sha256
    changed_files: list[ChangedFile] = Field(default_factory=list)
    criteria_coverage: list[CriterionCoverage] = Field(default_factory=list)
    tests: list[TestRunSummary] = Field(default_factory=list)
    blockers: list[Blocker] = Field(default_factory=list)
    clarification_requests: list[ClarificationRequest] = Field(default_factory=list)
    implementer: ImplementerInfo | None = None
    requires_human_review: bool = False
    transitions: list[Transition] = Field(default_factory=list)
    test_agent_request: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _status_invariants(self) -> CodingResult:
        if self.status == RunState.TESTING:
            missing = [
                name for name in ("pr_number", "pr_head_sha", "branch") if getattr(self, name) in (None, "")
            ]
            if missing:
                raise ValueError(f"TESTING handoff is missing {missing}")
            if self.blockers:
                raise ValueError("TESTING handoff cannot carry blockers")
            final = [t for t in self.tests if t.label == "final"]
            if not final or not final[-1].succeeded:
                raise ValueError("TESTING handoff requires a passing final test run")
            if final[-1].commit != self.pr_head_sha:
                raise ValueError("final test run commit does not match pr_head_sha")
            if self.next_owner != Owner.TEST_AGENT:
                raise ValueError("TESTING handoff must be owned by the test_agent")
        if self.status in (RunState.BLOCKED, RunState.STALE, RunState.INCONCLUSIVE) and not self.blockers:
            raise ValueError(f"{self.status} result requires at least one blocker")
        return self
