from app import fixtures as fx
from app.checks import run_check, security
from app.models import CheckStatus, Policy, Severity


def test_new_shell_injection_is_blocking(make_ctx):
    result = security.check(make_ctx(fx.SHELL_INJECTION_CHANGE))
    assert result.status == CheckStatus.FAIL
    high = [f for f in result.findings if f.severity == Severity.HIGH]
    assert high and high[0].blocking
    assert high[0].title.startswith("B602")
    assert high[0].file == "src/calc/runner.py"


def test_issue_already_on_base_is_not_blamed_on_pr(make_ctx):
    legacy = fx.SHELL_INJECTION_CHANGE["src/calc/runner.py"]
    ctx = make_ctx(
        {"src/calc/runner.py": legacy + "\n\ndef version():\n    return '0.2'\n"},
        extra_base={"src/calc/runner.py": legacy},
    )
    result = security.check(ctx)
    assert result.status == CheckStatus.PASS
    assert result.findings == []


def test_test_asserts_are_not_flagged(make_ctx):
    result = security.check(make_ctx(fx.CLEAN_CHANGE))
    assert result.status == CheckStatus.PASS
    assert result.findings == []


def test_no_python_files_is_not_applicable(make_ctx):
    result = security.check(make_ctx({"README.md": "# calc\n\nMore docs.\n"}))
    assert result.status == CheckStatus.NOT_APPLICABLE


def test_missing_bandit_is_retried_then_not_checked(make_ctx):
    ctx = make_ctx(fx.CLEAN_CHANGE, policy=Policy(bandit_command="bandit-is-not-installed"))
    result = run_check(security.NAME, security.check, ctx)
    assert result.status == CheckStatus.NOT_CHECKED
    assert result.attempts == 2
    assert "bandit could not run" in result.detail
