"""End-to-end: real Git repos, real bandit, real output files."""

import hashlib
import json

import pytest

import demo_acceptance as demo
from app import fixtures as fx
from app.agent import review
from app.cli import main as cli_main
from app.models import CheckStatus, Policy, QADecision, Verdict


@pytest.mark.parametrize("title, build, expected", demo.SCENARIOS, ids=[s[1].__name__ for s in demo.SCENARIOS])
def test_acceptance_scenarios(tmp_path, title, build, expected):
    repo, verification, policy = build(tmp_path)
    out = tmp_path / "qa"
    decision = review(repo, verification, "pr", out, policy)
    assert decision.verdict == expected, decision.reasons

    saved = QADecision.model_validate_json((out / "qa_decision.json").read_text())
    assert saved.verdict == expected
    assert saved.verification_sha256 == hashlib.sha256(verification.read_bytes()).hexdigest()
    report = (out / "qa_report.md").read_text()
    assert report.startswith(f"# QA report: {expected.value}")


def test_approved_binds_to_exact_head(tmp_path):
    repo, verification, policy = demo.scenario_clean(tmp_path)
    decision = review(repo, verification, "pr", tmp_path / "qa", policy)
    handoff = json.loads(verification.read_text())
    assert decision.head_sha == handoff["reviewed_head_sha"]
    assert decision.test_run_id == handoff["run_id"]
    assert decision.test_identity_hash == handoff["identity_hash"]


def test_secret_never_reaches_outputs(tmp_path):
    repo, verification, policy = demo.scenario_leaked_secret(tmp_path)
    out = tmp_path / "qa"
    review(repo, verification, "pr", out, policy)
    key = fx.fake_aws_key()
    assert key not in (out / "qa_report.md").read_text()
    assert key not in (out / "qa_decision.json").read_text()


def test_planned_checks_are_reported_not_faked(tmp_path):
    repo, verification, policy = demo.scenario_clean(tmp_path)
    decision = review(repo, verification, "pr", tmp_path / "qa", policy)
    planned = {c.name: c for c in decision.checks if c.detail.startswith("Planned")}
    assert {"spec_intent", "license", "performance", "adversarial_probe"} <= set(planned)
    assert all(c.status == CheckStatus.NOT_CHECKED and not c.required for c in planned.values())


def test_unknown_head_ref_is_blocked(tmp_path):
    repo, verification, policy = demo.scenario_clean(tmp_path)
    decision = review(repo, verification, "no-such-branch", tmp_path / "qa", policy)
    assert decision.verdict == Verdict.BLOCKED
    assert (tmp_path / "qa" / "qa_report.md").exists()


def test_cli_exit_codes(tmp_path, capsys):
    repo, verification, _ = demo.scenario_high_risk(tmp_path)
    code = cli_main(["--repo", str(repo), "--verification", str(verification), "--head", "pr", "--out", str(tmp_path / "o")])
    assert code == 2  # NEEDS_HUMAN
    assert "NEEDS_HUMAN" in capsys.readouterr().out


def test_pilot_off_lets_approval_stand(tmp_path):
    repo, verification, _ = demo.scenario_clean(tmp_path)
    decision = review(repo, verification, "pr", tmp_path / "qa", Policy(pilot_mode=False))
    assert decision.verdict == Verdict.APPROVED
    assert not decision.human_confirmation_required
