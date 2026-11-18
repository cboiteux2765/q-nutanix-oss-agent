"""Host-owned adapter between main's RPC broker and the existing verification core.

This is a capability boundary for tool RPC, not an OS sandbox. EVO's unrestricted
Python kernel must still run only trusted code. Untrusted test code requires the
core's isolated Docker backend. No agent, approval, command or path is accepted
as authority from a tool argument.
"""
from __future__ import annotations

import asyncio
from hashlib import sha256
import json
import os
from pathlib import Path

from pydantic import Field

from .store import require
from .test_stage.models import ImmutableContract, SpecDocument, canonical_hash
from .test_stage.prime_tools import (StageTarget, TestStageTools, VerifyInput,
                                    VerifyOutput, load_operator_tools)
from .workspaces import git


class RunBinding(ImmutableContract):
    """Installed by the operator after Spec/Code publish their approved inputs."""
    project_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    spec_artifact_id: str = Field(min_length=1)
    request: VerifyInput


class BridgeConfig(ImmutableContract):
    data_dir: Path
    policy_file: Path
    bindings: tuple[RunBinding, ...]


class MainVerifyOutput(VerifyOutput):
    main_artifact_id: str = Field(pattern=r"^[a-f0-9]{32}$")


def configured_bridge():
    """Optional CLI/server setup. The environment belongs to the operator host."""
    value = os.getenv("EVO_TEST_STAGE_CONFIG")
    if not value:
        return None
    path = Path(value)
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise PermissionError("Test-stage configuration must be an operator-owned absolute file.")
    config = BridgeConfig.model_validate_json(path.read_text(encoding="utf-8"))
    tools = load_operator_tools(config.data_dir, config.policy_file)
    # Configuration and policy may not live in any registered source checkout.
    for registration in tools.manager.registry.registrations.values():
        root = registration.repo_root.resolve()
        if any(p.resolve().is_relative_to(root) for p in (path, config.policy_file, config.data_dir)):
            raise PermissionError("Test-stage control state must live outside registered repositories.")
    return TestStageBridge(tools, config.bindings)


class TestStageBridge:
    """Injected into Runtime on the host; never constructed in model Python."""
    __test__ = False

    def __init__(self, tools: TestStageTools, bindings: tuple[RunBinding, ...] = ()):
        if not isinstance(tools, TestStageTools):
            raise PermissionError("A host-created TestStageTools capability is required.")
        self.tools = tools
        self._bindings: dict[str, RunBinding] = {}
        for binding in bindings:
            self.bind(binding)

    def bind(self, binding: RunBinding) -> None:
        """Host API only. Rebinding a revision requires a new approved handoff."""
        binding = RunBinding.model_validate(binding.model_dump(mode="json"))
        if binding.request.correlation_id != f"evo:{binding.run_id}":
            raise ValueError("Correlation must be bound to the main run ID.")
        self.tools._authorize_repo(binding.request.repo)
        self.tools._budget(binding.request.budget)
        self._bindings[binding.run_id] = binding

    def is_bound(self, run_id: str) -> bool:
        return run_id in self._bindings

    def _scope(self, scope) -> RunBinding:
        # agent_id is bound by Runtime's PythonREPL RPC closure, not model kwargs.
        agent = require(scope.store, "agents", scope.agent_id)
        run = require(scope.store, "runs", agent["run_id"])
        role = agent["role"].lower()
        if role not in {"test", "qa", "orchestrator"} or (
                role == "orchestrator" and run.get("root_agent_id") != agent["id"]):
            raise PermissionError("Only the host-recorded main, Test or QA agent may use Test tools.")
        binding = self._bindings.get(run["id"])
        if not binding or binding.project_id != run["project_id"]:
            raise PermissionError("No approved Test-stage binding for this main run and project.")
        self._current(scope.store, run, binding)
        return binding

    def _current(self, store, run, binding):
        request = binding.request
        if run.get("implementation_commit") != request.expected_head_sha:
            raise ValueError("STALE: main implementation commit differs from the approved Test target.")
        if run.get("phase") not in {"test", "qa", "ready"}:
            raise PermissionError("Test tools require the Test/QA phase of the approved run.")
        project = require(store, "projects", run["project_id"])
        registration = self.tools.manager.registry.registrations[(request.repo, request.pr_number)]
        def common_dir(path):
            root = Path(path).resolve()
            return (root / git(root, "rev-parse", "--git-common-dir")).resolve()
        if common_dir(project["path"]) != common_dir(registration.repo_root):
            raise PermissionError("Registered Test repository differs from the main project.")
        if git(Path(run["workdir"]), "rev-parse", "HEAD") != request.expected_head_sha or git(
                Path(run["workdir"]), "status", "--porcelain"):
            raise ValueError("STALE: publish a clean implementation worktree before verification.")
        spec = require(store, "artifacts", binding.spec_artifact_id)
        if spec["run_id"] != run["id"]:
            raise PermissionError("Approved specification artifact belongs to another run.")
        # A model-authored approval flag is insufficient: both operator binding and
        # the actual Spec -> Code handoff must name the same canonical document.
        handoffs = store.list("workflow_handoffs", run_id=run["id"])
        if not any(h["source"] == "spec" and h["target"] == "code"
                   and h.get("round", 1) == run.get("workflow_round", 1)
                   and binding.spec_artifact_id in h["artifacts"] for h in handoffs):
            raise PermissionError("Approved specification is absent from this round's Spec handoff.")
        from .workflow import ArtifactStore
        document = SpecDocument.model_validate_json(ArtifactStore(store).read(spec["id"])["content"])
        if canonical_hash(document) != request.spec_hash:
            raise ValueError("STALE: main specification differs from the approved specification hash.")

    @staticmethod
    def _target(request):
        return StageTarget.model_validate(request.model_dump(mode="json", exclude={"budget"}))

    def target(self, scope) -> dict:
        return self._scope(scope).request.model_dump(mode="json")

    def preflight(self, scope, target: dict) -> dict:
        binding = self._scope(scope)
        if isinstance(target, dict) and "budget" in target:
            supplied = VerifyInput.model_validate(target)
            if supplied != binding.request:
                raise ValueError("STALE: request differs from the approved host binding.")
            supplied = self._target(supplied)
        else:
            supplied = StageTarget.model_validate(target)
        if supplied != self._target(binding.request):
            raise ValueError("STALE: target differs from the approved host binding.")
        return self.tools.preflight_pr(supplied).model_dump(mode="json")

    async def verify(self, scope, request: dict) -> dict:
        binding = await asyncio.to_thread(self._scope, scope)
        supplied = VerifyInput.model_validate(request)
        if supplied != binding.request:
            raise ValueError("STALE: request or budget differs from the approved host binding.")
        result = await self.tools.verify_pr(supplied)
        # Code may have moved while pytest ran. Never attach that result to a new SHA.
        if await asyncio.to_thread(self._scope, scope) != binding:
            raise ValueError("STALE: host binding changed during verification.")
        manifest = self.tools.manifest(result.run_id).model_dump(mode="json")
        bundle = {"verification": result.model_dump(mode="json"), "evidence": manifest,
                  "main_run_id": scope.run_id, "spec_artifact_id": binding.spec_artifact_id}
        content = json.dumps(bundle, ensure_ascii=False, sort_keys=True)
        previous = scope.store.get("test_stage_results", result.run_id)
        if previous:
            self._owned(scope, result.run_id)
            artifact = scope.broker.artifacts.read(previous["artifact_id"])
            if artifact["content"] != content:
                raise ValueError("Imported Test evidence changed.")
        else:
            artifact = scope.broker.artifacts.write(scope.run_id, scope.agent_id,
                f"Test evidence {result.run_id}", content, "test_stage_evidence")
            scope.store.put("test_stage_results", result.run_id, {
                "run_id": scope.run_id, "project_id": binding.project_id,
                "identity_hash": result.identity_hash,
                "artifact_id": artifact["id"], "bundle_sha256": sha256(content.encode()).hexdigest()})
        output = MainVerifyOutput.model_validate(result.model_dump(mode="json") | {
            "main_artifact_id": artifact["id"]}).model_dump(mode="json")
        scope.store.event(scope.run_id, scope.agent_id, "test_stage.verified", output)
        return output

    def _owned(self, scope, run_id):
        self._scope(scope)
        record = require(scope.store, "test_stage_results", run_id)
        if record["run_id"] != scope.run_id or record["project_id"] != scope.project_id:
            raise PermissionError("Test evidence belongs to another main run or project.")
        return record

    def manifest(self, scope, run_id: str) -> dict:
        self._owned(scope, run_id)
        return self.tools.manifest(run_id).model_dump(mode="json")

    def artifact(self, scope, run_id: str, name: str) -> dict:
        self._owned(scope, run_id)
        return self.tools.artifact(run_id, name).model_dump(mode="json")

    def validate_handoff(self, store, run, handoff, phase):
        """Gate bound workflows on host-imported evidence; QA approval stays human."""
        from .workflow import ArtifactStore
        binding = self._bindings[run["id"]]
        self._current(store, run, binding)
        context, _ = self.tools._context(binding.request)
        records = store.list("test_stage_results", run_id=run["id"])
        for record in records:
            if record.get("identity_hash") != context.identity_hash:
                continue
            imported = ArtifactStore(store).read(record["artifact_id"])
            if sha256(imported["content"].encode()).hexdigest() != record["bundle_sha256"]:
                raise ValueError("Imported Test evidence integrity failure.")
            bundle = json.loads(imported["content"])
            result = bundle["verification"]
            if result["identity_hash"] != context.identity_hash:
                raise ValueError("Imported Test identity differs from its host record.")
            # Revalidate original envelopes and every checksum at the handoff gate.
            self.tools.manager.artifact_path(result["run_id"], "verification.json")
            manifest = self.tools.manifest(result["run_id"])
            if manifest.model_dump(mode="json") != bundle["evidence"]:
                raise ValueError("Test manifest changed after import.")
            for entry in manifest.manifest.evidence:
                self.tools.manager.artifact_path(result["run_id"], entry.path)
            if result["readiness"] != "READY_FOR_QA":
                raise ValueError("Test-stage verification is not READY_FOR_QA.")
            if phase == "test" and record["artifact_id"] not in handoff["artifacts"]:
                continue
            if phase == "qa":
                review = self.tools.manager.get_run(result["run_id"]).qa_review
                if review.status != "APPROVED":
                    raise PermissionError("Named human QA approval is still required; an agent cannot approve it.")
            return
        raise PermissionError("Attach host-imported passing Test evidence for the approved commit.")
