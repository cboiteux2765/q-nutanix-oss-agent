"""Deterministic plan checks; semantic judgments belong to the LLM reviewer."""
from collections import Counter
from graphlib import TopologicalSorter, CycleError

from spec_agent.models import ValidationIssue


def issue(kind, explanation, affected=None, correction="Revise the affected planning data."):
    return ValidationIssue(issue_type=kind, severity="critical", affected_id=affected,
                           explanation=explanation, suggested_correction=correction)


def validate_plan(requirements, draft, repository_files, idea, memory):
    issues = []
    req_ids = [r.id for r in requirements.items]
    task_ids = [t.id for t in draft.tasks]
    files = {f.path: f.content for f in repository_files}
    sources = [idea] + [m.content for m in memory] + list(files.values())
    if not req_ids:
        issues.append(issue("missing_requirements", "At least one requirement is required."))
    for label, ids in [("requirement", req_ids), ("task", task_ids)]:
        for id_, count in Counter(ids).items():
            if count > 1:
                issues.append(issue("duplicate_id", f"Duplicate {label} ID.", id_))
    covered = set()
    for item in [*requirements.items, *draft.tasks]:
        if not item.description.strip() or not item.id.strip():
            issues.append(issue("missing_field", "ID and description must be nonblank.", item.id))
        if not item.acceptance_criteria or any(not c.strip() for c in item.acceptance_criteria):
            issues.append(issue("missing_acceptance_criteria", "Measurable criteria are required.", item.id))
        for ev in item.evidence:
            if ev.path not in files or ev.quote not in files[ev.path]:
                issues.append(issue("invalid_evidence", "Evidence must quote a supplied file.", item.id))
    for req in requirements.items:
        if req.origin != "suggestion" and not any(req.source in source for source in sources):
            issues.append(issue("unsupported_source", "Requirement source is not a supplied excerpt.", req.id))
        if req.origin == "user" and req.source not in idea:
            issues.append(issue("unsupported_source", "User requirement must cite the original idea.", req.id))
    for task in draft.tasks:
        covered.update(task.requirement_ids)
        if not task.requirement_ids or set(task.requirement_ids) - set(req_ids):
            issues.append(issue("invalid_requirement_reference", "Task must reference existing requirements.", task.id))
        if set(task.depends_on) - set(task_ids):
            issues.append(issue("invalid_dependency", "Task dependency does not exist.", task.id))
        evidence_paths = {ev.path for ev in task.evidence}
        if set(task.relevant_files) - (files.keys() & evidence_paths):
            issues.append(issue("unsupported_file", "Relevant files require supplied file evidence.", task.id))
    for req in set(req_ids) - covered:
        issues.append(issue("missing_coverage", "Requirement has no corresponding task.", req))
    try:
        tuple(TopologicalSorter({t.id: t.depends_on for t in draft.tasks}).static_order())
    except CycleError:
        issues.append(issue("circular_dependencies", "Development tasks must form a DAG."))
    return issues


def validate_findings(findings, files):
    supplied = {f.path: f.content for f in files}
    issues = []
    if findings.context_available != bool(files):
        issues.append(issue("invalid_repository_context", "Context availability contradicts supplied files."))
    for finding in findings.findings:
        if finding.certainty == "confirmed" and not finding.evidence:
            issues.append(issue("unsupported_finding", "Confirmed findings need file evidence."))
        for ev in finding.evidence:
            if ev.path not in supplied or ev.quote not in supplied[ev.path]:
                issues.append(issue("invalid_evidence", "Repository finding cites unsupported evidence."))
    return issues
