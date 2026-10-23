"""Evidence-backed diagnosis and proposal-only repair adapters.

No adapter can grant a passing result or change the original checkout. The run
manager owns reservations, invocation limits and the independent test execution.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
import re
import shutil
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from .models import ResolvedContext, RootCause, TestExecution


@dataclass(frozen=True)
class CandidatePatchProposal:
    changes: dict[str, str]
    hypothesis: str
    evidence_ids: list[str] = field(default_factory=list)


@runtime_checkable
class HypothesisAdapter(Protocol):
    """OpenAI or another frozen model supplies hypotheses, never test verdicts."""

    def propose_hypotheses(self, context: Mapping[str, Any], evidence: Mapping[str, Any]) -> list[dict[str, Any]]: ...


@runtime_checkable
class RepairAdapter(Protocol):
    """Implement with mini-swe-agent-v2 behind an explicit bounded adapter."""

    quote_usd: float

    def propose(self, context: Any, execution: Any, root_cause: Any = None, *, attempt: int = 1) -> CandidatePatchProposal | None: ...


@runtime_checkable
class JudgeAdapter(Protocol):
    """Jev may add review advice; its verdict cannot replace runner evidence."""

    def review(self, proposal: CandidatePatchProposal, evidence: Mapping[str, Any]) -> Mapping[str, Any]: ...


@runtime_checkable
class PMNotificationAdapter(Protocol):
    def notify(self, run_id: str, owner_role: str, finding: Mapping[str, Any]) -> None: ...


class AdapterNotConfigured(RuntimeError):
    """An external adapter requires an explicitly configured implementation."""


class UnconfiguredRepairAdapter:
    quote_usd = 0.0

    def propose(self, context: Any, execution: Any, root_cause: Any = None,
                *, attempt: int = 1) -> CandidatePatchProposal | None:
        raise AdapterNotConfigured("No live repair provider configured; preserve evidence and escalate to an engineer.")


class NullPMNotificationAdapter:
    """Offline default. Persist notifications for inspection, with no network."""

    def __init__(self) -> None:
        self.notifications: list[dict[str, Any]] = []

    def notify(self, run_id: str, owner_role: str, finding: Mapping[str, Any]) -> None:
        self.notifications.append({"run_id": run_id, "owner_role": owner_role, "finding": dict(finding)})


def _get(value: Any, name: str, default: Any = None) -> Any:
    return value.get(name, default) if isinstance(value, Mapping) else getattr(value, name, default)


def _plain(value: Any) -> str:
    return str(getattr(value, "value", value))


def _evidence(execution: Any) -> list[str]:
    ids = list(_get(execution, "evidence_ids", []) or [])
    for case in _get(execution, "cases", []) or []:
        value = _get(case, "evidence_id")
        if value and value not in ids:
            ids.append(str(value))
    for name in ("execution_id", "junit_evidence_id", "log_evidence_id"):
        value = _get(execution, name)
        if value and value not in ids:
            ids.append(str(value))
    return ids


def _passed(execution: Any) -> bool:
    """A natural-language/advisory 'passed' field is intentionally ignored."""
    if execution is None:
        return False
    code = _get(execution, "exit_code", _get(execution, "returncode"))
    totals = _get(execution, "tests", execution)
    return (code == 0 and _get(totals, "failed", 0) == 0
            and _get(totals, "errors", 0) == 0
            and _get(totals, "passed", 0) > 0
            and _get(execution, "trusted", True)
            and not _get(execution, "timed_out", False)
            and not _get(execution, "cancelled", False))


def _failed(execution: Any) -> bool:
    if (execution is None or not _get(execution, "trusted", True)
            or _get(execution, "timed_out", False) or _get(execution, "cancelled", False)):
        return False
    # pytest exit 1 means tests failed; collection/configuration failures do not
    # establish a product-code regression.
    code = _get(execution, "exit_code", _get(execution, "returncode"))
    totals = _get(execution, "tests", execution)
    return code == 1 and _get(totals, "failed", 0) > 0


def _mapping_valid(context: ResolvedContext, execution: TestExecution, baseline: TestExecution) -> bool:
    # Preflight has already validated the approved spec mapping. Require actual
    # head failure cases to reference those tests, and the same cases to pass on
    # the baseline. Aggregate suite totals alone cannot establish regression.
    acceptance = {test.id: test.node_id for test in context.spec.acceptance_tests}
    mapped = {acceptance[test_id] for requirement in context.spec.requirements
              for test_id in requirement.test_ids if test_id in acceptance}
    failures = {case.node_id for case in execution.cases if case.outcome.upper() in {"FAIL", "FAILED"}}
    baseline_passes = {case.node_id for case in baseline.cases if case.outcome.upper() in {"PASS", "PASSED"}}
    return bool(mapped and failures and failures <= mapped and failures <= baseline_passes)


def classify_failure(context: ResolvedContext, execution: TestExecution,
                     base_execution: TestExecution | None = None,
                     rerun: TestExecution | None = None) -> RootCause:
    """Classify observed evidence conservatively; this is not an LLM decision.

    Context must carry a validated requirement mapping and preflight findings.
    A head failure with a passing base establishes only a bounded regression
    diagnosis, never whether an unexecuted patch fixes it.
    """
    evidence_ids = _evidence(execution)
    reproduction = list(_get(execution, "command", []) or [])
    if isinstance(_get(execution, "command"), str):
        reproduction = [_get(execution, "command")]
    common = dict(evidence_ids=evidence_ids, reproduction=reproduction)
    blockers = _get(context, "blockers", []) or _get(_get(context, "preflight", {}), "blockers", []) or []
    if context.spec.conflicts or context.spec.unresolved_questions or context.spec.status != "APPROVED":
        blockers = [*blockers, {"failure_class": "SPEC_AMBIGUOUS", "summary":
                    "; ".join([*context.spec.conflicts, *context.spec.unresolved_questions]) or "The specification is not approved."}]
    for prerequisite in context.environment.prerequisites:
        if not prerequisite.available:
            blockers = [*blockers, {"failure_class": "ENVIRONMENT_BLOCKED", "summary":
                        f"Missing prerequisite {prerequisite.name}: {prerequisite.remediation}"}]
    for category, owner in (("SPEC_AMBIGUOUS", "spec_owner"), ("ENVIRONMENT_BLOCKED", "environment_owner")):
        matched = [b for b in blockers if _plain(_get(b, "failure_class", _get(b, "category", ""))) == category]
        if matched:
            observations = [str(_get(b, "summary", _get(b, "message", b))) for b in matched]
            return RootCause(failure_class=category, summary=observations[0], observations=observations,
                             owner_role=owner, next_action="Resolve the preflight blocker, then start a fresh verification run.",
                             confidence=1.0, **common)

    failed_nodes = {case.node_id for case in execution.cases if case.outcome.upper() in {"FAIL", "FAILED"}}
    rerun_passes = {case.node_id for case in rerun.cases if case.outcome.upper() in {"PASS", "PASSED"}} if rerun else set()
    if _failed(execution) and _passed(rerun) and failed_nodes and failed_nodes <= rerun_passes:
        common["evidence_ids"] += _evidence(rerun)
        return RootCause(failure_class="TEST_FLAKY", summary="The same pinned test suite failed and then passed.",
                         observations=["Both observed outcomes are retained. A diagnostic rerun does not erase the failure."],
                         owner_role="engineer", next_action="Investigate nondeterminism; do not pass or quarantine automatically.",
                         confidence=1.0, **common)

    if _failed(execution) and _passed(base_execution) and _mapping_valid(context, execution, base_execution):
        common["evidence_ids"] += _evidence(base_execution)
        return RootCause(failure_class="CODE_REGRESSION", summary="The PR head fails a requirement-linked test that passes on the base.",
                         observations=["pytest reported a failing head execution and a passing base comparison.",
                                       "The requirement-to-test mapping was validated against collected test IDs."],
                         owner_role="code_agent", next_action="Reserve validation budget, propose a source-only candidate, and validate in a separate workspace.",
                         confidence=0.95, **common)

    return RootCause(failure_class="UNKNOWN", summary="The available evidence does not establish a safely repairable regression.",
                     observations=["A product regression requires a failing head, passing baseline, and validated requirement mapping."],
                     owner_role="engineer", next_action="Preserve evidence and obtain the missing comparison or diagnostic evidence; do not patch speculatively.",
                     confidence=0.0, **common)


class PatchPolicyError(ValueError):
    def __init__(self, issues: Sequence[str]) -> None:
        self.issues = list(issues)
        super().__init__("; ".join(self.issues))


_PROTECTED_PARTS = {"test", "tests", "testing", "__tests__", ".github", ".git", "ci", ".circleci",
                    "auth", "authentication", "authorization", "security", "permissions", "policies", "policy",
                    "node_modules", "venv", ".venv", "migrations"}
_DEPENDENCY_NAMES = {"pyproject.toml", "setup.py", "setup.cfg", "tox.ini", "pytest.ini", "conftest.py", "pipfile",
                     "pipfile.lock", "poetry.lock", "uv.lock", "package.json", "package-lock.json", "yarn.lock",
                     "pnpm-lock.yaml", "dockerfile", "makefile", "jenkinsfile"}


def validate_patch(changes: Mapping[str, str], allowed_paths: Sequence[str], original_root: str | Path) -> list[str]:
    """Reject any edit outside an exact allowlist or touching protected surfaces.

    This validates patch scope and Python syntax, not semantic correctness or
    containment of malicious Python. Actual execution still needs isolation.
    """
    issues: list[str] = []
    root = Path(original_root).resolve(strict=True)
    if not root.is_dir():
        raise PatchPolicyError(["original_root must be a directory"])
    allowlist = set(allowed_paths)
    if not changes or len(changes) > 5:
        issues.append("A proposal must change between one and five source files.")
    total = 0
    for raw, content in changes.items():
        if not isinstance(raw, str) or not isinstance(content, str):
            issues.append("Patch paths and replacement contents must be strings.")
            continue
        relative = PurePosixPath(raw)
        parts = {p.lower() for p in relative.parts}
        name = relative.name.lower()
        if ("\\" in raw or ":" in raw or relative.is_absolute() or ".." in relative.parts
                or "." in raw.split("/") or "//" in raw or raw != relative.as_posix()):
            issues.append(f"Unsafe relative path: {raw}")
            continue
        if raw not in allowlist:
            issues.append(f"Path is outside the exact source allowlist: {raw}")
        if (parts & _PROTECTED_PARTS or any(p.startswith(".") for p in relative.parts)
                or name in _DEPENDENCY_NAMES or name.startswith(("test_", "requirements", "auth_", "security_"))
                or re.search(r"(?:^|[._-])(test|spec|auth|security|policy|permission)(?:[._-]|$)", name)
                or relative.suffix.lower() not in {".py", ".ts", ".tsx", ".js", ".jsx"}):
            issues.append(f"Protected or non-source path: {raw}")
        path = root.joinpath(*relative.parts)
        ancestors = [path, *list(path.parents)[:len(relative.parts)]]
        if any(part.is_symlink() or (hasattr(part, "is_junction") and part.is_junction()) for part in ancestors):
            issues.append(f"Symlink paths are not editable: {raw}")
        if not path.resolve().is_relative_to(root) or not path.is_file():
            issues.append(f"Only existing regular source files may be replaced: {raw}")
        total += len(content.encode("utf-8"))
        if not content.strip():
            issues.append(f"Source deletion is prohibited: {raw}")
        elif relative.suffix == ".py":
            try:
                ast.parse(content, filename=raw)
            except (SyntaxError, ValueError) as exc:
                issues.append(f"Invalid Python source in {raw}: {exc}")
    if total > 250_000:
        issues.append("Candidate replacement content exceeds 250 KB.")
    if issues:
        raise PatchPolicyError(issues)
    return []


def materialize_candidate(proposal: CandidatePatchProposal, original_root: str | Path,
                          destination: str | Path, allowed_paths: Sequence[str]) -> Path:
    """Make an independent copy with source replacements; never mutate the PR."""
    original = Path(original_root).resolve(strict=True)
    target = Path(destination).resolve()
    if target == original or target.is_relative_to(original) or original.is_relative_to(target):
        raise PatchPolicyError(["Candidate workspace must be separate from the original checkout."])
    validate_patch(proposal.changes, allowed_paths, original)
    ignored = {".git", ".venv", "venv", "__pycache__", ".pytest_cache", "node_modules"}
    for child in original.rglob("*"):
        if any(part in ignored for part in child.relative_to(original).parts):
            continue
        if child.is_symlink() or (hasattr(child, "is_junction") and child.is_junction()):
            raise PatchPolicyError([f"Candidate copy rejects symlink: {child.relative_to(original)}"])
    shutil.copytree(original, target, ignore=shutil.ignore_patterns(*ignored))
    for relative, content in proposal.changes.items():
        target.joinpath(*PurePosixPath(relative).parts).write_text(content, encoding="utf-8")
    return target


class FixtureRepairAdapter:
    """Deterministic local demo adapter; no model, network, shell, or verdict."""
    quote_usd = 0.0

    def __init__(self, changes: Mapping[str, str] | None = None,
                 proposals: Sequence[CandidatePatchProposal] | None = None) -> None:
        self.proposals = list(proposals) if proposals is not None else ([CandidatePatchProposal(
            changes=dict(changes), hypothesis="Restore the approved empty-input behavior.", evidence_ids=[])] if changes else [])

    def propose(self, context: Any, execution: Any, root_cause: Any = None, *, attempt: int = 1) -> CandidatePatchProposal | None:
        if root_cause is not None and _plain(_get(root_cause, "failure_class")) != "CODE_REGRESSION":
            return None
        if not 1 <= attempt <= len(self.proposals):
            return None
        proposal = self.proposals[attempt - 1]
        return CandidatePatchProposal(changes=dict(proposal.changes), hypothesis=proposal.hypothesis,
                                      evidence_ids=list(proposal.evidence_ids or _evidence(execution)))
