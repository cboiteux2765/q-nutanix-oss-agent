"""Core governance checks using real, offline Git and pytest observations."""
import asyncio
from pathlib import Path
import time

import pytest

from app.models import (
    CreateRunRequest, ExecutionStatus, Principal, Readiness, ReviewRequest,
)
from app.providers import SourceRegistry, make_demo_repository
from app.rca_and_repair import CandidatePatchProposal, FixtureRepairAdapter
from app.run_manager import BudgetController, BudgetExceeded, RunManager


ENGINEER = Principal(name="test-engineer", roles=frozenset({"engineer"}))
QA = Principal(name="named-qa-reviewer", roles=frozenset({"qa"}))


def budget_request(**updates):
    values = dict(repo="org/service", pr_number=42, spec_id="SPEC-42", spec_uri="registry://spec",
        spec_hash="a" * 64, test_profile_id="pytest-default-v1", environment_manifest_id="sandbox-v1",
        correlation_id="core-test")
    return CreateRunRequest(**{**values, **updates})


def system(tmp_path, *, regression=False, adapter=None, request_updates=None):
    _, registration = make_demo_repository(tmp_path / "repo", regression=regression)
    registry = SourceRegistry(tmp_path / "registry")
    registry.register(registration)
    manager = RunManager(tmp_path / "manager", registry, repair_adapter=adapter)
    request = registry.request_for_pr("org/service", 42)
    if request_updates:
        request = request.model_copy(update=request_updates)
    run, duplicate = manager.create_run(request, ENGINEER)
    assert not duplicate
    return manager, registration, run.run_id


def test_budget_caps_invocations_runtime_and_validation_reservation():
    budget = BudgetController(budget_request(max_test_executions=2))
    budget.consume_test()
    budget.consume_test()
    with pytest.raises(BudgetExceeded, match="invocation budget"):
        budget.consume_test()
    with pytest.raises(BudgetExceeded, match="No validation invocation"):
        budget.reserve_patch(0.0)
    assert budget.test_executions == 2 and budget.debug_iterations == 0
    expired = BudgetController(budget_request(max_runtime_seconds=1), started=time.monotonic() - 2)
    with pytest.raises(BudgetExceeded, match="runtime"):
        expired.consume_test()
    assert expired.test_executions == 0


@pytest.mark.parametrize("quote", [float("nan"), float("inf"), float("-inf"), -0.01])
def test_nonfinite_or_negative_adapter_quotes_cannot_consume_budget(quote):
    budget = BudgetController(budget_request())
    with pytest.raises(ValueError, match="finite nonnegative"):
        budget.reserve_patch(quote)
    assert budget.debug_iterations == 0 and budget.reserved_usd == 0


def test_budget_charges_failed_reservations_and_enforces_attempt_and_spend_caps():
    budget = BudgetController(budget_request(max_llm_cost_usd=0.3))
    budget.reserve_patch(0.2)
    budget.settle()
    assert budget.model_spend_usd == pytest.approx(0.2) and budget.reserved_usd == 0
    with pytest.raises(BudgetExceeded, match="spend reservation"):
        budget.reserve_patch(0.2)
    assert budget.debug_iterations == 1
    free = BudgetController(budget_request(max_debug_iterations=2))
    for _ in range(2):
        free.reserve_patch(0.0)
        free.settle()
    with pytest.raises(BudgetExceeded, match="Patch attempt"):
        free.reserve_patch(0.0)
    assert free.debug_iterations == 2


def test_no_validation_slot_prevents_model_proposal_end_to_end(tmp_path):
    class MustNotCall:
        quote_usd = 0.0
        calls = 0

        async def propose(self, *args, **kwargs):
            self.calls += 1
            raise AssertionError("No model call is allowed without a validation invocation")

    adapter = MustNotCall()
    manager, _, run_id = system(tmp_path, regression=True, adapter=adapter,
                                request_updates={"max_test_executions": 2})
    asyncio.run(manager.execute(run_id))
    result = manager.get_run(run_id)
    assert result.execution_status == ExecutionStatus.COMPLETED
    assert result.readiness == Readiness.CHANGES_REQUIRED
    assert adapter.calls == 0 and result.metrics.debug_iterations == 0
    assert result.metrics.test_executions == 2 and result.candidate_patch is None
    assert any("No validation invocation" in reason for reason in result.reasons)


def test_cancel_during_async_model_proposal_is_bounded_and_charged(tmp_path):
    class SlowAdapter:
        quote_usd = 0.25

        def __init__(self):
            self.started = asyncio.Event()
            self.finished = asyncio.Event()

        async def propose(self, *args, **kwargs):
            self.started.set()
            try:
                await asyncio.sleep(60)
            finally:
                self.finished.set()

    async def scenario():
        adapter = SlowAdapter()
        manager, _, run_id = system(tmp_path, regression=True, adapter=adapter)
        task = asyncio.create_task(manager.execute(run_id))
        await asyncio.wait_for(adapter.started.wait(), 10)
        started = time.monotonic()
        manager.cancel_run(run_id, ENGINEER)
        await asyncio.wait_for(task, 3)
        assert time.monotonic() - started < 3
        assert adapter.finished.is_set()
        return manager.get_run(run_id)

    result = asyncio.run(scenario())
    assert result.execution_status == ExecutionStatus.CANCELLED
    assert result.readiness == Readiness.INCONCLUSIVE and result.candidate_patch is None
    assert result.metrics.debug_iterations == 1 and result.metrics.test_executions == 2
    assert result.metrics.model_spend_usd == 0.25 and result.metrics.model_reserved_usd == 0


def test_malicious_proposal_path_is_rejected_before_source_reads(tmp_path, monkeypatch):
    protected = tmp_path / "private-not-a-source-file.py"
    protected.write_text("SECRET_SHOULD_NEVER_BE_READ", encoding="utf-8")
    adapter = FixtureRepairAdapter(proposals=[CandidatePatchProposal(
        changes={str(protected.resolve()): "value = 1\n"}, hypothesis="Untrusted path escape")])
    manager, _, run_id = system(tmp_path, regression=True, adapter=adapter)
    original_read_text = Path.read_text
    reads = []

    def guarded_read(path, *args, **kwargs):
        if path.resolve() == protected.resolve():
            reads.append(path)
            raise AssertionError("Proposal was read before path validation")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded_read)
    asyncio.run(manager.execute(run_id))
    result = manager.get_run(run_id)
    assert reads == []
    assert result.readiness == Readiness.CHANGES_REQUIRED and result.candidate_patch is None
    assert any("rejected patch policy" in reason for reason in result.reasons)
    report = manager.artifact_path(run_id, "test_report.md").read_text(encoding="utf-8")
    assert "SECRET_SHOULD_NEVER_BE_READ" not in report


def test_named_qa_rejection_revokes_readiness_and_cannot_be_overridden(tmp_path):
    manager, _, run_id = system(tmp_path)
    asyncio.run(manager.execute(run_id))
    ready = manager.get_run(run_id)
    assert ready.readiness == Readiness.READY_FOR_QA
    rejection = ReviewRequest(decision="REJECTED", identity_hash=ready.identity_hash,
        artifact_manifest_hash=ready.artifact_manifest_hash, comment="Acceptance flow needs engineering review")
    rejected = manager.review_run(run_id, rejection, QA)
    assert rejected.readiness == Readiness.CHANGES_REQUIRED
    assert rejected.qa_review.status == "REJECTED" and rejected.qa_review.reviewer == QA.name
    with pytest.raises(ValueError, match="READY_FOR_QA"):
        manager.review_run(run_id, rejection.model_copy(update={"decision": "APPROVED",
            "artifact_manifest_hash": rejected.artifact_manifest_hash}), QA)


@pytest.mark.parametrize("changed_input", ["spec", "profile", "environment", "policy_version"])
def test_relevant_registered_input_change_invalidates_verified_evidence(tmp_path, changed_input):
    manager, registration, run_id = system(tmp_path)
    asyncio.run(manager.execute(run_id))
    original = manager.get_run(run_id)
    assert original.readiness == Readiness.READY_FOR_QA
    evidence_before = {item["path"]: item["sha256"] for item in manager.artifacts(run_id)["evidence"]
                       if item["path"].endswith(("junit.xml", "pytest.log"))}
    if changed_input == "spec":
        replacement = registration.spec.model_copy(update={"intent": "Revised, newly approved customer intent"})
    elif changed_input == "profile":
        replacement = registration.profile.model_copy(update={"timeout_seconds": 61})
    elif changed_input == "environment":
        replacement = registration.environment.model_copy(update={"id": "sandbox-v2"})
    else:
        replacement = "pilot-v2"
    manager.registry.register(registration.model_copy(update={changed_input: replacement}))
    stale = manager.get_run(run_id)
    assert stale.readiness == Readiness.STALE
    assert stale.reviewed_head_sha == original.reviewed_head_sha
    assert stale.identity_hash == original.identity_hash
    evidence_after = {item["path"]: item["sha256"] for item in manager.artifacts(run_id)["evidence"]
                      if item["path"].endswith(("junit.xml", "pytest.log"))}
    assert evidence_before and evidence_after == evidence_before
    assert stale.tests == original.tests
    with pytest.raises(ValueError, match="stale"):
        manager.review_run(run_id, ReviewRequest(decision="APPROVED", identity_hash=stale.identity_hash,
            artifact_manifest_hash=stale.artifact_manifest_hash), QA)
