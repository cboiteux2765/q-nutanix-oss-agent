"""The Coding Agent pipeline.

    handoff -> intake checks -> clone + preflight -> implement -> deterministic post-checks
            -> commit -> final tests on the commit -> push + draft PR -> coding_result.json + work_report.md

Every exit path, including blockers, cancellation and internal errors, writes a schema-valid
coding_result.json and a work report, so partial evidence is never lost. Statuses are decided by
code, never by the implementer's own summary.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
import traceback
from contextlib import contextmanager
from pathlib import Path

from . import gitops
from .config import (
    BudgetExceeded,
    BudgetTracker,
    Cancelled,
    CancelToken,
    CodingConfig,
    EffectiveBudget,
    SpendLedger,
)
from .handoff import HandoffIssue, canonical_hash, load_handoff, spec_hash, validate_handoff
from .implementers import ImplementationOutcome, Implementer, TaskBrief, make_implementer
from .implementers.base import read_notes
from .llm import LLMError
from .models import (
    Blocker,
    ClarificationRequest,
    CodingResult,
    CoverageStatus,
    CriterionCoverage,
    Handoff,
    ImplementerInfo,
    LocalTestEvidence,
    Owner,
    RevisionRequest,
    RiskTier,
    RunState,
    Transition,
    utc_now,
)
from .publish import PRRequest, PublishConflict, Publisher, PublishError, PullRequestRef, make_publisher
from .report import TraceLog, collect_artifacts, render_work_report, to_test_agent_request
from .safety import check_scope, env_secret_values, matches, redact, scan_for_secrets
from .workspace import JUnitResults, Workspace, WorkspaceError, evidence_for_hints, make_workspace, run_tests


class RunConflict(Exception):
    """The run_id was already used with different inputs."""


class RunInProgress(Exception):
    pass


class RunNotFound(Exception):
    pass


def _rmtree(path: Path) -> None:
    def onexc(func, target, _exc):
        os.chmod(target, stat.S_IWRITE)
        func(target)

    if path.exists():
        shutil.rmtree(path, onexc=onexc)


class CodingAgent:
    def __init__(self, config: CodingConfig | None = None, implementer: Implementer | None = None,
                 publisher: Publisher | None = None):
        self.config = config or CodingConfig()
        self._implementer = implementer
        self._publisher = publisher

    @property
    def implementer(self) -> Implementer:
        if self._implementer is None:
            self._implementer = make_implementer(self.config)
        return self._implementer

    @property
    def publisher(self) -> Publisher:
        if self._publisher is None:
            self._publisher = make_publisher(self.config)
        return self._publisher

    # ------------------------------------------------------------ run storage

    def run_root(self, run_id: str) -> Path:
        return (Path(self.config.work_dir) / "runs" / run_id).resolve()

    def results(self, run_id: str) -> list[CodingResult]:
        root = self.run_root(run_id)
        found = []
        for path in root.glob("r*/artifacts/coding_result.json"):
            found.append(CodingResult.model_validate_json(path.read_text(encoding="utf-8")))
        return sorted(found, key=lambda r: r.revision)

    def latest(self, run_id: str) -> CodingResult:
        results = self.results(run_id)
        if not results:
            raise RunNotFound(f"no results for run {run_id}")
        return results[-1]

    def cancel(self, run_id: str, reason: str = "cancelled by operator") -> None:
        CancelToken(self.run_root(run_id) / "CANCEL").cancel(reason)

    @contextmanager
    def _lock(self, rev_dir: Path):
        rev_dir.mkdir(parents=True, exist_ok=True)
        lock = rev_dir / ".lock"
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            raise RunInProgress(f"{rev_dir} is already being processed (remove {lock} if that run died)") from exc
        os.close(fd)
        try:
            yield
        finally:
            lock.unlink(missing_ok=True)

    # ------------------------------------------------------------ entry points

    def run(self, handoff_path: Path, spec_path: Path | None = None) -> CodingResult:
        """Process an approved handoff. Re-running the same handoff returns the recorded result."""
        handoff_path = Path(handoff_path).resolve()
        loaded = load_handoff(handoff_path, spec_path, self.config.spec_root)
        handoff = loaded.handoff
        spec_bytes = loaded.spec_path.read_bytes() if loaded.spec_text is not None else None
        input_hash = canonical_hash({"handoff": handoff, "spec": spec_hash(spec_bytes) if spec_bytes else None})
        run_root = self.run_root(handoff.run_id)
        for previous in self.results(handoff.run_id):
            if previous.revision == 0:
                if previous.input_hash != input_hash:
                    raise RunConflict(f"run {handoff.run_id} already exists with a different handoff")
                return previous
        rev_dir = run_root / "r0"
        with self._lock(rev_dir):
            inputs = run_root / "input"
            inputs.mkdir(parents=True, exist_ok=True)
            (inputs / "handoff.json").write_bytes(handoff_path.read_bytes())
            if spec_bytes is not None:
                (inputs / "product_spec.md").write_bytes(spec_bytes)
            job = _Job(self, handoff, loaded.spec_text or "", rev_dir, input_hash, revision=0)
            return job.execute_initial(loaded.issues)

    def revise(self, run_id: str, request: RevisionRequest) -> CodingResult:
        """Address CHANGES_REQUIRED feedback on the run's open PR with a new fast-forward commit."""
        if request.run_id != run_id:
            raise ValueError(f"revision request is for run {request.run_id}, not {run_id}")
        results = self.results(run_id)
        if not results:
            raise RunNotFound(f"no results for run {run_id}")
        input_hash = canonical_hash({"run_id": run_id, "request": request.model_dump(mode="json",
                                                                                     exclude={"timestamp"})})
        for previous in results:
            if previous.revision > 0 and previous.input_hash == input_hash:
                return previous
        run_root = self.run_root(run_id)
        handoff = Handoff.model_validate_json((run_root / "input" / "handoff.json").read_text(encoding="utf-8"))
        spec_file = run_root / "input" / "product_spec.md"
        spec_bytes = spec_file.read_bytes() if spec_file.exists() else None
        revision = results[-1].revision + 1
        rev_dir = run_root / f"r{revision}"
        with self._lock(rev_dir):
            job = _Job(self, handoff, spec_bytes.decode("utf-8") if spec_bytes else "", rev_dir, input_hash,
                       revision=revision, feedback=request, history=results)
            return job.execute_revision(validate_handoff(handoff, spec_bytes))


class _Job:
    """One attempt (initial run or one revision). Accumulates evidence and always finishes with a result."""

    def __init__(self, agent: CodingAgent, handoff: Handoff, spec_text: str, rev_dir: Path, input_hash: str,
                 revision: int, feedback: RevisionRequest | None = None, history: list[CodingResult] = ()):
        self.agent = agent
        self.cfg = agent.config
        self.h = handoff
        self.spec_text = spec_text
        self.rev_dir = rev_dir
        self.input_hash = input_hash
        self.revision = revision
        self.feedback = feedback
        self.history = list(history)
        self.published = next((r for r in reversed(self.history) if r.status == RunState.TESTING), None)
        self.ws_root = rev_dir / "workspace"
        self.artifacts = rev_dir / "artifacts"
        _rmtree(self.ws_root)  # leftovers from a crashed attempt
        _rmtree(self.artifacts)
        self.artifacts.mkdir(parents=True, exist_ok=True)
        self.ledger = SpendLedger(self.cfg.work_dir)
        self.limits = self.ledger.clamp(EffectiveBudget.resolve(self.cfg.policy, handoff.budget_limits),
                                        self.cfg.policy.lifetime_max_usd)
        self.budget = BudgetTracker(self.limits)
        self.cancel = CancelToken(rev_dir.parent / "CANCEL")
        self.secrets = env_secret_values()
        self.trace = TraceLog(self.artifacts / "trace.jsonl", self.secrets)
        self.state: RunState | None = RunState.SPEC_APPROVED if revision == 0 else RunState.CHANGES_REQUIRED
        self.transitions: list[Transition] = []
        self.tests = []
        self.changes = []
        self.coverage: list[CriterionCoverage] = []
        self.blockers: list[Blocker] = []
        self.clarifications: list[ClarificationRequest] = []
        self.unresolved: list[str] = []
        self.impl_info: ImplementerInfo | None = None
        self.summary = ""
        self.claims: dict[str, dict[str, str]] = {}
        self.branch: str | None = self.published.branch if self.published else None
        self.head: str | None = None
        self.pr: PullRequestRef | None = None
        self.trace.event("run", "start", run_id=handoff.run_id, revision=revision, input_hash=input_hash,
                         spec_hash=handoff.spec_hash, source_commit=handoff.source_commit,
                         budget=self.limits.__dict__)

    # ------------------------------------------------------------ bookkeeping

    def transition(self, to: RunState, reason: str = "") -> None:
        self.transitions.append(Transition(from_state=self.state, to_state=to, at=utc_now(), reason=reason))
        self.trace.event("state", "transition", from_state=self.state, to_state=to, reason=reason)
        self.state = to

    def block(self, code: str, message: str, owner: Owner, *, status: RunState = RunState.BLOCKED,
              details: dict | None = None, question: str | None = None) -> CodingResult:
        self.blockers.append(Blocker(code=code, message=message, owner=owner, details=details or {}))
        if question:
            self.clarifications.append(ClarificationRequest(question=question, owner=owner))
        self.trace.event("run", "blocked", code=code, message=message, owner=owner, details=details or {})
        return self.finish(status, owner, reason=f"{code}: {message}")

    def finish(self, status: RunState, owner: Owner, reason: str = "") -> CodingResult:
        self.transition(status, reason or str(status))
        h = self.h
        result_fields = dict(
            run_id=h.run_id, repository=h.repository, issue_id=h.issue_id,
            pr_number=self.pr.number if self.pr else (self.published.pr_number if self.published else None),
            pr_url=self.pr.url if self.pr else (self.published.pr_url if self.published else None),
            source_commit=h.source_commit,
            pr_head_sha=self.head if status == RunState.TESTING else (
                self.published.pr_head_sha if self.published else None),
            previous_head_sha=self.published.pr_head_sha if (self.published and self.revision) else None,
            base_branch=h.base_branch, branch=self.branch, revision=self.revision,
            spec_version=h.spec_version, spec_hash=h.spec_hash, requirement_ids=h.requirement_ids,
            acceptance_criteria=h.acceptance_criteria, status=status, risk_tier=h.risk_tier,
            unresolved_items=self.unresolved, next_owner=owner, budget_used=self.budget.snapshot(),
            input_hash=self.input_hash, changed_files=self.changes, criteria_coverage=self.coverage,
            tests=self.tests, blockers=self.blockers, clarification_requests=self.clarifications,
            implementer=self.impl_info, requires_human_review=h.risk_tier == RiskTier.HIGH,
            transitions=self.transitions,
        )
        if status == RunState.TESTING:
            persisted_spec = self.rev_dir.parent / "input" / "product_spec.md"
            spec_uri = persisted_spec.as_uri() if persisted_spec.exists() else h.spec_uri
            result_fields["test_agent_request"] = to_test_agent_request(
                CodingResult.model_construct(**result_fields), spec_uri, h.environment.test_profile_id,
                h.environment.environment_manifest_id)
        result = CodingResult(**result_fields)
        self.trace.event("run", "finish", status=status, next_owner=owner, budget=result.budget_used)
        (self.artifacts / "work_report.md").write_text(
            redact(render_work_report(result, h.issue_title, self.summary), self.secrets), encoding="utf-8")
        result.artifacts = collect_artifacts(self.artifacts, exclude={"coding_result.json"})
        result.evidence_refs = [a.uri for a in result.artifacts
                                if a.name.startswith(("junit-", "tests-")) or a.name == "diff.patch"]
        result = CodingResult.model_validate(result.model_dump())
        (self.artifacts / "coding_result.json").write_text(result.model_dump_json(indent=2), encoding="utf-8")
        self.ledger.record(at=utc_now(), run_id=h.run_id, revision=self.revision, status=status,
                           implementer=self.impl_info.name if self.impl_info else None,
                           model=self.impl_info.model if self.impl_info else None,
                           **result.budget_used.model_dump(exclude={"seconds", "steps"}))
        if not self.cfg.policy.keep_workspaces:
            _rmtree(self.ws_root)
        return result

    # ------------------------------------------------------------ shared phases

    def _guarded(self, body) -> CodingResult:
        try:
            self.cancel.raise_if_cancelled()
            lifetime = self.cfg.policy.lifetime_max_usd
            if lifetime is not None and self.limits.max_usd <= 0:
                return self.block("LIFETIME_BUDGET_EXHAUSTED",
                                  f"lifetime spend cap ${lifetime:.2f} reached ({self.ledger.path})", Owner.MAINTAINER)
            return body()
        except Cancelled:
            return self.finish(RunState.CANCELLED, Owner.MAINTAINER, reason="cancelled by operator")
        except BudgetExceeded as exc:
            return self.block("BUDGET_EXCEEDED", str(exc), Owner.MAINTAINER, details={"kind": exc.kind})
        except PublishConflict as exc:
            return self.block("BRANCH_MOVED", str(exc), Owner.ORCHESTRATOR, status=RunState.STALE)
        except PublishError as exc:
            return self.block("PUBLISH_FAILED", str(exc), Owner.MAINTAINER)
        except LLMError as exc:
            return self.block("MODEL_UNAVAILABLE", str(exc), Owner.MAINTAINER)
        except WorkspaceError as exc:
            return self.block("SANDBOX_UNAVAILABLE", str(exc), Owner.ENVIRONMENT_OWNER)
        except Exception as exc:  # noqa: BLE001 - every failure must still produce a result
            self.trace.event("run", "internal_error", error=repr(exc), traceback=traceback.format_exc())
            return self.block("INTERNAL_ERROR", f"{type(exc).__name__}: {exc}", Owner.MAINTAINER,
                              status=RunState.INCONCLUSIVE)

    def _timeout(self) -> float:
        self.budget.check()
        return max(1.0, min(self.cfg.workspace.command_timeout_seconds, self.budget.remaining_seconds))

    def _branch_name(self) -> str:
        slug = re.sub(r"[^A-Za-z0-9_-]+", "-", self.h.run_id).strip("-")[:40]
        return f"{self.cfg.policy.branch_prefix}issue-{self.h.issue_id}-{slug}"

    def _is_test(self, path: str) -> bool:
        return matches(path, self.h.scope.test_paths)

    def preflight(self, ws: Workspace) -> CodingResult | None:
        env = self.h.environment
        missing = []
        for prereq in env.prerequisites:
            result = ws.run(prereq.check_command, timeout=min(120.0, self._timeout()))
            self.trace.event("preflight", "prerequisite", name=prereq.name, returncode=result.returncode)
            if result.returncode != 0:
                missing.append(prereq)
        if missing:
            return self.block(
                "MISSING_PREREQUISITE", "missing environment prerequisites: " + ", ".join(p.name for p in missing),
                missing[0].owner,
                details={"prerequisites": [{"name": p.name, "check": p.check_command, "remediation": p.remediation}
                                           for p in missing]})
        setup_log = []
        for command in env.setup_commands:
            result = ws.run(command, timeout=self._timeout())
            setup_log.append(f"$ {command}\n{result.output}\n[exit {result.returncode}]\n")
            self.trace.event("preflight", "setup", command=command, returncode=result.returncode)
            if result.returncode != 0:
                (self.artifacts / "setup.log").write_text(redact("".join(setup_log), self.secrets), encoding="utf-8")
                return self.block("SETUP_FAILED", f"setup command failed: {command}", Owner.ENVIRONMENT_OWNER,
                                  details={"command": command, "returncode": result.returncode})
        if setup_log:
            (self.artifacts / "setup.log").write_text(redact("".join(setup_log), self.secrets), encoding="utf-8")
        return None

    def implement(self, ws: Workspace) -> ImplementationOutcome:
        brief = TaskBrief(
            run_id=self.h.run_id, repository=self.h.repository, issue_id=self.h.issue_id,
            issue_title=self.h.issue_title, spec_text=self.spec_text, requirements=self.h.requirements,
            criteria=self.h.acceptance_criteria, scope=self.h.scope, test_command=self.h.environment.test_command,
            revision=self.revision, feedback=self.feedback,
        )
        impl = self.agent.implementer
        self.impl_info = ImplementerInfo(name=impl.name, version=impl.version, model=impl.model,
                                         prompt_version=impl.prompt_version, exit_status="Started")
        self.trace.event("implement", "start", implementer=impl.name, model=impl.model)
        outcome = impl.implement(brief, ws, self.budget, self.cancel)
        self.impl_info.exit_status = outcome.exit_status
        self.impl_info.steps = outcome.steps
        summary, claims = (outcome.summary, outcome.claims) if outcome.claims else read_notes(ws.root)
        self.summary = outcome.summary or summary
        self.claims = claims
        (self.artifacts / "transcript.json").write_text(
            redact(json.dumps(outcome.transcript, indent=2, default=str), self.secrets), encoding="utf-8")
        self.trace.event("implement", "finish", exit_status=outcome.exit_status, steps=outcome.steps,
                         error=outcome.error, budget=self.budget.snapshot())
        return outcome

    def post_checks(self, outcome: ImplementationOutcome, revision_base: str) -> CodingResult | None:
        root = self.ws_root
        gitops.stage_changes(root, revision_base, self.branch)
        self.changes = gitops.staged_changes(root, self.h.source_commit, self._is_test)
        (self.artifacts / "diff.patch").write_text(redact(gitops.staged_diff(root, self.h.source_commit),
                                                          self.secrets), encoding="utf-8")
        self.trace.event("checks", "changes", files=[c.model_dump() for c in self.changes])
        if not outcome.submitted:
            return self.block("IMPLEMENTER_DID_NOT_FINISH",
                              f"implementer stopped with {outcome.exit_status}: {outcome.error or ''}".strip(),
                              Owner.MAINTAINER)
        if not gitops.staged_changes(root, revision_base):
            return self.block("NO_CHANGES", "the implementer finished without changing any files",
                              Owner.MAINTAINER)
        violations = check_scope(self.changes, self.h.scope, self.cfg.policy.extra_forbidden_paths,
                                 gitops.staged_symlinks(root))
        violations += scan_for_secrets(gitops.staged_added_lines(root, self.h.source_commit))
        if violations:
            return self.block(
                "SCOPE_VIOLATION", "; ".join(v.message for v in violations), Owner.MAINTAINER,
                details={"violations": [{"code": v.code, "path": v.path, "message": v.message} for v in violations]})
        return None

    def commit_and_verify(self, ws: Workspace) -> CodingResult | None:
        h = self.h
        trailer = (f"Evo-Run-Id: {h.run_id}\nEvo-Revision: {self.revision}\nEvo-Spec-Hash: {h.spec_hash}\n"
                   f"Evo-Criteria: {', '.join(c.id for c in h.acceptance_criteria)}")
        title = (f"Address review feedback for #{h.issue_id} (revision {self.revision})" if self.revision
                 else f"{h.issue_title} (#{h.issue_id})")
        body = redact(self.summary.strip(), self.secrets)
        message = f"{title}\n\n{body}\n\n{trailer}" if body else f"{title}\n\n{trailer}"
        self.head = gitops.commit(self.ws_root, message, self.cfg.policy.commit_author_name,
                                  self.cfg.policy.commit_author_email)
        self.trace.event("commit", "created", sha=self.head)
        final, junit = run_tests(ws, h.environment, "final", self.artifacts, self._timeout(), commit=self.head,
                                 secrets=self.secrets)
        self.tests.append(final)
        self.coverage = self._coverage(junit)
        self.unresolved = [f"{c.criterion_id}: {c.status.value}" for c in self.coverage
                           if c.status != CoverageStatus.IMPLEMENTED]
        self.trace.event("tests", "final", summary=final.model_dump())
        if not final.succeeded:
            return self.block(
                "TESTS_FAILING", f"tests fail on the implementation commit {self.head[:12]}"
                + (" (timed out)" if final.timed_out else ""), Owner.MAINTAINER,
                details={"failing_tests": final.failing_tests, "commit": self.head})
        return None

    def _coverage(self, junit: JUnitResults | None) -> list[CriterionCoverage]:
        coverage = []
        for criterion in self.h.acceptance_criteria:
            claim = self.claims.get(criterion.id, {})
            claimed = claim.get("status") or None
            evidence, ids = evidence_for_hints(junit, criterion.test_hints)
            if evidence == LocalTestEvidence.FAIL:
                status = CoverageStatus.FAILING
            elif claimed == "IMPLEMENTED" and evidence in (LocalTestEvidence.PASS, LocalTestEvidence.NO_HINTS):
                status = CoverageStatus.IMPLEMENTED
            elif claimed is None and evidence == LocalTestEvidence.PASS:
                status = CoverageStatus.IMPLEMENTED
            elif claimed in ("IMPLEMENTED", "PARTIAL"):
                status = CoverageStatus.PARTIAL
            else:
                status = CoverageStatus.NOT_ADDRESSED
            coverage.append(CriterionCoverage(criterion_id=criterion.id, status=status, implementer_claim=claimed,
                                              implementer_notes=claim.get("notes", ""), local_test_ids=ids,
                                              local_test_evidence=evidence))
        return coverage

    def _pr_body(self) -> str:
        h = self.h
        rows = "\n".join(f"| {c.criterion_id} | {c.status.value} | {c.local_test_evidence.value} |"
                         for c in self.coverage)
        return redact(
            f"Automated implementation of #{h.issue_id} by the EVO Coding Agent.\n\n"
            f"Refs #{h.issue_id}\n\n"
            f"{self.summary.strip()}\n\n"
            "| Criterion | Coding status | Local tests |\n|---|---|---|\n" + rows + "\n\n"
            f"- Run: `{h.run_id}`\n- Approved spec: v{h.spec_version} `{h.spec_hash}`\n"
            f"- Source commit: `{h.source_commit}`\n- Risk tier: {h.risk_tier}\n\n"
            "This draft PR still needs Test Agent verification, QA review and a human merge decision. "
            "Agents cannot merge it.", self.secrets)

    def publish(self, expected_remote: str | None) -> None:
        publisher = self.agent.publisher
        self.cancel.raise_if_cancelled()
        publisher.push(self.ws_root, self.h.repository, self.branch, expected_remote)
        self.trace.event("publish", "pushed", branch=self.branch, sha=self.head)
        self.pr = publisher.open_or_get_pr(PRRequest(
            repository=self.h.repository, branch=self.branch, base=self.h.base_branch,
            title=f"[EVO] {self.h.issue_title} (#{self.h.issue_id})", body=self._pr_body(),
            issue_id=self.h.issue_id, labels=list(self.cfg.publisher.labels)))
        self.trace.event("publish", "pull_request", number=self.pr.number, url=self.pr.url, created=self.pr.created)
        if self.revision:
            publisher.comment(self.h.repository, self.pr.number,
                              redact(f"EVO Coding Agent pushed revision {self.revision} (`{self.head}`) in response "
                                     f"to {self.feedback.source} feedback on `{self.feedback.tested_head_sha[:12]}`."
                                     f"\n\n{self.summary.strip()}", self.secrets), on="pull")
        elif self.pr.created and self.cfg.publisher.comment_on_issue:
            publisher.comment(self.h.repository, self.h.issue_id,
                              f"EVO Coding Agent opened draft PR #{self.pr.number} for this issue "
                              f"(run `{self.h.run_id}`).", on="issue")

    # ------------------------------------------------------------ initial run

    def execute_initial(self, issues: list[HandoffIssue]) -> CodingResult:
        self.transition(RunState.CODING, "approved handoff received")
        return self._guarded(lambda: self._initial(issues))

    def _initial(self, issues: list[HandoffIssue]) -> CodingResult:
        h = self.h
        if issues:
            for issue in issues:
                self.blockers.append(Blocker(code=issue.code, message=issue.message, owner=issue.owner,
                                             details={"ref": issue.ref} if issue.ref else {}))
                self.clarifications.append(ClarificationRequest(question=issue.message, ref=issue.ref,
                                                                owner=issue.owner))
            self.trace.event("intake", "rejected", issues=[i.code for i in issues])
            return self.finish(RunState.BLOCKED, issues[0].owner, reason="handoff failed intake checks")
        publisher = self.agent.publisher
        self.secrets += publisher.secrets()
        self.trace.secrets = self.secrets
        self.branch = self._branch_name()
        source = self.cfg.repo_url or publisher.git_url(h.repository)
        gitops.clone(source, self.ws_root, env=None if self.cfg.repo_url else publisher.git_env())
        if not gitops.commit_exists(self.ws_root, h.source_commit):
            return self.block("SOURCE_COMMIT_MISSING", f"source commit {h.source_commit} is not in the repository",
                              Owner.SPEC_AGENT, question="Re-run the spec phase against an existing commit.")
        existing = publisher.remote_branch_sha(h.repository, self.branch)
        if existing:
            return self.block("BRANCH_EXISTS", f"remote branch {self.branch} already exists at {existing}",
                              Owner.ORCHESTRATOR, status=RunState.STALE)
        gitops.checkout(self.ws_root, h.source_commit, self.branch)
        snapshot = gitops.GitSnapshot(self.ws_root)
        with make_workspace(self.ws_root, self.cfg.workspace) as ws:
            if (blocked := self.preflight(ws)) is not None:
                return blocked
            baseline, _ = run_tests(ws, h.environment, "baseline", self.artifacts, self._timeout(),
                                    commit=h.source_commit, secrets=self.secrets)
            self.tests.append(baseline)
            self.trace.event("preflight", "baseline", summary=baseline.model_dump())
            if not baseline.succeeded and h.environment.baseline_must_pass:
                return self.block(
                    "BASELINE_TESTS_FAILING",
                    "tests already fail on the source commit, so a regression could not be told apart",
                    Owner.MAINTAINER, details={"failing_tests": baseline.failing_tests})
            ws.enter_isolation()
            outcome = self.implement(ws)
            snapshot.restore()
            if (blocked := self.post_checks(outcome, h.source_commit)) is not None:
                return blocked
            if (blocked := self.commit_and_verify(ws)) is not None:
                return blocked
        self.publish(expected_remote=None)
        return self.finish(RunState.TESTING, Owner.TEST_AGENT, reason=f"draft PR #{self.pr.number} opened")

    # ------------------------------------------------------------ revision

    def execute_revision(self, issues: list[HandoffIssue]) -> CodingResult:
        self.transition(RunState.CODING, f"revision {self.revision} requested by {self.feedback.source}")
        return self._guarded(lambda: self._revision(issues))

    def _revision(self, issues: list[HandoffIssue]) -> CodingResult:
        h = self.h
        if issues:
            return self.block("HANDOFF_NO_LONGER_VALID", "; ".join(i.message for i in issues), Owner.SPEC_REVIEWER)
        if self.published is None:
            return self.block("NOTHING_TO_REVISE", "this run has no published pull request to revise",
                              Owner.ORCHESTRATOR)
        if self.feedback.tested_head_sha != self.published.pr_head_sha:
            return self.block("STALE_FEEDBACK",
                              f"feedback was for {self.feedback.tested_head_sha[:12]} but the PR head is "
                              f"{self.published.pr_head_sha[:12]}", Owner.ORCHESTRATOR, status=RunState.STALE)
        attempts = sum(1 for r in self.history if r.revision > 0 and r.implementer is not None)
        if attempts >= self.limits.max_revisions:
            return self.block("REVISION_LIMIT_REACHED",
                              f"{attempts} automated revision(s) already attempted (limit "
                              f"{self.limits.max_revisions}); a human needs to take over", Owner.HUMAN_REVIEWER)
        publisher = self.agent.publisher
        self.secrets += publisher.secrets()
        self.trace.secrets = self.secrets
        remote = publisher.remote_branch_sha(h.repository, self.branch)
        if remote != self.published.pr_head_sha:
            return self.block("BRANCH_MOVED", f"PR branch is at {remote}, expected {self.published.pr_head_sha}",
                              Owner.ORCHESTRATOR, status=RunState.STALE)
        source = self.cfg.repo_url or publisher.git_url(h.repository)
        gitops.clone(source, self.ws_root, env=None if self.cfg.repo_url else publisher.git_env())
        if not gitops.commit_exists(self.ws_root, self.published.pr_head_sha):
            publisher.fetch(self.ws_root, h.repository, f"refs/heads/{self.branch}")
        gitops.checkout(self.ws_root, self.published.pr_head_sha, self.branch)
        snapshot = gitops.GitSnapshot(self.ws_root)
        with make_workspace(self.ws_root, self.cfg.workspace) as ws:
            if (blocked := self.preflight(ws)) is not None:
                return blocked
            before, _ = run_tests(ws, h.environment, "before", self.artifacts, self._timeout(),
                                  commit=self.published.pr_head_sha, secrets=self.secrets)
            self.tests.append(before)
            ws.enter_isolation()
            outcome = self.implement(ws)
            snapshot.restore()
            if (blocked := self.post_checks(outcome, self.published.pr_head_sha)) is not None:
                return blocked
            if (blocked := self.commit_and_verify(ws)) is not None:
                return blocked
        self.publish(expected_remote=self.published.pr_head_sha)
        return self.finish(RunState.TESTING, Owner.TEST_AGENT,
                           reason=f"revision {self.revision} pushed to PR #{self.pr.number}")
