"""Secret scan over the lines a PR adds. Matches are redacted in every output."""

from __future__ import annotations

import re

from ..models import CheckResult, CheckStatus, Finding, Severity
from . import CheckContext

NAME = "secrets"

PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("AWS access key ID", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    ("Private key", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----")),
    (
        "Hard-coded credential",
        re.compile(r"""(?i)\b(?:api[_-]?key|secret|passw(?:or)?d|token)\b\s*[:=]\s*['"]([^'"\s]{12,})['"]"""),
    ),
]

PLACEHOLDER = re.compile(r"(?i)(example|changeme|dummy|placeholder|your[_-]|xxxx|\*\*\*\*|<.+>)")


def redact(secret: str) -> str:
    return secret[:4] + "*" * 8 if len(secret) > 4 else "****"


def check(ctx: CheckContext) -> CheckResult:
    findings: list[Finding] = []
    scanned = 0
    for path, lines in ctx.added.items():
        for line_no, text in lines:
            scanned += 1
            for label, pattern in PATTERNS:
                match = pattern.search(text)
                if not match:
                    continue
                value = match.group(1) if match.groups() else match.group(0)
                if label == "Hard-coded credential" and PLACEHOLDER.search(value):
                    continue
                findings.append(
                    Finding(
                        check=NAME,
                        severity=Severity.HIGH,
                        blocking=True,
                        title=f"{label} added",
                        file=path,
                        line=line_no,
                        evidence=f"Matched {label.lower()} pattern: {redact(value)}",
                        suggested_fix="Remove it, rotate the credential, and load it from the environment or a secret store",
                    )
                )
                break  # one finding per line is enough

    status = CheckStatus.FAIL if findings else CheckStatus.PASS
    if scanned == 0:
        status = CheckStatus.NOT_APPLICABLE
    return CheckResult(
        name=NAME,
        required=True,
        status=status,
        detail=f"Scanned {scanned} added line(s)",
        findings=findings,
    )
