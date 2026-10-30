import pytest

from evo_coding.models import ChangedFile, ChangeType, ScopePolicy
from evo_coding.safety import check_scope, find_secrets, glob_regex, matches, redact, scan_for_secrets


@pytest.mark.parametrize("pattern,path,expected", [
    ("src/**", "src/a.py", True),
    ("src/**", "src/pkg/a.py", True),
    ("src/**", "srcx/a.py", False),
    ("src/*.py", "src/a.py", True),
    ("src/*.py", "src/pkg/a.py", False),
    ("**/test_*.py", "test_a.py", True),
    ("**/test_*.py", "a/b/test_a.py", True),
    ("**/.env", ".env", True),
    ("docs/", "docs/x/y.md", True),
    ("./README.md", "README.md", True),
    ("a?c.py", "abc.py", True),
    ("a.c", "abc", False),  # dots are literal
])
def test_glob(pattern, path, expected):
    assert bool(glob_regex(pattern).match(path)) is expected


def change(path, kind=ChangeType.MODIFIED, is_test=False, add=1, delete=0):
    return ChangedFile(path=path, change=kind, additions=add, deletions=delete, is_test=is_test)


def codes(violations):
    return sorted(v.code for v in violations)


def test_in_scope_change_passes():
    policy = ScopePolicy(allowed_paths=["src/pkg/**"])
    assert check_scope([change("src/pkg/a.py"), change("tests/test_a.py", ChangeType.ADDED, True)], policy) == []


def test_out_of_scope_and_forbidden():
    policy = ScopePolicy(allowed_paths=["src/**"], forbidden_paths=["src/secrets/**"])
    found = check_scope([change("README.md"), change(".github/workflows/ci.yml"), change("src/secrets/k.py"),
                         change(".env", ChangeType.ADDED)], policy)
    assert codes(found) == ["FORBIDDEN_PATH", "FORBIDDEN_PATH", "FORBIDDEN_PATH", "OUT_OF_SCOPE"]


def test_forbidden_wins_over_allowed():
    policy = ScopePolicy(allowed_paths=["**"])
    assert codes(check_scope([change(".github/x.yml")], policy)) == ["FORBIDDEN_PATH"]


def test_test_file_rules():
    strict = ScopePolicy(allowed_paths=["src/**"], may_add_tests=True, may_modify_tests=False)
    assert codes(check_scope([change("tests/test_a.py", ChangeType.MODIFIED, True)], strict)) == ["TEST_MODIFIED"]
    assert codes(check_scope([change("tests/test_a.py", ChangeType.DELETED, True)], strict)) == ["TEST_DELETED"]
    assert check_scope([change("tests/test_new.py", ChangeType.ADDED, True)], strict) == []
    none = ScopePolicy(allowed_paths=["src/**"], may_add_tests=False)
    assert codes(check_scope([change("tests/test_new.py", ChangeType.ADDED, True)], none)) == ["TEST_ADD_NOT_ALLOWED"]
    loose = ScopePolicy(allowed_paths=["src/**"], may_modify_tests=True)
    assert check_scope([change("tests/test_a.py", ChangeType.DELETED, True)], loose) == []


def test_size_limits_and_symlinks():
    policy = ScopePolicy(allowed_paths=["**"], max_changed_files=2, max_changed_lines=10)
    found = check_scope([change("a.py", add=6), change("b.py", add=6), change("c.py")], policy, symlinks=["c.py"])
    assert codes(found) == ["SYMLINK", "TOO_MANY_FILES", "TOO_MANY_LINES"]


def test_extra_forbidden_from_operator_policy():
    policy = ScopePolicy(allowed_paths=["**"])
    assert codes(check_scope([change("deploy/prod.yml")], policy, extra_forbidden=["deploy/**"])) == ["FORBIDDEN_PATH"]


GH_TOKEN = "ghp_" + "A1b2C3d4" * 5


def test_secret_detection_and_redaction():
    assert find_secrets(f"token = '{GH_TOKEN}'") == ["github_token"]
    assert find_secrets("-----BEGIN RSA PRIVATE KEY-----") == ["private_key"]
    assert find_secrets("AKIAABCDEFGHIJKLMNOP") == ["aws_access_key"]
    assert find_secrets("nothing to see") == []
    text = f"Authorization: Bearer abc.def.ghi and {GH_TOKEN} and my-custom-secret-value"
    cleaned = redact(text, ["my-custom-secret-value"])
    assert GH_TOKEN not in cleaned and "abc.def.ghi" not in cleaned and "my-custom-secret-value" not in cleaned


def test_scan_for_secrets_in_added_lines():
    found = scan_for_secrets({"src/a.py": ["x = 1", f"TOKEN = '{GH_TOKEN}'"], "src/b.py": ["fine"]})
    assert [(v.code, v.path) for v in found] == [("SECRET_DETECTED", "src/a.py")]


def test_matches_handles_backslashes():
    assert matches("src\\pkg\\a.py", ["src/**"])
