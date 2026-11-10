"""Export requests for the team's Test and Coding prototypes; never submit them."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import gitutil
from .models import QADecision, Verdict


def test_review(decision: QADecision, *, human_reviewer: str | None = None) -> dict:
    if decision.verdict not in (Verdict.APPROVED, Verdict.CHANGES_REQUESTED):
        raise ValueError("Only APPROVED or CHANGES_REQUESTED can become a Test review")
    if decision.human_confirmation_required and not (human_reviewer or "").strip():
        raise ValueError("Human confirmation is required before exporting approval")
    if not decision.test_identity_hash or not decision.test_artifact_manifest_hash:
        raise ValueError("Test identity and artifact manifest hashes are required")
    return {
        "decision": "APPROVED" if decision.verdict == Verdict.APPROVED else "REJECTED",
        "identity_hash": decision.test_identity_hash,
        "artifact_manifest_hash": decision.test_artifact_manifest_hash,
        "comment": f"QA {decision.qa_run_id}; confirmation: {human_reviewer or 'agent'}; " + "; ".join(decision.reasons),
    }


def coding_revision(decision: QADecision, coding_result: dict) -> dict:
    if decision.verdict != Verdict.CHANGES_REQUESTED:
        raise ValueError("Only CHANGES_REQUESTED produces a Coding revision")
    for name, expected in (("repository", decision.repo), ("pr_number", decision.pr_number),
                           ("pr_head_sha", decision.head_sha), ("spec_hash", decision.spec_hash)):
        if coding_result.get(name) != expected:
            raise ValueError(f"Coding result {name} does not match the QA review")
    if not coding_result.get("run_id"):
        raise ValueError("Coding run_id is required")
    return {
        "schema_version": "0.1", "run_id": coding_result["run_id"],
        "source": "qa_agent", "verdict": "CHANGES_REQUIRED",
        "tested_head_sha": decision.head_sha, "findings": decision.reasons,
        "requested_by": f"qa-agent:{decision.qa_run_id}", "timestamp": decision.created_at,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=["test-review", "coding-revision"])
    parser.add_argument("--decision", required=True, type=Path)
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--head", required=True)
    parser.add_argument("--coding-result", type=Path)
    parser.add_argument("--human-reviewer")
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        decision = QADecision.model_validate_json(args.decision.read_bytes())
        if gitutil.resolve(args.repo, args.head) != decision.head_sha:
            raise ValueError("PR head changed after QA; rerun Test and QA")
        if args.kind == "test-review":
            request = test_review(decision, human_reviewer=args.human_reviewer)
        else:
            if args.coding_result is None:
                raise ValueError("--coding-result is required")
            request = coding_revision(decision, json.loads(args.coding_result.read_bytes()))
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(request, indent=2) + "\n", encoding="utf-8")
    except (ValueError, OSError, gitutil.GitError) as exc:
        parser.exit(2, f"Cannot export handoff: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
