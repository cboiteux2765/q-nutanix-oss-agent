"""QA review checks. Each one turns evidence from the PR into findings."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..models import ChangedFile, CheckResult, CheckStatus, Policy, RiskTier


class ToolError(RuntimeError):
    """A scanner could not run or returned unusable output. Never a pass."""


@dataclass
class CheckContext:
    repo: Path
    base_sha: str
    head_sha: str
    files: list[ChangedFile]
    added: dict[str, list[tuple[int, str]]]
    policy: Policy
    tier: RiskTier
    advisories_path: Path | None = None


CheckFn = Callable[[CheckContext], CheckResult]


def run_check(name: str, fn: CheckFn, ctx: CheckContext) -> CheckResult:
    """Run one check, retrying tool failures (PRD: retry once, then NOT_CHECKED)."""
    required = name in ctx.policy.required_checks
    attempts = 0
    last_error = ""
    start = time.monotonic()
    while attempts <= ctx.policy.tool_retries:
        attempts += 1
        try:
            result = fn(ctx)
            return result.model_copy(
                update={
                    "required": required,
                    "attempts": attempts,
                    "duration_seconds": round(time.monotonic() - start, 3),
                }
            )
        except ToolError as exc:
            last_error = str(exc)
    return CheckResult(
        name=name,
        required=required,
        status=CheckStatus.NOT_CHECKED,
        detail=f"Tool failed after {attempts} attempts: {last_error}",
        attempts=attempts,
        duration_seconds=round(time.monotonic() - start, 3),
    )


def planned(name: str, why: str) -> CheckFn:
    """A PRD check that phase 1 does not implement yet. Reported, never faked."""

    def _fn(ctx: CheckContext) -> CheckResult:
        return CheckResult(name=name, required=False, status=CheckStatus.NOT_CHECKED, detail=f"Planned: {why}")

    return _fn
