import json
from pathlib import Path

import pytest

from evo.common import Conflict
from evo.specification import SPEC_SCHEMA_VERSION, spec_prompt_snapshot
from evo.workflow import ArtifactStore, Workflow
from evo.workspaces import WorkspaceManager


@pytest.fixture
def spec_run(store, project):
    run = store.put("runs", "run1", {
        "project_id": project["id"],
        "phase": "spec",
        "root_agent_id": "root",
        "title": "Change the greeting",
        "spec_schema_version": SPEC_SCHEMA_VERSION,
        "spec_prompt": spec_prompt_snapshot(store, project["id"]),
        "spec_request": "Change the greeting to hello world.",
        **WorkspaceManager(store).create_run(project["id"], "run1"),
    })
    store.put("agents", "spec", {"run_id": run["id"], "role": "spec"})
    store.put("agents", "root", {"run_id": run["id"], "role": "orchestrator"})
    return run


@pytest.fixture
def draft():
    return {
        "problem": "The greeting needs explicit wording.",
        "scope": ["Change the greeting printed by app.py."],
        "non_goals": ["No change to invocation."],
        "assumptions": [],
        "current_behavior": [{"claim": "The app prints hello.", "source_ids": ["SRC-1"]}],
        "proposed_change": "Replace the printed string with hello world.",
        "api_changes": "The script prints hello world.",
        "alternatives": [],
        "risks": ["Callers might depend on the printed greeting."],
        "requirements": [{"id": "REQ-1", "description": "Print hello world.", "source_ids": ["REQUEST"],
                          "test_ids": ["TEST-1"]}],
        "acceptance_tests": [{"id": "TEST-1", "expected_behavior": "The stdout line is hello world."}],
        "work_items": [{"id": "TASK-1", "objective": "Update the greeting.", "depends_on": []}],
        "timeline": [{"id": "MILESTONE-1", "phase": "Implementation", "deliverable": "Updated script",
                      "exit_criteria": "TEST-1 passes", "depends_on": []}],
        "constraints": [],
        "rollout_rollback": "Revert the greeting change if the new output is incompatible.",
        "unresolved_questions": [],
    }


SOURCES = [{"path": "app.py", "start": 1, "lines": 1}]


def test_spec_handoff_pins_evidence_and_prompt(store, spec_run, draft):
    workflow = Workflow(store)
    (Path(spec_run["workdir"]) / "app.py").write_text("dirty worktree\n", encoding="utf-8")
    context = workflow.specification.context("spec", SOURCES)
    assert context["sources"][0]["id"] == "REQUEST"
    assert context["sources"][1]["text"] == "print('hello')"
    assert context["sources"][1]["commit"] == spec_run["base_commit"]
    store.put("prompts", "new-prompt", {"project_id": spec_run["project_id"], "role": "spec",
                                      "status": "active", "content": "New guidance.", "version": 2})

    published = workflow.specification.publish("spec", draft, SOURCES)
    assert published["status"] == "DRAFT"
    assert published["ready_for_code"]
    artifact = ArtifactStore(store).read(published["artifact_id"])
    assert artifact["run_id"] == spec_run["id"]
    contract = json.loads(artifact["content"])
    assert contract["draft"] == draft
    assert contract["sources"] == context["sources"]
    assert contract["prompt"]["hash"] == spec_run["spec_prompt"]["hash"]
    assert "node_id" not in contract["draft"]["acceptance_tests"][0]
    test_stage_document = ArtifactStore(store).write(
        spec_run["id"], "spec", "Approved Test-stage document", "{}", "specification"
    )
    handoff = workflow.handoff("spec", "code", "Ready to implement.",
                               [published["artifact_id"], published["markdown_artifact_id"], test_stage_document["id"]])
    assert workflow.advance("root", "code", handoff["id"])["phase"] == "code"


def test_blocking_decisions_and_invalid_contracts_cannot_advance(store, spec_run, draft):
    workflow = Workflow(store)
    assert not workflow.specification.validate("spec", draft)["valid"]  # Missing SRC-1.
    with pytest.raises(ValueError, match="Missing or unknown source"):
        workflow.specification.publish("spec", draft)

    ready = workflow.specification.publish("spec", draft, SOURCES)
    handoff = workflow.handoff("spec", "code", "Ready to implement.", [ready["artifact_id"]])
    draft["unresolved_questions"] = ["Which callers need the current greeting?"]
    blocked = workflow.specification.publish("spec", draft, SOURCES)
    assert blocked["status"] == "BLOCKED"
    assert blocked["blockers"] == draft["unresolved_questions"]
    with pytest.raises(ValueError, match="Which callers"):
        workflow.handoff("spec", "code", "Blocked", [blocked["artifact_id"]])
    with pytest.raises(ValueError, match="newer specification"):
        workflow.advance("root", "code", handoff["id"])
    assert workflow.status(spec_run["id"])["phase"] == "spec"


def test_forged_and_superseded_evidence_do_not_pass_handoff(store, spec_run, draft):
    workflow = Workflow(store)
    evidence = ArtifactStore(store).write(spec_run["id"], "spec", "placeholder", "{}",
                                          kind="specification")
    with pytest.raises(ValueError, match="publication"):
        workflow.handoff("spec", "code", "Forged specification", [evidence["id"]])
    published = workflow.specification.publish("spec", draft, SOURCES)
    handoff = workflow.handoff("spec", "code", "Draft recorded", [published["artifact_id"]])
    (store.data_dir / "artifacts" / published["artifact_id"]).write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="changed after publication"):
        workflow.advance("root", "code", handoff["id"])
    store.put("runs", spec_run["id"], {"workflow_round": 2})
    with pytest.raises(ValueError, match="current run, round"):
        workflow.handoff("spec", "code", "Old draft", [published["artifact_id"]])


def test_event_sources_must_come_from_the_same_run(store, spec_run):
    workflow = Workflow(store)
    message = {"role": "user", "visible": True, "text": "The greeting needs only one space."}
    clarification = store.event(spec_run["id"], "root", "message", message)
    context = workflow.specification.context("spec", [{"event_id": clarification["id"]}])
    assert context["sources"][-1]["id"] == "REQUEST-1"

    store.put("runs", "run2", {"project_id": spec_run["project_id"]})
    foreign = store.event("run2", "root", "message", message)
    with pytest.raises(ValueError, match="from this run"):
        workflow.specification.context("spec", [{"event_id": foreign["id"]}])


def test_routed_scope_revision_supersedes_spec_handoff(store, spec_run, draft):
    workflow = Workflow(store)
    publication = workflow.specification.publish("spec", draft, SOURCES)
    handoff = workflow.handoff("spec", "code", "Ready", [publication["artifact_id"]])
    store.put("runs", "supervisor", {"project_id": spec_run["project_id"], "kind": "supervisor",
                                     "root_agent_id": "supervisor-agent"})
    user_message = {"role": "user", "visible": True, "text": "Add a punctuation mark to the greeting."}
    original = store.event("supervisor", "supervisor-agent", "message", user_message)
    routed = store.event(spec_run["id"], "root", "message", {
        **user_message, "visible": False,
        "routing": {"source_event_id": original["id"], "source_run_id": "supervisor",
                    "source_agent_id": "supervisor-agent"},
    })
    with pytest.raises(PermissionError, match="main agent"):
        workflow.specification.revise_request("spec", routed["id"])
    assert workflow.specification.revise_request("root", routed["id"])["request_revision"] == 1
    assert workflow.specification.revise_request("root", routed["id"])["request_revision"] == 1
    context = workflow.specification.context("spec", SOURCES)
    assert context["sources"][1]["id"] == "REQUEST-1"
    assert context["sources"][1]["text"] == user_message["text"]
    assert context["sources"][1]["origin_event_id"] == original["id"]
    with pytest.raises(ValueError, match="request was revised"):
        workflow.advance("root", "code", handoff["id"])


def test_revision_after_spec_reopens_clean_workflow(store, spec_run):
    workflow = Workflow(store)
    store.put("runs", spec_run["id"], {"phase": "code", "writer_agent_id": "code",
                                      "tested_commit": spec_run["base_commit"]})
    store.put("agents", "code", {"run_id": spec_run["id"], "role": "code", "status": "running"})
    event = store.event(spec_run["id"], "root", "message", {
        "role": "user", "visible": True, "text": "Change the greeting twice.",
    })
    with pytest.raises(Conflict, match="finish the workflow workers"):
        workflow.specification.revise_request("root", event["id"])
    store.put("agents", "code", {"status": "cancelled"})
    path = Path(spec_run["workdir"]) / "app.py"
    original = path.read_bytes()
    path.write_text("dirty edits\n", encoding="utf-8")
    with pytest.raises(Conflict, match="restore the implementation worktree"):
        workflow.specification.revise_request("root", event["id"])
    path.write_bytes(original)
    assert workflow.specification.revise_request("root", event["id"])["phase"] == "spec"
    run = store.get("runs", spec_run["id"])
    assert run["workflow_round"] == 2
    assert run["spec_base_commit"] == run["base_commit"]
    assert run["writer_agent_id"] is None and run["tested_commit"] is None
