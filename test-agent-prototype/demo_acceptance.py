"""Run five acceptance scenarios with real local Git commits and pytest.

No GitHub, model, container pull, cloud credential, or network access is used.
"""
from __future__ import annotations
import argparse
import asyncio
from hashlib import sha256
import json
from pathlib import Path
from uuid import uuid4

from app.models import Principal, Readiness, ReviewRequest, FeedbackRequest, Verification
from app.providers import SourceRegistry, make_demo_repository, GOOD_SOURCE, BAD_SOURCE, git
from app.rca_and_repair import FixtureRepairAdapter, CandidatePatchProposal
from app.run_manager import RunManager

ENGINEER = Principal(name="demo-engineer", roles=frozenset({"engineer"}))
QA = Principal(name="named-human-qa-reviewer", roles=frozenset({"qa"}))


def configured(root: Path, **fixture):
    root.mkdir(parents=True, exist_ok=True)
    repo, entry = make_demo_repository(root / "repository", **fixture)
    registry = SourceRegistry(root / "sources")
    registry.register(entry)
    manager = RunManager(root / "state", registry)
    return manager, repo


async def verify(manager, trigger="demo"):
    request = manager.registry.request_for_pr("org/service", 42)
    run, reused = manager.create_run(request, ENGINEER, trigger_identity=trigger)
    assert not reused
    await manager.execute(run.run_id)
    result = manager.get_run(run.run_id)
    Verification.model_validate_json(manager.artifact_path(run.run_id, "verification.json").read_text())
    manifest = manager.artifacts(run.run_id)
    for entry in manifest["evidence"]:
        artifact = manager.artifact_path(run.run_id, entry["path"])
        assert sha256(artifact.read_bytes()).hexdigest() == entry["sha256"]
    return result


async def run_acceptance(output: Path):
    output.mkdir(parents=True, exist_ok=False)
    results = []
    # 1: Actual pytest success, independent named QA decision, immutable evidence.
    manager, _ = configured(output / "01-clean")
    clean = await verify(manager)
    assert clean.readiness == Readiness.READY_FOR_QA, clean.model_dump()
    approved = manager.review_run(clean.run_id, ReviewRequest(decision="APPROVED",
        identity_hash=clean.identity_hash, artifact_manifest_hash=clean.artifact_manifest_hash), QA)
    assert approved.qa_review.reviewer == QA.name and approved.qa_review.status == "APPROVED"
    assert approved.human_approval_required is True
    results.append({"scenario": 1, "name": "Clean pass and human QA approval", "run_id": clean.run_id, "passed": True})

    # 2: Source-only patch on separate local branch, then fresh actual-head run.
    manager, repo = configured(output / "02-repair", regression=True)
    manager.repair_adapter = FixtureRepairAdapter(changes={"src/service.py": GOOD_SOURCE})
    repaired = await verify(manager)
    assert repaired.readiness == Readiness.CHANGES_REQUIRED, repaired.model_dump()
    assert repaired.unresolved_findings[0].failure_class == "CODE_REGRESSION"
    assert repaired.candidate_patch and repaired.candidate_patch["validated"]
    assert repaired.metrics.debug_iterations == 1 and repaired.metrics.test_executions == 3
    assert (repo / "src" / "service.py").read_text() == BAD_SOURCE
    prior_sha = repaired.reviewed_head_sha
    (repo / "src" / "service.py").write_text(GOOD_SOURCE, encoding="utf-8")
    git(repo, "add", "src/service.py"); git(repo, "commit", "-m", "Engineer incorporates reviewed candidate")
    fresh = await verify(manager, "fresh-head")
    assert fresh.reviewed_head_sha != prior_sha
    assert fresh.readiness == Readiness.READY_FOR_QA
    manager.feedback_run(fresh.run_id, FeedbackRequest(kind="CODE_AGENT_ACK", identity_hash=fresh.identity_hash), ENGINEER)
    results.append({"scenario": 2, "name": "Regression, bounded repair, incorporation and fresh verification",
                    "run_id": repaired.run_id, "fresh_run_id": fresh.run_id, "passed": True})

    # 3: Missing prerequisites block before test execution or patching.
    manager, _ = configured(output / "03-environment", missing_env=True)
    blocked = await verify(manager)
    assert blocked.readiness == Readiness.BLOCKED
    assert any(x.failure_class == "ENVIRONMENT_BLOCKED" and x.owner_role == "environment_owner" for x in blocked.unresolved_findings)
    assert blocked.metrics.debug_iterations == blocked.metrics.test_executions == 0
    results.append({"scenario": 3, "name": "Missing environment prerequisite", "run_id": blocked.run_id, "passed": True})

    # 4: Conflicting approved intent blocks without speculative code changes.
    manager, _ = configured(output / "04-spec", ambiguous=True)
    blocked = await verify(manager)
    assert blocked.readiness == Readiness.BLOCKED
    assert any(x.failure_class == "SPEC_AMBIGUOUS" and "REQ-1" in x.summary for x in blocked.unresolved_findings)
    assert blocked.metrics.debug_iterations == blocked.metrics.test_executions == 0
    results.append({"scenario": 4, "name": "Ambiguous specification", "run_id": blocked.run_id, "passed": True})

    # 5: Signed duplicate webhook; two failed repairs; late commit invalidates ready.
    manager, _ = configured(output / "05-budget", regression=True)
    manager.repair_adapter = FixtureRepairAdapter(proposals=[
        CandidatePatchProposal({"src/service.py": BAD_SOURCE + "\n# failed candidate one\n"}, "Attempt one"),
        CandidatePatchProposal({"src/service.py": BAD_SOURCE + "\n# failed candidate two\n"}, "Attempt two")])
    exhausted = await verify(manager)
    assert exhausted.metrics.debug_iterations == 2 and exhausted.metrics.test_executions == 4
    assert exhausted.readiness == Readiness.CHANGES_REQUIRED and exhausted.candidate_patch is None
    assert any("NEEDS_ENGINEER" in reason for reason in exhausted.reasons)
    # Exercise the real webhook API (ASGI in-process), not a mock ledger.
    import httpx, hashlib, hmac
    from app.main import create_app
    app = create_app(manager, tokens={"engineer-token": ENGINEER, "qa-token": QA},
                     webhook_secret="offline-demo-secret", webhook_senders={"demo-engineer"})
    payload = dict(action="labeled", repository={"full_name": "org/service"},
                   sender={"login": "demo-engineer"}, pull_request={"number": 42}, label={"name": "test:verify"})
    raw = json.dumps(payload).encode()
    headers = {"X-Hub-Signature-256": "sha256=" + hmac.new(b"offline-demo-secret", raw, hashlib.sha256).hexdigest(),
               "X-GitHub-Delivery": "offline-delivery-1", "X-GitHub-Event": "pull_request"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://offline") as client:
        first = await client.post("/github/webhook", content=raw, headers=headers)
        second = await client.post("/github/webhook", content=raw, headers=headers)
        assert first.status_code == second.status_code == 202, (first.text, second.text)
        assert first.json()["run_id"] == second.json()["run_id"] and second.json()["reused"]
        task = app.state.tasks.get(first.json()["run_id"])
        if task:
            await task
    ready_manager, repo = configured(output / "05-stale")
    old = await verify(ready_manager)
    (repo / "src" / "service.py").write_text(GOOD_SOURCE + "\n# New unverified commit\n", encoding="utf-8")
    git(repo, "add", "."); git(repo, "commit", "-m", "New PR head invalidates prior result")
    assert ready_manager.get_run(old.run_id).readiness == Readiness.STALE
    try:
        ready_manager.review_run(old.run_id, ReviewRequest(decision="APPROVED", identity_hash=old.identity_hash,
            artifact_manifest_hash=old.artifact_manifest_hash), QA)
    except ValueError:
        pass
    else:
        raise AssertionError("Stale QA approval must be rejected")
    results.append({"scenario": 5, "name": "Budget exhaustion, duplicate webhook, stale invalidation",
                    "run_id": exhausted.run_id, "stale_run_id": old.run_id, "passed": True})
    summary = dict(schema_version="1.0", passed=len(results), scenarios=results,
        external_network_used=False, model_calls=0, evidence="Real local Git commits and pytest subprocesses",
        limitation="Fixture patch proposals are deterministic; live model/GitHub publication and Docker are not exercised.")
    (output / "acceptance_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    for item in results:
        print(f"PASS {item['scenario']}: {item['name']}")
    print(f"All five acceptance scenarios passed. Evidence: {output}")
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("demo-output") / uuid4().hex[:10])
    args = parser.parse_args()
    asyncio.run(run_acceptance(args.output.resolve()))
