import json
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from evo_coding.handoff import (
    HandoffSchemaError,
    canonical_hash,
    load_handoff,
    parse_handoff,
    spec_hash,
    validate_handoff,
)
from evo_coding.models import CodingResult, RevisionRequest, RunState, TestRunSummary

SHA1 = "a" * 40
SHA256 = "b" * 64


def test_spec_hash_normalizes_line_endings():
    assert spec_hash(b"a\r\nb\n") == spec_hash(b"a\nb\n")
    assert spec_hash(b"a\nb\n") != spec_hash(b"a\nB\n")


def test_canonical_hash_is_order_independent():
    assert canonical_hash({"a": 1, "b": [1, 2]}) == canonical_hash({"b": [1, 2], "a": 1})


def test_valid_pilot_handoff_has_no_issues(pilot):
    loaded = load_handoff(pilot.handoff())
    assert loaded.ok, loaded.issues


def test_schema_errors_are_reported_per_field(pilot):
    data = json.loads(pilot.handoff().read_text())
    data["source_commit"] = "not-a-sha"
    del data["scope"]
    with pytest.raises(HandoffSchemaError) as exc:
        parse_handoff(data)
    joined = " ".join(exc.value.errors)
    assert "source_commit" in joined and "scope" in joined


def test_extra_fields_are_rejected(pilot):
    data = json.loads(pilot.handoff().read_text())
    data["surprise"] = True
    with pytest.raises(HandoffSchemaError):
        parse_handoff(data)


def _codes(handoff, spec):
    return [i.code for i in validate_handoff(handoff, spec)]


def test_intake_rules(pilot):
    path = pilot.handoff()
    spec = (path.parent / "product_spec.md").read_bytes()
    base = json.loads(path.read_text())

    assert _codes(parse_handoff(base), spec) == []
    assert "SPEC_HASH_MISMATCH" in _codes(parse_handoff(base), spec + b"\nedited after approval")
    assert "SPEC_NOT_FOUND" in _codes(parse_handoff(base), None)
    assert "MISSING_APPROVAL" in _codes(parse_handoff({**base, "approval": None}), spec)
    stale = {**base, "approval": {**base["approval"], "spec_hash": SHA256}}
    assert "STALE_APPROVAL" in _codes(parse_handoff(stale), spec)
    assert "WRONG_STATUS" in _codes(parse_handoff({**base, "status": "SPEC_DRAFT"}), spec)
    assert "WRONG_NEXT_OWNER" in _codes(parse_handoff({**base, "next_owner": "qa_agent"}), spec)
    assert "UNRESOLVED_ITEM" in _codes(parse_handoff({**base, "unresolved_items": ["pick one"]}), spec)
    assert "REQUIREMENT_IDS_MISMATCH" in _codes(parse_handoff({**base, "requirement_ids": ["REQ-1"]}), spec)
    orphan = {**base, "requirements": base["requirements"] + [{"id": "REQ-9", "text": "x"}],
              "requirement_ids": base["requirement_ids"] + ["REQ-9"]}
    assert "REQUIREMENT_WITHOUT_CRITERION" in _codes(parse_handoff(orphan), spec)
    bad_ref = {**base, "acceptance_criteria": base["acceptance_criteria"] + [
        {"id": "AC-9", "requirement_ids": ["REQ-404"], "description": "x"}]}
    assert "UNKNOWN_REQUIREMENT" in _codes(parse_handoff(bad_ref), spec)
    dup = {**base, "acceptance_criteria": base["acceptance_criteria"] + [base["acceptance_criteria"][0]]}
    assert "DUPLICATE_ID" in _codes(parse_handoff(dup), spec)


def _result(**overrides):
    fields = dict(run_id="run-1", repository="o/r", issue_id=1, source_commit=SHA1, base_branch="main",
                  spec_version=1, spec_hash=SHA256, requirement_ids=["REQ-1"], acceptance_criteria=[],
                  status=RunState.TESTING, risk_tier="LOW", next_owner="test_agent", input_hash=SHA256,
                  pr_number=3, pr_head_sha="c" * 40, branch="evo/x",
                  tests=[TestRunSummary(label="final", command="pytest", returncode=0, passed=1, commit="c" * 40)])
    fields.update(overrides)
    return CodingResult(**fields)


def test_testing_result_invariants():
    assert _result().status == RunState.TESTING
    with pytest.raises(ValidationError, match="missing"):
        _result(pr_number=None)
    with pytest.raises(ValidationError, match="passing final"):
        _result(tests=[TestRunSummary(label="final", command="pytest", returncode=1, failed=1, commit="c" * 40)])
    with pytest.raises(ValidationError, match="commit does not match"):
        _result(tests=[TestRunSummary(label="final", command="pytest", returncode=0, commit="d" * 40)])
    with pytest.raises(ValidationError, match="test_agent"):
        _result(next_owner="qa_agent")


def test_blocked_result_requires_a_blocker():
    with pytest.raises(ValidationError, match="blocker"):
        _result(status=RunState.BLOCKED, next_owner="maintainer")


def test_revision_request_needs_content():
    with pytest.raises(ValidationError):
        RevisionRequest(run_id="run-1", source="human", tested_head_sha=SHA1, requested_by="me")


def test_revision_request_from_test_agent_verification():
    verification = {
        "run_id": "ta-9", "readiness": "CHANGES_REQUIRED", "reviewed_head_sha": SHA1,
        "requirements": [{"id": "REQ-1", "test_ids": ["tests/t.py::a"], "result": "FAIL", "reason": "boom"},
                         {"id": "REQ-2", "test_ids": [], "result": "PASS"}],
        "unresolved_findings": [{"failure_class": "REGRESSION", "summary": "empty input", "next_action": "fix"}],
        "reasons": ["1 failing acceptance test"],
    }
    request = RevisionRequest.from_test_agent_verification(verification, "run-1")
    assert request.tested_head_sha == SHA1 and request.source == "test_agent"
    assert [f.message for f in request.failures] == ["REQ-1: FAIL"]
    assert any("REGRESSION" in f for f in request.findings)
    with pytest.raises(ValueError):
        RevisionRequest.from_test_agent_verification({**verification, "readiness": "READY_FOR_QA"}, "run-1")


def test_timestamps_are_timezone_aware(pilot):
    loaded = load_handoff(pilot.handoff())
    assert loaded.handoff.timestamp.tzinfo is not None
    assert loaded.handoff.timestamp <= datetime.now(timezone.utc)
