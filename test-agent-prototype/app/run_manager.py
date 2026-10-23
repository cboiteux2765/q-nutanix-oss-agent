"""Single-process orchestration with immutable input identities and bounded work."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
import difflib
from hashlib import sha256
import inspect
import json
import math
from pathlib import Path
import subprocess
import time
from typing import Any
from uuid import uuid4

from .models import (CreateRunRequest, ExecutionStatus as Status, Readiness,
    RequirementOutcome, ResolvedContext, Principal, Verification, ReviewRequest,
    FeedbackRequest, QAReview, RootCause, FailureClass, canonical_hash,
    idempotency_key as resolve_idempotency)
from .providers import SourceRegistry, git, tree_hash
from .preflight import preflight, prepare_workspace
from .test_runner import execute as run_pytest, requirement_results, redact_log
from .rca_and_repair import (classify_failure, FixtureRepairAdapter, PatchPolicyError, validate_patch,
    materialize_candidate, NullPMNotificationAdapter)
from .evolution_memory import EvolutionMemory

TERMINAL = {Status.COMPLETED, Status.BLOCKED, Status.CANCELLED, Status.ERROR}
ORDER = [Status.QUEUED, Status.PREFLIGHT, Status.TESTING, Status.DIAGNOSING, Status.REPAIRING]


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


class BudgetExceeded(RuntimeError):
    pass


@dataclass
class BudgetController:
    request: CreateRunRequest
    started: float = field(default_factory=time.monotonic)
    test_executions: int = 0
    debug_iterations: int = 0
    model_spend_usd: float = 0.0
    reserved_usd: float = 0.0
    stopped_at: float | None = None

    @property
    def elapsed(self) -> float:
        return (self.stopped_at if self.stopped_at is not None else time.monotonic()) - self.started

    @property
    def remaining(self) -> float:
        return max(0.0, self.request.max_runtime_seconds - self.elapsed)

    def check(self) -> None:
        if self.remaining <= 0:
            raise BudgetExceeded("Active runtime budget exhausted")

    def consume_test(self) -> None:
        self.check()
        if self.test_executions >= self.request.max_test_executions:
            raise BudgetExceeded("Test invocation budget exhausted")
        self.test_executions += 1

    def reserve_patch(self, quote: float) -> None:
        self.check()
        if self.debug_iterations >= self.request.max_debug_iterations:
            raise BudgetExceeded("Patch attempt budget exhausted")
        if self.test_executions >= self.request.max_test_executions:
            raise BudgetExceeded("No validation invocation remains; do not propose a patch")
        if not math.isfinite(quote) or quote < 0:
            raise ValueError("Adapter must quote a finite nonnegative upper cost bound")
        if self.model_spend_usd + self.reserved_usd + quote > self.request.max_llm_cost_usd:
            raise BudgetExceeded("Model spend reservation exceeds the cap")
        self.reserved_usd += quote
        self.debug_iterations += 1

    def settle(self) -> None:
        # Charge the complete reservation conservatively, including failed calls.
        self.model_spend_usd += self.reserved_usd
        self.reserved_usd = 0.0


@dataclass
class RunState:
    request: CreateRunRequest
    context: ResolvedContext
    result: Verification
    actor: str
    created: float = field(default_factory=time.monotonic)
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    budget: BudgetController | None = None
    executions: list[dict[str, Any]] = field(default_factory=list)
    phase_started: float = field(default_factory=time.monotonic)


class RunManager:
    def __init__(self, storage: Path, registry: SourceRegistry | None = None,
                 repair_adapter: Any = None, notifier: Any = None):
        self.storage = Path(storage).resolve()
        self.storage.mkdir(parents=True, exist_ok=True)
        self.registry = registry or SourceRegistry(self.storage / "sources")
        self.repair_adapter = repair_adapter or FixtureRepairAdapter()
        self.notifier = notifier or NullPMNotificationAdapter()
        self.memory = EvolutionMemory(self.storage / "evolution")
        self.runs: dict[str, RunState] = {}
        self.idempotency: dict[str, dict[str, str]] = {}
        self.delivery_runs: dict[str, str] = {}
        self._executing: set[str] = set()
        self._load()

    @staticmethod
    def authorize(principal: Principal, repo: str, role: str | None = None) -> None:
        if repo not in principal.repos and "*" not in principal.repos:
            raise PermissionError("Repository access denied")
        if role and role not in principal.roles:
            raise PermissionError(f"{role} role required")

    def _load(self) -> None:
        path = self.storage / "state.json"
        if not path.exists():
            return
        data = json.loads(path.read_text(encoding="utf-8"))
        self.idempotency = data["idempotency"]
        self.delivery_runs = data["delivery_runs"]
        interrupted = []
        for key, item in data["runs"].items():
            result = Verification.model_validate(item["result"])
            state = RunState(request=CreateRunRequest.model_validate(item["request"]),
                context=ResolvedContext.model_validate(item["context"]), result=result,
                actor=item["actor"], executions=item.get("executions", []))
            self.runs[key] = state
            if result.execution_status not in TERMINAL:
                result.execution_status = Status.ERROR
                result.readiness = Readiness.INCONCLUSIVE
                result.reasons.append("Service restarted during incomplete work; explicit retry required")
                interrupted.append(state)
        for state in interrupted:
            self._publish(state)

    def persist(self) -> None:
        write_json(self.storage / "state.json", dict(
            idempotency=self.idempotency, delivery_runs=self.delivery_runs,
            runs={key: dict(request=v.request.model_dump(mode="json"),
                context=v.context.model_dump(mode="json"), result=v.result.model_dump(mode="json"),
                actor=v.actor, executions=v.executions) for key, v in self.runs.items()}))

    def create_run(self, request: CreateRunRequest, principal: Principal,
                   trigger_identity: str = "manual", idempotency_key: str | None = None):
        self.authorize(principal, request.repo, "engineer")
        # Copy via validation; callers cannot mutate a live run's input contract.
        request = CreateRunRequest.model_validate(request.model_dump(mode="json"))
        context = self.registry.resolve(request)
        trigger = f"{trigger_identity}:parent:{request.parent_run_id or 'none'}"
        identity = resolve_idempotency(context, trigger)
        key = canonical_hash([principal.name, request.repo, idempotency_key]) if idempotency_key else identity
        payload_hash = canonical_hash({"identity": identity, "request": request.model_dump(mode="json")})
        if key in self.idempotency:
            previous = self.idempotency[key]
            if previous["payload_hash"] != payload_hash:
                raise ValueError("Idempotency key conflicts with a different input, budget, or version")
            return self.get_run(previous["run_id"]), True
        if request.parent_run_id:
            parent = self.get_run(request.parent_run_id)
            if parent.repo != request.repo or parent.pr_number != request.pr_number:
                raise ValueError("Retry parent must belong to the same PR")
        run_id = "run-" + uuid4().hex
        result = Verification(run_id=run_id, repo=context.repo, pr_number=context.pr_number,
            base_sha=context.base_sha, reviewed_head_sha=context.head_sha,
            spec_id=context.spec.spec_id, spec_hash=context.spec_hash,
            test_profile_hash=context.profile.fingerprint,
            environment_fingerprint=context.environment.fingerprint,
            policy_version=context.policy_version, identity_hash=context.identity_hash,
            artifact_manifest_uri=f"/v1/test-runs/{run_id}/artifacts/evidence_manifest.json")
        result.lifecycle.append({"state": "QUEUED", "at": now(), "note": "Input versions pinned"})
        state = RunState(request=request, context=context, result=result, actor=principal.name)
        self.runs[run_id] = state
        self.idempotency[key] = dict(run_id=run_id, payload_hash=payload_hash)
        self._publish(state)
        return self._copy(result), False

    @staticmethod
    def _copy(result: Verification) -> Verification:
        return Verification.model_validate(result.model_dump(mode="json"))

    def _transition(self, state: RunState, target: Status, note: str = "") -> None:
        current = state.result.execution_status
        if current in TERMINAL:
            raise ValueError(f"Terminal lifecycle cannot transition: {current} -> {target}")
        if target not in TERMINAL and ORDER.index(target) != ORDER.index(current) + 1:
            raise ValueError(f"Invalid lifecycle transition: {current} -> {target}")
        state.result.metrics.phase_elapsed_seconds[current.value] = round(time.monotonic() - state.phase_started, 5)
        state.phase_started = time.monotonic()
        state.result.execution_status = target
        state.result.lifecycle.append({"state": target.value, "at": now(), "note": note})
        self.persist()

    def _fresh(self, state: RunState) -> bool:
        try:
            request = self.registry.request_for_pr(state.context.repo, state.context.pr_number)
            return self.registry.resolve(request).identity_hash == state.context.identity_hash
        except (KeyError, ValueError, OSError, subprocess.SubprocessError):
            return False

    def invalidate_stale(self, repo: str, pr_number: int) -> list[str]:
        changed = []
        for state in self.runs.values():
            result = state.result
            if result.repo != repo or result.pr_number != pr_number or result.readiness == Readiness.STALE:
                continue
            if not self._fresh(state):
                state.cancel_event.set()
                if result.execution_status not in TERMINAL:
                    self._transition(state, Status.CANCELLED, "Input identity changed")
                result.readiness = Readiness.STALE
                result.reasons.append("Relevant input version changed; prior evidence is retained for audit")
                self._archive_envelope(state, "before-stale")
                self._publish(state)
                changed.append(result.run_id)
        if changed:
            self.persist()
        return changed

    def get_run(self, run_id: str) -> Verification:
        state = self.runs[run_id]
        self.invalidate_stale(state.context.repo, state.context.pr_number)
        return self._copy(state.result)

    def _directory(self, state: RunState) -> Path:
        path = self.storage / "runs" / state.result.run_id / "artifacts"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _save_verification(self, state: RunState) -> None:
        # Enforce schema, including the READY_FOR_QA invariant, before every write.
        value = Verification.model_validate(state.result.model_dump(mode="json"))
        write_json(self._directory(state) / "verification.json", value.model_dump(mode="json"))

    def _archive_envelope(self, state: RunState, label: str) -> None:
        directory = self._directory(state)
        for name in ("verification.json", "test_report.md", "evidence_manifest.json"):
            path = directory / name
            if path.is_file() and not path.is_symlink():
                archive = directory / "audit" / label / name
                archive.parent.mkdir(parents=True, exist_ok=True)
                archive.write_bytes(path.read_bytes())

    def _publish(self, state: RunState) -> None:
        result, directory = state.result, self._directory(state)
        if state.budget:
            budget = state.budget
            result.metrics.debug_iterations = budget.debug_iterations
            result.metrics.test_executions = budget.test_executions
            result.metrics.runtime_seconds = round(budget.elapsed, 5)
            result.metrics.model_spend_usd = budget.model_spend_usd
            result.metrics.model_reserved_usd = budget.reserved_usd
        rows = ["| Requirement | Tests | Result | Evidence |", "| --- | --- | --- | --- |"]
        rows += [f"| {r.id} | {', '.join(r.test_ids)} | {r.result.value} | {', '.join(r.evidence_ids)} |" for r in result.requirements]
        report = [f"# Verification {result.run_id}", "", f"Customer intent: {state.context.spec.intent}",
            f"Scope: {result.repo} PR {result.pr_number}; actual head {result.reviewed_head_sha}.",
            f"Spec: {result.spec_id} / {result.spec_hash}",
            f"Lifecycle: {result.execution_status.value}; readiness: {result.readiness.value}.",
            "Human QA approval is required before the team's normal release process.",
            "", "## Requirement coverage", *rows, "", "## Preflight and remaining risks", *result.reasons,
            "", "## Observed original PR test totals", json.dumps(result.tests.model_dump()),
            "", "## Executed commands and evidence"]
        report += [json.dumps(item, ensure_ascii=False) for item in state.executions]
        report += ["", "## Root cause analysis and next owner"]
        report += [json.dumps(r.model_dump(mode="json"), ensure_ascii=False) for r in result.unresolved_findings]
        if result.candidate_patch:
            report += ["", "## Candidate fix for review", json.dumps(result.candidate_patch, ensure_ascii=False)]
        report += ["", "## Time and cost", json.dumps(result.metrics.model_dump()), "", "## Next action",
                   "Human QA review of this exact version." if result.readiness == Readiness.READY_FOR_QA else
                   "NEEDS_ENGINEER or the named blocker owner. No merge/deploy is performed."]
        (directory / "test_report.md").write_text(redact_log("\n".join(report) + "\n"), encoding="utf-8")
        entries = []
        for path in sorted(directory.rglob("*")):
            if not path.is_file() or (path.parent == directory and path.name in {"verification.json", "evidence_manifest.json"}) or path.suffix == ".tmp":
                continue
            name = path.relative_to(directory).as_posix()
            entries.append(dict(id=name, path=name, sha256=sha256(path.read_bytes()).hexdigest(),
                size=path.stat().st_size, uri=f"/v1/test-runs/{result.run_id}/artifacts/{name}"))
        manifest = dict(schema_version="1.0", run_id=result.run_id, identity=state.context.identity,
            identity_hash=state.context.identity_hash, evidence=entries, executions=state.executions,
            workflow_id=f"local:{result.run_id}", job_id="local-worker",
            reproduction="Use the registered immutable snapshot and test profile with the recorded commands.",
            retention="Local operator-managed storage; no external upload.",
            integrity_note="Checksums detect modification; this prototype does not implement signed attestation. Mutable verification/review envelopes are outside this manifest to avoid circular hashing.")
        write_json(directory / "evidence_manifest.json", manifest)
        result.artifact_manifest_hash = sha256((directory / "evidence_manifest.json").read_bytes()).hexdigest()
        self._save_verification(state)
        self.persist()

    async def _test(self, state: RunState, snapshot: Path, label: str):
        budget = state.budget
        if state.cancel_event.is_set():
            raise asyncio.CancelledError
        budget.consume_test()
        work = self.storage / "runs" / state.result.run_id / "work" / label
        prepare_workspace(snapshot, work)
        before = tree_hash(work)
        evidence_dir = self._directory(state) / label
        result = await run_pytest(state.context, work, evidence_dir, state.cancel_event, budget.remaining)
        if before != tree_hash(work):
            result.trusted = False
            result.blocked_reason = "Execution modified the pinned source or test tree"
        state.executions.append(dict(label=label, command=result.command, exit_code=result.exit_code,
            passed=result.passed, failed=result.failed, skipped=result.skipped,
            trusted=result.trusted, timed_out=result.timed_out, cancelled=result.cancelled,
            duration_seconds=result.duration_seconds, evidence_ids=[f"{label}/junit.xml", f"{label}/pytest.log"]))
        self._publish(state)
        return result

    async def execute(self, run_id: str) -> None:
        state = self.runs[run_id]
        if run_id in self._executing or state.result.execution_status != Status.QUEUED:
            return
        self._executing.add(run_id)
        result = state.result
        state.budget = BudgetController(state.request)
        result.metrics.queue_seconds = time.monotonic() - state.created
        try:
            if not self._fresh(state):
                self.invalidate_stale(result.repo, result.pr_number)
                return
            self._transition(state, Status.PREFLIGHT)
            checks = preflight(state.context)
            if not checks.ok:
                result.requirements = requirement_results(state.context.spec, [])
                for req in result.requirements:
                    req.result = RequirementOutcome.BLOCKED
                for finding in checks.findings:
                    failure = finding.code if finding.code in FailureClass._value2member_map_ else "UNKNOWN"
                    root = RootCause(failure_class=failure, summary=finding.message,
                        observations=[finding.message], owner_role=finding.owner_role,
                        next_action=finding.remediation, confidence=1.0)
                    result.unresolved_findings.append(root)
                    result.reasons.append(f"{finding.code}: {finding.message}; {finding.remediation}")
                self._transition(state, Status.BLOCKED)
                result.readiness = Readiness.BLOCKED
                return
            self._transition(state, Status.TESTING)
            head = await self._test(state, state.context.snapshot_root, "initial")
            result.tests.passed, result.tests.failed, result.tests.skipped = head.passed, head.failed, head.skipped
            result.requirements = requirement_results(state.context.spec, [head])
            if head.cancelled or state.cancel_event.is_set():
                raise asyncio.CancelledError
            if head.timed_out:
                raise BudgetExceeded("Test execution timeout; results are incomplete")
            self._transition(state, Status.DIAGNOSING)
            all_pass = (head.trusted and not head.blocked_reason and head.exit_code == 0 and head.passed > 0
                        and result.requirements and all(r.result == RequirementOutcome.PASS for r in result.requirements))
            if all_pass:
                self._transition(state, Status.REPAIRING, "Skipped: no failing code to repair")
                self._transition(state, Status.COMPLETED)
                if self._fresh(state):
                    result.readiness = Readiness.READY_FOR_QA
                else:
                    result.readiness = Readiness.STALE
                    result.reasons.append("Input changed during execution; fresh run required")
                return
            if head.exit_code == 0 and head.trusted and not head.blocked_reason:
                result.reasons.append("Required acceptance evidence is absent or skipped; add tests or reviewed evidence")
                self._transition(state, Status.REPAIRING, "Skipped: no safely diagnosed regression")
                self._transition(state, Status.COMPLETED)
                result.readiness = Readiness.CHANGES_REQUIRED
                return
            if head.blocked_reason or not head.trusted or head.exit_code not in {0, 1}:
                result.reasons.append(head.blocked_reason or "Untrusted or incomplete test evidence")
                self._transition(state, Status.REPAIRING, "Skipped: inconclusive evidence")
                self._transition(state, Status.COMPLETED)
                result.readiness = Readiness.INCONCLUSIVE
                return
            baseline = await self._test(state, state.context.base_snapshot_root, "base")
            root = classify_failure(state.context, head, baseline)
            if root.failure_class == FailureClass.UNKNOWN and state.budget.test_executions < state.request.max_test_executions:
                # A single same-head rerun is diagnostic only; it never erases failure.
                rerun = await self._test(state, state.context.snapshot_root, "diagnostic-rerun")
                root = classify_failure(state.context, head, baseline, rerun)
            result.unresolved_findings.append(root)
            self._transition(state, Status.REPAIRING)
            if root.failure_class == FailureClass.CODE_REGRESSION:
                await self._repair(state, head, root)
                result.readiness = Readiness.CHANGES_REQUIRED
            else:
                result.readiness = Readiness.INCONCLUSIVE
                result.reasons.append(root.next_action)
            self._transition(state, Status.COMPLETED)
        except asyncio.CancelledError:
            if result.execution_status not in TERMINAL:
                self._transition(state, Status.CANCELLED, "Cancellation requested")
            if result.readiness != Readiness.STALE:
                result.readiness = Readiness.INCONCLUSIVE
            result.reasons.append("Cancelled; partial evidence cannot establish a pass")
        except BudgetExceeded as error:
            if result.execution_status not in TERMINAL:
                self._transition(state, Status.COMPLETED, "Stopped by budget controller")
            result.readiness = Readiness.INCONCLUSIVE
            result.reasons.append(f"NEEDS_ENGINEER: {error}")
        except Exception as error:
            if result.execution_status not in TERMINAL:
                self._transition(state, Status.ERROR)
            result.readiness = Readiness.INCONCLUSIVE
            result.reasons.append(redact_log(f"Service error: {type(error).__name__}: {error}"))
        finally:
            state.budget.stopped_at = time.monotonic()
            if not self._fresh(state):
                result.readiness = Readiness.STALE
                if "Input changed; fresh verification is required" not in result.reasons:
                    result.reasons.append("Input changed; fresh verification is required")
            self._publish(state)
            try:
                self.memory.append_run(result)
                for finding in result.unresolved_findings:
                    try:
                        self.notifier.notify(run_id, finding.owner_role, finding.model_dump(mode="json"))
                    except Exception as error:
                        # Notification delivery never authorizes retries or changes an observed verdict.
                        result.reasons.append(redact_log(f"PM notification failed: {type(error).__name__}"))
                        self._publish(state)
            finally:
                self._executing.discard(run_id)

    async def _repair(self, state: RunState, head, root: RootCause) -> None:
        result, budget = state.result, state.budget
        while True:
            if state.cancel_event.is_set():
                raise asyncio.CancelledError
            try:
                quote = float(getattr(self.repair_adapter, "quote_usd", float("inf")))
                budget.reserve_patch(quote)
            except BudgetExceeded as error:
                result.reasons.append(f"NEEDS_ENGINEER: {error}")
                return
            attempt = budget.debug_iterations
            try:
                method = self.repair_adapter.propose
                if isinstance(self.repair_adapter, FixtureRepairAdapter):
                    proposal = method(state.context, head, root, attempt=attempt)
                elif inspect.iscoroutinefunction(method):
                    task = asyncio.create_task(method(state.context, head, root, attempt=attempt))
                    cancelled = asyncio.create_task(state.cancel_event.wait())
                    try:
                        done, _ = await asyncio.wait({task, cancelled}, timeout=budget.remaining,
                                                     return_when=asyncio.FIRST_COMPLETED)
                        if state.cancel_event.is_set():
                            raise asyncio.CancelledError
                        if not done:
                            raise BudgetExceeded("Model proposal deadline exhausted")
                        proposal = await task
                    finally:
                        for pending in (task, cancelled):
                            if not pending.done():
                                pending.cancel()
                        await asyncio.gather(task, cancelled, return_exceptions=True)
                else:
                    raise ValueError("External repair adapters must implement a cancellable async propose method")
            finally:
                budget.settle()
            if proposal is None:
                result.reasons.append("NEEDS_ENGINEER: repair adapter supplied no candidate")
                return
            # Validate before reading any path supplied by an adapter.
            try:
                validate_patch(proposal.changes, state.context.allowed_patch_paths, state.context.snapshot_root)
            except PatchPolicyError as error:
                result.reasons.append(f"NEEDS_ENGINEER: rejected patch policy: {error}")
                return
            # This text is diagnostic, not permission to run it or evidence it works.
            patch_text = "".join("".join(difflib.unified_diff(
                (state.context.snapshot_root / p).read_text(encoding="utf-8").splitlines(True),
                content.splitlines(True), fromfile="a/" + p, tofile="b/" + p))
                for p, content in proposal.changes.items() if (state.context.snapshot_root / p).is_file())
            try:
                candidate = materialize_candidate(proposal, state.context.snapshot_root,
                    self.storage / "runs" / result.run_id / "work" / f"candidate-source-{attempt}",
                    state.context.allowed_patch_paths)
            except PatchPolicyError as error:
                result.reasons.append(f"NEEDS_ENGINEER: rejected patch policy: {error}")
                return
            if budget.remaining <= 0 or state.cancel_event.is_set():
                result.reasons.append("Unverified candidate retained only as RCA text; no validated patch published")
                (self._directory(state) / "unverified_rca.txt").write_text(redact_log(patch_text), encoding="utf-8")
                raise BudgetExceeded("No runtime remains for candidate validation")
            branch = f"test-agent/{result.run_id}/attempt-{attempt}"
            async def candidate_git(*args):
                budget.check()
                if state.cancel_event.is_set():
                    raise asyncio.CancelledError
                return await asyncio.to_thread(git, candidate, *args, timeout_seconds=min(5.0, budget.remaining))
            await candidate_git("init", "--initial-branch=" + branch)
            await candidate_git("config", "user.name", "EVO Candidate")
            await candidate_git("config", "user.email", "candidate@localhost")
            await candidate_git("config", "core.hooksPath", str(candidate / "disabled-hooks"))
            await candidate_git("add", ".")
            await candidate_git("commit", "-m", "Candidate only; human review required")
            candidate_sha = await candidate_git("rev-parse", "HEAD")
            execution = await self._test(state, candidate, f"candidate-{attempt}")
            if execution.cancelled:
                raise asyncio.CancelledError
            if execution.timed_out:
                raise BudgetExceeded("Candidate validation timed out")
            valid = (execution.trusted and not execution.blocked_reason and execution.exit_code == 0
                and execution.passed > 0 and all(x.result == RequirementOutcome.PASS for x in requirement_results(state.context.spec, [execution])))
            if valid:
                patch_file = self._directory(state) / "candidate.patch"
                patch_file.write_text(redact_log(patch_text), encoding="utf-8")
                result.candidate_patch = dict(branch=branch, commit_sha=candidate_sha,
                    original_head_sha=state.context.head_sha, tested_candidate_sha=candidate_sha,
                    validated=True, patch_uri=f"/v1/test-runs/{result.run_id}/artifacts/candidate.patch",
                    publication="LOCAL_DRAFT_ONLY", hypothesis=redact_log(proposal.hypothesis),
                    changed_paths=sorted(proposal.changes), accepted=False)
                result.reasons.append("Candidate passes; original PR remains CHANGES_REQUIRED until incorporated and freshly verified")
                return
            result.reasons.append(f"Candidate attempt {attempt} failed validation; original failure remains")

    def cancel_run(self, run_id: str, principal: Principal) -> Verification:
        state = self.runs[run_id]
        self.authorize(principal, state.result.repo, "engineer")
        state.cancel_event.set()
        if state.result.execution_status == Status.QUEUED:
            self._transition(state, Status.CANCELLED)
            state.result.readiness = Readiness.INCONCLUSIVE
            self._publish(state)
            self.memory.append_run(state.result)
        return self._copy(state.result)

    def artifacts(self, run_id: str) -> dict:
        self.get_run(run_id)
        state = self.runs[run_id]
        path = self._directory(state) / "evidence_manifest.json"
        if path.is_symlink():
            raise ValueError("Linked manifest is not an evidence artifact")
        if sha256(path.read_bytes()).hexdigest() != state.result.artifact_manifest_hash:
            raise ValueError("Artifact manifest integrity mismatch")
        return json.loads(path.read_text(encoding="utf-8"))

    def artifact_path(self, run_id: str, name: str) -> Path:
        state = self.runs[run_id]
        directory = self._directory(state)
        if name in {"verification.json", "evidence_manifest.json"}:
            self.artifacts(run_id)
            path = directory / name
            if path.is_symlink():
                raise ValueError("Linked verification envelope is not allowed")
            if name == "verification.json" and json.loads(path.read_text(encoding="utf-8")) != state.result.model_dump(mode="json"):
                raise ValueError("Verification envelope differs from trusted run state")
            return path
        manifest = self.artifacts(run_id)
        match = next((e for e in manifest["evidence"] if e["path"] == name), None)
        path = (directory / name).resolve()
        if not match or not path.is_relative_to(directory.resolve()) or path.is_symlink():
            raise KeyError("Artifact not found")
        if not path.is_file() or sha256(path.read_bytes()).hexdigest() != match["sha256"]:
            raise ValueError("Evidence checksum mismatch")
        return path

    def review_run(self, run_id: str, request: ReviewRequest, principal: Principal) -> Verification:
        state = self.runs[run_id]
        self.authorize(principal, state.result.repo, "qa")
        result = self.get_run(run_id)
        if result.readiness == Readiness.STALE or result.execution_status not in TERMINAL:
            raise ValueError("Cannot review stale or incomplete verification")
        if request.identity_hash != result.identity_hash or request.artifact_manifest_hash != result.artifact_manifest_hash:
            raise ValueError("Review input identity or artifact manifest mismatch")
        self.artifact_path(run_id, "verification.json")
        for item in self.artifacts(run_id)["evidence"]:
            self.artifact_path(run_id, item["path"])
        if request.decision == "APPROVED" and result.readiness != Readiness.READY_FOR_QA:
            raise ValueError("Only READY_FOR_QA can receive an approval")
        review = QAReview(status=request.decision, reviewer=principal.name, timestamp=now(),
            identity_hash=result.identity_hash, artifact_manifest_hash=result.artifact_manifest_hash)
        state.result.qa_review = review
        if request.decision == "REJECTED":
            self._archive_envelope(state, "before-qa-rejection")
            state.result.readiness = Readiness.CHANGES_REQUIRED
            state.result.reasons.append("Human QA rejected this version; engineer action and fresh verification are required")
            self._publish(state)
        else:
            self._save_verification(state)
        self.memory.append_review(run_id, {**review.model_dump(mode="json"), "comment": redact_log(request.comment)},
                                  source_identity=state.context.identity)
        self.persist()
        return self._copy(state.result)

    def feedback_run(self, run_id: str, request: FeedbackRequest, principal: Principal) -> dict:
        state = self.runs[run_id]
        self.authorize(principal, state.result.repo, "engineer")
        result = self.get_run(run_id)
        if request.identity_hash != result.identity_hash or result.readiness == Readiness.STALE:
            raise ValueError("Feedback identity is stale or mismatched")
        payload = {**request.model_dump(mode="json"), "actor": principal.name, "at": now()}
        event = self.memory.append_feedback(run_id, payload, source_identity=state.context.identity)
        return dict(recorded=True, run_id=run_id, event_id=event["event_id"])
