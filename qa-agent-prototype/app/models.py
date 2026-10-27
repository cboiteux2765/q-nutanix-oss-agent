"""Contracts for the QA phase.

The QA Agent reads the Test Agent's verification.json (schema v1.0 on the
feat/test-agent-prototype branch) and writes qa_decision.json + qa_report.md.
"""

from __future__ import annotations

from enum import IntEnum, StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

QA_SCHEMA_VERSION = "1.0"
POLICY_VERSION = "qa-policy-0.1"


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Verdict(StrEnum):
    APPROVED = "APPROVED"
    CHANGES_REQUESTED = "CHANGES_REQUESTED"
    NEEDS_HUMAN = "NEEDS_HUMAN"
    BLOCKED = "BLOCKED"
    STALE = "STALE"


class CheckStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    NOT_CHECKED = "NOT_CHECKED"


class Severity(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class RiskTier(IntEnum):
    """Ordered so the PR tier is the max over its files."""

    LOW = 1
    MEDIUM = 2
    HIGH = 3


class Finding(Contract):
    check: str
    severity: Severity
    blocking: bool
    title: str
    file: str | None = None
    line: int | None = None
    evidence: str
    suggested_fix: str


class CheckResult(Contract):
    name: str
    required: bool
    status: CheckStatus
    detail: str = ""
    findings: list[Finding] = Field(default_factory=list)
    attempts: int = 1
    duration_seconds: float = 0.0


class ChangedFile(Contract):
    path: str
    status: str  # git name-status letter: A, M, D, R...


class Handoff(Contract):
    """The fields QA needs from the Test Agent's verification.json."""

    model_config = ConfigDict(extra="ignore")

    schema_version: str = "1.0"
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
    artifact_manifest_uri: str
    readiness: str | None = None
    human_approval_required: bool | None = None


class Policy(Contract):
    """Team-owned rules. The agent never edits these (PRD: never evolves)."""

    version: str = POLICY_VERSION
    high_risk_paths: list[str] = Field(
        default_factory=lambda: [
            ".github/workflows/*",
            "*/auth/*",
            "auth/*",
            "*/security/*",
            "security/*",
            "*/migrations/*",
            "migrations/*",
            "LICENSE*",
            "COPYING*",
        ]
    )
    low_risk_paths: list[str] = Field(
        default_factory=lambda: ["*.md", "docs/*", "tests/*", "*/tests/*", "test_*.py", "*/test_*.py"]
    )
    dependency_files: list[str] = Field(default_factory=lambda: ["requirements*.txt", "*/requirements*.txt"])
    required_checks: list[str] = Field(default_factory=lambda: ["security", "secrets", "dependencies"])
    agent_may_approve: list[str] = Field(default_factory=lambda: ["LOW", "MEDIUM"])
    # Team PRD open question: during our own pilot, humans make every final call.
    pilot_mode: bool = True
    tool_timeout_seconds: int = 120
    tool_retries: int = 1
    bandit_command: str = "bandit"


class Reviewer(Contract):
    kind: str  # "agent" or "human"
    name: str


class QADecision(Contract):
    schema_version: str = QA_SCHEMA_VERSION
    qa_run_id: str
    created_at: str
    repo: str | None = None
    pr_number: int | None = None
    base_sha: str | None = None
    head_sha: str | None = None
    spec_id: str | None = None
    spec_hash: str | None = None
    test_run_id: str | None = None
    test_identity_hash: str | None = None
    verification_sha256: str | None = None
    risk_tier: str | None = None
    risk_reasons: list[str] = Field(default_factory=list)
    verdict: Verdict
    reasons: list[str]
    next_owner: str
    checks: list[CheckResult] = Field(default_factory=list)
    reviewer: Reviewer
    human_confirmation_required: bool
    policy_version: str
    duration_seconds: float = 0.0
    extra: dict[str, Any] = Field(default_factory=dict)
