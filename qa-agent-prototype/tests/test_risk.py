import pytest

from app.models import ChangedFile, Policy, RiskTier
from app.risk import classify


def tier_of(*entries):
    files = [ChangedFile(path=p, status=s) for p, s in entries]
    return classify(files, Policy())


@pytest.mark.parametrize(
    "entries, expected",
    [
        ([("README.md", "M")], RiskTier.LOW),
        ([("docs/guide.md", "A")], RiskTier.LOW),
        ([("tests/test_util.py", "M")], RiskTier.LOW),
        ([("src/pkg/tests/test_x.py", "A")], RiskTier.LOW),
        ([("src/calc/util.py", "M")], RiskTier.MEDIUM),
        ([("requirements.txt", "M")], RiskTier.MEDIUM),
        ([("README.md", "M"), ("src/calc/util.py", "M")], RiskTier.MEDIUM),
        ([(".github/workflows/ci.yml", "M")], RiskTier.HIGH),
        ([("src/app/auth/login.py", "M")], RiskTier.HIGH),
        ([("security/policy.py", "A")], RiskTier.HIGH),
        ([("db/migrations/0002_users.py", "A")], RiskTier.HIGH),
        ([("LICENSE", "M")], RiskTier.HIGH),
        ([("tests/test_util.py", "D")], RiskTier.HIGH),
        ([("README.md", "M"), (".github/workflows/ci.yml", "M")], RiskTier.HIGH),
    ],
)
def test_tiers(entries, expected):
    tier, reasons = tier_of(*entries)
    assert tier == expected
    assert reasons


def test_no_files_is_low():
    tier, reasons = classify([], Policy())
    assert tier == RiskTier.LOW
    assert reasons == ["No files changed"]


def test_high_reasons_name_the_rule():
    _, reasons = tier_of(("src/calc/util.py", "M"), (".github/workflows/ci.yml", "M"))
    assert reasons == [".github/workflows/ci.yml: matches high-risk rule '.github/workflows/*'"]


def test_deleting_non_test_file_is_not_high():
    tier, _ = tier_of(("src/calc/old.py", "D"))
    assert tier == RiskTier.MEDIUM
