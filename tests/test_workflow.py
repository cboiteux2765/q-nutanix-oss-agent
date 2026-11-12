import pytest

from evo.workflow import ArtifactStore, Workflow
from evo.workspaces import WorkspaceManager, git


@pytest.fixture
def run(store, project):
    workspace = WorkspaceManager(store).create_run(project["id"], "run1")
    run = store.put("runs", "run1", {"project_id": project["id"], "phase": "triage", "status": "running",
                                     "root_agent_id": "root", **workspace})
    for agent_id, role in (("root", "orchestrator"), ("spec", "spec"), ("code", "code"), ("test", "test")):
        store.put("agents", agent_id, {"run_id": run["id"], "role": role, "name": role, "status": "running"})
    return run


def test_forward_moves_need_evidence(store, run):
    workflow = Workflow(store)
    assert workflow.advance("root", "spec")["phase"] == "spec"
    with pytest.raises(ValueError, match="cannot advance directly"):
        workflow.advance("spec", "test")
    with pytest.raises(ValueError, match="specification artifact"):
        workflow.advance("spec", "code")
    artifact = ArtifactStore(store).write(run["id"], "spec", "spec.md", "# Plan")
    handoff = workflow.handoff("spec", "code", "Plan ready", artifacts=[artifact["id"]])
    assert workflow.advance("spec", "code", handoff["id"])["phase"] == "code"


def test_only_active_role_or_root_can_advance(store, run):
    workflow = Workflow(store)
    with pytest.raises(PermissionError):
        workflow.advance("code", "spec")


def test_code_publishes_commit_before_test(store, run):
    workflow = Workflow(store)
    store.put("runs", run["id"], {"phase": "code"})
    workdir = run["workdir"]
    from pathlib import Path
    (Path(workdir) / "fix.py").write_text("x = 1\n", encoding="utf-8")
    commit = WorkspaceManager(store).commit("code", "Fix")["commit"]
    assert git(Path(workdir), "rev-parse", "HEAD") == commit
    with pytest.raises(ValueError, match="current published implementation commit"):
        workflow.advance("code", "test")
    handoff = workflow.handoff("code", "test", "Ready for test", commit=commit)
    status = workflow.advance("code", "test", handoff["id"])
    assert status["phase"] == "test"
    assert status["implementation_commit"] == commit


def test_findings_validate_severity(store, run):
    workflow = Workflow(store)
    with pytest.raises(ValueError):
        workflow.finding("test", "Broken", "details", severity="urgent")
    finding = workflow.finding("test", "Broken", "details", severity="high")
    assert workflow.status(run["id"])["findings"][0]["id"] == finding["id"]
