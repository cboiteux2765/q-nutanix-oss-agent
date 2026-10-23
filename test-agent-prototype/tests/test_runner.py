"""Exercise actual pytest subprocesses and preflight safety boundaries."""
import asyncio
from pathlib import Path
import time

import pytest

from app.models import (
    AcceptanceTest, EnvironmentManifest, Prerequisite, Requirement,
    RequirementOutcome, ResolvedContext, SpecDocument, TestCase, TestExecution,
    TestProfile, canonical_hash,
)
from app.preflight import preflight, prepare_workspace, validate_test_path
from app.test_runner import execute, redact_log, requirement_results


def context_for(root: Path, *, profile=None, environment=None, spec_updates=None):
    spec = SpecDocument(spec_id="SPEC-1", intent="Return zero for empty input.",
        requirements=(Requirement(id="REQ-1", description="Empty input is zero", test_ids=("AT-1",)),),
        acceptance_tests=(AcceptanceTest(id="AT-1", node_id="tests/test_sample.py::test_empty",
                                         expected_behavior="Empty input returns zero"),))
    if spec_updates:
        spec = spec.model_copy(update=spec_updates)
    return ResolvedContext(repo="org/service", pr_number=1, base_sha="a" * 40,
        head_sha="b" * 40, snapshot_root=root, base_snapshot_root=root, spec=spec,
        spec_uri="fixture://spec", spec_hash=canonical_hash(spec),
        profile=profile or TestProfile(trusted=True, backend="local_trusted"),
        environment=environment or EnvironmentManifest(id="local"))


def fixture_tree(tmp_path, source):
    snapshot = tmp_path / "snapshot"
    (snapshot / "tests").mkdir(parents=True)
    (snapshot / "tests" / "test_sample.py").write_text(source, encoding="utf-8")
    return snapshot


def run(context, workspace, evidence, timeout=10, cancel=None):
    async def invoke():
        return await execute(context, workspace, evidence, cancel or asyncio.Event(), timeout)
    return asyncio.run(invoke())


def test_real_pass_creates_junit_and_requirement_evidence(tmp_path):
    snapshot = fixture_tree(tmp_path, "def test_empty():\n    assert sum([]) == 0\n")
    context = context_for(snapshot)
    assert preflight(context).ok
    workspace = prepare_workspace(snapshot, tmp_path / "workspace")
    observation = run(context, workspace, tmp_path / "evidence")
    assert observation.exit_code == 0
    assert observation.passed == 1 and observation.failed == 0
    assert observation.trusted and not observation.blocked_reason
    assert observation.cases[0].node_id == "tests/test_sample.py::test_empty"
    result = requirement_results(context.spec, [observation])[0]
    assert result.result == RequirementOutcome.PASS and result.evidence_ids
    assert observation.junit_path.is_file() and observation.log_path.is_file()


def test_real_failure_is_not_overridden_and_credentials_are_redacted(tmp_path, monkeypatch):
    secret = "runner-test-secret-042"
    monkeypatch.setenv("TEST_AGENT_TEST_SECRET", secret)
    source = ("def test_empty():\n"
              "    import os\n"
              "    assert 'TEST_AGENT_TEST_SECRET' not in os.environ\n"
              f"    raise AssertionError('password={secret}')\n")
    snapshot = fixture_tree(tmp_path, source)
    context = context_for(snapshot)
    observation = run(context, prepare_workspace(snapshot, tmp_path / "workspace"), tmp_path / "evidence")
    assert observation.exit_code == 1 and observation.failed == 1
    assert requirement_results(context.spec, [observation])[0].result == RequirementOutcome.FAIL
    assert secret not in observation.log_path.read_text(encoding="utf-8")
    assert secret not in observation.junit_path.read_text(encoding="utf-8")
    assert "[REDACTED]" in observation.log_path.read_text(encoding="utf-8")


def test_skipped_acceptance_is_never_verified(tmp_path):
    snapshot = fixture_tree(tmp_path, "import pytest\n@pytest.mark.skip(reason='missing fixture')\ndef test_empty():\n    pass\n")
    context = context_for(snapshot)
    observation = run(context, prepare_workspace(snapshot, tmp_path / "workspace"), tmp_path / "evidence")
    assert observation.exit_code == 0 and observation.skipped == 1
    assert requirement_results(context.spec, [observation])[0].result == RequirementOutcome.NOT_VERIFIED


def test_timeout_terminates_subprocess_and_keeps_partial_log(tmp_path):
    snapshot = fixture_tree(tmp_path, "import time\ndef test_empty():\n    time.sleep(30)\n")
    context = context_for(snapshot)
    started = time.monotonic()
    observation = run(context, prepare_workspace(snapshot, tmp_path / "workspace"), tmp_path / "evidence", timeout=0.5)
    assert observation.timed_out and not observation.cancelled
    assert time.monotonic() - started < 10
    assert "deadline exceeded" in observation.log_path.read_text(encoding="utf-8")
    assert requirement_results(context.spec, [observation])[0].result == RequirementOutcome.NOT_VERIFIED


def test_active_cancellation_terminates_execution(tmp_path):
    snapshot = fixture_tree(tmp_path, "import time\ndef test_empty():\n    time.sleep(30)\n")
    context = context_for(snapshot)
    workspace = prepare_workspace(snapshot, tmp_path / "workspace")

    async def scenario():
        cancelled = asyncio.Event()
        task = asyncio.create_task(execute(context, workspace, tmp_path / "evidence", cancelled, 30))
        await asyncio.sleep(0.5)
        cancelled.set()
        return await asyncio.wait_for(task, 10)

    observation = asyncio.run(scenario())
    assert observation.cancelled and not observation.timed_out
    assert "cancelled" in observation.log_path.read_text(encoding="utf-8")


def test_cancellation_kills_spawned_child_process_tree(tmp_path):
    source = ("import subprocess,sys,time,pathlib\n"
              "def test_empty():\n"
              "    child = subprocess.Popen([sys.executable, '-c', "
              "\"import time,pathlib; time.sleep(1.5); pathlib.Path('child_survived.txt').write_text('unsafe')\"])\n"
              "    pathlib.Path('child.pid').write_text(str(child.pid))\n"
              "    time.sleep(30)\n")
    snapshot = fixture_tree(tmp_path, source)
    context = context_for(snapshot)
    workspace = prepare_workspace(snapshot, tmp_path / "workspace")

    async def scenario():
        cancelled = asyncio.Event()
        task = asyncio.create_task(execute(context, workspace, tmp_path / "evidence", cancelled, 15))
        deadline = time.monotonic() + 5
        while not (workspace / "child.pid").exists() and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        assert (workspace / "child.pid").exists(), "Fixture must actually spawn a child before cancellation"
        cancelled.set()
        observation = await asyncio.wait_for(task, 10)
        await asyncio.sleep(2)
        return observation

    observation = asyncio.run(scenario())
    assert observation.cancelled
    assert not (workspace / "child_survived.txt").exists()


def test_untrusted_code_is_never_executed_by_local_backend(tmp_path):
    snapshot = fixture_tree(tmp_path, "def test_empty():\n    assert True\n")
    context = context_for(snapshot, profile=TestProfile(trusted=False, backend="local_trusted"))
    assert not preflight(context).ok
    observation = run(context, prepare_workspace(snapshot, tmp_path / "workspace"), tmp_path / "evidence")
    assert not observation.trusted and observation.command == []
    assert "rejects untrusted" in observation.blocked_reason


@pytest.mark.parametrize("target", ["--override-ini=addopts=-x", "../tests", "C:/outside.py", "/tmp/a.py", "tests/../../x.py", "tests/a.py\n--collect-only"])
def test_targets_reject_cli_options_and_path_escape(target):
    with pytest.raises(ValueError):
        validate_test_path(target)


def test_environment_block_routes_to_declared_owner(tmp_path):
    snapshot = fixture_tree(tmp_path, "def test_empty():\n    assert True\n")
    manifest = EnvironmentManifest(id="missing", prerequisites=(Prerequisite(
        name="sandbox_db", available=False, owner_role="platform_engineer",
        remediation="Provision sandbox_db in the test namespace."),))
    result = preflight(context_for(snapshot, environment=manifest))
    assert not result.ok
    finding = next(item for item in result.findings if item.code == "ENVIRONMENT_BLOCKED")
    assert finding.owner_role == "platform_engineer" and "Provision" in finding.remediation


def test_ambiguous_and_incomplete_spec_are_blocked(tmp_path):
    snapshot = fixture_tree(tmp_path, "def test_empty():\n    assert True\n")
    context = context_for(snapshot, spec_updates={"conflicts": ("REQ-1: zero versus error",)})
    findings = preflight(context).findings
    assert any(f.code == "SPEC_AMBIGUOUS" and "REQ-1" in f.message for f in findings)
    context = context_for(snapshot, spec_updates={"acceptance_tests": ()})
    assert not preflight(context).ok


def test_latest_execution_cannot_combine_partial_requirement_passes(tmp_path):
    snapshot = fixture_tree(tmp_path, "def test_empty():\n    assert True\n")
    context = context_for(snapshot)
    failed = TestExecution(exit_code=1, failed=1, cases=[TestCase(
        node_id="tests/test_sample.py::test_empty", outcome="failed", evidence_id="initial/junit.xml")])
    unrelated_pass = TestExecution(exit_code=0, passed=1, cases=[TestCase(
        node_id="tests/test_sample.py::test_other", outcome="passed", evidence_id="candidate/junit.xml")])
    assert requirement_results(context.spec, [failed])[0].result == RequirementOutcome.FAIL
    assert requirement_results(context.spec, [failed, unrelated_pass])[0].result == RequirementOutcome.NOT_VERIFIED


def test_workspace_copy_requires_new_destination(tmp_path):
    snapshot = fixture_tree(tmp_path, "def test_empty():\n    assert True\n")
    workspace = prepare_workspace(snapshot, tmp_path / "workspace")
    with pytest.raises(ValueError, match="must be new"):
        prepare_workspace(snapshot, workspace)
    assert (snapshot / "tests/test_sample.py").read_text() == (workspace / "tests/test_sample.py").read_text()
    with pytest.raises(ValueError, match="outside the source"):
        prepare_workspace(snapshot, snapshot / "nested")


def test_common_secret_formats_are_redacted():
    original = "password=abc123 token: pretend-token Authorization: Bearer sample-token-123"
    sanitized = redact_log(original)
    assert "abc123" not in sanitized and "pretend-token" not in sanitized
    assert "sample-token-123" not in sanitized
