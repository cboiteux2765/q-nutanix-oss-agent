"""Release readiness (non-blocking in phase 1): was the changelog updated?"""

from __future__ import annotations

from .. import gitutil
from ..models import CheckResult, CheckStatus, Finding, RiskTier, Severity
from . import CheckContext

NAME = "release_readiness"
CHANGELOG_NAMES = ("CHANGELOG.md", "CHANGELOG.rst", "CHANGELOG", "CHANGES.md", "HISTORY.md")


def check(ctx: CheckContext) -> CheckResult:
    if ctx.tier == RiskTier.LOW:
        return CheckResult(name=NAME, required=False, status=CheckStatus.NOT_APPLICABLE, detail="Low-risk change")
    changelog = next((n for n in CHANGELOG_NAMES if gitutil.read_file(ctx.repo, ctx.head_sha, n) is not None), None)
    if changelog is None:
        return CheckResult(
            name=NAME, required=False, status=CheckStatus.NOT_APPLICABLE, detail="Repository has no changelog"
        )
    if any(f.path == changelog for f in ctx.files):
        return CheckResult(name=NAME, required=False, status=CheckStatus.PASS, detail=f"{changelog} updated")
    return CheckResult(
        name=NAME,
        required=False,
        status=CheckStatus.PASS,
        detail=f"{changelog} not updated",
        findings=[
            Finding(
                check=NAME,
                severity=Severity.LOW,
                blocking=False,
                title="Changelog not updated",
                file=changelog,
                evidence=f"A {ctx.tier.name.lower()}-risk change does not touch {changelog}",
                suggested_fix=f"Add an entry to {changelog} describing the user-visible change",
            )
        ],
    )
