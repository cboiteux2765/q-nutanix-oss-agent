# EVO QA Agent Prototype (phase 1)

A runnable Python prototype of the QA phase. It picks up where the Test Agent stops: it reads the Test Agent's `verification.json`, refuses to start unless that handoff is `READY_FOR_QA` for the current PR head, assigns a risk tier, runs real scanners on what the PR changed, and returns one verdict with evidence.

Everything runs offline against local Git repositories. No GitHub, cloud, or model account is needed. QA never pushes, merges, or deploys.

## Quick start

Requirements: Python 3.11+ and Git on PATH.

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python demo_acceptance.py          # 8 scenarios, prints PASS lines
python -m pytest -q                # 74 tests
```

Each demo run writes a new folder under `demo-output/` with a fixture repo, a `verification.json`, and QA's `qa_report.md` + `qa_decision.json` for every scenario.

## Acceptance scenarios

These match the "done" list in the QA PRD.

| # | Scenario | Verdict |
| --- | --- | --- |
| 1 | Clean source change | APPROVED (pilot: a team member confirms) |
| 2 | `subprocess.run(..., shell=True)` on user input | CHANGES_REQUESTED (bandit B602) |
| 3 | AWS-style access key committed | CHANGES_REQUESTED, key redacted in every output |
| 4 | Dependency pinned to a version with an advisory | CHANGES_REQUESTED |
| 5 | CI workflow edited | NEEDS_HUMAN (high-risk tier) |
| 6 | New commit pushed after the Test run | STALE |
| 7 | Security scanner missing | BLOCKED, never a pass |
| 8 | Test handoff says CHANGES_REQUIRED | BLOCKED, QA does not start |

## Run it on the real Test Agent output

The Test Agent prototype lives on the `feat/test-agent-prototype` branch. Run its demo, then point QA at the result:

```bash
# in a checkout of feat/test-agent-prototype
cd test-agent-prototype && python demo_acceptance.py --output ../ta-demo

# back in this folder
python integrate_test_agent.py path/to/ta-demo
```

Result on the Test Agent's five scenarios (Oct 27, 2026):

| Test Agent scenario | Test readiness | QA verdict |
| --- | --- | --- |
| 01-clean | READY_FOR_QA | APPROVED |
| 02-repair (fresh run on incorporated head) | READY_FOR_QA | NEEDS_HUMAN: the final PR has no net change against its base |
| 03-environment | BLOCKED | BLOCKED (QA does not start) |
| 04-spec | BLOCKED | BLOCKED (QA does not start) |
| 05-budget | CHANGES_REQUIRED | BLOCKED (QA does not start) |
| 05-stale | STALE | STALE |

## Run it on one PR

```bash
python -m app.cli --repo PATH/TO/REPO --verification PATH/TO/verification.json --head pr-branch --out qa-output
```

Exit code: 0 APPROVED, 1 CHANGES_REQUESTED, 2 NEEDS_HUMAN, 3 BLOCKED, 4 STALE. `--advisories FILE` swaps in another advisory feed. `--no-pilot` lets agent approvals stand without a human confirming.

## How a review runs

1. **Intake** (`app/intake.py`). Validates the handoff against the fields of Test's `verification-v1` schema. A head SHA, spec hash, or test profile mismatch gives STALE. Readiness other than READY_FOR_QA gives BLOCKED.
2. **Risk tier** (`app/risk.py`). Path rules: CI workflows, auth, security, migrations, licenses, and deleted tests are HIGH. Other source and dependency files are MEDIUM. Docs and tests are LOW. The PR gets the highest tier of its files.
3. **Checks** (`app/checks/`).
   - `security`: real bandit on changed Python files at base and head. Only issues the PR introduces count. HIGH severity blocks.
   - `secrets`: regex scan of added lines for AWS keys, GitHub and Slack tokens, private keys, and hard-coded credentials. Matches are redacted.
   - `dependencies`: new or changed requirement pins against an advisory feed. A HIGH advisory blocks; unpinned packages are suggestions.
   - `release_readiness`: changelog updated for medium/high changes (suggestion only).
   - Tool failures retry once, then the check is NOT_CHECKED.
4. **Verdict** (`app/decision.py`), in order: blocking finding gives CHANGES_REQUESTED; a required check NOT_CHECKED gives BLOCKED; no net change or a HIGH tier gives NEEDS_HUMAN; otherwise APPROVED.
5. **Outputs** (`app/report.py`). `qa_decision.json` for agents and `qa_report.md` for people, both bound to the head SHA, the Test run ID and identity hash, and the SHA-256 of the exact `verification.json` read.

## Current limits

- The advisory feed in `app/data/advisories.json` is **fictional demo data**. A live source (pip-audit or OSV) is the next step before real use.
- Spec and intent review, conventions, license, performance, stability, and the adversarial probe appear in every report as `NOT_CHECKED` with "Planned". They are not required in phase 1, so they never block, and they are never reported as passing.
- QA reads a local Git checkout. No GitHub App, PR comments, or post-merge watch yet.
- Pilot mode is on by default: every agent approval still needs a team member, per the team's plan to make final calls during our own pilot.
- Secret patterns are a small regex set, not a full scanner like gitleaks.

## Layout

```text
app/agent.py          orchestrator: intake -> tier -> checks -> verdict -> outputs
app/models.py         verdicts, findings, policy, qa_decision contract
app/intake.py         Test Agent handoff validation and freshness
app/risk.py           path-based risk tiers
app/checks/           security (bandit), secrets, dependencies, release readiness
app/decision.py       verdict rules
app/report.py         qa_report.md and qa_decision.json
app/fixtures.py       local Git fixtures for the demo and tests
app/cli.py            command line entry point
demo_acceptance.py    8 end-to-end scenarios
integrate_test_agent.py  runs QA over the Test Agent demo output
tests/                74 unit and end-to-end tests
```
