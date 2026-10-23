"""Black-box API security/compatibility checks with an injected deterministic manager.

Manager execution and sandbox behavior are exercised separately by core tests and
acceptance demos; these tests isolate HTTP trust boundaries and context handling.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.models import CreateRunRequest, Principal, canonical_hash


REQUEST = {
    "repo": "org/service", "pr_number": 42,
    "spec_id": "SPEC-42", "spec_uri": "registry://spec-42", "spec_hash": "a" * 64,
    "test_profile_id": "pytest-default-v1", "environment_manifest_id": "sandbox-v1",
    "correlation_id": "api-test",
}
TOKENS = {
    "alice-secret": Principal(name="alice", roles=frozenset({"engineer"}), repos=frozenset({"org/service"})),
    "bob-secret": Principal(name="bob", roles=frozenset({"engineer"}), repos=frozenset({"org/service"})),
    "qa-secret": Principal(name="named-qa", roles=frozenset({"qa"}), repos=frozenset({"org/service"})),
    "outsider-secret": Principal(name="outsider", roles=frozenset({"engineer", "qa"}), repos=frozenset({"other/repo"})),
}
ENGINEER = {"Authorization": "Bearer alice-secret"}
QA = {"Authorization": "Bearer qa-secret"}


class Registry:
    def __init__(self):
        self.head = "b" * 40

    def request_for_pr(self, repo, pr):
        if (repo, pr) != ("org/service", 42):
            raise KeyError("Unregistered repository")
        return CreateRunRequest(**REQUEST)

    def resolve(self, request):
        return SimpleNamespace(
            identity_hash=canonical_hash({"head": self.head, "spec": request.spec_hash}),
            head_sha=self.head,
            spec=SimpleNamespace(intent="Verify approved behavior for empty inputs"),
            environment=SimpleNamespace(prerequisites=[]),
        )

    def diff(self, context):
        return "--- base\n+++ head\n+ implementation change\n"


class Manager:
    def __init__(self, root):
        self.registry = Registry()
        self.delivery_runs = {}
        self.runs = {}
        self.keys = {}
        self.persist_count = 0
        self.root = root
        self.reviewed_by = None
        self.feedback_by = None
        self.cancelled_by = None
        self.requests = []

    def create_run(self, request, principal, *, trigger_identity="", idempotency_key=None):
        self.requests.append(request)
        binding = canonical_hash({"request": request.model_dump(), "trigger": trigger_identity})
        key = idempotency_key or binding
        if key in self.keys:
            old_binding, run_id = self.keys[key]
            if old_binding != binding:
                raise ValueError("Idempotency key conflicts with immutable inputs")
            return self.runs[run_id], True
        run_id = f"run-{len(self.runs) + 1}"
        self.runs[run_id] = {
            "run_id": run_id, "repo": request.repo, "pr_number": request.pr_number, "execution_status": "QUEUED",
            "readiness": "INCONCLUSIVE", "identity_hash": self.registry.resolve(request).identity_hash,
            "artifact_manifest_hash": "c" * 64, "human_approval_required": True,
            "candidate_patch": None, "unresolved_findings": [], "qa_review": {"status": "PENDING"},
        }
        self.keys[key] = (binding, run_id)
        return self.runs[run_id], False

    async def execute(self, run_id):
        self.runs[run_id].update(execution_status="COMPLETED", readiness="READY_FOR_QA")

    def get_run(self, run_id):
        return self.runs[run_id]

    def artifacts(self, run_id):
        return {"schema_version": "1.0", "run_id": run_id, "artifacts": []}

    def artifact_path(self, run_id, name):
        if name != "test_report.md":
            raise KeyError("Artifact not listed")
        path = self.root / name
        path.write_text("Verified report", encoding="utf-8")
        return path

    def cancel_run(self, run_id, principal):
        self.cancelled_by = principal.name
        self.runs[run_id].update(execution_status="CANCELLED", readiness="INCONCLUSIVE")
        return self.runs[run_id]

    def review_run(self, run_id, request, principal):
        run = self.runs[run_id]
        if request.identity_hash != run["identity_hash"] or request.artifact_manifest_hash != run["artifact_manifest_hash"]:
            raise ValueError("Review must bind exact identity and evidence manifest")
        if run["readiness"] == "STALE":
            raise ValueError("Cannot review stale verification")
        self.reviewed_by = principal.name
        run["qa_review"] = {"status": request.decision, "reviewer": principal.name}
        return run

    def feedback_run(self, run_id, request, principal):
        if request.identity_hash != self.runs[run_id]["identity_hash"]:
            raise ValueError("Feedback identity mismatch")
        self.feedback_by = principal.name
        return {"recorded": True, "actor": principal.name, "kind": request.kind}

    def invalidate_stale(self, repo, pr_number):
        invalidated = []
        current_hash = self.registry.resolve(CreateRunRequest(**REQUEST)).identity_hash
        for run_id, run in self.runs.items():
            if run["repo"] == repo and run["identity_hash"] != current_hash:
                run["readiness"] = "STALE"
                invalidated.append(run_id)
        return invalidated

    def persist(self):
        self.persist_count += 1


@pytest.fixture
def manager(tmp_path):
    return Manager(tmp_path)


@pytest.fixture
def client(manager):
    with TestClient(create_app(manager, tokens=TOKENS, webhook_secret="webhook-test-secret", webhook_senders={"trusted-sender"})) as instance:
        yield instance


def new_run(client, **updates):
    response = client.post("/v1/test-runs", json={**REQUEST, **updates}, headers=ENGINEER)
    assert response.status_code == 202, response.text
    return response.json()["run_id"]


def signed(payload, *, delivery="delivery-42", event="pull_request", secret="webhook-test-secret"):
    body = json.dumps(payload, separators=(",", ":")).encode()
    signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return body, {"X-Hub-Signature-256": signature, "X-GitHub-Delivery": delivery, "X-GitHub-Event": event, "Content-Type": "application/json"}


def payload():
    return {"repository": {"full_name": "org/service"}, "sender": {"login": "trusted-sender"},
            "action": "labeled", "label": {"name": "test:verify"}, "pull_request": {"number": 42}}


def test_authentication_is_required_and_headers_cannot_forge_roles(client):
    assert client.post("/v1/test-runs", json=REQUEST).status_code == 401
    assert client.post("/v1/test-runs", json=REQUEST, headers={"X-Role": "engineer", "X-Actor": "alice"}).status_code == 401
    assert client.post("/v1/test-runs", json=REQUEST, headers={"Authorization": "Bearer bad"}).status_code == 401
    assert client.post("/v1/test-runs", json={**REQUEST, "actor": "qa"}, headers=ENGINEER).status_code == 422


def test_unconfigured_service_has_no_guessable_development_credentials(manager, monkeypatch):
    monkeypatch.delenv("EVO_ENGINEER_TOKEN", raising=False)
    monkeypatch.delenv("EVO_QA_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_WEBHOOK_SECRET", raising=False)
    with TestClient(create_app(manager)) as instance:
        assert instance.post("/v1/test-runs", json=REQUEST, headers={"Authorization": "Bearer engineer-token"}).status_code == 401
        assert instance.post("/github/webhook", json=payload()).status_code == 503


def test_repository_acl_protects_status_artifacts_and_writes(client):
    run_id = new_run(client)
    denied = {"Authorization": "Bearer outsider-secret"}
    for path in (f"/v1/test-runs/{run_id}", f"/v1/test-runs/{run_id}/artifacts", f"/v1/test-runs/{run_id}/artifacts/test_report.md"):
        assert client.get(path, headers=denied).status_code == 403
    assert client.post(f"/v1/test-runs/{run_id}/cancel", headers=denied).status_code == 403
    assert client.post("/v1/test-runs", json=REQUEST, headers=denied).status_code == 403


def test_idempotency_reuses_same_run_and_rejects_different_inputs(client, manager):
    headers = {**ENGINEER, "Idempotency-Key": "request-42"}
    first = client.post("/v1/test-runs", json=REQUEST, headers=headers)
    second = client.post("/v1/test-runs", json=REQUEST, headers=headers)
    assert first.status_code == second.status_code == 202
    assert first.json()["run_id"] == second.json()["run_id"]
    assert second.json()["reused"] is True
    changed = client.post("/v1/test-runs", json={**REQUEST, "max_debug_iterations": 1}, headers=headers)
    assert changed.status_code == 409
    assert len(manager.runs) == 1


def test_webhook_verifies_raw_signature_before_any_json_or_source_access(client, manager):
    assert client.post("/github/webhook", content=b"not-json", headers={"X-Hub-Signature-256": "sha256=bad"}).status_code == 401
    body, headers = signed(payload(), secret="wrong-secret")
    assert client.post("/github/webhook", content=body, headers=headers).status_code == 401
    assert not manager.runs
    assert manager.persist_count == 0


def test_webhook_duplicate_delivery_survives_app_reconstruction(client, manager):
    body, headers = signed(payload())
    first = client.post("/github/webhook", content=body, headers=headers)
    assert first.status_code == 202
    with TestClient(create_app(manager, tokens=TOKENS, webhook_secret="webhook-test-secret", webhook_senders={"trusted-sender"})) as restarted:
        duplicate = restarted.post("/github/webhook", content=body, headers=headers)
    assert duplicate.status_code == 202
    assert duplicate.json()["run_id"] == first.json()["run_id"]
    assert duplicate.json()["reused"] is True
    assert len(manager.runs) == 1
    assert manager.persist_count == 1


def test_only_allowlisted_signed_github_senders_and_eligible_events_trigger(client, manager):
    spoof = payload()
    spoof["sender"]["login"] = "unknown-sender"
    body, headers = signed(spoof)
    assert client.post("/github/webhook", content=body, headers=headers).status_code == 403
    ignored = payload()
    ignored["label"]["name"] = "unrelated"
    body, headers = signed(ignored)
    response = client.post("/github/webhook", content=body, headers=headers)
    assert response.status_code == 202
    assert response.json()["accepted"] is False
    assert not manager.runs


def test_issue_comment_requires_pull_request_and_exact_command(client, manager):
    comment = {"repository": {"full_name": "org/service"}, "sender": {"login": "trusted-sender"},
               "action": "created", "issue": {"number": 42}, "comment": {"body": "/test-agent run"}}
    body, headers = signed(comment, event="issue_comment")
    assert client.post("/github/webhook", content=body, headers=headers).json()["accepted"] is False
    comment["issue"]["pull_request"] = {"url": "https://api.github.com/repos/org/service/pulls/42"}
    body, headers = signed(comment, event="issue_comment")
    assert client.post("/github/webhook", content=body, headers=headers).status_code == 202
    assert len(manager.runs) == 1


def test_review_requires_qa_and_records_server_authenticated_name(client, manager):
    run_id = new_run(client)
    run = client.get(f"/v1/test-runs/{run_id}", headers=ENGINEER).json()
    review = {"decision": "APPROVED", "identity_hash": run["identity_hash"], "artifact_manifest_hash": run["artifact_manifest_hash"]}
    assert client.post(f"/v1/test-runs/{run_id}/review", json=review, headers={**ENGINEER, "X-Role": "qa"}).status_code == 403
    assert client.post(f"/v1/test-runs/{run_id}/review", json={**review, "reviewer": "forged"}, headers=QA).status_code == 422
    assert client.post(f"/v1/test-runs/{run_id}/review", json={**review, "artifact_manifest_hash": "d" * 64}, headers=QA).status_code == 409
    result = client.post(f"/v1/test-runs/{run_id}/review", json=review, headers=QA)
    assert result.status_code == 200
    assert manager.reviewed_by == "named-qa"
    manager.runs[run_id]["readiness"] = "STALE"
    assert client.post(f"/v1/test-runs/{run_id}/review", json=review, headers=QA).status_code == 409


def test_evidence_serving_is_authenticated_and_filename_allowlisted(client):
    run_id = new_run(client)
    assert client.get(f"/v1/test-runs/{run_id}/artifacts").status_code == 401
    manifest = client.get(f"/v1/test-runs/{run_id}/artifacts", headers=ENGINEER)
    assert manifest.status_code == 200 and manifest.json()["schema_version"] == "1.0"
    report = client.get(f"/v1/test-runs/{run_id}/artifacts/test_report.md", headers=ENGINEER)
    assert report.status_code == 200 and report.text == "Verified report"
    assert client.get(f"/v1/test-runs/{run_id}/artifacts/unlisted.txt", headers=ENGINEER).status_code == 404
    assert client.get(f"/v1/test-runs/{run_id}/artifacts/..%5Csecret", headers=ENGINEER).status_code == 404


def test_cancel_and_feedback_use_authenticated_actor(client, manager):
    run_id = new_run(client)
    run = client.get(f"/v1/test-runs/{run_id}", headers=ENGINEER).json()
    feedback = {"kind": "RCA_CORRECTION", "comment": "Observed dependency issue", "identity_hash": run["identity_hash"]}
    assert client.post(f"/v1/test-runs/{run_id}/feedback", json=feedback, headers=ENGINEER).status_code == 200
    assert manager.feedback_by == "alice"
    assert client.post(f"/v1/test-runs/{run_id}/cancel", headers=QA).status_code == 403
    cancel = client.post(f"/v1/test-runs/{run_id}/cancel", headers=ENGINEER)
    assert cancel.status_code == 200 and cancel.json()["readiness"] == "INCONCLUSIVE"
    assert manager.cancelled_by == "alice"


def legacy_context(client, headers=ENGINEER):
    response = client.get("/listenToSpecAndCodeAgents", params={"repo_url": "https://github.com/org/service", "pr_number": 42, "prd_reference": "SPEC-42", "memory_source": "local", "timeout": 5}, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["prd_intent"]
    return response.json()["context_id"]


def test_legacy_adapter_requires_explicit_context_and_never_shares_other_users_context(client):
    missing = {"pytest_suite": "pytest-default-v1", "max_debug_iterations": 2, "compute_budget": 2.0}
    assert client.post("/verifyAndJudgeReadiness", json=missing, headers=ENGINEER).status_code == 422
    context_id = legacy_context(client)
    body = {**missing, "context_id": context_id}
    assert client.post("/verifyAndJudgeReadiness", json=body, headers={"Authorization": "Bearer bob-secret"}).status_code == 404
    result = client.post("/verifyAndJudgeReadiness", json=body, headers=ENGINEER)
    assert result.status_code == 200, result.text
    assert result.json()["deployment_readiness"] is True
    assert result.json()["human_approval_required"] is True
    assert result.json()["next_phase_target"] == "QA"


def test_legacy_context_rejects_changed_head_and_suite_injection(client, manager):
    context_id = legacy_context(client)
    body = {"context_id": context_id, "pytest_suite": "../../untrusted.py", "max_debug_iterations": 2, "compute_budget": 2.0}
    assert client.post("/verifyAndJudgeReadiness", json=body, headers=ENGINEER).status_code == 422
    body["pytest_suite"] = "pytest-default-v1"
    manager.registry.head = "d" * 40
    assert client.post("/verifyAndJudgeReadiness", json=body, headers=ENGINEER).status_code == 409
    assert not manager.runs


def test_duplicate_delivery_cannot_be_reused_for_another_repository(client):
    body, headers = signed(payload())
    assert client.post("/github/webhook", content=body, headers=headers).status_code == 202
    different_repo = payload()
    different_repo["repository"]["full_name"] = "other/repo"
    body, headers = signed(different_repo)
    assert client.post("/github/webhook", content=body, headers=headers).status_code == 409
    different_pr = payload()
    different_pr["pull_request"]["number"] = 43
    body, headers = signed(different_pr)
    assert client.post("/github/webhook", content=body, headers=headers).status_code == 409


def test_synchronize_requires_updated_registered_pointer_and_invalidates_prior_run(client, manager):
    run_id = new_run(client)
    update = payload()
    update["action"] = "synchronize"
    update["pull_request"]["head"] = {"sha": "f" * 40}
    body, headers = signed(update, delivery="synchronize-42")
    assert client.post("/github/webhook", content=body, headers=headers).status_code == 409
    manager.registry.head = "f" * 40
    result = client.post("/github/webhook", content=body, headers=headers)
    assert result.status_code == 202
    assert result.json()["invalidated_run_ids"] == [run_id]
    assert result.json()["queued"] is False
    assert manager.runs[run_id]["readiness"] == "STALE"
    assert len(manager.runs) == 1

# These tests use the real manager, Git snapshots and actual pytest subprocesses.
@pytest.fixture
def real_manager(tmp_path):
    from app.providers import SourceRegistry, make_demo_repository
    from app.run_manager import RunManager
    repo_root, registration = make_demo_repository(tmp_path / "repo")
    registry = SourceRegistry(tmp_path / "registry")
    registry.register(registration)
    return RunManager(tmp_path / "state", registry=registry)


@pytest.fixture
def real_client(real_manager):
    with TestClient(create_app(real_manager, tokens=TOKENS, webhook_secret="webhook-test-secret", webhook_senders={"trusted-sender"})) as instance:
        yield instance


def real_request(manager):
    return manager.registry.request_for_pr("org/service", 42).model_dump(mode="json")


def complete_real_run(client, manager, **updates):
    import time
    response = client.post("/v1/test-runs", json={**real_request(manager), **updates}, headers=ENGINEER)
    assert response.status_code == 202, response.text
    run_id = response.json()["run_id"]
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        result = client.get(f"/v1/test-runs/{run_id}", headers=ENGINEER)
        assert result.status_code == 200, result.text
        run = result.json()
        if run["execution_status"] in {"COMPLETED", "BLOCKED", "CANCELLED", "ERROR"}:
            return run
        time.sleep(0.02)
    pytest.fail(f"Real run {run_id} did not terminate within 20 seconds")


def review_payload(run):
    return {"decision": "APPROVED", "identity_hash": run["identity_hash"], "artifact_manifest_hash": run["artifact_manifest_hash"]}


def test_real_manager_nested_evidence_and_named_qa_review_survive_restart(real_client, real_manager):
    from app.run_manager import RunManager
    run = complete_real_run(real_client, real_manager)
    assert run["readiness"] == "READY_FOR_QA", run
    manifest = real_client.get(f"/v1/test-runs/{run['run_id']}/artifacts", headers=ENGINEER).json()
    junit = next(item for item in manifest["evidence"] if item["path"] == "initial/junit.xml")
    evidence = real_client.get(junit["uri"], headers=ENGINEER)
    assert evidence.status_code == 200, evidence.text
    assert hashlib.sha256(evidence.content).hexdigest() == junit["sha256"]
    assert b"test_empty" in evidence.content
    review = real_client.post(f"/v1/test-runs/{run['run_id']}/review", json=review_payload(run), headers=QA)
    assert review.status_code == 200, review.text
    assert review.json()["qa_review"]["reviewer"] == "named-qa"
    loaded = RunManager(real_manager.storage, registry=real_manager.registry)
    assert loaded.get_run(run["run_id"]).qa_review.status == "APPROVED"


def test_real_manager_tampered_execution_evidence_blocks_download_and_approval(real_client, real_manager):
    run = complete_real_run(real_client, real_manager)
    assert run["readiness"] == "READY_FOR_QA", run
    path = real_manager.artifact_path(run["run_id"], "initial/pytest.log")
    path.write_text("Forged passing result", encoding="utf-8")
    result = real_client.get(f"/v1/test-runs/{run['run_id']}/artifacts/initial/pytest.log", headers=ENGINEER)
    assert result.status_code == 409
    review = real_client.post(f"/v1/test-runs/{run['run_id']}/review", json=review_payload(run), headers=QA)
    assert review.status_code == 409
    assert real_manager.get_run(run["run_id"]).qa_review.status == "PENDING"


def test_real_manager_tampered_verification_envelope_is_never_served_or_approved(real_client, real_manager):
    run = complete_real_run(real_client, real_manager)
    assert run["readiness"] == "READY_FOR_QA", run
    path = real_manager.artifact_path(run["run_id"], "verification.json")
    forged = json.loads(path.read_text(encoding="utf-8"))
    forged["reviewed_head_sha"] = "f" * 40
    path.write_text(json.dumps(forged), encoding="utf-8")
    result = real_client.get(f"/v1/test-runs/{run['run_id']}/artifacts/verification.json", headers=ENGINEER)
    assert result.status_code == 409
    review = real_client.post(f"/v1/test-runs/{run['run_id']}/review", json=review_payload(run), headers=QA)
    assert review.status_code == 409


def test_real_manager_signed_new_commit_stales_previous_run_then_fresh_run_passes(real_client, real_manager):
    from app.providers import git
    run = complete_real_run(real_client, real_manager)
    assert run["readiness"] == "READY_FOR_QA", run
    registration = real_manager.registry.registrations[("org/service", 42)]
    source = registration.repo_root / "src" / "service.py"
    source.write_text(source.read_text(encoding="utf-8") + "\n# Fresh PR version\n", encoding="utf-8")
    git(registration.repo_root, "add", "src/service.py")
    git(registration.repo_root, "commit", "-m", "New PR head for stale assessment test")
    new_head = git(registration.repo_root, "rev-parse", "HEAD")
    update = payload()
    update["action"] = "synchronize"
    update["pull_request"]["head"] = {"sha": new_head}
    raw, headers = signed(update, delivery="real-synchronize")
    synchronize = real_client.post("/github/webhook", content=raw, headers=headers)
    assert synchronize.status_code == 202, synchronize.text
    assert run["run_id"] in synchronize.json()["invalidated_run_ids"]
    assert real_client.get(f"/v1/test-runs/{run['run_id']}", headers=ENGINEER).json()["readiness"] == "STALE"
    assert real_client.post(f"/v1/test-runs/{run['run_id']}/review", json=review_payload(run), headers=QA).status_code == 409
    fresh = complete_real_run(real_client, real_manager)
    assert fresh["run_id"] != run["run_id"]
    assert fresh["reviewed_head_sha"] == new_head
    assert fresh["readiness"] == "READY_FOR_QA", fresh


def test_real_manager_restart_recovery_preserves_later_completed_runs(real_client, real_manager):
    from app.models import CreateRunRequest
    from app.run_manager import RunManager
    queued, _ = real_manager.create_run(CreateRunRequest(**real_request(real_manager)), TOKENS["alice-secret"], trigger_identity="queued-before-completed")
    completed = complete_real_run(real_client, real_manager)
    assert completed["readiness"] == "READY_FOR_QA", completed
    first_restart = RunManager(real_manager.storage, registry=real_manager.registry)
    assert first_restart.get_run(queued.run_id).execution_status == "ERROR"
    assert first_restart.get_run(completed["run_id"]).readiness == "READY_FOR_QA"
    second_restart = RunManager(real_manager.storage, registry=real_manager.registry)
    assert second_restart.get_run(completed["run_id"]).readiness == "READY_FOR_QA"

def test_environment_credentials_have_distinct_roles_and_named_reviewers(manager, monkeypatch):
    monkeypatch.setenv("EVO_ENGINEER_TOKEN", "configured-engineer-secret")
    monkeypatch.setenv("EVO_QA_TOKEN", "configured-qa-secret")
    monkeypatch.setenv("EVO_QA_NAME", "Jordan QA")
    with TestClient(create_app(manager)) as instance:
        engineer = {"Authorization": "Bearer configured-engineer-secret"}
        qa = {"Authorization": "Bearer configured-qa-secret"}
        result = instance.post("/v1/test-runs", json=REQUEST, headers=engineer)
        assert result.status_code == 202
        run_id = result.json()["run_id"]
        run = instance.get(f"/v1/test-runs/{run_id}", headers=engineer).json()
        review = instance.post(f"/v1/test-runs/{run_id}/review", json=review_payload(run), headers=qa)
        assert review.status_code == 200
        assert review.json()["qa_review"]["reviewer"] == "Jordan QA"
    monkeypatch.setenv("EVO_QA_TOKEN", "configured-engineer-secret")
    with pytest.raises(ValueError, match="must be distinct"):
        create_app(manager)
