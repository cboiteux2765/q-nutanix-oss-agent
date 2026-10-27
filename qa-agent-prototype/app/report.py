"""Write qa_decision.json (for agents) and qa_report.md (for people)."""

from __future__ import annotations

import json
from pathlib import Path

from .models import CheckStatus, QADecision

SEVERITY_ORDER = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}


def write_outputs(decision: QADecision, out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "qa_decision.json"
    md_path = out_dir / "qa_report.md"
    json_path.write_text(json.dumps(decision.model_dump(mode="json"), indent=2) + "\n", encoding="utf-8")
    md_path.write_text(render_markdown(decision), encoding="utf-8")
    return json_path, md_path


def render_markdown(d: QADecision) -> str:
    lines = [f"# QA report: {d.verdict.value}", ""]
    if d.repo:
        lines.append(f"- PR: {d.repo} #{d.pr_number}")
    if d.head_sha:
        lines.append(f"- Reviewed head: `{d.head_sha[:12]}` (base `{(d.base_sha or '')[:12]}`)")
    if d.test_run_id:
        lines.append(f"- Test run: `{d.test_run_id}`")
    if d.risk_tier:
        lines.append(f"- Risk tier: {d.risk_tier}")
    lines.append(f"- Next owner: {d.next_owner}")
    lines.append(f"- Human confirmation required: {'yes' if d.human_confirmation_required else 'no'}")
    lines.append(f"- Policy: {d.policy_version}")
    lines += ["", "## Why", ""]
    lines += [f"- {r}" for r in d.reasons]

    if d.risk_reasons:
        lines += ["", "## Risk tier reasons", ""]
        lines += [f"- {r}" for r in d.risk_reasons]

    findings = sorted(
        (f for c in d.checks for f in c.findings), key=lambda f: (not f.blocking, SEVERITY_ORDER[f.severity.value])
    )
    lines += ["", "## Findings", ""]
    if not findings:
        lines.append("None.")
    for f in findings:
        where = f"`{f.file}:{f.line}`" if f.file and f.line else (f"`{f.file}`" if f.file else "")
        tag = "BLOCKING" if f.blocking else "suggestion"
        lines.append(f"- **{f.severity.value} {tag}** [{f.check}] {f.title} {where}".rstrip())
        lines.append(f"  - Evidence: {f.evidence}")
        lines.append(f"  - Fix: {f.suggested_fix}")

    if d.checks:
        lines += ["", "## Checks", "", "| Check | Required | Status | Detail |", "| --- | --- | --- | --- |"]
        for c in d.checks:
            detail = c.detail.replace("|", "\\|").replace("\n", " ")
            lines.append(f"| {c.name} | {'yes' if c.required else 'no'} | {c.status.value} | {detail} |")

        not_checked = [c for c in d.checks if c.status == CheckStatus.NOT_CHECKED]
        if not_checked:
            lines += ["", "## Not checked", ""]
            lines += [f"- {c.name}: {c.detail}" for c in not_checked]

    lines.append("")
    return "\n".join(lines)
