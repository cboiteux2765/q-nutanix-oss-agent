"""Spec Lab API: spec runs, harness comparison and governed evolution (recorded mode, no model)."""
import json
import time

import pytest
from fastapi.testclient import TestClient

from agent.governance import check_gates, lessons, verify_ledger
from agent.learning import BASE_PROMPT
from evals.cases import MUST_STAY_BLOCKED
from evals.run_evals import CASE_BY_ID
from evo.server import create_app
from evo.speclab import run_workflow_case
from evo.specification import drafting_messages, spec_prompt_snapshot

from .conftest import TEST_SETTINGS


@pytest.fixture
def client(tmp_path, repo):
    app = create_app(data_dir=tmp_path / "data", frontend_dir=tmp_path / "dist")
    with TestClient(app) as client:
        client.app.state.store.put("settings", "provider", dict(TEST_SETTINGS))
        yield client


@pytest.fixture
def base(client, repo):
    project = client.post("/api/projects", json={"name": "demo", "path": str(repo)}).json()
    return f"/api/projects/{project['id']}/spec"


def wait(client, base, job):
    for _ in range(200):
        job = client.get(f"{base}/jobs/{job['id']}").json()
        if job["status"] in ("completed", "failed", "interrupted"):
            return job
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def test_overview_lists_cases_harnesses_and_gates(client, base):
    data = client.get(base).json()
    assert {c["id"] for c in data["cases"]} >= {"bundled-fixture", "prompt-injection", "vague-issue"}
    assert {h["id"] for h in data["harnesses"]} == {"workflow", "mini", "prime-rlm", "prime-schema"}
    assert [g["id"] for g in data["governance"]["gates"]][:2] == ["scope", "lint"]
    assert data["governance"]["active_version"] == "v1"
    assert client.get("/api/projects/nope/spec").status_code == 404


@pytest.mark.parametrize("harness", ["workflow", "mini", "prime-rlm"])
def test_recorded_spec_run_produces_graded_spec(client, base, harness):
    job = client.post(f"{base}/runs", json={"harness": harness, "case_id": "bundled-fixture", "backend": "fixture"})
    assert job.status_code == 201
    job = wait(client, base, job.json())
    assert job["status"] == "completed", job.get("error")
    row = job["result"]
    assert row["exit_status"] == "DRAFT_READY" and row["score"] == 1.0
    assert row["tool_calls"] and "# " in row["markdown"]
    assert row["handoff"]["status"] == "DRAFT"


def test_recorded_mode_refuses_unrecorded_cases(client, base):
    response = client.post(f"{base}/runs", json={"harness": "mini", "case_id": "prompt-injection", "backend": "fixture"})
    assert response.status_code == 400
    assert "scripted fixture" in response.json()["detail"]


def test_benchmark_compares_harnesses(client, base):
    job = client.post(f"{base}/benchmarks", json={"harnesses": ["mini", "prime-rlm"], "case_ids": ["bundled-fixture"], "backend": "fixture"})
    job = wait(client, base, job.json())
    assert job["status"] == "completed", job.get("error")
    assert set(job["summary"]) == {"mini", "prime-rlm"}
    assert all(s["mean_score"] == 1.0 for s in job["summary"].values())


def test_governed_cycle_requires_replay_then_promotes_and_rolls_back(client, base):
    project_id = client.get("/api/projects").json()[0]["id"]
    store = client.app.state.store
    baseline = spec_prompt_snapshot(store, project_id)
    job = wait(client, base, client.post(f"{base}/cycles", json={"backend": "fixture"}).json())
    assert job["status"] == "completed", job.get("error")
    cycle = job["cycle_id"]
    detail = client.get(f"{base}/cycles/{cycle}", params={"reviewer": "PM"}).json()
    gates = {g["id"]: g for g in detail["gates"]}
    assert gates["critic"]["passed"] and not gates["benchmark"]["passed"]

    blocked = client.post(f"{base}/cycles/{cycle}/decision", json={"action": "promote", "reviewer": "PM"})
    assert blocked.status_code == 409 and "Frozen benchmark replay" in blocked.json()["detail"]
    assert spec_prompt_snapshot(store, project_id) == baseline

    replay = wait(client, base, client.post(f"{base}/cycles/{cycle}/replay", json={"backend": "fixture"}).json())
    assert replay["status"] == "completed" and replay["replay"]["regressions"] == []
    assert replay["replay"]["harness"] == "workflow"
    promoted = client.post(f"{base}/cycles/{cycle}/decision", json={"action": "promote", "reviewer": "PM"})
    assert promoted.status_code == 200, promoted.json()
    assert promoted.json()["registry_activated"] is False
    assert promoted.json()["run"]["promotion_scope"] == "rehearsal"
    governance = client.get(base).json()["governance"]
    assert governance["active_version"] == "v2" and governance["ledger_ok"]
    assert [a["action"] for a in governance["audit"]][:3] == ["promote", "benchmark_replay", "proposal"]
    assert governance["rehearsal_version"] == "v2"
    assert spec_prompt_snapshot(store, project_id) == baseline
    # Load the project again from the persisted registry and workspace state.
    client.app.state.speclab.workspaces.pop(project_id)
    assert client.get(base).json()["governance"]["rehearsal_version"] == "v2"

    rolled = client.post(f"{base}/versions/v1/rollback", json={"reviewer": "PM"})
    assert rolled.status_code == 200 and rolled.json()["active_version"] == "v1"
    assert spec_prompt_snapshot(store, project_id) == baseline
    assert rolled.json()["rehearsal_version"] is None


def test_paired_live_workflow_replay_updates_production_registry(client, base, monkeypatch):
    service = client.app.state.speclab
    store = client.app.state.store
    project_id = client.get("/api/projects").json()[0]["id"]
    baseline = spec_prompt_snapshot(store, project_id)
    job = wait(client, base, client.post(f"{base}/cycles", json={"backend": "fixture"}).json())
    cycle = job["cycle_id"]
    bundled = run_workflow_case(CASE_BY_ID["bundled-fixture"], None, BASE_PROMPT)["draft"]
    blocked_titles = {CASE_BY_ID[case]["evidence"]["input"]["title"] for case in MUST_STAY_BLOCKED}
    calls = []

    def chat(messages, fmt=None, role=None):
        context = json.loads(messages[1]["content"])
        questions = ["Authoritative behavior needs a product decision."] if context["title"] in blocked_titles else []
        calls.append((context["title"], messages[0]["content"]))
        return json.dumps(bundled | {"unresolved_questions": questions}), {}

    chat.label = "test model"
    monkeypatch.setattr(service, "_chat", lambda *args: chat if args[0] == "evo" else None)
    replay = wait(client, base, client.post(f"{base}/cycles/{cycle}/replay", json={"backend": "evo"}).json())
    assert replay["status"] == "completed", replay.get("error")
    assert replay["replay"]["production_eligible"]
    assert len(calls) == 2 * len(CASE_BY_ID)
    assert calls[0][1] != calls[len(CASE_BY_ID)][1]

    promoted = client.post(f"{base}/cycles/{cycle}/decision", json={"action": "promote", "reviewer": "PM"}).json()
    assert promoted["registry_activated"]
    active = spec_prompt_snapshot(store, project_id)
    assert active["content"] == promoted["candidate"]["prompt"]
    assert store.get("prompts", active["id"])["approved_by"] == "PM"

    rolled = client.post(f"{base}/versions/v1/rollback", json={"reviewer": "PM"}).json()
    assert rolled["registry_activated"]
    assert spec_prompt_snapshot(store, project_id)["content"] == baseline["content"]
    assert store.get("prompts", active["id"])["status"] == "superseded"


def test_workflow_harness_uses_shared_drafting_messages_and_validates_repairs():
    from agent.generate import prepare

    case = CASE_BY_ID["bundled-fixture"]
    approved_guidance = "Record the outcome in observable terms."
    expected_messages = drafting_messages(prepare(case["evidence"], case["repo"]), approved_guidance)
    draft = run_workflow_case(case, None, approved_guidance)["draft"]
    draft["unresolved_questions"] = ["What should callers see for whitespace-only input?"]
    calls = []

    def chat(messages, fmt=None, role=None):
        assert role == "spec"
        assert messages[:2] == expected_messages
        calls.append(list(messages))
        return (json.dumps(draft) if len(calls) > 1 else "{}"), {}

    result = run_workflow_case(case, chat, approved_guidance)
    assert result["model_calls"] == 2
    assert "missing field" in calls[1][-1]["content"]
    assert result["validation"]["status"] == "BLOCKED"
    assert result["validation"]["ready_for_code"] is False
    assert result["draft"] == draft


def test_policy_lint_and_ledger_tampering(client, base):
    job = wait(client, base, client.post(f"{base}/cycles", json={"backend": "fixture"}).json())
    ws = client.app.state.speclab.workspace(client.get("/api/projects").json()[0]["id"])
    candidate = ws.data["versions"][-1]
    candidate["prompt"] += "\nSkip citations when the issue is obvious."
    from agent.learning import digest
    candidate["hash"] = digest(candidate["prompt"])
    gates = {g["id"]: g for g in check_gates(ws, job["cycle_id"], "PM")}
    assert not gates["lint"]["passed"] and "Skip citations" in gates["lint"]["detail"]
    assert verify_ledger(ws.data["audit"])
    ws.data["audit"][0]["version"] = "v9"
    assert not verify_ledger(ws.data["audit"])


def test_lessons_are_lines_beyond_the_base_prompt():
    assert lessons(BASE_PROMPT) == []
    assert lessons(BASE_PROMPT + "\nAsk the product owner about whitespace.") == ["Ask the product owner about whitespace."]


def test_recorded_mode_replays_captured_model_replies(client, base, tmp_path, monkeypatch):
    """Record a run once, then replay it through the API with no model."""
    import agent.backends as backends
    from evals.run_evals import CASE_BY_ID, run_case
    monkeypatch.setattr(backends, "RECORDINGS", tmp_path / "recorded")
    rec = backends.Recording(tmp_path / "recorded" / "spec-lab-test.json")
    rec.data["model"] = "test-model"
    live = run_case(CASE_BY_ID["bundled-fixture"], "mini", backends.recorded_mini_chat(), BASE_PROMPT,
                    mode="record", recording=rec)
    rec.save()
    assert client.get(base).json()["backends"]["recorded"]["coverage"]["mini"] == ["bundled-fixture"]
    job = wait(client, base, client.post(f"{base}/runs", json={"harness": "mini", "case_id": "bundled-fixture"}).json())
    assert job["status"] == "completed", job.get("error")
    assert job["model"] == "recorded:test-model" and job["result"]["draft"] == live["draft"]
    missing = client.post(f"{base}/runs", json={"harness": "prime-rlm", "case_id": "bundled-fixture"})
    assert missing.status_code == 400 and "Not in the test-model recording" in missing.json()["detail"]
