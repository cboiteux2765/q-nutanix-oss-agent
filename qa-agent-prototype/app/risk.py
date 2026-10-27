"""Risk tiers from path rules (PRD QA-02). The agent cannot lower a tier."""

from __future__ import annotations

from fnmatch import fnmatch

from .models import ChangedFile, Policy, RiskTier


def _matches(path: str, patterns: list[str]) -> str | None:
    for pattern in patterns:
        if fnmatch(path, pattern):
            return pattern
    return None


def is_test_file(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return path.startswith("tests/") or "/tests/" in path or name.startswith("test_") or name.endswith("_test.py")


def classify(files: list[ChangedFile], policy: Policy) -> tuple[RiskTier, list[str]]:
    """Return the PR's tier (the highest of its files) and why."""
    if not files:
        return RiskTier.LOW, ["No files changed"]

    high: list[str] = []
    medium: list[str] = []
    for f in files:
        if f.status == "D" and is_test_file(f.path):
            high.append(f"{f.path}: deletes a test")
            continue
        hit = _matches(f.path, policy.high_risk_paths)
        if hit:
            high.append(f"{f.path}: matches high-risk rule {hit!r}")
        elif _matches(f.path, policy.low_risk_paths):
            continue
        elif _matches(f.path, policy.dependency_files):
            medium.append(f"{f.path}: dependency change")
        else:
            medium.append(f"{f.path}: source change")

    if high:
        return RiskTier.HIGH, high
    if medium:
        return RiskTier.MEDIUM, medium
    return RiskTier.LOW, ["Only docs or test files changed"]
