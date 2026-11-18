"""Operator-bound Test-stage tools for a separate Prime Agent MCP process.

Only registered inputs are accepted. Tool arguments never contain a caller role,
repository path, test command, approval, or artifact filesystem path. The MCP
process must be isolated from the agent kernel at the OS/deployment boundary.
"""
from __future__ import annotations

import asyncio
from hashlib import sha256
from pathlib import Path
import re
from typing import Annotated, Literal

from pydantic import ConfigDict, Field, model_validator

from .models import (ImmutableContract, ExecutionStatus, Metrics, Principal,
                     Readiness, RequirementResult, RootCause, TestTotals, Verification)
from .preflight import preflight
from .run_manager import RunManager, TERMINAL


_SHA = r"^[a-f0-9]{40}(?:[a-f0-9]{24})?$"
_DIGEST = r"^[a-f0-9]{64}$"
_REPO = r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$"
_RUN = r"^run-[a-f0-9]{32}$"
_ARTIFACT_PART = re.compile(r"[A-Za-z0-9_.-]+\Z")
RepoId = Annotated[str, Field(pattern=_REPO)]


class ToolPolicy(ImmutableContract):
    """An operator-owned capability set, loaded before MCP accepts requests."""
    schema_version: Literal["1.0"] = "1.0"
    operator_name: str = Field(min_length=1, max_length=100)
    allowed_repos: tuple[RepoId, ...] = Field(min_length=1)
    allowed_backends: tuple[Literal["docker", "local_trusted"], ...] = ("docker",)
    max_runtime_seconds: int = Field(default=1200, ge=1, le=1200, strict=True)
    max_test_executions: int = Field(default=4, ge=1, le=4, strict=True)
    max_debug_iterations: int = Field(default=0, ge=0, le=2, strict=True)
    max_model_spend_usd: float = Field(default=0.0, ge=0, le=2.0, allow_inf_nan=False)
    max_artifact_bytes: int = Field(default=2_097_152, ge=1, le=2_097_152, strict=True)

    @model_validator(mode="after")
    def scope_is_concrete(self):
        if (not self.operator_name.strip() or not self.allowed_repos or not self.allowed_backends
                or any(not re.fullmatch(_REPO, repo) for repo in self.allowed_repos)
                or len(set(self.allowed_repos)) != len(self.allowed_repos)
                or len(set(self.allowed_backends)) != len(self.allowed_backends)):
            raise ValueError("Tool policy requires concrete unique repositories and backends")
        return self


class StageTarget(ImmutableContract):
    model_config = ConfigDict(revalidate_instances="always")
    schema_version: Literal["1.0"] = "1.0"
    repo: str = Field(pattern=_REPO)
    pr_number: int = Field(gt=0, strict=True)
    expected_base_sha: str = Field(pattern=_SHA)
    expected_head_sha: str = Field(pattern=_SHA)
    spec_id: str = Field(min_length=1, max_length=200)
    spec_hash: str = Field(pattern=_DIGEST)
    test_profile_id: str = Field(min_length=1, max_length=200)
    test_profile_hash: str = Field(pattern=_DIGEST)
    environment_manifest_id: str = Field(min_length=1, max_length=200)
    environment_fingerprint: str = Field(pattern=_DIGEST)
    policy_version: str = Field(min_length=1, max_length=200)
    correlation_id: str = Field(min_length=1, max_length=128)


class ToolBudget(ImmutableContract):
    model_config = ConfigDict(revalidate_instances="always")
    max_debug_iterations: int = Field(default=0, ge=0, le=2, strict=True)
    max_test_executions: int = Field(default=4, ge=1, le=4, strict=True)
    max_runtime_seconds: int = Field(default=1200, ge=1, le=1200, strict=True)
    max_llm_cost_usd: float = Field(default=0.0, ge=0, le=2.0, allow_inf_nan=False)


class VerifyInput(StageTarget):
    budget: ToolBudget = Field(default_factory=ToolBudget)


class Blocker(ImmutableContract):
    code: str
    message: str
    owner_role: str
    remediation: str
    requirement_ids: tuple[str, ...] = ()


class PreflightOutput(ImmutableContract):
    schema_version: Literal["1.0"] = "1.0"
    ready: bool
    identity_hash: str = Field(pattern=_DIGEST)
    head_sha: str = Field(pattern=_SHA)
    findings: tuple[Blocker, ...]
    next_owner_role: str


class CandidatePatchOutput(ImmutableContract):
    branch: str
    commit_sha: str = Field(pattern=_SHA)
    original_head_sha: str = Field(pattern=_SHA)
    tested_candidate_sha: str = Field(pattern=_SHA)
    validated: Literal[True]
    patch_uri: str
    publication: Literal["LOCAL_DRAFT_ONLY"]
    hypothesis: str
    changed_paths: tuple[str, ...]
    accepted: Literal[False]


class VerifyOutput(ImmutableContract):
    schema_version: Literal["1.0"] = "1.0"
    run_id: str = Field(pattern=_RUN)
    repo: str
    pr_number: int
    reviewed_head_sha: str = Field(pattern=_SHA)
    spec_hash: str = Field(pattern=_DIGEST)
    test_profile_hash: str = Field(pattern=_DIGEST)
    environment_fingerprint: str = Field(pattern=_DIGEST)
    identity_hash: str = Field(pattern=_DIGEST)
    execution_status: ExecutionStatus
    readiness: Readiness
    requirements: tuple[RequirementResult, ...]
    findings: tuple[RootCause, ...]
    reasons: tuple[str, ...]
    tests: TestTotals
    metrics: Metrics
    candidate_patch: CandidatePatchOutput | None
    artifact_manifest_uri: str
    artifact_manifest_hash: str = Field(pattern=_DIGEST)
    next_owner_role: str
    human_approval_required: Literal[True] = True


class PinnedIdentity(ImmutableContract):
    repo: str = Field(pattern=_REPO)
    pr_number: int = Field(gt=0, strict=True)
    base_sha: str = Field(pattern=_SHA)
    pr_head_sha: str = Field(pattern=_SHA)
    spec_hash: str = Field(pattern=_DIGEST)
    test_profile_hash: str = Field(pattern=_DIGEST)
    environment_fingerprint: str = Field(pattern=_DIGEST)
    policy_version: str
    prompt_version: str


class EvidenceEntry(ImmutableContract):
    id: str
    path: str
    sha256: str = Field(pattern=_DIGEST)
    size: int = Field(ge=0, strict=True)
    uri: str


class ExecutionLedgerEntry(ImmutableContract):
    label: str
    command: tuple[str, ...]
    exit_code: int
    passed: int = Field(ge=0)
    failed: int = Field(ge=0)
    skipped: int = Field(ge=0)
    trusted: bool
    timed_out: bool
    cancelled: bool
    duration_seconds: float = Field(ge=0, allow_inf_nan=False)
    evidence_ids: tuple[str, ...]


class EvidenceManifest(ImmutableContract):
    schema_version: Literal["1.0"] = "1.0"
    run_id: str = Field(pattern=_RUN)
    identity: PinnedIdentity
    identity_hash: str = Field(pattern=_DIGEST)
    evidence: tuple[EvidenceEntry, ...]
    executions: tuple[ExecutionLedgerEntry, ...]
    workflow_id: str
    job_id: str
    reproduction: str
    retention: str
    integrity_note: str


class ManifestOutput(ImmutableContract):
    schema_version: Literal["1.0"] = "1.0"
    run_id: str = Field(pattern=_RUN)
    artifact_manifest_hash: str = Field(pattern=_DIGEST)
    manifest: EvidenceManifest


class ArtifactOutput(ImmutableContract):
    schema_version: Literal["1.0"] = "1.0"
    run_id: str = Field(pattern=_RUN)
    name: str
    sha256: str = Field(pattern=_DIGEST)
    size: int = Field(ge=0)
    content_utf8: str


class TestStageTools:
    """Trusted-side implementation. Do not instantiate inside an agent REPL.

    The operator binds identity and policy once in the MCP server process.
    Agent-controlled request schemas contain no caller, capability or path.
    """
    __test__ = False

    def __init__(self, manager: RunManager, principal: Principal, policy: ToolPolicy):
        if not isinstance(principal, Principal) or not isinstance(policy, ToolPolicy):
            raise PermissionError("Trusted operator identity and policy are required")
        if ("engineer" not in principal.roles or "qa" in principal.roles
                or "*" in principal.repos or not set(policy.allowed_repos) <= principal.repos
                or principal.name != policy.operator_name):
            raise PermissionError("MCP service identity must be scoped to Test-stage engineering")
        self.manager, self.principal, self.policy = manager, principal, policy
        self._execution_lock = asyncio.Lock()

    def _authorize_repo(self, repo: str) -> None:
        if repo not in self.policy.allowed_repos:
            raise PermissionError("Repository is outside the Test tool capability")
        self.manager.authorize(self.principal, repo, "engineer")

    def _context(self, target: StageTarget):
        self._authorize_repo(target.repo)  # Before reading registry or source.
        registered = self.manager.registry.request_for_pr(target.repo, target.pr_number)
        if (registered.spec_id != target.spec_id or registered.spec_hash != target.spec_hash
                or registered.test_profile_id != target.test_profile_id
                or registered.environment_manifest_id != target.environment_manifest_id):
            raise ValueError("STALE: approved registered inputs differ from the handoff")
        context = self.manager.registry.resolve(registered)
        if (context.base_sha != target.expected_base_sha
                or context.head_sha != target.expected_head_sha
                or context.spec_hash != target.spec_hash
                or context.profile.fingerprint != target.test_profile_hash
                or context.environment.fingerprint != target.environment_fingerprint
                or context.policy_version != target.policy_version):
            raise ValueError("STALE: pinned code, profile, environment or policy changed")
        if context.profile.backend not in self.policy.allowed_backends:
            raise PermissionError("Runner backend is outside the Test tool capability")
        if context.profile.backend == "local_trusted" and not context.profile.trusted:
            raise PermissionError("Host execution requires operator-reviewed trusted fixtures")
        return context, registered

    def preflight_pr(self, target: StageTarget) -> PreflightOutput:
        target = StageTarget.model_validate(target)
        context, _ = self._context(target)
        result = preflight(context)
        findings = tuple(Blocker(code=f.code, message=f.message, owner_role=f.owner_role,
                                 remediation=f.remediation, requirement_ids=tuple(f.requirement_ids))
                         for f in result.findings)
        return PreflightOutput(ready=result.ok, identity_hash=context.identity_hash,
                              head_sha=context.head_sha, findings=findings,
                              next_owner_role="test_owner" if result.ok else findings[0].owner_role)

    def _budget(self, budget: ToolBudget) -> None:
        policy = self.policy
        if (budget.max_debug_iterations > policy.max_debug_iterations
                or budget.max_test_executions > policy.max_test_executions
                or budget.max_runtime_seconds > policy.max_runtime_seconds
                or budget.max_llm_cost_usd > policy.max_model_spend_usd):
            raise PermissionError("Requested budget exceeds the Test tool capability")

    async def verify_pr(self, request: VerifyInput) -> VerifyOutput:
        request = VerifyInput.model_validate(request)
        self._budget(request.budget)
        async with self._execution_lock:
            context, registered = self._context(request)
            values = {**registered.model_dump(mode="json"), **request.budget.model_dump(mode="json"),
                      "correlation_id": request.correlation_id}
            from .models import CreateRunRequest
            run, _ = self.manager.create_run(CreateRunRequest.model_validate(values), self.principal,
                                             trigger_identity=f"prime-test:{request.correlation_id}")
            # Reject a source update between our first resolve and create_run.
            if run.identity_hash != context.identity_hash:
                self.manager.cancel_run(run.run_id, self.principal)
                raise ValueError("STALE: registered source moved before execution")
            if run.execution_status == ExecutionStatus.QUEUED:
                await self.manager.execute(run.run_id)
            result = self.manager.get_run(run.run_id)
            if result.execution_status not in TERMINAL:
                raise RuntimeError("Verification did not reach a terminal state")
            # A terminal result is not evidence until its envelope is validated.
            self.manager.artifact_path(run.run_id, "verification.json")
            manifest = self.manifest(run.run_id).manifest
            for entry in manifest.evidence:
                self.manager.artifact_path(run.run_id, entry.path)
            if result.readiness == Readiness.READY_FOR_QA:
                owner = "qa"
            elif result.unresolved_findings:
                owner = result.unresolved_findings[0].owner_role
            else:
                owner = "engineer"
            return VerifyOutput(run_id=result.run_id, repo=result.repo, pr_number=result.pr_number,
                reviewed_head_sha=result.reviewed_head_sha, spec_hash=result.spec_hash,
                test_profile_hash=result.test_profile_hash,
                environment_fingerprint=result.environment_fingerprint,
                identity_hash=result.identity_hash, execution_status=result.execution_status,
                readiness=result.readiness, requirements=tuple(result.requirements),
                findings=tuple(result.unresolved_findings), reasons=tuple(result.reasons),
                tests=result.tests, metrics=result.metrics, candidate_patch=result.candidate_patch,
                artifact_manifest_uri=result.artifact_manifest_uri,
                artifact_manifest_hash=result.artifact_manifest_hash, next_owner_role=owner)

    def _run(self, run_id: str) -> Verification:
        if not re.fullmatch(_RUN, run_id):
            raise ValueError("Invalid run identifier")
        state = self.manager.runs[run_id]
        self._authorize_repo(state.context.repo)  # Before any artifact access.
        return self.manager.get_run(run_id)

    def manifest(self, run_id: str) -> ManifestOutput:
        run = self._run(run_id)
        manifest = EvidenceManifest.model_validate(self.manager.artifacts(run_id))
        if (manifest.run_id != run_id or manifest.identity_hash != run.identity_hash
                or manifest.identity.repo != run.repo):
            raise ValueError("Evidence manifest identity mismatch")
        return ManifestOutput(run_id=run_id, artifact_manifest_hash=run.artifact_manifest_hash,
                              manifest=manifest)

    def artifact(self, run_id: str, name: str) -> ArtifactOutput:
        run = self._run(run_id)
        if (not isinstance(name, str) or not name or len(name) > 500
                or any(part in {"", ".", ".."} or not _ARTIFACT_PART.fullmatch(part)
                       for part in name.split("/"))):
            raise ValueError("Invalid artifact name")
        manifest = self.manifest(run_id).manifest
        entry = next((item for item in manifest.evidence if item.path == name), None)
        if entry is None:
            raise PermissionError("Artifact is not in the evidence allowlist")
        if entry.size > self.policy.max_artifact_bytes:
            raise ValueError("Artifact exceeds the Test tool transfer limit")
        path = self.manager.artifact_path(run_id, name)
        data = path.read_bytes()
        if len(data) > self.policy.max_artifact_bytes or sha256(data).hexdigest() != entry.sha256:
            raise ValueError("Evidence changed during transfer")
        return ArtifactOutput(run_id=run.run_id, name=name, sha256=entry.sha256,
                              size=len(data), content_utf8=data.decode("utf-8"))


def load_operator_tools(data_dir: Path, policy_path: Path) -> TestStageTools:
    """Build the trusted side from operator-provided paths, never MCP arguments."""
    data_dir, policy_path = Path(data_dir), Path(policy_path)
    if not data_dir.is_absolute() or not policy_path.is_absolute():
        raise ValueError("Operator data and policy paths must be absolute")
    if not data_dir.is_dir() or not policy_path.is_file() or policy_path.is_symlink():
        raise ValueError("Operator data directory and policy file must already exist")
    if policy_path.resolve().is_relative_to(Path(__file__).resolve().parents[1]):
        raise PermissionError("Operator policy must be stored outside the agent project")
    policy = ToolPolicy.model_validate_json(policy_path.read_text(encoding="utf-8"))
    principal = Principal(name=policy.operator_name, roles=frozenset({"engineer"}),
                          repos=frozenset(policy.allowed_repos))
    return TestStageTools(RunManager(data_dir), principal, policy)
