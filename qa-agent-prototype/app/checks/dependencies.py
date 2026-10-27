"""Dependency check: new or changed pins in requirements files vs. advisories.

Phase 1 reads advisories from a local JSON file so the demo runs offline.
A live source (pip-audit or OSV) can replace `load_advisories` later.
"""

from __future__ import annotations

import json
import re
from fnmatch import fnmatch
from pathlib import Path

from .. import gitutil
from ..models import CheckResult, CheckStatus, Finding, Severity
from . import CheckContext, ToolError

NAME = "dependencies"
DEFAULT_ADVISORIES = Path(__file__).resolve().parent.parent / "data" / "advisories.json"
REQ_LINE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*(==|>=|<=|~=|>|<|!=)?\s*([^\s;#]*)")


def normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_requirements(text: str | None) -> dict[str, tuple[str | None, str | None]]:
    """Map package -> (operator, version). Skips comments, options, and URLs."""
    result: dict[str, tuple[str | None, str | None]] = {}
    for raw in (text or "").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith(("-", "git+", "http")):
            continue
        m = REQ_LINE.match(line)
        if m:
            result[normalize(m.group(1))] = (m.group(2), m.group(3) or None)
    return result


def load_advisories(path: Path) -> dict[str, list[dict]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ToolError(f"advisory database unavailable: {exc}") from exc
    db: dict[str, list[dict]] = {}
    for adv in data.get("advisories", []):
        db.setdefault(normalize(adv["package"]), []).append(adv)
    return db


def check(ctx: CheckContext) -> CheckResult:
    dep_files = [
        f.path
        for f in ctx.files
        if f.status != "D" and any(fnmatch(f.path, p) for p in ctx.policy.dependency_files)
    ]
    if not dep_files:
        return CheckResult(
            name=NAME, required=True, status=CheckStatus.NOT_APPLICABLE, detail="No dependency files changed"
        )

    db = load_advisories(ctx.advisories_path or DEFAULT_ADVISORIES)
    findings: list[Finding] = []
    checked = 0
    for path in dep_files:
        before = parse_requirements(gitutil.read_file(ctx.repo, ctx.base_sha, path))
        after = parse_requirements(gitutil.read_file(ctx.repo, ctx.head_sha, path))
        for pkg, (op, version) in after.items():
            if before.get(pkg) == (op, version):
                continue  # unchanged by this PR
            checked += 1
            if op != "==" or not version:
                findings.append(
                    Finding(
                        check=NAME,
                        severity=Severity.MEDIUM,
                        blocking=False,
                        title=f"{pkg} is not pinned",
                        file=path,
                        evidence=f"Requirement {pkg}{op or ''}{version or ''} can resolve to an unreviewed version",
                        suggested_fix=f"Pin an exact version, e.g. {pkg}==<version>",
                    )
                )
                continue
            for adv in db.get(pkg, []):
                if version in adv.get("affected_versions", []):
                    severity = Severity(adv.get("severity", "HIGH"))
                    fixed = adv.get("fixed_in")
                    findings.append(
                        Finding(
                            check=NAME,
                            severity=severity,
                            blocking=severity == Severity.HIGH,
                            title=f"{pkg}=={version} has advisory {adv['id']}",
                            file=path,
                            evidence=adv.get("summary", "Known vulnerable version"),
                            suggested_fix=f"Upgrade to {pkg}=={fixed}" if fixed else "Replace or remove the package",
                        )
                    )

    blocking = any(f.blocking for f in findings)
    return CheckResult(
        name=NAME,
        required=True,
        status=CheckStatus.FAIL if blocking else CheckStatus.PASS,
        detail=f"Checked {checked} new or changed requirement(s) in {len(dep_files)} file(s)",
        findings=findings,
    )
