from fastapi.testclient import TestClient

from evo import server
from evo.server import create_app


def test_default_repository_is_registered_once(tmp_path, repo):
    data = tmp_path / "data"
    for _ in range(2):  # restarting must not add a second project
        with TestClient(create_app(data_dir=data, frontend_dir=tmp_path / "dist", default_project=repo)) as client:
            projects = client.get("/api/projects").json()
    assert [(p["name"], p["path"]) for p in projects] == [(repo.name, str(repo.resolve()))]


def test_no_default_unless_asked(tmp_path, repo):
    with TestClient(create_app(data_dir=tmp_path / "data", frontend_dir=tmp_path / "dist")) as client:
        assert client.get("/api/projects").json() == []


def test_existing_projects_are_left_alone(tmp_path, repo):
    other = tmp_path / "other"
    other.mkdir()
    data = tmp_path / "data"
    with TestClient(create_app(data_dir=data, frontend_dir=tmp_path / "dist")) as client:
        client.post("/api/projects", json={"name": "mine", "path": str(other)})
    with TestClient(create_app(data_dir=data, frontend_dir=tmp_path / "dist", default_project=repo)) as client:
        assert [p["name"] for p in client.get("/api/projects").json()] == ["mine"]


def test_default_repo_setting(monkeypatch, tmp_path):
    monkeypatch.setenv("EVO_DEFAULT_REPO", str(tmp_path))
    assert server.default_repo() == tmp_path
    monkeypatch.setenv("EVO_DEFAULT_REPO", "")
    assert server.default_repo() is None
    monkeypatch.delenv("EVO_DEFAULT_REPO")
    assert server.default_repo() is not None and (server.default_repo() / ".git").exists()
