"""Spec Lab API: spec runs, harness comparison and governed evolution (recorded mode, no model)."""
import time

import pytest
from fastapi.testclient import TestClient

from agent.governance import check_gates, lessons, verify_ledger
from agent.learning import BASE_PROMPT
from evo.server import create_app

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
    assert {h["id"] for h in data["harnesses"]} == {"mini", "prime-rlm", "prime-schema"}
    assert [g["id"] for g in data["governance"]["gates"]][:2] == ["scope", "lint"]
    assert data["governance"]["active_version"] == "v1"
    assert client.get("/api/projects/nope/spec").status_code == 404


@pytest.mark.parametrize("harness", ["mini", "prime-rlm"])
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
    job = wait(client, base, client.post(f"{base}/cycles", json={"backend": "fixture"}).json())
    assert job["status"] == "completed", job.get("error")
    cycle = job["cycle_id"]
    detail = client.get(f"{base}/cycles/{cycle}", params={"reviewer": "PM"}).json()
    gates = {g["id"]: g for g in detail["gates"]}
    assert gates["critic"]["passed"] and not gates["benchmark"]["passed"]

    blocked = client.post(f"{base}/cycles/{cycle}/decision", json={"action": "promote", "reviewer": "PM"})
    assert blocked.status_code == 409 and "Frozen benchmark replay" in blocked.json()["detail"]

    replay = wait(client, base, client.post(f"{base}/cycles/{cycle}/replay", json={"backend": "fixture"}).json())
    assert replay["status"] == "completed" and replay["replay"]["regressions"] == []
    promoted = client.post(f"{base}/cycles/{cycle}/decision", json={"action": "promote", "reviewer": "PM"})
    assert promoted.status_code == 200, promoted.json()
    governance = client.get(base).json()["governance"]
    assert governance["active_version"] == "v2" and governance["ledger_ok"]
    assert [a["action"] for a in governance["audit"]][:3] == ["promote", "benchmark_replay", "proposal"]

    rolled = client.post(f"{base}/versions/v1/rollback", json={"reviewer": "PM"})
    assert rolled.status_code == 200 and rolled.json()["active_version"] == "v1"


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
