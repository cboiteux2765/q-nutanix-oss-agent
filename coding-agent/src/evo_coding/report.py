"""Evidence output: the redacted JSONL trace, work_report.md, the artifact manifest and the
Test Agent request adapter.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .handoff import sha256_file
from .models import ArtifactRef, CodingResult, RunState, utc_now
from .safety import redact


class TraceLog:
    def __init__(self, path: Path, secrets: list[str] = ()):
        self.path = path
        self.secrets = list(secrets)
        path.parent.mkdir(parents=True, exist_ok=True)

    def event(self, phase: str, event: str, **data: Any) -> None:
        record = {"ts": utc_now().isoformat(), "phase": phase, "event": event, **data}
        line = redact(json.dumps(record, default=str, ensure_ascii=False), self.secrets)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


MEDIA_TYPES = {".json": "application/json", ".md": "text/markdown", ".xml": "application/xml",
               ".log": "text/plain", ".patch": "text/x-diff", ".jsonl": "application/x-ndjson"}


def collect_artifacts(artifact_dir: Path, exclude: set[str] = frozenset()) -> list[ArtifactRef]:
    refs = []
    for path in sorted(p for p in artifact_dir.rglob("*") if p.is_file()):
        rel = path.relative_to(artifact_dir).as_posix()
        if rel in exclude:
            continue
        refs.append(ArtifactRef(name=rel, uri=rel, sha256=sha256_file(path),
                                media_type=MEDIA_TYPES.get(path.suffix, "application/octet-stream")))
    return refs


def to_test_agent_request(result: CodingResult, spec_uri: str, test_profile_id: str,
                          environment_manifest_id: str) -> dict:
    """Build the Test Agent prototype's CreateRunRequest (schema 1.0) from a TESTING handoff."""
    return {
        "schema_version": "1.0",
        "repo": result.repository,
        "pr_number": result.pr_number,
        "spec_id": f"{result.repository}#{result.issue_id}@v{result.spec_version}",
        "spec_uri": spec_uri,
        "spec_hash": result.spec_hash,
        "test_profile_id": test_profile_id,
        "environment_manifest_id": environment_manifest_id,
        "correlation_id": f"{result.run_id}:r{result.revision}",
    }


def _table(headers: list[str], rows: list[list[str]]) -> str:
    if not rows:
        return "_none_\n"
    clean = [[str(c).replace("|", "\\|").replace("\n", " ") for c in row] for row in rows]
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    lines += ["| " + " | ".join(row) + " |" for row in clean]
    return "\n".join(lines) + "\n"


def render_work_report(result: CodingResult, issue_title: str, summary: str = "") -> str:
    status_line = {
        RunState.TESTING: "Draft PR opened and handed to the Test Agent.",
        RunState.BLOCKED: "Blocked; no PR update was published.",
        RunState.CANCELLED: "Cancelled; partial evidence retained.",
        RunState.STALE: "Rejected as stale; nothing was changed.",
        RunState.INCONCLUSIVE: "Inconclusive; the run hit an internal error.",
    }.get(result.status, "")
    out = [
        f"# Work report: {result.repository}#{result.issue_id} {issue_title}",
        "",
        f"**Status:** `{result.status}`, next owner `{result.next_owner}`. {status_line}",
        "",
        _table(["Field", "Value"], [
            ["Run", f"`{result.run_id}` (revision {result.revision})"],
            ["Pull request", f"[#{result.pr_number}]({result.pr_url})" if result.pr_number else "-"],
            ["Branch", f"`{result.branch}` -> `{result.base_branch}`" if result.branch else "-"],
            ["Source commit", f"`{result.source_commit}`"],
            ["Head commit", f"`{result.pr_head_sha}`" if result.pr_head_sha else "-"],
            ["Spec", f"v{result.spec_version} `{result.spec_hash[:16]}...`"],
            ["Risk tier", f"{result.risk_tier}" + (" (named human reviewer required)"
                                                    if result.requires_human_review else "")],
        ]),
    ]
    if summary:
        out += ["## Implementation summary (implementer's own words)", "", summary, ""]
    out += ["## Changed files", "", _table(["Path", "Change", "+", "-", "Test"], [
        [f"`{c.path}`", c.change.value, str(c.additions), str(c.deletions), "yes" if c.is_test else ""]
        for c in result.changed_files])]
    out += ["## Acceptance criteria coverage", "",
            "Statuses combine the implementer's claim with local test evidence. The Test Agent's verification of "
            "the PR head is authoritative.", "",
            _table(["Criterion", "Status", "Claim", "Local tests", "Notes"], [
                [c.criterion_id, c.status.value, c.implementer_claim or "-",
                 f"{c.local_test_evidence.value} {', '.join(c.local_test_ids)}".strip(), c.implementer_notes]
                for c in result.criteria_coverage])]
    out += ["## Test runs", "", _table(["Run", "Commit", "Result", "Passed", "Failed", "Errors", "Seconds"], [
        [t.label, (t.commit or "-")[:12], "PASS" if t.succeeded else ("TIMEOUT" if t.timed_out else "FAIL"),
         str(t.passed), str(t.failed), str(t.errors), f"{t.duration_seconds:.1f}"] for t in result.tests])]
    failing = [f"- `{name}` ({t.label})" for t in result.tests for name in t.failing_tests]
    if failing:
        out += ["Failing tests:", "", *failing, ""]
    if result.blockers:
        out += ["## Blockers", "", _table(["Code", "Owner", "Message"],
                                          [[b.code, b.owner.value, b.message] for b in result.blockers])]
    if result.clarification_requests:
        out += ["## Clarification requests", "",
                *[f"- ({c.owner.value}) {c.question}" for c in result.clarification_requests], ""]
    if result.unresolved_items:
        out += ["## Unresolved items", "", *[f"- {item}" for item in result.unresolved_items], ""]
    b = result.budget_used
    out += ["## Budget used", "",
            f"US${b.usd:.4f}, {b.seconds:.1f}s, {b.llm_calls} model calls "
            f"({b.input_tokens} in / {b.output_tokens} out tokens), {b.steps} implementer steps.", ""]
    if result.implementer:
        i = result.implementer
        out += ["## Implementer", "",
                f"`{i.name}` {i.version}, model `{i.model or '-'}`, prompt `{i.prompt_version or '-'}`, "
                f"exit `{i.exit_status}` after {i.steps} steps.", ""]
    if result.pr_head_sha:
        out += ["## Reproduce", "", "```bash",
                f"git fetch origin {result.branch} && git checkout {result.pr_head_sha}",
                *[t.command for t in result.tests if t.label == "final"], "```", ""]
    return "\n".join(out)
