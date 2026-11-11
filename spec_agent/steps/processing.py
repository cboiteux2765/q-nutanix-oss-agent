"""The four initial stages are small, reusable structured transformations."""
from pydantic import BaseModel

from spec_agent.models import IdeaAnalysis, RepositoryFindings, Requirements, TaskDraft
from spec_agent.workflow.registry import WorkflowStep


def payload(inputs):
    def encode(value):
        if isinstance(value, BaseModel):
            return value.model_dump(mode="json")
        if isinstance(value, list):
            return [encode(v) for v in value]
        return value
    return {k: encode(v) for k, v in inputs.items()}


class AnalyzeIdea(WorkflowStep):
    requires = frozenset({"idea", "memory"})
    produces = frozenset({"analysis"})

    def run(self, inputs, parameters, llm):
        return {"analysis": llm.structured("analyze_idea", payload(inputs), IdeaAnalysis)}


class InspectRepository(WorkflowStep):
    requires = frozenset({"analysis", "repository_files", "memory"})
    produces = frozenset({"repository"})

    def run(self, inputs, parameters, llm):
        if not inputs["repository_files"]:
            findings = RepositoryFindings(context_available=False, missing_context=[
                "No repository files supplied; existing behavior and feature presence are unknown."])
        else:
            findings = llm.structured("inspect_repository", payload(inputs), RepositoryFindings)
        return {"repository": findings}


class GenerateRequirements(WorkflowStep):
    requires = frozenset({"idea", "memory", "analysis", "repository"})
    produces = frozenset({"requirements"})

    def run(self, inputs, parameters, llm):
        return {"requirements": llm.structured("generate_requirements", payload(inputs), Requirements)}


class GenerateTasks(WorkflowStep):
    requires = frozenset({"requirements", "repository"})
    produces = frozenset({"task_draft"})

    def run(self, inputs, parameters, llm):
        return {"task_draft": llm.structured("generate_tasks", payload(inputs), TaskDraft)}
