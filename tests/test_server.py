import pytest
from fastapi.testclient import TestClient

from evo.server import create_app

from .conftest import TEST_SETTINGS


@pytest.fixture
def client(tmp_path, repo):
    app = create_app(data_dir=tmp_path / "data", frontend_dir=tmp_path / "dist")
    with TestClient(app) as client:
        client.app.state.store.put("settings", "provider", dict(TEST_SETTINGS))
        yield client


@pytest.fixture
def project(client, repo):
    response = client.post("/api/projects", json={"name": "demo", "path": str(repo)})
    assert response.status_code == 201
    return response.json()


def test_unknown_records_are_404(client):
    response = client.get("/api/projects/nope")
    assert response.status_code == 404
    assert response.json()["detail"] == "Project not found."
    assert client.get("/api/runs/nope").json()["detail"] == "Run not found."
    assert client.post("/api/agents/nope/compact").status_code == 404
    assert client.get("/api/projects/nope/memory").status_code == 404


def test_projects_are_deduplicated_by_path(client, project, repo):
    again = client.post("/api/projects", json={"name": "other", "path": str(repo)}).json()
    assert again["id"] == project["id"]
    assert [p["id"] for p in client.get("/api/projects").json()] == [project["id"]]


def test_main_agent_profile(client, project):
    response = client.put(f"/api/projects/{project['id']}/main-agent",
                          json={"name": "Helper", "instructions": "Be brief.", "model": " m "})
    assert response.json()["main_agent"]["model"] == "m"


def test_memory_write_conflict_is_409(client, project):
    url = f"/api/projects/{project['id']}/memory/notes.md"
    first = client.put(url, json={"content": "# Notes", "expected_revision": ""})
    assert first.status_code == 200, first.text
    stale = client.put(url, json={"content": "# Again", "expected_revision": ""})
    assert stale.status_code == 409
    assert stale.json()["detail"].startswith("Wiki page changed.")


def test_settings_round_trip(client):
    response = client.put("/api/settings", json={"max_steps": 12})
    assert response.status_code == 200
    assert response.json()["max_steps"] == 12
    assert client.get("/api/settings").json()["api_key_configured"] in (True, False)


def test_run_detail(client, project):
    store = client.app.state.store
    store.put("runs", "r1", {"project_id": project["id"], "status": "waiting", "root_agent_id": "a1"})
    store.put("agents", "a1", {"run_id": "r1", "role": "orchestrator", "name": "EVO", "status": "waiting",
                               "inbox": []})
    store.event("r1", "a1", "message", {"role": "user", "text": "hi", "visible": True})
    detail = client.get("/api/runs/r1").json()
    assert detail["run"]["id"] == "r1"
    assert [a["id"] for a in detail["agents"]] == ["a1"]
    assert "inbox" not in detail["agents"][0]
    assert len(detail["events"]) == 1
    light = client.get("/api/runs/r1", params={"events": "false"}).json()
    assert "events" not in light and light["agents"] == detail["agents"]
    conversation = client.get(f"/api/projects/{project['id']}/conversation").json()
    assert conversation["run_id"] == "r1"
    assert [m["data"]["text"] for m in conversation["messages"]] == ["hi"]


def test_cross_origin_writes_are_rejected(client):
    response = client.put("/api/settings", json={}, headers={"Origin": "http://evil.example"})
    assert response.status_code == 403


def test_evolution_endpoints(client, project):
    overview = client.get(f"/api/projects/{project['id']}/evolution").json()
    assert overview["settings"]["mode"] == "manual"
    assert {t["split"] for t in overview["tasks"]} == {"train", "validation", "heldout"}
    saved = client.put(f"/api/projects/{project['id']}/evolution/settings",
                       json={"mode": "auto", "limits": {"max_metric_calls": 4}}).json()
    assert saved["mode"] == "auto" and saved["limits"]["max_metric_calls"] == 4
    assert client.put(f"/api/projects/{project['id']}/evolution/settings",
                      json={"limits": {"max_metric_calls": 10_000}}).status_code == 400
    # An experiment needs source runs; the old UI omitted them.
    response = client.post(f"/api/projects/{project['id']}/experiments", json={"role": "code"})
    assert response.status_code == 400 and "source runs" in response.json()["detail"]
    assert client.post(f"/api/projects/{project['id']}/evolve", json={"role": "code"}).status_code == 400
    rollback = client.post(f"/api/projects/{project['id']}/prompts/rollback", json={"role": "code"}).json()
    assert rollback["active"] is None
