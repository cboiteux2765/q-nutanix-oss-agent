"""Small trusted fixture for main-agent integration checks, with no remote writes."""
from __future__ import annotations

from pathlib import Path

from .config import DEFAULT_SETTINGS
from .runtime import Runtime, USAGE_FIELDS
from .store import Store
from .test_stage.models import Principal
from .test_stage.prime_tools import TestStageTools, ToolPolicy, VerifyInput
from .test_stage.providers import SourceRegistry, make_demo_repository
from .test_stage.run_manager import RunManager
from .test_stage_bridge import RunBinding, TestStageBridge


def prepare_demo(root: Path, *, settings=None, regression=False, missing_env=False, ambiguous=False):
    """Operator fixture: publish Spec/Code, then install a scoped host capability."""
    repo, registration = make_demo_repository(root / "repository", regression=regression,
                                               missing_env=missing_env, ambiguous=ambiguous)
    store = Store(root / "main-state")
    registry = SourceRegistry(root / "verification-state/sources")
    policy = ToolPolicy(operator_name="main-test-host", allowed_repos=(registration.repo,),
                        allowed_backends=("local_trusted",), max_runtime_seconds=60)
    tools = TestStageTools(RunManager(root / "verification-state", registry),
        Principal(name=policy.operator_name, roles=frozenset({"engineer"}),
                  repos=frozenset(policy.allowed_repos)), policy)
    bridge = TestStageBridge(tools)
    runtime = Runtime(store, test_stage=bridge)
    store.put("projects", "demo", {"name": "Test tool integration", "path": str(repo)})
    workspace = runtime.workspaces.create_run("demo", "test-tool-demo")
    run = store.put("runs", "test-tool-demo", {
        "project_id": "demo", "title": "Bound Test-stage verification", "status": "running",
        "phase": "triage", "settings_snapshot": DEFAULT_SETTINGS | {
            "runtime_preset": "custom", "base_url": "http://127.0.0.1:9/v1", "model": "test-model",
            "api_style": "chat_completions", "max_steps": 8, "max_agents": 4,
            "max_output_tokens": 2500, "max_run_tokens": 100000, "repl_timeout": 90,
        } | (settings or {}), "token_usage": dict.fromkeys(USAGE_FIELDS, 0),
        "skill_ids": [], "prompt_ids": {}, **workspace})
    agents = {}
    for role in ("orchestrator", "spec", "code", "test", "qa"):
        agents[role] = runtime._new_agent(run, role, "Verify trusted integration fixture")
    main = agents["orchestrator"]
    store.put("runs", run["id"], {"root_agent_id": main["id"]})
    runtime.workflow.advance(main["id"], "spec")
    spec = runtime.artifacts.write(run["id"], agents["spec"]["id"], "approved-spec.json",
                                   registration.spec.model_dump_json(indent=2), "specification")
    handoff = runtime.workflow.handoff(agents["spec"]["id"], "code", "Approved fixture spec", [spec["id"]])
    runtime.workflow.advance(main["id"], "code", handoff["id"])
    # Publish through main's existing workspaces, leaving the source checkout intact.
    source = Path(workspace["workdir"]) / "src/service.py"
    source.write_text(source.read_text(encoding="utf-8") + "\n# main Code publication\n", encoding="utf-8")
    commit = runtime.workspaces.commit(agents["code"]["id"], "Publish integration fixture")["commit"]
    registration = registration.model_copy(update={"head_ref": "refs/heads/" + workspace["branch"]})
    registry.register(registration)
    handoff = runtime.workflow.handoff(agents["code"]["id"], "test", "Verify published SHA", commit=commit)
    runtime.workflow.advance(main["id"], "test", handoff["id"])
    for role in ("test", "qa"):
        runtime.workspaces.prepare_agent(agents[role]["id"], commit)
    context = registry.resolve(registry.request_for_pr(registration.repo, registration.pr_number))
    request = VerifyInput(repo=context.repo, pr_number=context.pr_number,
        expected_base_sha=context.base_sha, expected_head_sha=commit,
        spec_id=context.spec.spec_id, spec_hash=context.spec_hash,
        test_profile_id=context.profile.id, test_profile_hash=context.profile.fingerprint,
        environment_manifest_id=context.environment.id, environment_fingerprint=context.environment.fingerprint,
        policy_version=context.policy_version, correlation_id=f"evo:{run['id']}",
        budget={"max_runtime_seconds": 60})
    bridge.bind(RunBinding(project_id="demo", run_id=run["id"], spec_artifact_id=spec["id"], request=request))
    return runtime, store, agents, request
