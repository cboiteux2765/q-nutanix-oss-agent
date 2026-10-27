"""Command line entry point.

    python -m app.cli --repo PATH --verification PATH/verification.json --head pr-branch --out qa-output/
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .agent import review
from .models import Policy, Verdict

EXIT_CODES = {
    Verdict.APPROVED: 0,
    Verdict.CHANGES_REQUESTED: 1,
    Verdict.NEEDS_HUMAN: 2,
    Verdict.BLOCKED: 3,
    Verdict.STALE: 4,
}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="EVO QA Agent prototype")
    p.add_argument("--repo", required=True, type=Path, help="local Git checkout of the PR repository")
    p.add_argument("--verification", required=True, type=Path, help="Test Agent verification.json")
    p.add_argument("--head", required=True, help="current PR head ref or SHA")
    p.add_argument("--out", required=True, type=Path, help="directory for qa_decision.json and qa_report.md")
    p.add_argument("--advisories", type=Path, help="advisory JSON feed (defaults to the bundled demo feed)")
    p.add_argument("--no-pilot", action="store_true", help="let agent approvals stand without human confirmation")
    args = p.parse_args(argv)

    policy = Policy(pilot_mode=not args.no_pilot)
    decision = review(args.repo, args.verification, args.head, args.out, policy, args.advisories)
    print(f"{decision.verdict.value}  next: {decision.next_owner}")
    for reason in decision.reasons:
        print(f"  - {reason}")
    print(f"Wrote {args.out / 'qa_report.md'} and {args.out / 'qa_decision.json'}")
    return EXIT_CODES[decision.verdict]


if __name__ == "__main__":
    sys.exit(main())
