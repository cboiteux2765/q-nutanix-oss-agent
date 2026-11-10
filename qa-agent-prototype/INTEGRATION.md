# QA integration update — November 10, 2026

This update changes only the QA prototype. Tested against:

| Component | Branch | Commit |
| --- | --- | --- |
| QA baseline | timothy | 70264e8 |
| Test | shanqin | 26d6417 |
| Coding | mihir-coding | 52c5c3a |
| Spec contract inspected | clement | a927629 |

## Install the update

Replace your existing `qa-agent-prototype` folder with the folder in the ZIP.
Keep your own virtual environment and uncommitted changes separately; inspect the
included patch first if you have made local edits since 70264e8.

```bash
cd qa-agent-prototype
python -m pip install -r requirements.txt
python -m pytest -q
python demo_acceptance.py
```

There are 90 tests: 88 run standalone; two integration tests require sibling
`test-agent-prototype` and `coding-agent` folders checked out from the branches
above. Use separate processes for these prototypes: both QA and Test call their
Python package `app`, so importing both into the same process causes collisions.

## Test → QA → Test

Run Test's demo and point QA at its output, as in the README. For an individual run:

```bash
python -m app.cli --repo /path/to/repository --head pr-branch \
  --verification /path/to/artifacts/verification.json --out qa-output
```

After a real human has reviewed and confirmed an approval:

```bash
python -m app.handoff test-review --decision qa-output/qa_decision.json \
  --repo /path/to/repository --head pr-branch \
  --human-reviewer "Timothy Nguyen" --out qa-output/test_review_request.json
```

For CHANGES_REQUESTED, the same command exports REJECTED without requiring an
approval confirmation. NEEDS_HUMAN, BLOCKED and STALE cannot become a Test review.
An orchestrator submits the resulting body to
`POST /v1/test-runs/{test_run_id}/review` using a configured principal with the
Test Agent's `qa` role. The local counterpart is
`RunManager.review_run(run_id, ReviewRequest(...), principal)`.
The `--human-reviewer` flag records an operator assertion of confirmation; it
does not authenticate that person. Test still enforces authorization, current
identity, and evidence integrity when accepting the request.

## QA → Coding revision

```bash
python -m app.handoff coding-revision --decision qa-output/qa_decision.json \
  --repo /path/to/repository --head pr-branch \
  --coding-result /path/to/coding_result.json --out qa-output/revision_request.json
```

This maps QA's CHANGES_REQUESTED to Coding's CHANGES_REQUIRED and uses the original
Coding run_id. Repository, PR number, reviewed head, and spec hash must all match.
Pass that file to the Coding workflow's revision entry point. BLOCKED, STALE, and
NEEDS_HUMAN do not trigger automatic revisions. Every export checks that the PR
head still matches QA's reviewed commit. The maintainer must check freshness
again at merge time.

## What was verified

- 90 QA tests passed with sibling prototypes available, including real Test
  execution → QA CLI → export → acceptance by Test's RunManager.
- Coding revision and Test review exports validated against the teammates'
  actual Pydantic models in separate processes.
- All eight QA acceptance scenarios passed.
- Test's 95 existing tests passed unchanged.
- Test's demo outputs were reviewed through QA's integration script.

Regression coverage includes contradictory READY_FOR_QA evidence, changed
manifest/evidence, missing base commits, Unicode/space/tab filenames, upstream
human confirmation, and mismatched Coding identity.

## Remaining team integration work

The Spec prototype exports a `coding-input-0.1` wrapper with a nested handoff and
APPROVED/AUTO_CLEARED status. Coding expects a different schema `0.1` Handoff
with SPEC_APPROVED, scope rules, environment settings, and requirement-to-criterion
mapping. The team needs an explicit adapter and agreement on automatic clearance.
QA cannot safely invent these policy and test mappings. A complete live
Spec → Coding → Test → QA pipeline has not been demonstrated by this update.

Test also needs its registered repository/spec/profile/environment to match
Coding's generated test request; exporting that request alone does not register
them. This update verifies QA's adjacent interfaces, not arbitrary coding runs.

Dependency advisories remain fictional demo data. GitHub event triggering,
HTTP submission, live model calls, and deployment remain outside this prototype.
