"""Real main RPC/REPL integration with real pytest and deterministic model replies."""
import asyncio
from hashlib import sha256
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from evo.test_stage.models import Principal, ReviewRequest
from evo.test_stage_demo import prepare_demo
from evo.tools import TOOL_MANIFEST

from .conftest import python_call, reply, settle


@pytest.fixture
def demo(tmp_path):
    runtime, store, agents, request = prepare_demo(tmp_path)
    yield runtime, store, agents, request
    asyncio.run(runtime.close())
    store.close()


async def call(demo, name, *args, role="orchestrator", **kwargs):
    runtime, _, agents, _ = demo
    return await runtime.broker.dispatch(agents[role]["id"], name, list(args), kwargs)


FLOW = """
import json
t = test.target()
p = test.preflight(t)
v = test.verify(t)
m = test.manifest(v['run_id'])
a = test.artifact(v['run_id'], 'initial/junit.xml')
assert p['ready'] and v['readiness'] == 'READY_FOR_QA'
assert v['reviewed_head_sha'] == t['expected_head_sha']
assert m['manifest']['identity_hash'] == v['identity_hash']
assert 'test_empty' in a['content_utf8']
shared.set('verified', v)
print(json.dumps({'passed': v['tests']['passed'], 'artifact': v['main_artifact_id']}))
"""


def test_main_agent_model_loop_calls_registered_tools_via_real_repl(demo, fake_provider):
    runtime, store, agents, request = demo
    # The model is scripted; Runtime, child REPL, RPC, runner and evidence are real.
    fake_provider.script = [python_call(FLOW),
        python_call("print(v['reviewed_head_sha'], len(m['manifest']['evidence']))", "cell-2"),
        reply("Verification completed; human QA is pending.")]

    async def scenario():
        root = agents["orchestrator"]
        runtime._enqueue(root, "Verify the host-bound PR with test tools.", user=True)
        runtime._schedule(root["id"])
        await settle(runtime, timeout=60)
        await runtime.close()
    asyncio.run(scenario())
    root = store.get("agents", agents["orchestrator"]["id"])
    assert root["status"] == "waiting", root
    messages = store.messages(root["id"])
    results = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
    assert len(results) == 2 and all(r["ok"] for r in results), results
    assert request.expected_head_sha in results[1]["stdout"]
    events = store.events("test-tool-demo")
    names = {e["data"].get("name") for e in events if e["kind"] == "tool.finished"}
    assert {"test.target", "test.preflight", "test.verify", "test.manifest", "test.artifact"} <= names
    record = store.list("test_stage_results", run_id="test-tool-demo")[0]
    bundle = json.loads(runtime.artifacts.read(record["artifact_id"])["content"])
    assert bundle["verification"]["tests"]["passed"] == 2
    assert bundle["verification"]["human_approval_required"] is True
    assert store.get("runs", "test-tool-demo")["phase"] == "test"


def test_tool_manifest_and_full_result_evidence_flow(demo):
    assert {"test.target", "test.preflight", "test.verify", "test.manifest", "test.artifact"} <= TOOL_MANIFEST.keys()

    async def scenario():
        request = await call(demo, "test.target")
        assert (await call(demo, "test.preflight", request))["ready"]
        result = await call(demo, "test.verify", request)
        manifest = await call(demo, "test.manifest", result["run_id"])
        for entry in manifest["manifest"]["evidence"]:
            artifact = await call(demo, "test.artifact", result["run_id"], entry["path"])
            assert sha256(artifact["content_utf8"].encode()).hexdigest() == entry["sha256"]
        again = await call(demo, "test.verify", request)
        assert again["run_id"] == result["run_id"]
        assert again["main_artifact_id"] == result["main_artifact_id"]
        return result
    result = asyncio.run(scenario())
    assert result["tests"]["passed"] == 2 and result["requirements"][0]["result"] == "PASS"
    assert result["metrics"]["test_executions"] == 1 and result["candidate_patch"] is None
    assert result["next_owner_role"] == "qa"


@pytest.mark.parametrize("field,value", [
    ("expected_head_sha", "f" * 40), ("spec_hash", "f" * 64),
    ("environment_fingerprint", "f" * 64), ("test_profile_hash", "f" * 64),
    ("environment_manifest_id", "unapproved"), ("correlation_id", "other-main-run"),
    ("budget", {"max_runtime_seconds": 61}),
])
def test_unapproved_pins_and_budget_rejected_before_runner(demo, monkeypatch, field, value):
    from evo.test_stage import run_manager
    async def forbidden(*args, **kwargs):
        pytest.fail("Test subprocess must not be spawned for an unapproved request")
    monkeypatch.setattr(run_manager, "run_pytest", forbidden)
    target = demo[3].model_dump(mode="json") | {field: value}
    with pytest.raises(ValueError, match="STALE"):
        asyncio.run(call(demo, "test.verify", target))
    assert demo[0].test_stage.tools.manager.runs == {}


@pytest.mark.parametrize("field,value", [("role", "qa"), ("approved", True),
    ("command", "echo hacked"), ("path", "../secrets.env"), ("pr_number", True)])
def test_model_supplied_authority_and_malformed_inputs_rejected(demo, field, value):
    with pytest.raises(ValidationError):
        asyncio.run(call(demo, "test.verify", demo[3].model_dump(mode="json") | {field: value}))
    assert not demo[0].test_stage.tools.manager.runs


def test_missing_host_binding_and_wrong_stored_role_fail_closed(demo):
    with pytest.raises(PermissionError, match="host-recorded"):
        asyncio.run(call(demo, "test.target", role="code"))
    runtime = demo[0]
    runtime.test_stage = None
    with pytest.raises(PermissionError, match="not configured"):
        asyncio.run(call(demo, "test.target"))


@pytest.mark.parametrize("name,args", [("shell.run", ["echo hacked"]),
    ("repo.write", ["tests/test_service.py", ""]), ("repo.apply_patch", ["patch"]),
    ("repo.commit", ["bypass"]), ("github.create_draft", ["title", "body"])])
def test_bound_verification_cannot_bypass_runner_with_host_write_tools(demo, name, args):
    with pytest.raises(PermissionError, match="approved Test-stage"):
        asyncio.run(call(demo, name, *args, role="test"))


@pytest.mark.parametrize("field,value", [("implementation_commit", "f" * 40), ("phase", "code")])
def test_main_run_changes_invalidate_binding(demo, field, value):
    demo[1].put("runs", "test-tool-demo", {field: value})
    with pytest.raises((ValueError, PermissionError)):
        asyncio.run(call(demo, "test.verify", demo[3].model_dump(mode="json")))
    assert not demo[0].test_stage.tools.manager.runs


def test_changed_main_spec_and_registry_environment_rejected(demo):
    runtime, store, _, request = demo
    binding = runtime.test_stage._bindings["test-tool-demo"]
    artifact = runtime.artifacts.read(binding.spec_artifact_id)
    path = runtime.artifacts.root / artifact["id"]
    original = path.read_text(encoding="utf-8")
    path.write_text(original.replace("Summarize", "Changed"), encoding="utf-8")
    with pytest.raises(ValueError, match="specification hash"):
        asyncio.run(call(demo, "test.verify", request.model_dump(mode="json")))
    path.write_text(original, encoding="utf-8")
    registry = runtime.test_stage.tools.manager.registry
    entry = registry.registrations[(request.repo, request.pr_number)]
    registry.register(entry.model_copy(update={"environment": entry.environment.model_copy(update={"id": "changed"})}))
    with pytest.raises(ValueError, match="STALE"):
        asyncio.run(call(demo, "test.verify", request.model_dump(mode="json")))
    assert not runtime.test_stage.tools.manager.runs


@pytest.mark.parametrize("options,readiness,owner", [
    ({"missing_env": True}, "BLOCKED", "environment_owner"),
    ({"ambiguous": True}, "BLOCKED", "spec_owner"),
    ({"regression": True}, "CHANGES_REQUIRED", "code_agent"),
])
def test_blockers_and_real_code_regression_return_to_main(tmp_path, options, readiness, owner):
    demo = prepare_demo(tmp_path, **options)
    runtime, store, _, request = demo
    try:
        result = asyncio.run(call(demo, "test.verify", request.model_dump(mode="json")))
        assert result["readiness"] == readiness and result["next_owner_role"] == owner
        assert result["candidate_patch"] is None and result["metrics"]["debug_iterations"] == 0
        if readiness == "BLOCKED":
            assert result["metrics"]["test_executions"] == 0
        else:
            assert result["findings"][0]["failure_class"] == "CODE_REGRESSION"
        assert json.loads(runtime.artifacts.read(result["main_artifact_id"])["content"])["verification"]["readiness"] == readiness
    finally:
        asyncio.run(runtime.close())
        store.close()


def test_artifact_escape_and_cross_run_evidence_rejected(demo):
    runtime, store, agents, request = demo
    async def scenario():
        result = await call(demo, "test.verify", request.model_dump(mode="json"))
        for name in ("../secrets.env", "tests/test_service.py", "verification.json", "C:/secrets.env"):
            with pytest.raises((ValueError, PermissionError)):
                await call(demo, "test.artifact", result["run_id"], name)
        store.put("test_stage_results", result["run_id"], {"run_id": "another-main-run"})
        with pytest.raises(PermissionError, match="another main run"):
            await call(demo, "test.manifest", result["run_id"])
    asyncio.run(scenario())


def passing_handoff(demo, artifact_id, *, phase="test"):
    runtime, _, agents, request = demo
    return runtime.workflow.handoff(agents[phase]["id"], "qa" if phase == "test" else "ready",
        "Bound verification", [artifact_id], request.expected_head_sha, True)


def test_test_handoff_consumes_actual_evidence_and_qa_remains_human(demo):
    runtime, store, agents, request = demo
    result = asyncio.run(call(demo, "test.verify", request.model_dump(mode="json")))
    fake = runtime.artifacts.write("test-tool-demo", agents["test"]["id"], "fake.json", '{"passed":true}')
    handoff = passing_handoff(demo, fake["id"])
    with pytest.raises(PermissionError, match="host-imported"):
        runtime.workflow.advance(agents["orchestrator"]["id"], "qa", handoff["id"])
    handoff = passing_handoff(demo, result["main_artifact_id"])
    status = runtime.workflow.advance(agents["orchestrator"]["id"], "qa", handoff["id"])
    assert status["phase"] == "qa" and store.get("runs", "test-tool-demo")["tested_commit"] == request.expected_head_sha
    handoff = passing_handoff(demo, result["main_artifact_id"], phase="qa")
    with pytest.raises(PermissionError, match="human QA approval"):
        runtime.workflow.advance(agents["orchestrator"]["id"], "ready", handoff["id"])
    # Simulate an independently authenticated human action on the host, never a tool.
    runtime.test_stage.tools.manager.review_run(result["run_id"], ReviewRequest(decision="APPROVED",
        identity_hash=result["identity_hash"], artifact_manifest_hash=result["artifact_manifest_hash"]),
        Principal(name="Named human reviewer", roles=frozenset({"qa"}), repos=frozenset({request.repo})))
    assert runtime.workflow.advance(agents["orchestrator"]["id"], "ready", handoff["id"])["phase"] == "ready"


@pytest.mark.parametrize("location", ["imported", "original"])
def test_evidence_tampering_blocks_workflow(demo, location):
    runtime, _, agents, request = demo
    result = asyncio.run(call(demo, "test.verify", request.model_dump(mode="json")))
    if location == "imported":
        path = runtime.artifacts.root / result["main_artifact_id"]
    else:
        path = runtime.test_stage.tools.manager.artifact_path(result["run_id"], "initial/junit.xml")
    path.write_text("tampered", encoding="utf-8")
    handoff = passing_handoff(demo, result["main_artifact_id"])
    with pytest.raises(ValueError):
        runtime.workflow.advance(agents["orchestrator"]["id"], "qa", handoff["id"])


def test_tool_core_has_no_orchestration_imports():
    import ast
    from evo.test_stage import prime_tools
    tree = ast.parse(Path(prime_tools.__file__).read_text(encoding="utf-8"))
    attrs = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    assert not attrs & {"spawn", "merge", "create_subprocess_shell"}


def test_new_spec_on_same_sha_requires_fresh_evidence_and_skips_old_result(demo):
    from evo.test_stage.models import canonical_hash
    from evo.test_stage_bridge import RunBinding
    runtime, store, agents, request = demo
    first = asyncio.run(call(demo, "test.verify", request.model_dump(mode="json")))
    registry = runtime.test_stage.tools.manager.registry
    entry = registry.registrations[(request.repo, request.pr_number)]
    spec = entry.spec.model_copy(update={"intent": entry.spec.intent + " Confirm both approved cases."})
    registry.register(entry.model_copy(update={"spec": spec}))
    artifact = runtime.artifacts.write("test-tool-demo", agents["spec"]["id"], "revised-spec.json", spec.model_dump_json())
    runtime.workflow.handoff(agents["spec"]["id"], "code", "Operator-reviewed revised spec", [artifact["id"]])
    revised = request.model_copy(update={"spec_hash": canonical_hash(spec)})
    runtime.test_stage.bind(RunBinding(project_id="demo", run_id="test-tool-demo",
        spec_artifact_id=artifact["id"], request=revised))
    second = asyncio.run(call(demo, "test.verify", revised.model_dump(mode="json")))
    assert first["run_id"] != second["run_id"] and first["identity_hash"] != second["identity_hash"]
    handoff = passing_handoff(demo, second["main_artifact_id"])
    assert runtime.workflow.advance(agents["orchestrator"]["id"], "qa", handoff["id"])["phase"] == "qa"


def test_host_binding_change_during_execution_does_not_import_result(demo, monkeypatch):
    from evo.test_stage_bridge import RunBinding
    runtime, store, _, request = demo
    binding = runtime.test_stage._bindings["test-tool-demo"]
    original = runtime.test_stage.tools.verify_pr
    async def rebind_after_verify(supplied):
        result = await original(supplied)
        runtime.test_stage.bind(RunBinding(project_id="demo", run_id="test-tool-demo",
            spec_artifact_id=binding.spec_artifact_id,
            request=request.model_copy(update={"budget": request.budget.model_copy(update={"max_test_executions": 2})})))
        return result
    monkeypatch.setattr(runtime.test_stage.tools, "verify_pr", rebind_after_verify)
    with pytest.raises(ValueError, match="binding changed during"):
        asyncio.run(call(demo, "test.verify", request.model_dump(mode="json")))
    assert not store.list("test_stage_results", run_id="test-tool-demo")


def test_cli_operator_configuration_loads_but_in_repo_configuration_is_rejected(demo, tmp_path, monkeypatch):
    from evo.test_stage_bridge import configured_bridge
    runtime = demo[0]
    binding = runtime.test_stage._bindings["test-tool-demo"]
    policy = tmp_path / "operator-policy.json"
    policy.write_text(runtime.test_stage.tools.policy.model_dump_json(), encoding="utf-8")
    config = tmp_path / "host-config.json"
    content = {"data_dir": str(runtime.test_stage.tools.manager.storage), "policy_file": str(policy),
               "bindings": [binding.model_dump(mode="json")]}
    config.write_text(json.dumps(content), encoding="utf-8")
    monkeypatch.setenv("EVO_TEST_STAGE_CONFIG", str(config.resolve()))
    assert configured_bridge().is_bound("test-tool-demo")
    entry = runtime.test_stage.tools.manager.registry.registrations[(binding.request.repo, binding.request.pr_number)]
    malicious = entry.repo_root / "model-config.json"
    malicious.write_text(json.dumps(content), encoding="utf-8")
    monkeypatch.setenv("EVO_TEST_STAGE_CONFIG", str(malicious.resolve()))
    with pytest.raises(PermissionError, match="outside registered"):
        configured_bridge()
