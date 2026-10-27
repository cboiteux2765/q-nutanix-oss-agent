from app.decision import decide
from app.models import CheckResult, CheckStatus, Finding, Policy, RiskTier, Severity, Verdict


def check(name="security", status=CheckStatus.PASS, required=True, blocking=None):
    findings = []
    if blocking is not None:
        findings.append(
            Finding(
                check=name,
                severity=Severity.HIGH if blocking else Severity.LOW,
                blocking=blocking,
                title="issue",
                file="a.py",
                line=3,
                evidence="e",
                suggested_fix="f",
            )
        )
    return CheckResult(name=name, required=required, status=status, findings=findings)


def all_pass():
    return [check("security"), check("secrets"), check("dependencies", CheckStatus.NOT_APPLICABLE)]


def test_clean_medium_is_approved_with_pilot_confirmation():
    verdict, reasons, owner, confirm = decide(RiskTier.MEDIUM, all_pass(), Policy())
    assert verdict == Verdict.APPROVED
    assert confirm and "Team member confirms" in owner


def test_without_pilot_agent_approval_stands():
    verdict, _, owner, confirm = decide(RiskTier.LOW, all_pass(), Policy(pilot_mode=False))
    assert verdict == Verdict.APPROVED
    assert not confirm and owner == "Code PM merges"


def test_high_tier_needs_human():
    verdict, _, owner, confirm = decide(RiskTier.HIGH, all_pass(), Policy())
    assert verdict == Verdict.NEEDS_HUMAN and confirm
    assert owner == "Named human reviewer"


def test_blocking_finding_requests_changes_even_on_high_tier():
    checks = all_pass() + [check("security", CheckStatus.FAIL, blocking=True)]
    verdict, reasons, owner, _ = decide(RiskTier.HIGH, checks, Policy())
    assert verdict == Verdict.CHANGES_REQUESTED
    assert "(a.py:3)" in reasons[0]


def test_suggestions_do_not_block():
    checks = all_pass() + [check("release_readiness", required=False, blocking=False)]
    verdict, *_ = decide(RiskTier.MEDIUM, checks, Policy())
    assert verdict == Verdict.APPROVED


def test_required_not_checked_is_blocked_never_approved():
    checks = [check("security", CheckStatus.NOT_CHECKED), check("secrets"), check("dependencies")]
    verdict, reasons, *_ = decide(RiskTier.LOW, checks, Policy())
    assert verdict == Verdict.BLOCKED
    assert "NOT_CHECKED" in reasons[0]


def test_optional_not_checked_does_not_block():
    checks = all_pass() + [check("performance", CheckStatus.NOT_CHECKED, required=False)]
    verdict, *_ = decide(RiskTier.MEDIUM, checks, Policy())
    assert verdict == Verdict.APPROVED


def test_missing_required_check_is_blocked():
    verdict, reasons, *_ = decide(RiskTier.LOW, [check("security"), check("secrets")], Policy())
    assert verdict == Verdict.BLOCKED
    assert "never ran" in reasons[0]


def test_blocking_finding_still_reports_unchecked():
    checks = [check("security", CheckStatus.NOT_CHECKED), check("secrets", CheckStatus.FAIL, blocking=True), check("dependencies")]
    verdict, reasons, *_ = decide(RiskTier.LOW, checks, Policy())
    assert verdict == Verdict.CHANGES_REQUESTED
    assert any("NOT_CHECKED" in r for r in reasons)


def test_no_net_change_needs_human():
    verdict, reasons, *_ = decide(RiskTier.LOW, all_pass(), Policy(), changed_files=0)
    assert verdict == Verdict.NEEDS_HUMAN
    assert "no net change" in reasons[0]
