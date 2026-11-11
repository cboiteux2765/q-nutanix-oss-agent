import pytest
from pydantic import ValidationError

from spec_agent.models import Requirement, Requirements, Task, TaskDraft
from spec_agent.services.validation import validate_plan


def fixtures():
    req = Requirement(id="REQ-001", description="Health", origin="user", source="Health",
                      acceptance_criteria=["HTTP 200"])
    task = Task(id="TASK-001", description="Implement health", requirement_ids=[req.id],
                acceptance_criteria=["HTTP 200"])
    return Requirements(items=[req]), TaskDraft(tasks=[task])


@pytest.mark.parametrize("mutation,kind", [
    (lambda r, d: d.tasks.append(d.tasks[0]), "duplicate_id"),
    (lambda r, d: r.items.append(r.items[0]), "duplicate_id"),
    (lambda r, d: d.tasks[0].depends_on.append("TASK-001"), "circular_dependencies"),
    (lambda r, d: d.tasks[0].depends_on.append("missing"), "invalid_dependency"),
    (lambda r, d: d.tasks.clear(), "missing_coverage"),
    (lambda r, d: d.tasks[0].requirement_ids.append("REQ-X"), "invalid_requirement_reference"),
    (lambda r, d: d.tasks[0].acceptance_criteria.clear(), "missing_acceptance_criteria"),
    (lambda r, d: r.items[0].acceptance_criteria.append(" "), "missing_acceptance_criteria"),
    (lambda r, d: d.tasks[0].relevant_files.append("absent.py"), "unsupported_file"),
    (lambda r, d: setattr(r.items[0], "source", "invented"), "unsupported_source"),
])
def test_deterministic_validation(mutation, kind):
    reqs, draft = fixtures()
    mutation(reqs, draft)
    assert kind in {i.issue_type for i in validate_plan(reqs, draft, [], "Health", [])}


def test_valid_plan():
    assert validate_plan(*fixtures(), [], "Health", []) == []


@pytest.mark.parametrize("id", [None, ""])
def test_missing_task_id(id):
    data = dict(description="Task", requirement_ids=["REQ-001"], acceptance_criteria=[])
    if id is not None:
        data["id"] = id
    with pytest.raises(ValidationError):
        Task.model_validate(data)
