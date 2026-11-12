"""Prime MCP tool boundary using registered Git fixtures and real pytest."""
from __future__ import annotations

import asyncio
from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from pydantic import ValidationError

from app.models import Principal
from app.prime_tools import (StageTarget, TestStageTools, ToolBudget, ToolPolicy,
                             VerifyInput, load_operator_tools)
from app.providers import GOOD_SOURCE, SourceRegistry, git, make_demo_repository
from app.rca_and_repair import FixtureRepairAdapter
from app.run_manager import RunManager
from scripts.export_prime_schemas import contract


def system(tmp_path, *, regression=False, missing_env=False, ambiguous=False):
    repo, registration = make_demo_repository(tmp_path / "repo", regression=regression,
                                              missing_env=missing_env, ambiguous=ambiguous)
    registry = SourceRegistry(tmp_path / "state/sources")
    registry.register(registration)
    manager = RunManager(tmp_path / "state", registry=registry)
    policy = ToolPolicy(operator_name="bound-test-service", allowed_repos=("org/service",),
                        allowed_backends=("local_trusted",), max_runtime_seconds=60,
                        max_test_executions=4)
    principal = Principal(name=policy.operator_name, roles=frozenset({"engineer"}),
                          repos=frozenset(policy.allowed_repos))
    return TestStageTools(manager, principal, policy), repo


def target_for(tools):
    manager = tools.manager
    request = manager.registry.request_for_pr("org/service", 42)
    context = manager.registry.resolve(request)
    return StageTarget(repo="org/service", pr_number=42, expected_base_sha=context.base_sha,
        expected_head_sha=context.head_sha, spec_id=request.spec_id, spec_hash=request.spec_hash,
        test_profile_id=request.test_profile_id, test_profile_hash=context.profile.fingerprint,
        environment_manifest_id=request.environment_manifest_id,
        environment_fingerprint=context.environment.fingerprint,
        policy_version=context.policy_version, correlation_id="prime-tool-test")


def request_for(tools):
    return VerifyInput.model_validate({**target_for(tools).model_dump(mode="json"),
        "budget": ToolBudget(max_runtime_seconds=60).model_dump(mode="json")})


def test_preflight_and_verify_real_registered_pr_with_bound_evidence(tmp_path):
    tools, _ = system(tmp_path)
    target = target_for(tools)
    check = tools.preflight_pr(target)
    assert check.ready and check.next_owner_role == "test_owner"
    result = asyncio.run(tools.verify_pr(request_for(tools)))
    assert result.execution_status == "COMPLETED"
    assert result.readiness == "READY_FOR_QA" and result.next_owner_role == "qa"
    assert result.human_approval_required is True
    assert result.tests.passed == 2 and result.tests.failed == 0
    assert len(result.requirements) == 1 and result.requirements[0].result == "PASS"
    assert result.metrics.test_executions == 1 and result.metrics.model_spend_usd == 0
    assert result.artifact_manifest_hash and result.identity_hash == check.identity_hash
    assert result.candidate_patch is None
    assert tools.manager.get_run(result.run_id).qa_review.status == "PENDING"
    manifest = tools.manifest(result.run_id)
    assert manifest.artifact_manifest_hash == result.artifact_manifest_hash
    entry = next(e for e in manifest.manifest.evidence if e.path == "initial/junit.xml")
    artifact = tools.artifact(result.run_id, entry.path)
    assert artifact.sha256 == entry.sha256
    assert sha256(artifact.content_utf8.encode()).hexdigest() == entry.sha256
    assert "test_empty" in artifact.content_utf8
    again = asyncio.run(tools.verify_pr(request_for(tools)))
    assert again.run_id == result.run_id


@pytest.mark.parametrize("changed", ["expected_head_sha", "expected_base_sha", "spec_hash",
    "test_profile_hash", "environment_fingerprint", "policy_version", "test_profile_id",
    "environment_manifest_id"])
def test_stale_or_mismatched_handoff_is_rejected_before_run(tmp_path, changed):
    tools, _ = system(tmp_path)
    target = target_for(tools).model_dump(mode="json")
    target[changed] = "f" * (40 if changed in {"expected_head_sha", "expected_base_sha"}
                               else 64) if changed not in {
                                   "policy_version", "test_profile_id", "environment_manifest_id"} else "wrong"
    with pytest.raises(ValueError, match="STALE"):
        asyncio.run(tools.verify_pr(VerifyInput.model_validate({**target,
            "budget": ToolBudget(max_runtime_seconds=60).model_dump(mode="json")})))
    assert not tools.manager.runs


def test_head_change_between_handoff_and_execution_is_rejected(tmp_path):
    tools, repo = system(tmp_path)
    target = target_for(tools)
    source = repo / "src/service.py"
    source.write_text(source.read_text(encoding="utf-8") + "\n# newer head\n", encoding="utf-8")
    git(repo, "add", "src/service.py")
    git(repo, "commit", "-m", "New version")
    with pytest.raises(ValueError, match="STALE"):
        tools.preflight_pr(target)


def test_missing_prerequisite_blocks_without_testing_or_patch(tmp_path):
    tools, _ = system(tmp_path, missing_env=True)
    check = tools.preflight_pr(target_for(tools))
    assert not check.ready and check.next_owner_role == "environment_owner"
    result = asyncio.run(tools.verify_pr(request_for(tools)))
    assert result.execution_status == "BLOCKED" and result.readiness == "BLOCKED"
    assert result.next_owner_role == "environment_owner"
    assert result.metrics.test_executions == result.metrics.debug_iterations == 0
    assert result.candidate_patch is None


def test_ambiguous_specification_routes_to_spec_owner(tmp_path):
    tools, _ = system(tmp_path, ambiguous=True)
    result = asyncio.run(tools.verify_pr(request_for(tools)))
    assert result.readiness == "BLOCKED" and result.next_owner_role == "spec_owner"
    assert result.metrics.test_executions == 0


def test_regression_cannot_be_approved_or_patched_without_operator_budget(tmp_path):
    tools, _ = system(tmp_path, regression=True)
    result = asyncio.run(tools.verify_pr(request_for(tools)))
    assert result.readiness == "CHANGES_REQUIRED"
    assert result.next_owner_role == "code_agent" and result.candidate_patch is None
    assert result.metrics.debug_iterations == 0
    assert tools.manager.repair_adapter is None
    assert tools.manager.get_run(result.run_id).qa_review.status == "PENDING"


def test_operator_enabled_candidate_remains_separate_from_original_pr(tmp_path):
    tools, _ = system(tmp_path, regression=True)
    tools.manager.repair_adapter = FixtureRepairAdapter(changes={"src/service.py": GOOD_SOURCE})
    tools = TestStageTools(tools.manager, tools.principal,
        tools.policy.model_copy(update={"max_debug_iterations": 1}))
    request = request_for(tools).model_copy(update={"budget": ToolBudget(
        max_debug_iterations=1, max_runtime_seconds=60)})
    result = asyncio.run(tools.verify_pr(request))
    assert result.readiness == "CHANGES_REQUIRED" and result.next_owner_role == "code_agent"
    assert result.candidate_patch is not None and result.candidate_patch.validated is True
    assert result.candidate_patch.accepted is False
    assert result.candidate_patch.original_head_sha == result.reviewed_head_sha
    assert tools.manager.get_run(result.run_id).qa_review.status == "PENDING"


@pytest.mark.parametrize("budget", [
    {"max_debug_iterations": 1}, {"max_runtime_seconds": 61},
    {"max_test_executions": 3}, {"max_llm_cost_usd": .01},
])
def test_operator_budget_cap_rejects_before_side_effects(tmp_path, budget):
    tools, _ = system(tmp_path)
    tools = TestStageTools(tools.manager, tools.principal,
                           tools.policy.model_copy(update={"max_test_executions": 2}))
    requested = ToolBudget.model_validate({**ToolBudget(max_runtime_seconds=60,
        max_test_executions=2).model_dump(), **budget})
    request = request_for(tools).model_copy(update={"budget": requested})
    with pytest.raises(PermissionError, match="budget"):
        asyncio.run(tools.verify_pr(request))
    assert not tools.manager.runs


def test_repository_scope_and_backend_are_enforced_before_execution(tmp_path, monkeypatch):
    tools, _ = system(tmp_path)
    target = target_for(tools).model_copy(update={"repo": "other/repo"})
    def forbidden_lookup(*args):
        pytest.fail("Unauthorized repository was read")
    monkeypatch.setattr(tools.manager.registry, "request_for_pr", forbidden_lookup)
    with pytest.raises(PermissionError, match="outside"):
        tools.preflight_pr(target)
    assert not tools.manager.runs
    monkeypatch.undo()
    with pytest.raises(PermissionError, match="backend"):
        TestStageTools(tools.manager, tools.principal,
            ToolPolicy(operator_name=tools.principal.name, allowed_repos=("org/service",)))\
            .preflight_pr(target_for(tools))


@pytest.mark.parametrize("invalid", [{"role": "qa"}, {"command": "pytest -x"},
    {"backend": "local_trusted"},
    {"repo_root": "C:/private"}, {"approval": "APPROVED"}, {"max_runtime_seconds": 9999}])
def test_model_cannot_supply_permissions_commands_or_approval(tmp_path, invalid):
    tools, _ = system(tmp_path)
    with pytest.raises(ValidationError):
        VerifyInput.model_validate({**request_for(tools).model_dump(mode="json"), **invalid})


def test_missing_or_excessive_operator_identity_fails_closed(tmp_path):
    tools, _ = system(tmp_path)
    with pytest.raises(PermissionError):
        TestStageTools(tools.manager, None, tools.policy)
    with pytest.raises(PermissionError):
        TestStageTools(tools.manager, Principal(name="qa", roles=frozenset({"qa"})), tools.policy)
    with pytest.raises(PermissionError):
        TestStageTools(tools.manager, Principal(name="bound-test-service",
            roles=frozenset({"engineer"}), repos=frozenset({"*"})), tools.policy)
    with pytest.raises(ValidationError):
        ToolPolicy(operator_name="service", allowed_repos=("*",))


@pytest.mark.parametrize("name", ["../state.json", "initial/../pytest.log", "initial\\pytest.log",
    "/initial/junit.xml", "verification.json"])
def test_artifacts_are_allowlisted_and_paths_cannot_escape(tmp_path, name):
    tools, _ = system(tmp_path)
    result = asyncio.run(tools.verify_pr(request_for(tools)))
    with pytest.raises((ValueError, PermissionError)):
        tools.artifact(result.run_id, name)


def test_damaged_evidence_and_transfer_ceiling_fail_closed(tmp_path):
    tools, _ = system(tmp_path)
    result = asyncio.run(tools.verify_pr(request_for(tools)))
    path = tools.manager.artifact_path(result.run_id, "initial/pytest.log")
    path.write_text("forged passing evidence", encoding="utf-8")
    with pytest.raises(ValueError, match="checksum"):
        tools.artifact(result.run_id, "initial/pytest.log")
    limited = TestStageTools(tools.manager, tools.principal,
        tools.policy.model_copy(update={"max_artifact_bytes": 1}))
    with pytest.raises(ValueError, match="transfer limit"):
        limited.artifact(result.run_id, "initial/junit.xml")


def test_manifest_named_artifact_cannot_be_replaced_by_link(tmp_path):
    tools, _ = system(tmp_path)
    result = asyncio.run(tools.verify_pr(request_for(tools)))
    original = tools.manager.artifact_path(result.run_id, "initial/junit.xml").parent
    real = original.with_name("initial-real")
    assert original.resolve().is_relative_to(tmp_path.resolve())
    assert real.resolve().is_relative_to(tmp_path.resolve())
    original.rename(real)
    if os.name == "nt":
        subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(original), str(real)],
                       check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    else:
        original.symlink_to(real, target_is_directory=True)
    with pytest.raises(ValueError, match="[Ll]ink"):
        tools.artifact(result.run_id, "initial/junit.xml")


def test_operator_configuration_is_explicit_and_outside_agent_project(tmp_path, monkeypatch):
    tools, _ = system(tmp_path)
    policy_path = tmp_path / "operator-policy.json"
    policy_path.write_text(tools.policy.model_dump_json(), encoding="utf-8")
    loaded = load_operator_tools(tools.manager.storage, policy_path)
    assert loaded.policy == tools.policy
    with pytest.raises(ValueError, match="absolute"):
        load_operator_tools(tools.manager.storage, policy_path.name)
    project_policy = Path(__file__).resolve().parents[1] / "schemas/prime-test-permissions-v1.json"
    with pytest.raises(PermissionError, match="outside the agent project"):
        load_operator_tools(tools.manager.storage, project_policy)
    if importlib.util.find_spec("mcp") is None:
        return  # The stdio adapter is an optional installation extra.
    from app.prime_mcp import main
    monkeypatch.delenv("EVO_TEST_DATA_DIR", raising=False)
    monkeypatch.delenv("EVO_TEST_POLICY_FILE", raising=False)
    with pytest.raises(RuntimeError, match="Operator must configure"):
        main()


def test_prime_mcp_exposes_only_four_scoped_tools(tmp_path):
    pytest.importorskip("mcp")
    from app.prime_mcp import build_server
    tools, _ = system(tmp_path)
    server = build_server(tools)
    async def call():
        names = {tool.name for tool in await server.list_tools()}
        assert names == {"test_preflight", "test_verify", "test_manifest", "test_artifact"}
        check = await server.call_tool("test_preflight",
            {"target": target_for(tools).model_dump(mode="json")})
        assert check.is_error is not True and check.structured_content["ready"] is True
        run = await server.call_tool("test_verify",
            {"request": request_for(tools).model_dump(mode="json")})
        assert run.is_error is not True and run.structured_content["readiness"] == "READY_FOR_QA"
        run_id = run.structured_content["run_id"]
        manifest = await server.call_tool("test_manifest", {"run_id": run_id})
        assert manifest.is_error is not True
        assert manifest.structured_content["manifest"]["run_id"] == run_id
        artifact = await server.call_tool("test_artifact",
            {"run_id": run_id, "name": "initial/junit.xml"})
        assert artifact.is_error is not True
        assert "test_empty" in artifact.structured_content["content_utf8"]
    asyncio.run(call())


def test_checked_in_contracts_and_permissions_match_runtime_models():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    exported = json.loads((root / "schemas/prime-test-tools-v1.json").read_text(encoding="utf-8"))
    permissions = json.loads((root / "schemas/prime-test-permissions-v1.json").read_text(encoding="utf-8"))
    assert exported == contract()
    assert set(exported["tools"]) == set(permissions["tool_access"])
    assert exported["tools"]["test_verify"]["input"]["additionalProperties"] is False
    assert permissions["execution"]["default_allowed_backends"] == ["docker"]


@pytest.mark.parametrize("changes", [
    {"spec_hash": "invalid"}, {"pr_number": True},
    {"budget": ToolBudget().model_copy(update={"max_debug_iterations": 99})},
])
def test_unvalidated_model_copies_are_revalidated_before_registry_reads(tmp_path, monkeypatch, changes):
    tools, _ = system(tmp_path)
    request = request_for(tools).model_copy(update=changes)
    def forbidden_lookup(*args):
        pytest.fail("Malformed input reached the registry")
    monkeypatch.setattr(tools.manager.registry, "request_for_pr", forbidden_lookup)
    with pytest.raises(ValidationError):
        asyncio.run(tools.verify_pr(request))
    assert not tools.manager.runs


def test_missing_docker_backend_never_falls_back_to_host_pytest(tmp_path, monkeypatch):
    tools, _ = system(tmp_path)
    registry = tools.manager.registry
    registration = registry.registrations[("org/service", 42)]
    profile = registration.profile.model_copy(update={"backend": "docker", "trusted": False,
        "docker_image": "evo-pytest@sha256:" + "a" * 64})
    registry.register(registration.model_copy(update={"profile": profile}))
    tools = TestStageTools(tools.manager, tools.principal,
        tools.policy.model_copy(update={"allowed_backends": ("docker",)}))
    monkeypatch.setattr("app.preflight.shutil.which", lambda name: "docker")
    commands = []
    async def unavailable(*command, **options):
        commands.append(command)
        assert command[0] == "docker" and "--network" in command
        assert command[command.index("--network") + 1] == "none"
        assert "--read-only" in command and "--pull=never" in command
        assert command[command.index("--cap-drop") + 1] == "ALL"
        assert "no-new-privileges" in command
        assert "shell" not in options
        assert not any("TOKEN" in name or "SECRET" in name for name in options["env"])
        raise OSError("Docker daemon unavailable in the execution sandbox")
    monkeypatch.setattr("app.test_runner.asyncio.create_subprocess_exec", unavailable)
    result = asyncio.run(tools.verify_pr(request_for(tools)))
    assert len(commands) == 1
    assert result.readiness == "INCONCLUSIVE" and result.candidate_patch is None
    assert tools.manager.get_run(result.run_id).qa_review.status == "PENDING"
    assert "Docker daemon unavailable" in tools.artifact(result.run_id, "initial/pytest.log").content_utf8


def test_retry_after_restart_reuses_evidence_and_reads_old_store_format(tmp_path, monkeypatch):
    tools, _ = system(tmp_path)
    request = request_for(tools)
    first = asyncio.run(tools.verify_pr(request))
    state_path = tools.manager.storage / "state.json"
    old_state = json.loads(state_path.read_text(encoding="utf-8"))
    old_state["delivery_runs"] = {"retired-webhook": first.run_id}
    state_path.write_text(json.dumps(old_state), encoding="utf-8")
    manager = RunManager(tools.manager.storage, registry=tools.manager.registry)
    loaded = TestStageTools(manager, tools.principal, tools.policy)
    async def forbidden_execution(*args):
        pytest.fail("Idempotent Harness retry started new execution")
    monkeypatch.setattr(manager, "execute", forbidden_execution)
    retried = asyncio.run(loaded.verify_pr(request))
    assert retried.run_id == first.run_id
    assert retried.artifact_manifest_hash == first.artifact_manifest_hash
    assert not hasattr(manager, "delivery_runs")
    manager.persist()
    assert "delivery_runs" not in json.loads(state_path.read_text(encoding="utf-8"))


def test_stdio_mcp_process_uses_operator_policy_not_model_identity(tmp_path):
    pytest.importorskip("mcp")
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    tools, _ = system(tmp_path)
    policy = tmp_path / "policy.json"
    policy.write_text(tools.policy.model_dump_json(), encoding="utf-8")
    process = StdioServerParameters(command=sys.executable, args=["-m", "app.prime_mcp"],
        cwd=str(Path(__file__).resolve().parents[1]),
        env={**os.environ, "EVO_TEST_DATA_DIR": str(tools.manager.storage),
             "EVO_TEST_POLICY_FILE": str(policy)})

    async def invoke():
        async with stdio_client(process) as (reader, writer):
            async with ClientSession(reader, writer) as client:
                await client.initialize()
                names = {tool.name for tool in (await client.list_tools()).tools}
                assert names == {"test_preflight", "test_verify", "test_manifest", "test_artifact"}
                result = await client.call_tool("test_preflight",
                    {"target": target_for(tools).model_dump(mode="json")})
                assert result.is_error is not True
                assert result.structured_content["ready"] is True

    asyncio.run(invoke())
