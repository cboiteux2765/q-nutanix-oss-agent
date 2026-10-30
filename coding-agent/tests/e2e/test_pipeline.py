"""End-to-end Coding Agent runs: real git clones, real pytest subprocesses, a local 'origin' remote.

The last two tests need a real environment and skip otherwise:
  Docker:      docker build -t evo-coding-sandbox:py312 -f docker/sandbox.Dockerfile docker
  Live GitHub: EVO_E2E_GITHUB_REPO=owner/sandbox-repo (seeded from pilot/repo) and EVO_GITHUB_TOKEN with
               contents:write + pull_requests:write on that repo only. Opens a real draft PR.
"""
import json
import os
import subprocess
import uuid

import pytest

from conftest import SCENARIOS
from evo_coding import gitops
from evo_coding.agent import CodingAgent, RunConflict
from evo_coding.config import CodingConfig, SpendLedger, WorkspaceConfig
from evo_coding.handoff import sha256_file
from evo_coding.implementers.scripted import ScriptedImplementer
from evo_coding.models import CodingResult, Owner, RevisionRequest, RunState
from evo_coding.pilot import build_handoff, check_expected, feedback_for, load_scenario, scenario_files
from evo_coding.publish import GitHubPublisher
from evo_coding.workspace import docker_available

pytestmark = pytest.mark.e2e


def artifacts(agent, result):
    return agent.run_root(result.run_id) / f"r{result.revision}" / "artifacts"


@pytest.mark.parametrize("scenario", [p.stem for p in scenario_files()])
def test_pilot_scenario(pilot, scenario):
    expected = load_scenario(SCENARIOS / f"{scenario}.toml")["expected"]
    agent = pilot.agent(pilot.script(scenario))
    result = agent.run(pilot.handoff(scenario, f"run-{scenario}"))
    assert check_expected(expected, result) == [], result.blockers
    out = artifacts(agent, result)
    saved = CodingResult.model_validate_json((out / "coding_result.json").read_text(encoding="utf-8"))
    assert saved == result
    assert (out / "work_report.md").is_file() and (out / "trace.jsonl").is_file()
    for ref in result.artifacts:  # every artifact is hash-bound
        assert sha256_file(out / ref.uri) == ref.sha256
    if result.status != RunState.TESTING:
        assert len(pilot.remote_log()) == 1, "nothing may be pushed when blocked"


def test_happy_path_publishes_exactly_the_tested_commit(pilot):
    agent = pilot.agent()
    result = agent.run(pilot.handoff())
    assert result.status == RunState.TESTING and result.next_owner == Owner.TEST_AGENT
    assert result.branch == "evo/issue-1-run-001"
    remote_head = gitops.git(pilot.remote, "rev-parse", f"refs/heads/{result.branch}")
    assert remote_head == result.pr_head_sha
    assert gitops.git(pilot.remote, "rev-parse", "refs/heads/main") == pilot.sha, "main is never touched"
    final = [t for t in result.tests if t.label == "final"][-1]
    assert final.commit == result.pr_head_sha and final.passed == 8
    assert [t.label for t in result.tests] == ["baseline", "final"]
    assert "(ordered[middle - 1] + ordered[middle]) / 2" in pilot.remote_file(result.pr_head_sha,
                                                                              "src/pilot_stats/stats.py")
    message = gitops.git(pilot.remote, "log", "-1", "--format=%B", result.pr_head_sha)
    assert f"Evo-Spec-Hash: {result.spec_hash}" in message and "Evo-Run-Id: run-001" in message
    author = gitops.git(pilot.remote, "log", "-1", "--format=%an <%ae>", result.pr_head_sha)
    assert author == "EVO Coding Agent <evo-coding-agent@users.noreply.github.com>"
    assert {c.path for c in result.changed_files} == {"src/pilot_stats/stats.py", "tests/test_median.py"}
    assert all(c.status == "IMPLEMENTED" for c in result.criteria_coverage)
    pr = pilot.publisher.pulls()[0]
    assert pr["draft"] and pr["head"] == result.branch and pr["base"] == "main" and "Refs #1" in pr["body"]
    request = result.test_agent_request
    assert request["pr_number"] == result.pr_number and request["spec_hash"] == result.spec_hash
    assert request["spec_uri"].startswith("file:")
    assert [t.to_state for t in result.transitions] == [RunState.CODING, RunState.TESTING]


def test_rerun_is_idempotent_and_conflicting_reuse_is_rejected(pilot):
    agent = pilot.agent()
    handoff = pilot.handoff()
    first = agent.run(handoff)
    log_before = pilot.remote_log()
    again = pilot.agent({"actions": [{"write": "src/pilot_stats/stats.py", "content": "boom"}]}).run(handoff)
    assert again == first
    assert pilot.remote_log() == log_before and len(pilot.publisher.pulls()) == 1
    data = json.loads(handoff.read_text(encoding="utf-8"))
    data["issue_title"] = "something else"
    handoff.write_text(json.dumps(data))
    with pytest.raises(RunConflict):
        agent.run(handoff)


def test_spec_edited_after_approval_blocks(pilot):
    handoff = pilot.handoff()
    with (handoff.parent / "product_spec.md").open("a") as f:
        f.write("\n- sneaky new requirement\n")
    result = pilot.agent().run(handoff)
    assert result.status == RunState.BLOCKED and result.next_owner == Owner.SPEC_REVIEWER
    assert [b.code for b in result.blockers] == ["SPEC_HASH_MISMATCH"]
    assert result.clarification_requests and result.implementer is None


def test_missing_approval_blocks_without_cloning(pilot):
    result = pilot.agent().run(pilot.handoff(approved_by=None))
    assert [b.code for b in result.blockers] == ["MISSING_APPROVAL"]
    assert not (pilot.work_dir / "runs" / "run-001" / "r0" / "workspace").exists()


def test_unknown_source_commit_blocks(pilot):
    result = pilot.agent().run(pilot.handoff(source_commit="f" * 40))
    assert [b.code for b in result.blockers] == ["SOURCE_COMMIT_MISSING"]
    assert result.next_owner == Owner.SPEC_AGENT


def test_failing_baseline_blocks_before_implementation(pilot):
    work = pilot.root / "breaker"
    gitops.clone(str(pilot.remote), work)
    (work / "tests" / "test_broken.py").write_text("def test_broken():\n    assert False\n")
    gitops.git(work, "add", "-A")
    sha = gitops.commit(work, "break the build", "Someone", "someone@example.invalid")
    gitops.git(work, "push", "--quiet", str(pilot.remote), "HEAD:refs/heads/main")
    result = pilot.agent().run(pilot.handoff(source_commit=sha))
    assert [b.code for b in result.blockers] == ["BASELINE_TESTS_FAILING"]
    assert result.implementer is None
    assert any("test_broken" in t for t in result.blockers[0].details["failing_tests"])


def test_implementation_that_breaks_tests_is_not_published(pilot):
    script = {"actions": [{"replace": "src/pilot_stats/stats.py", "old": "return sum(values) / len(values)",
                           "new": "return sum(values)"}],
              "notes": {"criteria": {"AC-1": "IMPLEMENTED"}}}
    result = pilot.agent(script).run(pilot.handoff())
    assert result.status == RunState.BLOCKED and [b.code for b in result.blockers] == ["TESTS_FAILING"]
    assert any("test_mean" in t for t in result.blockers[0].details["failing_tests"])
    assert len(pilot.remote_log()) == 1 and pilot.publisher.pulls() == []
    assert result.pr_head_sha is None


def test_no_changes_blocks(pilot):
    result = pilot.agent({"actions": [{"run": "ls"}]}).run(pilot.handoff())
    assert [b.code for b in result.blockers] == ["NO_CHANGES"]


def test_unfinished_implementer_blocks_but_keeps_diff_evidence(pilot):
    script = {"actions": [{"write": "src/pilot_stats/stats.py", "content": "# partial\n"}], "exit_status": "StepLimit"}
    agent = pilot.agent(script)
    result = agent.run(pilot.handoff())
    assert [b.code for b in result.blockers] == ["IMPLEMENTER_DID_NOT_FINISH"]
    assert "# partial" in (artifacts(agent, result) / "diff.patch").read_text(encoding="utf-8")


def test_secret_in_diff_is_blocked_and_redacted_from_evidence(pilot):
    token = "ghp_" + "Z9y8X7w6" * 5
    script = pilot.script()
    script["actions"].append({"replace": "src/pilot_stats/stats.py", "old": "from collections import Counter\n",
                              "new": f"from collections import Counter\n\nTOKEN = '{token}'\n"})
    agent = pilot.agent(script)
    result = agent.run(pilot.handoff())
    violations = [v["code"] for v in result.blockers[0].details["violations"]]
    assert violations == ["SECRET_DETECTED"]
    out = artifacts(agent, result)
    for path in out.iterdir():
        assert token not in path.read_text(encoding="utf-8", errors="replace"), path.name


def test_implementer_commits_and_git_tampering_are_neutralized(pilot):
    script = pilot.script()
    script["actions"] += [
        {"run": "git -c user.name=x -c user.email=x@x commit -qam 'agent commit' && git checkout -q -b rogue"},
        {"write": ".git/hooks/pre-commit", "content": "#!/bin/sh\necho HOOK-RAN > hook-ran.txt\n"},
        {"run": "git config core.hooksPath .git/hooks && git config user.name Mallory"},
    ]
    agent = pilot.agent(script)
    result = agent.run(pilot.handoff())
    assert result.status == RunState.TESTING, result.blockers
    assert not (agent.run_root("run-001") / "r0" / "workspace" / "hook-ran.txt").exists()
    log = gitops.git(pilot.remote, "log", "--format=%an %s", f"{pilot.sha}..{result.pr_head_sha}").splitlines()
    assert len(log) == 1 and log[0].startswith("EVO Coding Agent ")


def test_budget_exhaustion_blocks(pilot):
    script = pilot.script()
    script["cost_usd_per_action"] = 0.75
    result = pilot.agent(script, config=pilot.config(hard_max_usd=1.0)).run(pilot.handoff())
    assert [b.code for b in result.blockers] == ["BUDGET_EXCEEDED"]
    assert result.blockers[0].details["kind"] == "usd" and result.budget_used.usd == pytest.approx(1.5)


def test_lifetime_spend_cap_across_runs(pilot):
    script = pilot.script()
    script["cost_usd_per_action"] = 0.75
    agent = pilot.agent(script, config=pilot.config(lifetime_max_usd=1.0))
    first = agent.run(pilot.handoff(run_id="spend-1"))
    assert [b.code for b in first.blockers] == ["BUDGET_EXCEEDED"]  # run cap clamped to the $1 lifetime cap
    second = agent.run(pilot.handoff(run_id="spend-2"))
    assert [b.code for b in second.blockers] == ["LIFETIME_BUDGET_EXHAUSTED"] and second.budget_used.usd == 0
    ledger = SpendLedger(pilot.work_dir)
    assert [e["run_id"] for e in ledger.entries()] == ["spend-1", "spend-2"]
    assert ledger.total_usd() == pytest.approx(1.5)


def test_step_budget_from_handoff_blocks(pilot):
    result = pilot.agent().run(pilot.handoff(budget_limits={"max_steps": 2}))
    assert [b.code for b in result.blockers] == ["BUDGET_EXCEEDED"]
    assert result.blockers[0].details["kind"] == "steps"


def test_cancellation_mid_implementation(pilot):
    holder = {}
    script = pilot.script()
    script["actions"].insert(1, {"hook": "cancel"})
    agent = pilot.agent(script, hooks={"cancel": lambda: holder["agent"].cancel("run-001", "maintainer stop")})
    holder["agent"] = agent
    result = agent.run(pilot.handoff())
    assert result.status == RunState.CANCELLED and result.blockers == []
    assert pilot.publisher.pulls() == [] and len(pilot.remote_log()) == 1
    assert (artifacts(agent, result) / "coding_result.json").is_file()


def test_cancelled_run_cannot_be_revised(pilot):
    agent = pilot.agent(pilot.script("02-percentile"))
    first = agent.run(pilot.handoff("02-percentile"))
    agent.cancel("run-001")
    revised = agent.revise("run-001", feedback_for(SCENARIOS / "02-percentile.toml", "run-001", first.pr_head_sha))
    assert revised.status == RunState.CANCELLED
    assert gitops.git(pilot.remote, "rev-parse", f"refs/heads/{first.branch}") == first.pr_head_sha


def test_preexisting_branch_is_reported_stale(pilot):
    gitops.git(pilot.remote, "branch", "evo/issue-1-run-001", "main")
    result = pilot.agent().run(pilot.handoff())
    assert result.status == RunState.STALE and [b.code for b in result.blockers] == ["BRANCH_EXISTS"]


def test_high_risk_requires_named_human(pilot):
    result = pilot.agent().run(pilot.handoff(risk_tier="HIGH"))
    assert result.status == RunState.TESTING and result.requires_human_review


def test_internal_errors_become_inconclusive_with_evidence(pilot):
    script = {"actions": [{"replace": "src/pilot_stats/stats.py", "old": "not in the file", "new": "x"}]}
    agent = pilot.agent(script)
    result = agent.run(pilot.handoff())
    assert result.status == RunState.INCONCLUSIVE and result.blockers[0].code == "INTERNAL_ERROR"
    trace = (artifacts(agent, result) / "trace.jsonl").read_text(encoding="utf-8")
    assert "internal_error" in trace and "Traceback" in trace


# ---------------------------------------------------------------- revisions


@pytest.fixture
def published(pilot):
    agent = pilot.agent(pilot.script("02-percentile"))
    first = agent.run(pilot.handoff("02-percentile"))
    assert first.status == RunState.TESTING
    return agent, first


def test_revision_pushes_fast_forward_fix(pilot, published):
    agent, first = published
    revised = agent.revise("run-001", feedback_for(SCENARIOS / "02-percentile.toml", "run-001", first.pr_head_sha))
    assert revised.status == RunState.TESTING and revised.revision == 1
    assert revised.pr_number == first.pr_number and revised.previous_head_sha == first.pr_head_sha
    assert gitops.git(pilot.remote, "merge-base", "--is-ancestor", first.pr_head_sha, revised.pr_head_sha) == ""
    assert [t.label for t in revised.tests] == ["before", "final"]
    assert "math.ceil" in pilot.remote_file(revised.pr_head_sha, "src/pilot_stats/stats.py")
    assert revised.test_agent_request["correlation_id"] == "run-001:r1"
    pr = pilot.publisher.pulls()[0]
    assert len(pilot.publisher.pulls()) == 1 and "revision 1" in pr["comments"][0]
    # Re-delivering the same feedback is a no-op.
    again = agent.revise("run-001", feedback_for(SCENARIOS / "02-percentile.toml", "run-001", first.pr_head_sha))
    assert again.revision == 1 and again.pr_head_sha == revised.pr_head_sha
    assert [r.revision for r in agent.results("run-001")] == [0, 1]


def test_feedback_for_an_old_commit_is_stale(pilot, published):
    agent, first = published
    result = agent.revise("run-001", feedback_for(SCENARIOS / "02-percentile.toml", "run-001", "0" * 40))
    assert result.status == RunState.STALE and result.blockers[0].code == "STALE_FEEDBACK"
    assert gitops.git(pilot.remote, "rev-parse", f"refs/heads/{first.branch}") == first.pr_head_sha


def test_human_push_to_pr_branch_makes_revision_stale(pilot, published):
    agent, first = published
    work = pilot.root / "human"
    gitops.clone(str(pilot.remote), work, branch=first.branch)
    (work / "NOTES.txt").write_text("human edit\n")
    gitops.git(work, "add", "-A")
    gitops.commit(work, "human tweak", "Human", "h@example.invalid")
    gitops.git(work, "push", "--quiet", str(pilot.remote), f"HEAD:refs/heads/{first.branch}")
    result = agent.revise("run-001", feedback_for(SCENARIOS / "02-percentile.toml", "run-001", first.pr_head_sha))
    assert result.status == RunState.STALE and result.blockers[0].code == "BRANCH_MOVED"


def test_revision_limit_routes_to_human(pilot):
    script = {"revisions": {
        "0": pilot.script("02-percentile")["revisions"]["0"],
        "1": {"actions": [{"write": "src/pilot_stats/extra1.py", "content": "X = 1\n"}]},
        "2": {"actions": [{"write": "src/pilot_stats/extra2.py", "content": "X = 2\n"}]},
    }}
    handoff = pilot.handoff("02-percentile", budget_limits={"max_revisions": 1},
                            scope={"allowed_paths": ["src/pilot_stats/**"], "may_modify_tests": True})
    agent = pilot.agent(script)
    first = agent.run(handoff)
    second = agent.revise("run-001", RevisionRequest(run_id="run-001", source="qa_agent", requested_by="qa",
                                                     tested_head_sha=first.pr_head_sha, findings=["style"]))
    assert second.status == RunState.TESTING
    third = agent.revise("run-001", RevisionRequest(run_id="run-001", source="qa_agent", requested_by="qa",
                                                    tested_head_sha=second.pr_head_sha, findings=["more"]))
    assert third.status == RunState.BLOCKED and third.next_owner == Owner.HUMAN_REVIEWER
    assert third.blockers[0].code == "REVISION_LIMIT_REACHED"
    assert gitops.git(pilot.remote, "rev-parse", f"refs/heads/{first.branch}") == second.pr_head_sha


def test_revision_scope_is_checked_against_original_base(pilot, published):
    agent, first = published
    agent._implementer.script["revisions"]["1"] = {"actions": [
        {"replace": "README.md", "old": "# pilot-stats", "new": "# pilot-stats (edited)"}]}
    result = agent.revise("run-001", feedback_for(SCENARIOS / "02-percentile.toml", "run-001", first.pr_head_sha))
    assert result.status == RunState.BLOCKED
    assert [v["code"] for v in result.blockers[0].details["violations"]] == ["OUT_OF_SCOPE"]
    assert gitops.git(pilot.remote, "rev-parse", f"refs/heads/{first.branch}") == first.pr_head_sha


IMAGE = os.environ.get("EVO_DOCKER_IMAGE", "evo-coding-sandbox:py312")


def _image_present() -> bool:
    return subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True).returncode == 0


@pytest.mark.docker
@pytest.mark.e2e
@pytest.mark.skipif(not docker_available(), reason="Docker daemon not available")
def test_docker_sandbox_happy_path_and_no_network(pilot):
    if not _image_present():
        pytest.skip(f"sandbox image {IMAGE} not built")
    script = pilot.script()
    script["actions"].append({"run": "python -c \"import urllib.request as u; u.urlopen('https://pypi.org', "
                                     "timeout=5)\" && echo NETWORK-OPEN > .evo-artifacts/net.txt || true"})
    config = pilot.config()
    config.workspace = WorkspaceConfig(backend="docker", docker_image=IMAGE, command_timeout_seconds=300)
    agent = pilot.agent(script, config=config)
    result = agent.run(pilot.handoff())
    assert result.status == RunState.TESTING, result.blockers
    assert not (agent.run_root("run-001") / "r0" / "workspace" / ".evo-artifacts" / "net.txt").exists()


@pytest.mark.live_github
@pytest.mark.e2e
@pytest.mark.skipif(not os.environ.get("EVO_E2E_GITHUB_REPO"), reason="set EVO_E2E_GITHUB_REPO to run")
def test_live_github_draft_pr(tmp_path):
    repository = os.environ["EVO_E2E_GITHUB_REPO"]
    publisher = GitHubPublisher.from_env("EVO_GITHUB_TOKEN")
    probe = tmp_path / "probe"
    publisher.clone(repository, probe)
    run_id = f"live-{uuid.uuid4().hex[:8]}"
    handoff = build_handoff(SCENARIOS / "01-median-even.toml", gitops.head_sha(probe), tmp_path / "h", run_id,
                            repository=repository)
    agent = CodingAgent(CodingConfig(work_dir=tmp_path / "work"),
                        implementer=ScriptedImplementer(SCENARIOS / "01-median-even.toml"),
                        publisher=publisher)
    result = agent.run(handoff)
    assert result.status == RunState.TESTING, result.blockers
    assert result.pr_url.startswith("https://github.com/")
    assert publisher.remote_branch_sha(repository, result.branch) == result.pr_head_sha
