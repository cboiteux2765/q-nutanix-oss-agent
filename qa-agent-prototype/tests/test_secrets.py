from app import fixtures as fx
from app.checks import secrets
from app.models import CheckStatus


def test_aws_key_is_blocking_and_redacted(make_ctx):
    result = secrets.check(make_ctx(fx.secret_change()))
    assert result.status == CheckStatus.FAIL
    finding = result.findings[0]
    assert finding.blocking
    assert finding.file == "src/calc/config.py" and finding.line == 1
    assert fx.fake_aws_key() not in finding.evidence
    assert "AKIA****" in finding.evidence


def test_github_token(make_ctx):
    token = "gh" + "p_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
    result = secrets.check(make_ctx({"src/calc/gh.py": f"TOKEN = '{token}'\n"}))
    assert result.status == CheckStatus.FAIL
    assert token not in result.findings[0].evidence


def test_private_key_header(make_ctx):
    header = "-----BEGIN " + "RSA PRIVATE KEY-----"
    result = secrets.check(make_ctx({"deploy/key.pem": header + "\nabc\n"}))
    assert result.status == CheckStatus.FAIL


def test_hard_coded_password(make_ctx):
    result = secrets.check(make_ctx({"src/calc/db.py": 'password = "hunter2hunter2!!"\n'}))
    assert result.status == CheckStatus.FAIL


def test_placeholders_are_ignored(make_ctx):
    text = 'api_key = "your-api-key-here-please"\npassword = "changeme-changeme"\n'
    result = secrets.check(make_ctx({"src/calc/settings.py": text}))
    assert result.status == CheckStatus.PASS
    assert result.findings == []


def test_clean_change_passes(make_ctx):
    result = secrets.check(make_ctx(fx.CLEAN_CHANGE))
    assert result.status == CheckStatus.PASS


def test_only_added_lines_are_scanned(make_ctx):
    # The key exists on the base; the PR only edits another line.
    base = fx.secret_change()
    ctx = make_ctx({"src/calc/config.py": base["src/calc/config.py"] + "DEBUG = False\n"}, extra_base=base)
    assert secrets.check(ctx).status == CheckStatus.PASS


def test_redact():
    assert secrets.redact("abcdefgh") == "abcd********"
    assert secrets.redact("ab") == "****"
