from pydantic import Field

from spec_agent.models import (Model, Revision, SemanticReview, PlanningModel,
                               TaskPlan, ValidationResult)
from spec_agent.services.llm_client import LLMError
from spec_agent.services.validation import issue, validate_plan, validate_findings
from spec_agent.steps.processing import payload
from spec_agent.workflow.registry import WorkflowStep


class ValidationParameters(Model):
    max_revisions: int = Field(default=2, ge=0, le=10)


class ValidatePlan(WorkflowStep):
    requires = frozenset({"idea", "analysis", "repository", "requirements", "task_draft",
                          "repository_files", "memory"})
    produces = frozenset({"planning"})
    parameters_model = ValidationParameters

    def run(self, inputs, parameters, llm):
        requirements, draft = inputs["requirements"], inputs["task_draft"]
        original_ids = {r.id for r in requirements.items}
        constraints = set(requirements.constraints) | set(inputs["analysis"].constraints)
        static_issues = validate_findings(inputs["repository"], inputs["repository_files"])
        revisions = 0
        while True:
            issues = static_issues + validate_plan(requirements, draft,
                inputs["repository_files"], inputs["idea"], inputs["memory"])
            if original_ids - {r.id for r in requirements.items}:
                issues.append(issue("removed_requirement", "Revision removed original requirement IDs."))
            if constraints - set(requirements.constraints):
                issues.append(issue("removed_constraint", "Planning must preserve known constraints."))
            review_input = payload({**inputs, "requirements": requirements, "task_draft": draft})
            review_input["original_requirements"] = payload({"original": inputs["requirements"]})["original"]
            review = llm.structured("semantic_review", review_input, SemanticReview)
            issues.extend(review.issues)
            if not any(i.severity == "critical" for i in issues) or revisions >= parameters.max_revisions:
                break
            revisions += 1
            try:
                revised = llm.structured("revise_plan", {**review_input,
                    "issues": [i.model_dump() for i in issues]}, Revision)
            except LLMError:
                # Retain the current plan, and terminate safely without discarding issues.
                issues.append(issue("revision_failed", "Revision returned invalid data or failed."))
                break
            if original_ids - {r.id for r in revised.requirements.items}:
                issues.append(issue("removed_requirement", "Rejected revision that removed original requirement IDs."))
                break
            if constraints - set(revised.requirements.constraints):
                issues.append(issue("removed_constraint", "Rejected revision that removed known constraints."))
                break
            requirements, draft = revised.requirements, revised.task_draft
        status = "BLOCKED" if any(i.severity == "critical" for i in issues) else "DRAFT_READY"
        unresolved = list(dict.fromkeys(inputs["analysis"].unresolved_items +
            requirements.unresolved_items + inputs["repository"].missing_context +
            [i.explanation for i in issues if i.severity == "critical"]))
        plan = TaskPlan(requirements=requirements.items, tasks=draft.tasks,
            constraints=sorted(constraints | set(requirements.constraints)),
            assumptions=requirements.assumptions, unresolved_items=unresolved,
            risks=requirements.risks, status=status)
        return {"planning": PlanningModel(original_idea=inputs["idea"], analysis=inputs["analysis"],
            repository=inputs["repository"], plan=plan,
            validation=ValidationResult(issues=issues, revisions=revisions, status=status))}
