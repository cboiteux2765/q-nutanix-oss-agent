"""Security check: run bandit on the PR's changed Python files.

Only issues the PR introduces count. Each file is scanned at the base and at
the head, and an issue already present on the base is not blamed on the PR.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

from .. import gitutil
from ..models import CheckResult, CheckStatus, Finding, Severity
from . import CheckContext, ToolError

NAME = "security"
# B101 flags every `assert`, which is how pytest tests are written.
SKIPPED_TESTS = "B101"


def _materialize(ctx: CheckContext, rev: str, paths: list[str], root: Path) -> list[Path]:
    written = []
    for path in paths:
        text = gitutil.read_file(ctx.repo, rev, path)
        if text is None:
            continue
        target = root / path
        if not target.resolve().is_relative_to(root.resolve()):
            continue  # refuse path tricks like ../../
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        written.append(target)
    return written


def _bandit(ctx: CheckContext, root: Path, files: list[Path]) -> list[dict]:
    if not files:
        return []
    cmd = [ctx.policy.bandit_command, "-f", "json", "-q", "-s", SKIPPED_TESTS, *[str(f) for f in files]]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=ctx.policy.tool_timeout_seconds, check=False, cwd=root
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ToolError(f"bandit could not run: {exc}") from exc
    # bandit exits 0 with no issues and 1 with issues; anything else is an error.
    if proc.returncode not in (0, 1):
        raise ToolError(f"bandit exited {proc.returncode}: {proc.stderr.strip()[:300]}")
    try:
        report = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise ToolError("bandit produced unreadable output") from exc
    if report.get("errors"):
        raise ToolError(f"bandit reported scan errors: {report['errors'][:2]}")
    issues = []
    for issue in report.get("results", []):
        rel = Path(issue["filename"]).resolve().relative_to(root.resolve()).as_posix()
        issues.append({**issue, "rel": rel})
    return issues


def _key(issue: dict, text: str | None) -> tuple[str, str, str]:
    lines = (text or "").splitlines()
    n = issue.get("line_number", 0)
    line = lines[n - 1].strip() if 0 < n <= len(lines) else ""
    return issue["rel"], issue["test_id"], line


def check(ctx: CheckContext) -> CheckResult:
    paths = [f.path for f in ctx.files if f.status != "D" and f.path.endswith(".py")]
    if not paths:
        return CheckResult(name=NAME, required=True, status=CheckStatus.NOT_APPLICABLE, detail="No Python files changed")

    with tempfile.TemporaryDirectory(prefix="qa-head-") as head_dir, tempfile.TemporaryDirectory(
        prefix="qa-base-"
    ) as base_dir:
        head_root, base_root = Path(head_dir), Path(base_dir)
        head_issues = _bandit(ctx, head_root, _materialize(ctx, ctx.head_sha, paths, head_root))
        base_issues = _bandit(ctx, base_root, _materialize(ctx, ctx.base_sha, paths, base_root))

    head_text = {p: gitutil.read_file(ctx.repo, ctx.head_sha, p) for p in paths}
    base_text = {p: gitutil.read_file(ctx.repo, ctx.base_sha, p) for p in paths}
    existing = {_key(i, base_text.get(i["rel"])) for i in base_issues}

    findings = []
    for issue in head_issues:
        if _key(issue, head_text.get(issue["rel"])) in existing:
            continue
        raw = str(issue.get("issue_severity", "")).upper()
        severity = Severity(raw) if raw in Severity.__members__ else Severity.LOW
        findings.append(
            Finding(
                check=NAME,
                severity=severity,
                blocking=severity == Severity.HIGH,
                title=f"{issue['test_id']} {issue['test_name']}",
                file=issue["rel"],
                line=issue.get("line_number"),
                evidence=f"bandit: {issue['issue_text']} (confidence {issue['issue_confidence']})",
                suggested_fix=f"See {issue.get('more_info', 'bandit docs')}",
            )
        )

    blocking = any(f.blocking for f in findings)
    detail = f"bandit scanned {len(paths)} changed file(s); {len(findings)} new issue(s)"
    return CheckResult(
        name=NAME,
        required=True,
        status=CheckStatus.FAIL if blocking else CheckStatus.PASS,
        detail=detail,
        findings=findings,
    )
