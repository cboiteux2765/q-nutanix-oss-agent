"""Combine check results into one verdict, following the QA PRD rules.

Order matters:
  1. A confirmed blocking finding -> CHANGES_REQUESTED.
  2. A required check that could not run -> BLOCKED (never a pass).
  3. A PR with no net change vs. its base -> NEEDS_HUMAN (the intent may be lost).
  4. A tier the agent may not approve -> NEEDS_HUMAN.
  5. Otherwise -> APPROVED.
"""

from __future__ import annotations

from .models import CheckResult, CheckStatus, Policy, RiskTier, Verdict


def decide(
    tier: RiskTier, checks: list[CheckResult], policy: Policy, changed_files: int = 1
) -> tuple[Verdict, list[str], str, bool]:
    """Return (verdict, reasons, next_owner, human_confirmation_required)."""
    reasons: list[str] = []

    blocking = [f for c in checks for f in c.findings if f.blocking]
    unchecked = [c for c in checks if c.required and c.status == CheckStatus.NOT_CHECKED]
    missing = [name for name in policy.required_checks if name not in {c.name for c in checks}]

    if blocking:
        for f in blocking:
            where = f" ({f.file}:{f.line})" if f.file and f.line else (f" ({f.file})" if f.file else "")
            reasons.append(f"[{f.check}] {f.title}{where}")
        for c in unchecked:
            reasons.append(f"[{c.name}] also NOT_CHECKED: {c.detail}")
        return Verdict.CHANGES_REQUESTED, reasons, "Implementer, via Code PM", False

    if unchecked or missing:
        reasons += [f"[{c.name}] required check NOT_CHECKED: {c.detail}" for c in unchecked]
        reasons += [f"[{name}] required check never ran" for name in missing]
        return Verdict.BLOCKED, reasons, "Environment owner (fix the scanner), then rerun QA", False

    if changed_files == 0:
        reasons.append("PR makes no net change against its base; confirm the intended change was not lost")
        return Verdict.NEEDS_HUMAN, reasons, "Named human reviewer", True

    if tier.name not in policy.agent_may_approve:
        reasons.append(f"Risk tier {tier.name}: the agent may only recommend; a named human approves")
        return Verdict.NEEDS_HUMAN, reasons, "Named human reviewer", True

    reasons.append(f"All required checks passed with no blocking findings; tier {tier.name} allows agent approval")
    if policy.pilot_mode:
        reasons.append("Pilot mode: a team member confirms this approval before merge")
        return Verdict.APPROVED, reasons, "Team member confirms, then Code PM merges", True
    return Verdict.APPROVED, reasons, "Code PM merges", False
