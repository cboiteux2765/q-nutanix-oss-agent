"""Deterministic checks only; no claim of semantic completeness."""
from graphlib import CycleError, TopologicalSorter
from collections import Counter
from collections.abc import Mapping

from .errors import TaskGraphError
from .models import GraphData, ValidationIssue


def order(data: GraphData) -> list[str]:
    predecessors = {t.id: [] for t in data.tasks}
    for edge in data.dependencies:
        predecessors[edge.dependent_id].append(edge.prerequisite_id)
    try:
        return list(TopologicalSorter(predecessors).static_order())
    except CycleError as exc:
        raise TaskGraphError("CYCLE_DETECTED", "Task dependencies must form a DAG.") from exc


def check_structure(data: GraphData) -> None:
    ids = [t.id for t in data.tasks]
    if len(ids) != len(set(ids)):
        raise TaskGraphError("DUPLICATE_TASK", "Task IDs must be unique.")
    seen = set()
    for edge in data.dependencies:
        pair = (edge.prerequisite_id, edge.dependent_id)
        if any(id_ not in ids for id_ in pair):
            raise TaskGraphError("TASK_NOT_FOUND", "Dependency references a nonexistent task.", task_ids=list(pair))
        if pair[0] == pair[1] or pair in seen:
            raise TaskGraphError("INVALID_DEPENDENCY", "Self or duplicate dependency.", task_ids=list(pair))
        seen.add(pair)
    order(data)
    previous = None
    for entry in data.history:
        if entry.new_version != entry.previous_version + 1 or (
            previous is not None and entry.previous_version != previous
        ) or entry.new_version > data.version:
            raise TaskGraphError("INVALID_GRAPH", "Inconsistent operation history versions.")
        previous = entry.new_version
    if data.history and previous != data.version:
        raise TaskGraphError("INVALID_GRAPH", "History must end at the current graph version.")


def validate_graph(graph) -> list[ValidationIssue]:
    data = graph._snapshot()
    check_structure(data)
    issues = []
    for task in data.tasks:
        if not task.acceptance_criteria:
            issues.append(ValidationIssue(code="MISSING_ACCEPTANCE_CRITERIA", message="Task needs acceptance criteria.", task_id=task.id))
        if not task.requirement_ids:
            issues.append(ValidationIssue(code="MISSING_REQUIREMENT_REFERENCE", message="Task needs a requirement reference.", task_id=task.id))
    return issues


def validate_requirement_coverage(graph, requirements) -> list[ValidationIssue]:
    """Accept requirement IDs, existing Requirement models, or dictionaries with id."""
    if hasattr(requirements, "items") and not isinstance(requirements, Mapping):
        requirements = requirements.items
    if not isinstance(requirements, (list, tuple)):
        raise TaskGraphError("VALIDATION_FAILED", "Requirements must be a list of IDs or requirement records.")
    ids = []
    for req in requirements:
        id_ = req if isinstance(req, str) else req.get("id") if isinstance(req, Mapping) else getattr(req, "id", None)
        if not isinstance(id_, str) or not id_.strip() or id_ != id_.strip():
            raise TaskGraphError("VALIDATION_FAILED", "Requirement IDs must be nonblank strings without surrounding whitespace.")
        ids.append(id_)
    issues = [ValidationIssue(code="DUPLICATE_REQUIREMENT", message="Duplicate requirement ID.", requirement_id=id_)
              for id_, count in Counter(ids).items() if count > 1]
    known, covered = set(ids), set()
    for task in graph.list_tasks():
        if not task.requirement_ids:
            issues.append(ValidationIssue(code="MISSING_REQUIREMENT_REFERENCE", message="Task needs a requirement reference.", task_id=task.id))
        for id_ in task.requirement_ids:
            covered.add(id_)
            if id_ not in known:
                issues.append(ValidationIssue(code="UNKNOWN_REQUIREMENT", message="Referenced requirement does not exist.", task_id=task.id, requirement_id=id_))
    issues.extend(ValidationIssue(code="MISSING_COVERAGE", message="Requirement has no task.", requirement_id=id_)
                  for id_ in dict.fromkeys(ids) if id_ not in covered)
    return issues
