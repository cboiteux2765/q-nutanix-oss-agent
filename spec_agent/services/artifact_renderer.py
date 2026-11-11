import json

from spec_agent.models import Artifacts, PlanningModel


def render(model: PlanningModel) -> Artifacts:
    """Render both artifacts from one final snapshot, including blocked drafts."""
    plan = model.plan
    lines = ["# Product Specification", "", "## 1. Overview and original idea", "",
             model.original_idea, "", "## 2. Goals and scope", "", model.analysis.primary_goal]

    def bullets(items):
        lines.append("")
        lines.extend([f"- {item}" for item in items] or ["- None recorded."])

    bullets(model.analysis.scope)
    lines.extend(["", "## 3. Relevant repository context"])
    bullets([f"[{f.certainty}] {f.description}" + "".join(
        f" (Source: {e.path}; excerpt: {e.quote})" for e in f.evidence)
        for f in model.repository.findings] + model.repository.missing_context)
    for heading, kind in [("4. Functional requirements", "functional"),
                          ("5. Nonfunctional requirements and constraints", "nonfunctional")]:
        lines.extend(["", "## " + heading])
        bullets([f"{r.id}: {r.description} (origin: {r.origin}; source: {r.source})"
                 for r in plan.requirements if r.kind == kind])
        if kind == "nonfunctional":
            bullets(plan.constraints)
    lines.extend(["", "## 6. Acceptance criteria"])
    bullets([f"{r.id}: {criterion}" for r in plan.requirements for criterion in r.acceptance_criteria])
    lines.extend(["", "## 7. Proposed development tasks"])
    for task in plan.tasks:
        lines.extend(["", f"### {task.id}: {task.description}", "",
                      "Requirements: " + ", ".join(task.requirement_ids)])
        bullets(task.acceptance_criteria)
        bullets(["Relevant file: " + f for f in task.relevant_files] +
                [f"Evidence: {e.path}: {e.quote}" for e in task.evidence])
    lines.extend(["", "## 8. Dependencies and implementation considerations"])
    bullets([f"{t.id} depends on: {', '.join(t.depends_on) or 'none'}" for t in plan.tasks])
    lines.extend(["", "## 9. Assumptions, risks, and unresolved items"])
    bullets(["Assumption: " + a for a in plan.assumptions] +
            ["Risk: " + r for r in plan.risks] + ["Unresolved: " + u for u in plan.unresolved_items])
    bullets([f"{i.severity}: {i.issue_type}: {i.explanation} Correction: {i.suggested_correction}"
             for i in model.validation.issues])
    lines.extend(["", "## 10. Specification status", "", plan.status,
                  "", "Human review is required. This specification is not approved.", ""])
    return Artifacts(product_spec="\n".join(lines), task_plan_json=json.dumps(
        plan.model_dump(mode="json"), indent=2, ensure_ascii=False, sort_keys=True) + "\n")
