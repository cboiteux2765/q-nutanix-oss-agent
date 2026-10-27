import json

import pytest

from app import fixtures as fx
from app.checks import ToolError, dependencies, run_check
from app.models import CheckStatus


def test_parse_requirements():
    parsed = dependencies.parse_requirements(
        "# comment\nRequests==2.32.3\nfoo_bar>=1.0 ; python_version>'3'\n-r other.txt\nbaz[extra]==0.1\nqux\n"
    )
    assert parsed == {
        "requests": ("==", "2.32.3"),
        "foo-bar": (">=", "1.0"),
        "baz": ("==", "0.1"),
        "qux": (None, None),
    }


def test_vulnerable_pin_blocks(make_ctx):
    result = dependencies.check(make_ctx(fx.VULNERABLE_DEP_CHANGE))
    assert result.status == CheckStatus.FAIL
    assert result.findings[0].blocking
    assert "DEMO-2026-0001" in result.findings[0].title
    assert "1.0.2" in result.findings[0].suggested_fix


def test_low_advisory_is_a_suggestion(make_ctx):
    result = dependencies.check(make_ctx({"requirements.txt": "requests==2.32.3\nevo-demo-colors==2.3.0\n"}))
    assert result.status == CheckStatus.PASS
    assert result.findings and not result.findings[0].blocking


def test_unpinned_is_a_suggestion(make_ctx):
    result = dependencies.check(make_ctx({"requirements.txt": "requests==2.32.3\nflask>=3\n"}))
    assert result.status == CheckStatus.PASS
    assert "not pinned" in result.findings[0].title


def test_fixed_version_passes(make_ctx):
    result = dependencies.check(make_ctx({"requirements.txt": "requests==2.32.3\nevo-demo-yamlish==1.0.2\n"}))
    assert result.status == CheckStatus.PASS
    assert result.findings == []


def test_unchanged_vulnerable_pin_is_not_blamed_on_pr(make_ctx):
    ctx = make_ctx(
        {"requirements.txt": "requests==2.32.3\nevo-demo-yamlish==1.0.1\nidna==3.7\n"},
        extra_base=fx.VULNERABLE_DEP_CHANGE,
    )
    result = dependencies.check(ctx)
    assert result.status == CheckStatus.PASS


def test_no_dependency_files_is_not_applicable(make_ctx):
    assert dependencies.check(make_ctx(fx.CLEAN_CHANGE)).status == CheckStatus.NOT_APPLICABLE


def test_custom_advisory_feed(make_ctx, tmp_path):
    feed = tmp_path / "feed.json"
    feed.write_text(
        json.dumps({"advisories": [{"id": "X-1", "package": "requests", "affected_versions": ["2.0.0"], "severity": "HIGH"}]})
    )
    ctx = make_ctx({"requirements.txt": "requests==2.0.0\n"}, advisories_path=feed)
    result = dependencies.check(ctx)
    assert result.status == CheckStatus.FAIL
    assert result.findings[0].suggested_fix == "Replace or remove the package"


def test_missing_feed_is_tool_error_then_not_checked(make_ctx, tmp_path):
    ctx = make_ctx(fx.VULNERABLE_DEP_CHANGE, advisories_path=tmp_path / "missing.json")
    with pytest.raises(ToolError):
        dependencies.check(ctx)
    result = run_check(dependencies.NAME, dependencies.check, ctx)
    assert result.status == CheckStatus.NOT_CHECKED
    assert result.required and result.attempts == 2
