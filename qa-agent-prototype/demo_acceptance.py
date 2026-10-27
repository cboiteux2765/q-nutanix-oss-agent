"""Run the QA Agent's acceptance scenarios end to end.

Each scenario builds a real local Git repository, a Test Agent style
verification.json, and runs the real QA Agent (including real bandit).
Matches the "Done means" list in the QA PRD.

    python demo_acceptance.py            # writes demo-output/<timestamp>/
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from app import fixtures as fx
from app.agent import review
from app.models import Policy, Verdict


def scenario_clean(root: Path):
    repo, base = fx.make_repo(root)
    head = fx.open_pr(repo, fx.CLEAN_CHANGE, "Add sub()")
    return repo, fx.write_verification(root / "verification.json", base, head), Policy()


def scenario_shell_injection(root: Path):
    repo, base = fx.make_repo(root)
    head = fx.open_pr(repo, fx.SHELL_INJECTION_CHANGE, "Add expression runner")
    return repo, fx.write_verification(root / "verification.json", base, head), Policy()


def scenario_leaked_secret(root: Path):
    repo, base = fx.make_repo(root)
    head = fx.open_pr(repo, fx.secret_change(), "Add config")
    return repo, fx.write_verification(root / "verification.json", base, head), Policy()


def scenario_vulnerable_dependency(root: Path):
    repo, base = fx.make_repo(root)
    head = fx.open_pr(repo, fx.VULNERABLE_DEP_CHANGE, "Add yaml helper dependency")
    return repo, fx.write_verification(root / "verification.json", base, head), Policy()


def scenario_high_risk(root: Path):
    repo, base = fx.make_repo(root)
    head = fx.open_pr(repo, fx.HIGH_RISK_CHANGE, "Add deploy step to CI")
    return repo, fx.write_verification(root / "verification.json", base, head), Policy()


def scenario_stale(root: Path):
    repo, base = fx.make_repo(root)
    tested_head = fx.open_pr(repo, fx.CLEAN_CHANGE, "Add sub()")
    verification = fx.write_verification(root / "verification.json", base, tested_head)
    fx.push_to_pr(repo, {"src/calc/util.py": fx.CLEAN_CHANGE["src/calc/util.py"] + "\n# late edit\n"})
    return repo, verification, Policy()


def scenario_broken_scanner(root: Path):
    repo, base = fx.make_repo(root)
    head = fx.open_pr(repo, fx.CLEAN_CHANGE, "Add sub()")
    return (
        repo,
        fx.write_verification(root / "verification.json", base, head),
        Policy(bandit_command="bandit-is-not-installed"),
    )


def scenario_not_ready(root: Path):
    repo, base = fx.make_repo(root)
    head = fx.open_pr(repo, fx.CLEAN_CHANGE, "Add sub()")
    return repo, fx.write_verification(root / "verification.json", base, head, readiness="CHANGES_REQUIRED"), Policy()


SCENARIOS = [
    ("clean PR is approved (pilot: team confirms)", scenario_clean, Verdict.APPROVED),
    ("shell injection is caught by bandit", scenario_shell_injection, Verdict.CHANGES_REQUESTED),
    ("leaked AWS key is caught and redacted", scenario_leaked_secret, Verdict.CHANGES_REQUESTED),
    ("vulnerable dependency is caught", scenario_vulnerable_dependency, Verdict.CHANGES_REQUESTED),
    ("CI workflow change goes to a human", scenario_high_risk, Verdict.NEEDS_HUMAN),
    ("new commit after testing is STALE", scenario_stale, Verdict.STALE),
    ("broken scanner is BLOCKED, never a pass", scenario_broken_scanner, Verdict.BLOCKED),
    ("Test handoff not READY_FOR_QA is BLOCKED", scenario_not_ready, Verdict.BLOCKED),
]


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--output", type=Path, default=None)
    args = p.parse_args()
    out = args.output or Path("demo-output") / datetime.now().strftime("%Y%m%d-%H%M%S")
    if out.exists():
        print(f"{out} already exists; pick a new directory", file=sys.stderr)
        return 2

    summary, failures = [], 0
    for i, (title, build, expected) in enumerate(SCENARIOS, 1):
        root = out / f"{i:02d}-{build.__name__.removeprefix('scenario_')}"
        repo, verification, policy = build(root)
        decision = review(repo, verification, "pr", root / "qa", policy)
        ok = decision.verdict == expected
        failures += not ok
        print(f"{'PASS' if ok else 'FAIL'}  {i}. {title}: {decision.verdict.value}")
        summary.append(
            {"scenario": title, "expected": expected.value, "actual": decision.verdict.value, "passed": ok}
        )

    (out / "acceptance_summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(f"\n{len(SCENARIOS) - failures}/{len(SCENARIOS)} scenarios passed. Reports in {out}/")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
