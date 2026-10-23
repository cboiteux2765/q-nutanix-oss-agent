"""Safety boundaries for evidence-backed repair and offline learning records."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path

import pytest

from app.evolution_memory import EvolutionMemory, redact
from app.models import (AcceptanceTest, EnvironmentManifest, FailureClass, Prerequisite, Requirement,
                        ResolvedContext, SpecDocument, TestCase, TestExecution, TestProfile)
from app.rca_and_repair import (AdapterNotConfigured, CandidatePatchProposal, FixtureRepairAdapter,
                                NullPMNotificationAdapter, PatchPolicyError, RepairAdapter,
                                UnconfiguredRepairAdapter, classify_failure, materialize_candidate, validate_patch)


NODE = "tests/test_service.py::test_empty"


@pytest.fixture
def context(tmp_path):
    spec = SpecDocument(spec_id="SPEC-1", intent="Return zero for empty input.",
                        requirements=(Requirement(id="REQ-1", description="Empty input is supported.", test_ids=("TEST-1",)),),
                        acceptance_tests=(AcceptanceTest(id="TEST-1", node_id=NODE, expected_behavior="Empty input returns zero."),))
    return ResolvedContext(repo="org/service", pr_number=1, base_sha="a" * 40, head_sha="b" * 40,
                           snapshot_root=tmp_path, base_snapshot_root=tmp_path, spec=spec,
                           spec_uri="fixture://approved", spec_hash="c" * 64,
                           profile=TestProfile(trusted=True, backend="local_trusted"),
                           environment=EnvironmentManifest(id="sandbox"), allowed_patch_paths=("src/service.py",))


def execution(outcome="FAIL", *, node=NODE, evidence="head:test-empty", **overrides):
    passed = outcome == "PASS"
    values = dict(exit_code=0 if passed else 1, passed=int(passed), failed=int(not passed),
                  cases=[TestCase(node_id=node, outcome=outcome, evidence_id=evidence)],
                  command=["python", "-m", "pytest", "tests", "--junitxml=results.xml"])
    values.update(overrides)
    return TestExecution(**values)


def test_regression_requires_same_requirement_linked_case_passing_base(context):
    head, base = execution(), execution("PASS", evidence="base:test-empty")
    rca = classify_failure(context, head, base)
    assert rca.failure_class == FailureClass.CODE_REGRESSION
    assert set(rca.evidence_ids) == {"head:test-empty", "base:test-empty"}
    assert rca.reproduction == head.command
    assert rca.owner_role == "code_agent"
    assert "Reserve validation budget" in rca.next_action


@pytest.mark.parametrize("baseline", [None, execution(), execution("PASS", node="tests/test_other.py::test_unrelated"),
                                      execution("PASS", trusted=False), execution("PASS", timed_out=True)])
def test_missing_or_untrusted_baseline_cannot_authorize_patch(context, baseline):
    assert classify_failure(context, execution(), baseline).failure_class == FailureClass.UNKNOWN


def test_unmapped_failure_cannot_authorize_patch(context):
    head = execution(node="tests/test_other.py::test_unapproved")
    base = execution("PASS", node=head.cases[0].node_id)
    assert classify_failure(context, head, base).failure_class == FailureClass.UNKNOWN


@pytest.mark.parametrize("overrides", [{"timed_out": True}, {"cancelled": True}, {"trusted": False}, {"exit_code": 2}])
def test_timeout_cancellation_collection_and_untrusted_results_are_unknown(context, overrides):
    assert classify_failure(context, execution(**overrides), execution("PASS")).failure_class == FailureClass.UNKNOWN


def test_fail_then_pass_is_flaky_and_retains_both_outcomes(context):
    rca = classify_failure(context, execution(), execution("PASS", evidence="base:pass"),
                           execution("PASS", evidence="head:rerun-pass"))
    assert rca.failure_class == FailureClass.TEST_FLAKY
    assert set(rca.evidence_ids) == {"head:test-empty", "head:rerun-pass"}
    assert "do not pass" in rca.next_action.lower()


def test_passing_different_suite_is_not_flaky_evidence(context):
    rca = classify_failure(context, execution(), rerun=execution("PASS", node="tests/other.py::test_other"))
    assert rca.failure_class == FailureClass.UNKNOWN


def test_preflight_spec_and_environment_blockers_prevent_regression_patch(context):
    ambiguous = context.model_copy(update={"spec": context.spec.model_copy(update={"conflicts": ("REQ-1 conflicts with REQ-2",)})})
    rca = classify_failure(ambiguous, execution(), execution("PASS"))
    assert rca.failure_class == FailureClass.SPEC_AMBIGUOUS
    assert "REQ-1" in rca.summary and rca.owner_role == "spec_owner"
    blocked = context.model_copy(update={"environment": EnvironmentManifest(id="cloud", prerequisites=(
        Prerequisite(name="test-database", available=False, remediation="Start the sandbox database."),))})
    rca = classify_failure(blocked, execution(), execution("PASS"))
    assert rca.failure_class == FailureClass.ENVIRONMENT_BLOCKED
    assert "database" in rca.summary and rca.owner_role == "environment_owner"


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "original"
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "src/service.py").write_text("def calculate(items):\n    return items[0]\n", encoding="utf-8")
    (root / "tests/test_service.py").write_text("def test_empty():\n    assert True\n", encoding="utf-8")
    return root


@pytest.mark.parametrize("relative", ["tests/test_service.py", "src/test_service.py", "src/service_test.py", "conftest.py",
                                      ".github/workflows/ci.py", "src/auth/login.py", "src/auth.py", "src/security.py",
                                      "src/policy.py", "src/requirements.py", "setup.py", "src/new.py",
                                      "../outside.py", "/tmp/outside.py", "C:/outside.py", "src\\service.py",
                                      "src/./service.py", "src//service.py", "src/service.py/"])
def test_patch_policy_rejects_protected_and_unsafe_paths_even_if_allowlisted(source, relative):
    with pytest.raises(PatchPolicyError):
        validate_patch({relative: "x = 1\n"}, (relative,), source)


def test_patch_exact_allowlist_and_no_source_deletion(source):
    with pytest.raises(PatchPolicyError, match="exact source allowlist"):
        validate_patch({"src/service.py": "x = 1\n"}, ("src/",), source)
    with pytest.raises(PatchPolicyError, match="deletion"):
        validate_patch({"src/service.py": ""}, ("src/service.py",), source)
    with pytest.raises(PatchPolicyError, match="Invalid Python"):
        validate_patch({"src/service.py": "def broken(:\n"}, ("src/service.py",), source)


def test_candidate_is_separate_and_original_tests_and_source_are_unchanged(source, tmp_path, monkeypatch):
    import subprocess
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("Proposal/materialization must not run arbitrary commands."))
    original = (source / "src/service.py").read_bytes()
    tests = (source / "tests/test_service.py").read_bytes()
    replacement = "def calculate(items):\n    return items[0] if items else 0\n"
    proposal = CandidatePatchProposal({"src/service.py": replacement}, "Restore empty-input behavior.", ["head:fail"])
    candidate = materialize_candidate(proposal, source, tmp_path / "candidate", ("src/service.py",))
    assert (source / "src/service.py").read_bytes() == original
    assert (candidate / "src/service.py").read_text(encoding="utf-8") == replacement
    assert (candidate / "tests/test_service.py").read_bytes() == tests
    assert not hasattr(proposal, "passed")
    with pytest.raises(PatchPolicyError, match="separate"):
        materialize_candidate(proposal, source, source / "candidate", ("src/service.py",))


def test_fixture_adapter_never_fixes_non_regression_and_has_no_verdict(context):
    adapter = FixtureRepairAdapter(changes={"src/service.py": "x = 0\n"})
    assert isinstance(adapter, RepairAdapter)
    regression = classify_failure(context, execution(), execution("PASS"))
    proposal = adapter.propose(context, execution(), regression)
    assert proposal.evidence_ids == ["head:test-empty"] and not hasattr(proposal, "readiness")
    unknown = classify_failure(context, execution())
    assert adapter.propose(context, execution(), unknown) is None
    assert adapter.propose(context, execution(), regression, attempt=2) is None


def test_unconfigured_adapter_and_pm_fallback_need_no_network(context):
    with pytest.raises(AdapterNotConfigured, match="No live repair provider"):
        UnconfiguredRepairAdapter().propose(context, execution())
    notifier = NullPMNotificationAdapter()
    notifier.notify("run-1", "engineer", {"action": "NEEDS_ENGINEER"})
    assert notifier.notifications == [{"run_id": "run-1", "owner_role": "engineer", "finding": {"action": "NEEDS_ENGINEER"}}]


def test_memory_redacts_traces_and_keeps_pinned_source_and_config(tmp_path):
    memory = EvolutionMemory(tmp_path / "memory")
    memory.append_run({"run_id": "run-1", "repo": "org/service", "reviewed_head_sha": "a" * 40,
                       "spec_hash": "b" * 64, "identity_hash": "c" * 64, "policy_version": "pilot-v1",
                       "api_key": "must-never-appear", "diagnostic": "Authorization: Bearer thisisasecret password=hunter2"})
    memory.append_review({"run_id": "run-1", "status": "APPROVED", "reviewer": "qa@example.test", "identity_hash": "c" * 64})
    memory.append_feedback({"run_id": "run-1", "kind": "RCA_CORRECTION", "comment": "secret=anothersecret"})
    text = memory.path.read_text(encoding="utf-8")
    for secret in ("must-never-appear", "thisisasecret", "hunter2", "anothersecret"):
        assert secret not in text
    events = memory.read_events()
    assert len(events) == 3
    assert events[0]["source_identity"]["reviewed_head_sha"] == "a" * 40
    assert events[0]["configuration"]["model_policy"] == "frozen"
    assert events[1]["source_identity"]["identity_hash"] == "c" * 64
    assert not (tmp_path / "SKILL.md").exists()


def test_redaction_covers_nested_credentials_urls_and_private_keys():
    value = redact({"nested": [{"access_token": "hide"}],
                    "log": "https://alice:password@example.test Bearer tokenvalue sk-abcdefghijklmnop",
                    "pem": "-----BEGIN PRIVATE KEY-----\nsensitive\n-----END PRIVATE KEY-----"})
    text = json.dumps(value)
    assert all(word not in text for word in ("hide", "alice", "password", "tokenvalue", "abcdefghijklmnop", "sensitive"))


def test_export_is_offline_reviewed_snapshot_and_preserves_original_evidence(tmp_path):
    memory = EvolutionMemory(tmp_path / "events.jsonl")
    memory.append_run({"run_id": "reviewed", "readiness": "CHANGES_REQUIRED", "tests": {"failed": 1}})
    memory.append_run({"run_id": "unreviewed", "tests": {"failed": 0}})
    memory.append_review("reviewed", {"reviewer": "human-qa", "status": "REJECTED"})
    before = memory.path.read_bytes()
    result = memory.export_offline_dataset(tmp_path / "offline.jsonl")
    assert result["examples"] == 1 and result["purpose"] == "OFFLINE_EVALUATION_ONLY"
    sample = json.loads((tmp_path / "offline.jsonl").read_text(encoding="utf-8"))
    assert sample["traces"][0]["payload"]["tests"]["failed"] == 1
    assert sample["eligible_for_live_activation"] is False and sample["split"] == "UNASSIGNED"
    assert memory.path.read_bytes() == before
    with pytest.raises(ValueError, match="overwrite"):
        memory.export_offline_dataset(memory.path)


def test_memory_detects_modified_event_and_serializes_parallel_threads(tmp_path):
    memory = EvolutionMemory(tmp_path / "events.jsonl")
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda i: memory.append_feedback(f"run-{i}", {"comment": str(i)}), range(30)))
    assert len(memory.read_events()) == 30
    event = json.loads(memory.path.read_text(encoding="utf-8").splitlines()[0])
    event["payload"]["comment"] = "modified"
    memory.path.write_text(json.dumps(event) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="integrity"):
        memory.read_events()


def test_promotion_plan_requires_named_review_evidence_canary_and_rollback(tmp_path):
    memory = EvolutionMemory(tmp_path / "events.jsonl")
    values = dict(candidate_version="skill-candidate-v2", candidate_sha256="a" * 64,
                  approved_by="human-owner", evaluation_evidence=["heldout-evaluation:sha256:b"],
                  canary_percentage=5, rollback_version="skill-v1", source_identity={"policy_version": "pilot-v1"})
    output = tmp_path / "promotion.json"
    manifest = memory.create_promotion_manifest(**values, destination=output)
    assert manifest["activated"] is False and manifest["requires_operator_activation"] is True
    assert manifest["model_weights_updated"] is False and manifest["skill_files_updated"] is False
    assert output.exists() and not (tmp_path / "SKILL.md").exists()
    for field, invalid in (("approved_by", ""), ("rollback_version", ""), ("evaluation_evidence", []),
                           ("canary_percentage", 100), ("candidate_sha256", "not-a-sha"), ("source_identity", {})):
        with pytest.raises(ValueError):
            memory.create_promotion_manifest(**{**values, field: invalid})
    with pytest.raises(ValueError, match="STALE"):
        memory.create_promotion_manifest(**values, expected_source_identity={"policy_version": "pilot-v2"})
