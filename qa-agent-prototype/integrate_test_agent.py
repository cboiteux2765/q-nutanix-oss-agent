"""Run the QA Agent on real Test Agent output (Test -> QA handoff).

1. On the feat/test-agent-prototype branch, run the Test Agent demo:
       cd test-agent-prototype && python demo_acceptance.py --output ../ta-demo
2. From this folder, point QA at that output:
       python integrate_test_agent.py ../ta-demo

For each Test Agent scenario, QA treats the fixture repository's checked-out
branch tip as the current PR head, picks the verification.json for that head
(or the newest one, which then comes back STALE), and writes its own
qa_report.md / qa_decision.json next to the evidence.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from app import gitutil
from app.agent import review


def pick_verification(scenario_dir: Path, head_sha: str) -> Path | None:
    """Prefer the run that verified the current head; otherwise the newest run."""
    candidates = [p for p in scenario_dir.glob("state/runs/*/artifacts/verification.json") if "audit" not in p.parts]
    if not candidates:
        return None
    for path in candidates:
        try:
            if json.loads(path.read_text(encoding="utf-8")).get("reviewed_head_sha") == head_sha:
                return path
        except (OSError, json.JSONDecodeError):
            continue
    return max(candidates, key=lambda p: p.stat().st_mtime)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    demo = Path(argv[1])
    scenarios = sorted(p for p in demo.iterdir() if (p / "repository" / ".git").exists())
    if not scenarios:
        print(f"No Test Agent scenarios found under {demo}")
        return 2

    for scenario in scenarios:
        repo = scenario / "repository"
        head = gitutil.resolve(repo, "HEAD")
        verification = pick_verification(scenario, head)
        if verification is None:
            print(f"{scenario.name:16} no verification.json")
            continue
        decision = review(repo, verification, head, scenario / "qa")
        print(f"{scenario.name:16} QA verdict {decision.verdict.value:18} {decision.reasons[0]}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
