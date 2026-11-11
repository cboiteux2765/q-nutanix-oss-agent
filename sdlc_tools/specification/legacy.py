"""Explicit migration from the old task-plan schema to the graph schema."""
from pydantic import ValidationError
from spec_agent.models import TaskPlan
from sdlc_tools.task_graph import TaskGraph, TaskGraphError


def from_task_plan(plan: TaskPlan | dict) -> TaskGraph:
    try:
        plan = TaskPlan.model_validate(plan.model_dump() if isinstance(plan, TaskPlan) else plan)
    except ValidationError as exc:
        raise TaskGraphError("INVALID_GRAPH", "Invalid legacy task plan.", reason=str(exc)) from exc
    return TaskGraph.load({
        "tasks": [dict(id=t.id, title=t.description, description=t.description,
                       requirement_ids=t.requirement_ids, acceptance_criteria=t.acceptance_criteria,
                       relevant_files=t.relevant_files,
                       metadata={"evidence": [e.model_dump() for e in t.evidence]}) for t in plan.tasks],
        "dependencies": [dict(prerequisite_id=p, dependent_id=t.id) for t in plan.tasks for p in t.depends_on],
        "metadata": {"legacy_plan": plan.model_dump(exclude={"tasks"})},
    })
