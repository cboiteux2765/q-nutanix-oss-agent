import json
import subprocess
import sys
from pathlib import Path

import pytest

import demo_acceptance as demo
from app import fixtures as fx
from app.agent import review
from app.handoff import test_review, coding_revision
from app.models import Verdict


@pytest.mark.parametrize('field,value', [
    ('execution_status', 'ERROR'), ('tests', {'failed': 1}),
    ('tests', {'errors': 1}), ('requirements', []),
    ('requirements', [{'id': 'REQ-1', 'result': 'FAIL'}]),
    ('unresolved_findings', [{'summary': 'unfixed'}]),
])
def test_contradictory_ready_is_blocked(tmp_path, field, value):
    repo, verification, policy = demo.scenario_clean(tmp_path)
    data = json.loads(verification.read_text())
    data[field] = value
    verification.write_text(json.dumps(data))
    assert review(repo, verification, 'pr', tmp_path/'qa', policy).verdict == Verdict.BLOCKED


@pytest.mark.parametrize('path', ['docs/café.md', 'docs/with space.md', 'docs/tab\tfile.md'])
def test_secret_in_unusual_filename(tmp_path, path):
    repo, base = fx.make_repo(tmp_path)
    head = fx.open_pr(repo, {path: 'key = "' + fx.fake_aws_key() + '"\n'})
    v = fx.write_verification(tmp_path/'verification.json', base, head)
    d = review(repo, v, 'pr', tmp_path/'qa')
    assert d.verdict == Verdict.CHANGES_REQUESTED
    finding = next(f for c in d.checks for f in c.findings if f.check == 'secrets')
    assert finding.file == path


def test_missing_base_writes_blocked_report(tmp_path):
    repo, v, policy = demo.scenario_clean(tmp_path)
    data = json.loads(v.read_text()); data['base_sha'] = '0'*40
    v.write_text(json.dumps(data))
    assert review(repo, v, 'pr', tmp_path/'qa', policy).verdict == Verdict.BLOCKED
    assert (tmp_path/'qa/qa_decision.json').is_file()


def test_approval_requires_confirmation(tmp_path):
    repo, v, policy = demo.scenario_clean(tmp_path)
    d = review(repo, v, 'pr', tmp_path/'qa', policy)
    with pytest.raises(ValueError, match='Human confirmation'):
        test_review(d)
    assert test_review(d, human_reviewer='Timothy')['decision'] == 'APPROVED'


# Avoid pytest collecting the imported adapter as a test.
test_review.__test__ = False


def test_revision_binds_coding_identity(tmp_path):
    repo, v, policy = demo.scenario_shell_injection(tmp_path)
    d = review(repo, v, 'pr', tmp_path/'qa', policy)
    coding = dict(run_id='coding-demo', repository=d.repo, pr_number=d.pr_number,
                  pr_head_sha=d.head_sha, spec_hash=d.spec_hash)
    request = coding_revision(d, coding)
    assert request['verdict'] == 'CHANGES_REQUIRED'
    coding['pr_head_sha'] = '0'*40
    with pytest.raises(ValueError, match='pr_head_sha'):
        coding_revision(d, coding)


def test_requests_validate_against_team_models(tmp_path):
    root = Path(__file__).resolve().parents[2]
    if not (root/'test-agent-prototype').exists() or not (root/'coding-agent').exists():
        pytest.skip('Place team prototypes alongside QA to run contract integration')
    repo, v, policy = demo.scenario_clean(tmp_path)
    d = review(repo, v, 'pr', tmp_path/'qa', policy)
    request = test_review(d, human_reviewer='Timothy')
    subprocess.run([sys.executable, '-c',
        'import json,sys; from app.models import ReviewRequest; ReviewRequest.model_validate(json.load(sys.stdin))'],
        input=json.dumps(request), text=True, cwd=root/'test-agent-prototype', check=True)
    repo2, v2, policy2 = demo.scenario_shell_injection(tmp_path/'bad')
    bad = review(repo2, v2, 'pr', tmp_path/'qa2', policy2)
    request2 = coding_revision(bad, dict(run_id='coding-demo', repository=bad.repo,
        pr_number=bad.pr_number, pr_head_sha=bad.head_sha, spec_hash=bad.spec_hash))
    subprocess.run([sys.executable, '-c',
        'import json,sys; sys.path.insert(0,"src"); from evo_coding.models import RevisionRequest; RevisionRequest.model_validate(json.load(sys.stdin))'],
        input=json.dumps(request2), text=True, cwd=root/'coding-agent', check=True)


def test_real_test_to_qa_to_test_review(tmp_path):
    root = Path(__file__).resolve().parents[2]
    if not (root/'test-agent-prototype').exists():
        pytest.skip('Place Test prototype alongside QA for live local integration')
    # Separate processes keep both prototypes' top-level `app` packages isolated.
    driver = '''
import asyncio, json, subprocess, sys
from pathlib import Path
from demo_acceptance import configured, verify, QA
from app.models import ReviewRequest
async def run():
    root, qa_root = Path(sys.argv[1]), Path(sys.argv[2])
    manager, repo = configured(root / "test-run")
    result = await verify(manager)
    verification = manager.artifact_path(result.run_id, "verification.json")
    subprocess.run([sys.executable, "-m", "app.cli", "--repo", str(repo),
        "--verification", str(verification), "--head", "HEAD", "--out", str(root/"qa")],
        cwd=qa_root, check=True)
    subprocess.run([sys.executable, "-m", "app.handoff", "test-review", "--repo", str(repo),
        "--head", "HEAD", "--decision", str(root/"qa/qa_decision.json"),
        "--human-reviewer", QA.name, "--out", str(root/"review.json")], cwd=qa_root, check=True)
    request = ReviewRequest.model_validate_json((root/"review.json").read_text())
    reviewed = manager.review_run(result.run_id, request, QA)
    assert reviewed.qa_review.status == "APPROVED"
    assert reviewed.human_approval_required is True
asyncio.run(run())
'''
    subprocess.run([sys.executable, '-c', driver, str(tmp_path), str(root/'qa-agent-prototype')],
                   cwd=root/'test-agent-prototype', check=True)


@pytest.mark.parametrize('target', ['test_report.md', 'evidence_manifest.json'])
def test_tampered_evidence_is_blocked(tmp_path, target):
    repo, v, policy = demo.scenario_clean(tmp_path)
    (v.parent/target).write_text('modified evidence')
    assert review(repo, v, 'pr', tmp_path/'qa', policy).verdict == Verdict.BLOCKED
