import json

import pytest

from spec_agent import SpecAgentRequest, run_spec_agent, default_workflow
from spec_agent.services.llm_client import DemoLLM, ScriptedLLM
from conftest import PassingLLM


@pytest.mark.parametrize("files,memory", [([], []),
    ([{"path": "app.py", "content": "def health(): return 'ok'"}], []),
    ([], [{"content": "Keep existing conventions", "source": "past review"}]),
    ([{"path": "app.py", "content": "def health(): return 'ok'"}], [{"content": "Old context"}])])
def test_normal_execution_and_consistency(llm, files, memory):
    result = run_spec_agent(SpecAgentRequest(idea="Add health endpoint", repository_files=files,
                                           memory=memory), llm=llm)
    assert result.status == "DRAFT_READY"
    assert json.loads(result.task_plan_json) == result.task_plan.model_dump(mode="json")
    for item in [*result.task_plan.requirements, *result.task_plan.tasks]:
        assert item.id in result.product_spec
        assert item.description in result.product_spec
        assert all(c in result.product_spec for c in item.acceptance_criteria)
    assert "not approved" in result.product_spec
    assert len(result.execution_trace) == 6
    assert all(t.ended_at >= t.started_at and t.duration_seconds >= 0 for t in result.execution_trace)
    assert all(t.status == "SUCCEEDED" for t in result.execution_trace)


@pytest.mark.parametrize("response", ["not JSON", {}, {"primary_goal": 42}, RuntimeError("secret-key")])
def test_bad_provider_results(response):
    result = run_spec_agent(SpecAgentRequest(idea="secret idea"), llm=ScriptedLLM([response]))
    assert result.status == "FAILED"
    assert result.execution_trace[-1].status == "FAILED"
    serialized = result.model_dump_json()
    assert "secret-key" not in serialized and "secret idea" not in serialized


@pytest.mark.parametrize("maximum", [0, 1, 2])
def test_revision_bound_and_blocked(maximum):
    class Counting(DemoLLM):
        reviews = 0
        revisions = 0
        def complete(self, *, operation, **kwargs):
            self.reviews += operation == "semantic_review"
            self.revisions += operation == "revise_plan"
            return super().complete(operation=operation, **kwargs)
    client = Counting()
    workflow = default_workflow()
    workflow.steps[4].parameters["max_revisions"] = maximum
    result = run_spec_agent(SpecAgentRequest(idea="Health", workflow=workflow), llm=client)
    assert result.status == "BLOCKED"
    assert client.revisions == maximum and client.reviews == maximum + 1
    assert result.validation_issues and result.product_spec and result.unresolved_items


def test_revision_can_repair_plan():
    class Repair(PassingLLM):
        def complete(self, *, operation, **kwargs):
            result = super().complete(operation=operation, **kwargs)
            if operation == "generate_tasks":
                result["tasks"][0]["acceptance_criteria"] = []
            if operation == "revise_plan":
                result["task_draft"]["tasks"][0]["acceptance_criteria"] = ["HTTP 200"]
            return result
    result = run_spec_agent(SpecAgentRequest(idea="Health"), llm=Repair())
    assert result.status == "DRAFT_READY"
    assert result.task_plan.tasks[0].acceptance_criteria == ["HTTP 200"]
    assert "HTTP 200" in result.product_spec


def test_revision_cannot_remove_requirements():
    class Removing(DemoLLM):
        def complete(self, *, operation, **kwargs):
            result = super().complete(operation=operation, **kwargs)
            if operation == "revise_plan":
                result["requirements"]["items"] = []
                result["task_draft"]["tasks"] = []
            return result
    result = run_spec_agent(SpecAgentRequest(idea="Health"), llm=Removing())
    assert result.status == "BLOCKED"
    assert any(i.issue_type == "removed_requirement" for i in result.validation_issues)


def test_unconfigured_and_invalid_workflow():
    assert run_spec_agent(SpecAgentRequest(idea="Health")).status == "FAILED"
    config = default_workflow()
    config.steps.clear()
    result = run_spec_agent(SpecAgentRequest(idea="Health", workflow=config))
    assert result.status == "FAILED" and not result.execution_trace


def test_malformed_revision_is_blocked():
    class BadRevision(DemoLLM):
        def complete(self, *, operation, **kwargs):
            return "bad JSON" if operation == "revise_plan" else super().complete(operation=operation, **kwargs)
    result = run_spec_agent(SpecAgentRequest(idea="Health"), llm=BadRevision())
    assert result.status == "BLOCKED"
    assert any(i.issue_type == "revision_failed" for i in result.validation_issues)


def test_invalid_repository_evidence_is_blocked():
    class Ungrounded(PassingLLM):
        def complete(self, *, operation, **kwargs):
            if operation == "inspect_repository":
                return {"context_available": True, "findings": [{
                    "description": "Endpoint exists", "certainty": "confirmed",
                    "evidence": [{"path": "app.py", "quote": "fabricated code"}]}]}
            return super().complete(operation=operation, **kwargs)
    result = run_spec_agent(SpecAgentRequest(idea="Health", repository_files=[{
        "path": "app.py", "content": "# existing file"}]), llm=Ungrounded())
    assert result.status == "BLOCKED"
    assert any(i.issue_type == "invalid_evidence" for i in result.validation_issues)


@pytest.mark.parametrize("kind", ["contradiction", "scope_expansion", "unsupported_assumption",
                                 "missing_essential_step", "unverifiable_criteria"])
def test_semantic_issues_block(kind):
    class Reviewer(PassingLLM):
        def complete(self, *, operation, **kwargs):
            if operation == "semantic_review":
                return {"issues": [{"issue_type": kind, "severity": "critical",
                                    "affected_id": "REQ-001", "explanation": kind,
                                    "suggested_correction": "Ask for clarification."}]}
            return super().complete(operation=operation, **kwargs)
    result = run_spec_agent(SpecAgentRequest(idea="Health"), llm=Reviewer())
    assert result.status == "BLOCKED"
    assert result.validation_issues[0].issue_type == kind


def test_schema_issue_is_structured():
    result = run_spec_agent(SpecAgentRequest(idea="Health"), llm=ScriptedLLM([{}]))
    assert result.validation_issues[0].issue_type == "schema_violation"


def test_rendering_is_repeatable(llm):
    request = SpecAgentRequest(idea="Health")
    first = run_spec_agent(request, llm=llm)
    second = run_spec_agent(request, llm=llm)
    assert first.product_spec == second.product_spec
    assert first.task_plan_json == second.task_plan_json
