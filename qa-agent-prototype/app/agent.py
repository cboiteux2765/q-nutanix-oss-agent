"""QA Agent orchestrator: intake -> risk tier -> checks -> verdict -> outputs."""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import gitutil
from .checks import CheckContext, planned, run_check
from .checks import dependencies, release, secrets, security
from .decision import decide
from .intake import load_handoff
from .models import POLICY_VERSION, CheckResult, Policy, QADecision, Reviewer, Verdict
from .report import write_outputs
from .risk import classify

AGENT = Reviewer(kind="agent", name="evo-qa-agent-prototype")

PLANNED_CHECKS = [
    ("spec_intent", "LLM review of the diff against the user story (phase 2)"),
    ("conventions", "per-repo convention rubric shared with the Code phase (phase 2)"),
    ("license", "license allowlist for new dependencies (phase 2)"),
    ("performance", "benchmark comparison against the base commit (phase 3)"),
    ("stability", "repeated suite runs to catch flaky tests (phase 3)"),
    ("adversarial_probe", "edge-case probe agent for medium/high risk (phase 3)"),
]


def review(
    repo: Path,
    verification_path: Path,
    head_ref: str,
    out_dir: Path,
    policy: Policy | None = None,
    advisories_path: Path | None = None,
    expected_spec_hash: str | None = None,
    expected_test_profile_hash: str | None = None,
) -> QADecision:
    policy = policy or Policy()
    start = time.monotonic()
    base = dict(
        qa_run_id=f"qa-{uuid.uuid4().hex[:12]}",
        created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        reviewer=AGENT,
        policy_version=policy.version,
    )

    try:
        head_sha = gitutil.resolve(repo, head_ref)
    except gitutil.GitError as exc:
        decision = QADecision(
            **base,
            verdict=Verdict.BLOCKED,
            reasons=[f"Cannot resolve PR head {head_ref!r}: {exc}"],
            next_owner="Operator (check the repo path and PR ref)",
            human_confirmation_required=False,
        )
        write_outputs(decision, out_dir)
        return decision

    intake = load_handoff(verification_path, head_sha, expected_spec_hash, expected_test_profile_hash)
    h = intake.handoff
    identity = dict(
        repo=h.repo if h else None,
        pr_number=h.pr_number if h else None,
        base_sha=h.base_sha if h else None,
        head_sha=head_sha,
        spec_id=h.spec_id if h else None,
        spec_hash=h.spec_hash if h else None,
        test_run_id=h.run_id if h else None,
        test_identity_hash=h.identity_hash if h else None,
        verification_sha256=intake.verification_sha256,
    )

    if not intake.ok:
        decision = QADecision(
            **base,
            **identity,
            verdict=intake.stop_verdict,
            reasons=intake.reasons,
            next_owner=intake.next_owner,
            human_confirmation_required=False,
            duration_seconds=round(time.monotonic() - start, 3),
        )
        write_outputs(decision, out_dir)
        return decision

    files = gitutil.changed_files(repo, h.base_sha, head_sha)
    tier, risk_reasons = classify(files, policy)
    ctx = CheckContext(
        repo=repo,
        base_sha=h.base_sha,
        head_sha=head_sha,
        files=files,
        added=gitutil.added_lines(repo, h.base_sha, head_sha),
        policy=policy,
        tier=tier,
        advisories_path=advisories_path,
    )

    checks: list[CheckResult] = [
        run_check(security.NAME, security.check, ctx),
        run_check(secrets.NAME, secrets.check, ctx),
        run_check(dependencies.NAME, dependencies.check, ctx),
        run_check(release.NAME, release.check, ctx),
    ]
    checks += [run_check(name, planned(name, why), ctx) for name, why in PLANNED_CHECKS]

    verdict, reasons, next_owner, confirm = decide(tier, checks, policy, changed_files=len(files))
    decision = QADecision(
        **base,
        **identity,
        risk_tier=tier.name,
        risk_reasons=risk_reasons,
        verdict=verdict,
        reasons=reasons,
        next_owner=next_owner,
        checks=checks,
        human_confirmation_required=confirm,
        duration_seconds=round(time.monotonic() - start, 3),
        extra={"changed_files": [f.model_dump() for f in files]},
    )
    write_outputs(decision, out_dir)
    return decision


__all__ = ["review", "POLICY_VERSION"]
