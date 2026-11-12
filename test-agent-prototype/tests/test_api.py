"""Human QA API boundaries over results produced by the shared verification core."""
from __future__ import annotations

import asyncio
import hashlib
import json

import pytest

pytest.importorskip("fastapi", reason="Install the qa or dev extra to test the optional human API")
from fastapi.testclient import TestClient

from app.main import create_app
from app.models import Principal
from app.providers import SourceRegistry, git, make_demo_repository
from app.run_manager import RunManager


TOKENS = {
    "alice-secret": Principal(name="alice", roles=frozenset({"engineer"}), repos=frozenset({"org/service"})),
    "qa-secret": Principal(name="named-qa", roles=frozenset({"qa"}), repos=frozenset({"org/service"})),
    "outsider-secret": Principal(name="outsider", roles=frozenset({"engineer", "qa"}), repos=frozenset({"other/repo"})),
}
ENGINEER = {"Authorization": "Bearer alice-secret"}
QA = {"Authorization": "Bearer qa-secret"}


@pytest.fixture
def manager(tmp_path):
    _, registration = make_demo_repository(tmp_path / "repo")
    registry = SourceRegistry(tmp_path / "registry")
    registry.register(registration)
    return RunManager(tmp_path / "state", registry=registry)


def complete_run(manager, trigger="api-fixture"):
    request = manager.registry.request_for_pr("org/service", 42)
    run, _ = manager.create_run(request, TOKENS["alice-secret"], trigger_identity=trigger)
    asyncio.run(manager.execute(run.run_id))
    result = manager.get_run(run.run_id)
    assert result.readiness == "READY_FOR_QA"
    return result.model_dump(mode="json")


@pytest.fixture
def run(manager):
    return complete_run(manager)


@pytest.fixture
def client(manager):
    with TestClient(create_app(manager, tokens=TOKENS)) as instance:
        yield instance


def review_payload(run):
    return {"decision": "APPROVED", "identity_hash": run["identity_hash"],
            "artifact_manifest_hash": run["artifact_manifest_hash"]}


def test_authentication_is_required_and_headers_cannot_forge_roles(client, run):
    path = f"/v1/test-runs/{run['run_id']}"
    assert client.get(path).status_code == 401
    assert client.get(path, headers={"X-Role": "qa", "X-Actor": "named-qa"}).status_code == 401
    assert client.get(path, headers={"Authorization": "Bearer bad"}).status_code == 401


def test_unconfigured_service_has_no_guessable_development_credentials(manager, run, monkeypatch):
    monkeypatch.delenv("EVO_ENGINEER_TOKEN", raising=False)
    monkeypatch.delenv("EVO_QA_TOKEN", raising=False)
    with TestClient(create_app(manager)) as instance:
        result = instance.get(f"/v1/test-runs/{run['run_id']}",
                              headers={"Authorization": "Bearer engineer-token"})
        assert result.status_code == 401


@pytest.mark.parametrize(("method", "suffix"), [
    ("GET", ""), ("GET", "/artifacts"), ("GET", "/artifacts/initial/junit.xml"),
    ("POST", "/review"), ("POST", "/feedback"),
])
def test_repository_acl_is_checked_before_evidence_access(client, run, manager, monkeypatch, method, suffix):
    def forbidden_read(*args):
        pytest.fail("Unauthorized caller reached freshness or evidence access")
    monkeypatch.setattr(manager, "get_run", forbidden_read)
    body = review_payload(run) if suffix == "/review" else {
        "kind": "RCA_CORRECTION", "identity_hash": run["identity_hash"]}
    result = client.request(method, f"/v1/test-runs/{run['run_id']}{suffix}",
        headers={"Authorization": "Bearer outsider-secret"}, json=body)
    assert result.status_code == 403


@pytest.mark.parametrize(("method", "path"), [
    ("POST", "/v1/test-runs"), ("POST", "/github/webhook"),
    ("GET", "/listenToSpecAndCodeAgents"), ("POST", "/verifyAndJudgeReadiness"),
    ("POST", "/v1/test-runs/run-example/cancel"),
])
def test_retired_agent_intake_is_absent_and_cannot_start_work(client, manager, monkeypatch, method, path):
    async def forbidden_execution(*args):
        pytest.fail("Retired endpoint started verification")
    monkeypatch.setattr(manager, "execute", forbidden_execution)
    response = client.request(method, path, json={"command": "arbitrary", "approved": True}, headers=ENGINEER)
    assert response.status_code in {404, 405}
    assert not manager.runs
    assert not hasattr(client.app.state, "tasks")
    assert not hasattr(client.app.state, "legacy_contexts")


def test_review_requires_qa_and_records_server_authenticated_name(client, manager, run):
    path = f"/v1/test-runs/{run['run_id']}/review"
    review = review_payload(run)
    assert client.post(path, json=review, headers={**ENGINEER, "X-Role": "qa"}).status_code == 403
    assert client.post(path, json={**review, "reviewer": "forged"}, headers=QA).status_code == 422
    assert client.post(path, json={**review, "artifact_manifest_hash": "d" * 64}, headers=QA).status_code == 409
    result = client.post(path, json=review, headers=QA)
    assert result.status_code == 200, result.text
    assert result.json()["qa_review"]["reviewer"] == "named-qa"
    loaded = RunManager(manager.storage, registry=manager.registry)
    assert loaded.get_run(run["run_id"]).qa_review.status == "APPROVED"


def test_evidence_serving_is_authenticated_and_filename_allowlisted(client, run):
    path = f"/v1/test-runs/{run['run_id']}/artifacts"
    assert client.get(path).status_code == 401
    manifest = client.get(path, headers=ENGINEER).json()
    junit = next(item for item in manifest["evidence"] if item["path"] == "initial/junit.xml")
    response = client.get(junit["uri"], headers=ENGINEER)
    assert response.status_code == 200 and b"test_empty" in response.content
    assert response.headers["cache-control"] == "no-store"
    assert hashlib.sha256(response.content).hexdigest() == junit["sha256"]
    assert client.get(path + "/unlisted.txt", headers=ENGINEER).status_code == 404
    assert client.get(path + "/..%5Csecret", headers=ENGINEER).status_code == 404


def test_tampered_execution_evidence_blocks_download_and_approval(client, manager, run):
    path = manager.artifact_path(run["run_id"], "initial/pytest.log")
    path.write_text("Forged passing result", encoding="utf-8")
    prefix = f"/v1/test-runs/{run['run_id']}"
    assert client.get(prefix + "/artifacts/initial/pytest.log", headers=ENGINEER).status_code == 409
    assert client.post(prefix + "/review", json=review_payload(run), headers=QA).status_code == 409
    assert manager.get_run(run["run_id"]).qa_review.status == "PENDING"


def test_tampered_verification_envelope_is_never_served_or_approved(client, manager, run):
    path = manager.artifact_path(run["run_id"], "verification.json")
    forged = json.loads(path.read_text(encoding="utf-8"))
    forged["reviewed_head_sha"] = "f" * 40
    path.write_text(json.dumps(forged), encoding="utf-8")
    prefix = f"/v1/test-runs/{run['run_id']}"
    assert client.get(prefix, headers=ENGINEER).status_code == 409
    assert client.get(prefix + "/artifacts/verification.json", headers=ENGINEER).status_code == 409
    assert client.post(prefix + "/review", json=review_payload(run), headers=QA).status_code == 409


def test_new_commit_stales_previous_run_and_requires_fresh_verification(client, manager, run):
    registration = manager.registry.registrations[("org/service", 42)]
    source = registration.repo_root / "src/service.py"
    source.write_text(source.read_text(encoding="utf-8") + "\n# fresh head\n", encoding="utf-8")
    git(registration.repo_root, "add", "src/service.py")
    git(registration.repo_root, "commit", "-m", "Fresh PR version")
    prefix = f"/v1/test-runs/{run['run_id']}"
    assert client.get(prefix, headers=ENGINEER).json()["readiness"] == "STALE"
    assert client.post(prefix + "/review", json=review_payload(run), headers=QA).status_code == 409
    fresh = complete_run(manager, "fresh-api-fixture")
    assert fresh["run_id"] != run["run_id"]
    assert fresh["reviewed_head_sha"] != run["reviewed_head_sha"]


def test_qa_rejection_cannot_be_overridden_without_fresh_run(client, run):
    path = f"/v1/test-runs/{run['run_id']}/review"
    rejected = client.post(path, json={**review_payload(run), "decision": "REJECTED"}, headers=QA)
    assert rejected.status_code == 200
    assert rejected.json()["readiness"] == "CHANGES_REQUIRED"
    assert client.post(path, json=review_payload(rejected.json()), headers=QA).status_code == 409


def test_feedback_records_authenticated_actor_without_changing_execution(client, manager, run):
    path = f"/v1/test-runs/{run['run_id']}/feedback"
    body = {"kind": "RCA_CORRECTION", "identity_hash": run["identity_hash"],
            "comment": "password=private-value"}
    assert client.post(path, json=body, headers=QA).status_code == 403
    assert client.post(path, json={**body, "actor": "forged"}, headers=ENGINEER).status_code == 422
    assert client.post(path, json=body, headers=ENGINEER).status_code == 200
    event = manager.memory.read_events()[-1]
    assert event["payload"]["actor"] == "alice"
    assert "private-value" not in json.dumps(event)
    assert manager.get_run(run["run_id"]).readiness == run["readiness"]


def test_environment_credentials_have_distinct_roles_and_named_reviewers(manager, run, monkeypatch):
    monkeypatch.setenv("EVO_ENGINEER_TOKEN", "configured-engineer-secret")
    monkeypatch.setenv("EVO_QA_TOKEN", "configured-qa-secret")
    monkeypatch.setenv("EVO_QA_NAME", "Jordan QA")
    with TestClient(create_app(manager)) as instance:
        path = f"/v1/test-runs/{run['run_id']}/review"
        result = instance.post(path, json=review_payload(run),
            headers={"Authorization": "Bearer configured-qa-secret"})
        assert result.status_code == 200
        assert result.json()["qa_review"]["reviewer"] == "Jordan QA"
    monkeypatch.setenv("EVO_QA_TOKEN", "configured-engineer-secret")
    with pytest.raises(ValueError, match="must be distinct"):
        create_app(manager)


def test_missing_run_is_not_found(client):
    assert client.get("/v1/test-runs/missing", headers=ENGINEER).status_code == 404
