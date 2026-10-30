# Product Spec Agent prototype

Based on the supplied MVP PRD. Implementation proceeds one component at a time,
with a manual test checkpoint after each component.

For the new key-free mini-SWE-agent integration, start with [LOCAL_DEMO.md](LOCAL_DEMO.md).
It includes a recorded correction demo and real local Ollama generation commands.

## Component 1: issue intake (ready to test)

One Python file, standard library only, Python 3.10+. All GitHub requests are
read-only. No install, server, database, or LLM key is required.
The client uses the [GitHub REST issue API](https://docs.github.com/en/rest/issues/issues).

Run from the repository directory in PowerShell:

```powershell
py -3.12 -m unittest -v
$runPath = py -3.12 -m agent.spec_agent https://github.com/python/cpython/issues/159961 --allowed-repo python/cpython
Get-Content -LiteralPath $runPath -Raw
```

This public issue is just a smoke-test input, not a selected pilot repository.
Replace the URL and allowed repository with your own issue when ready.
On macOS/Linux, use `python3` instead of `py -3.12`.

Expected: the command prints the path to `runs/<run-id>.json`. The file contains
`status: CONTEXT_COLLECTED`, the issue title/body/labels, all comment pages,
`input.source_commit` (a full 40-character SHA), source URLs, a correlation ID,
collection time, elapsed seconds, and zero LLM cost. The `actor` is the issue
author, not an authenticated approver. Each invocation creates a new run.

Check the repository boundary (expected exit code 1, no new run):

```powershell
py -3.12 -m agent.spec_agent https://github.com/python/cpython/issues/159961 --allowed-repo another/repo
$LASTEXITCODE
```

Optional: use `--ref <commit-SHA>` to select a historical source snapshot.
Otherwise the default branch is resolved to a SHA at collection time. Issue and
comment text is a current snapshot, not historical content pinned by that SHA.
Comments may change during pagination; this is not an atomic GitHub snapshot.

For private repositories or higher API limits, set `GITHUB_TOKEN` in your shell
to a token with read access to the repository metadata, issues, and contents.
The program never writes the token to the run file. Run files can contain private
issue content and are ignored by Git. HTTP failures exit with a short error;
automatic retries, GitHub App authentication, and webhook deduplication are deferred.

## Component 2: repository evidence (ready to test)

`agent/evidence.py` reads an intake run and fetches selected files using the
[GitHub contents API](https://docs.github.com/en/rest/repos/contents) at that run's
exact commit. It writes a new run, preserving the original and correlation ID.
File selection is manual for this checkpoint; automatic relevance search is deferred.

Run the complete flow, or reuse your existing `$runPath`:

```powershell
py -3.12 -m unittest -v
$runPath = py -3.12 -m agent.spec_agent https://github.com/python/cpython/issues/159961 --allowed-repo python/cpython
$evidencePath = py -3.12 -m agent.evidence $runPath --allowed-repo python/cpython --file README.rst:1-40 --file Lib/pathlib/__init__.py:1-60
$result = Get-Content -LiteralPath $evidencePath -Raw | ConvertFrom-Json
$result.status
$result.evidence | Format-List id,path,start_line,end_line,url,text
```

Expected: `EVIDENCE_COLLECTED`, two excerpts (`SRC-1`, `SRC-2`), and clickable
GitHub URLs containing the original commit SHA and matching line ranges.
These paths demonstrate docs and code retrieval, not relevance to this sample issue.
For your own repository, select files related to your issue.

Use `--file PATH` for the first 200 lines (or the whole file when shorter), or
`--file PATH:START-END` for an exact inclusive range. Paths use forward slashes.
Limits: five selections, 200 lines and 32 KB per excerpt, UTF-8 text files up to
1 MB. Unsupported files and invalid ranges fail explicitly without creating a
new run. Partial excerpts are marked `is_excerpt`; omitted lines are not evidence.
The collector does not execute repository content or send it to an LLM.

Check rejection (expected exit code 1):

```powershell
py -3.12 -m agent.evidence $runPath --allowed-repo another/repo --file README.rst
$LASTEXITCODE
```

## Component 3: draft generation (ready to test)

Evidence lets a reviewer trace claims about current behavior back to issue reports,
code, and documentation. It is not proof of relevance or correctness. The model
must separate proposed changes from current facts and flag missing information.

`agent/generate.py` uses one OpenAI Responses API call with
[Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs).
It renders Markdown and JSON from the same draft. No Python packages are required.
Live mode sends the issue, comments, and selected excerpts to OpenAI; it supplies
no tools and executes no repository content. Set a model available to your project
that supports Responses and Structured Outputs (for example, `gpt-4o-mini`).

With `$evidencePath` from component 2, first test the artifact pipeline offline:

```powershell
py -3.12 -m unittest -v
$draftDir = py -3.12 -m agent.generate $evidencePath --allowed-repo python/cpython --demo
Get-Content (Join-Path $draftDir product_spec.md) -Raw -Encoding UTF8
Get-Content (Join-Path $draftDir handoff.json) -Raw -Encoding UTF8
```

The demo is explicitly a placeholder, with no generated requirements and an open
question requesting live generation. It tests file creation, not model quality.

For a real draft, enter the key locally (not in chat) and select your model:

```powershell
$credential = Get-Credential -UserName 'OpenAI' -Message 'Enter your API key as the password'
$env:OPENAI_API_KEY = $credential.GetNetworkCredential().Password
$env:OPENAI_MODEL = 'gpt-4o-mini'
$draftDir = py -3.12 -m agent.generate $evidencePath --allowed-repo python/cpython
if ($LASTEXITCODE -ne 0) { throw 'Generation failed; see the error above.' }
Get-Content (Join-Path $draftDir product_spec.md) -Raw -Encoding UTF8
Get-Content (Join-Path $draftDir handoff.json) -Raw -Encoding UTF8
```

Use your own repository name if different. If your PowerShell session has lost
`$evidencePath`, set it to the path printed by `agent/evidence.py`, or rerun component 2.
To choose a different model, set `OPENAI_MODEL` or pass `--model MODEL`.

Each output directory contains:

- `product_spec.md`: scope, current/proposed behavior, requirements, tests, tasks,
  risks, assumptions, open questions, and source links.
- `handoff.json`: draft contract with repository, commit, requirement/test mappings,
  and SHA-256 of the exact Markdown bytes. `spec_uri` is relative to this directory.
- `context.json`: the supplied issue/evidence snapshot for review.
- `draft.json`: structured model output used to render both artifacts.
- `trace.json`: model, response ID, token usage, latency, mode, and zero corrections.

Expected live outcome: `DRAFT` artifacts. Check whether the current-behavior claims
actually follow from the cited sources and whether acceptance tests are measurable.
The sample README/pathlib excerpts may be unrelated to the issue; a good draft
should flag that gap. Select relevant files before judging spec quality.

Local checks reject malformed drafts, unknown source IDs, broken requirement/test
links, and invalid task dependencies. They do not verify factual support, detect
all secret content, or perform correction passes yet. Local approval is available
in component 4; dispatch is not implemented. Requests are capped at 6,000 output tokens and 220 KB of input; these
are not dollar-budget limits. Live cost is recorded as `null` (unknown), not zero.
There are no automatic retries. Missing keys, refusals, incomplete responses, and
validation failures produce an error without publishing a draft. Failed API calls
may still incur charges; failed-run tracing is deferred.

## Component 4: local review and handoff (ready to test)

This component can be tested without live generation. `agent/review.py` supports
inspection, explicit approval, consumer contract checks, and ACK/REJECT records.
All operations are local and use the standard library.

Run this complete independent test in PowerShell:

```powershell
py -3.12 -m unittest -v
$reviewDir = py -3.12 -m agent.review fixture
Get-Content "$reviewDir/product_spec.md" -Raw -Encoding UTF8
$report = py -3.12 -m agent.review inspect $reviewDir | ConvertFrom-Json
$report.blockers

# After reviewing the synthetic example, approve its exact bundle:
py -3.12 -m agent.review approve $reviewDir --reviewer "$env:USERNAME" --expected-hash $report.bundle_hash --allow-fixture

# Simulate a coding agent checking the contract, then recording acceptance:
$baseCommit = $report.handoff.base_commit
py -3.12 -m agent.review consume $reviewDir --repo example/review-fixture --commit $baseCommit --allow-fixture
py -3.12 -m agent.review feedback $reviewDir --repo example/review-fixture --commit $baseCommit --actor test-agent --decision ACK --reason 'Contract checked; no implementation performed' --allow-fixture
Get-ChildItem $reviewDir
```

Expected: an `approval.json` containing an `APPROVED` handoff and a unique
`feedback-*.json` with `ACK`. The original `handoff.json` remains `DRAFT`; consumers
use the approved contract returned by `consume`, not the original draft file.
The synthetic fixture requires `--allow-fixture` for approval and consumption.
It proves the workflow, not real spec quality. The earlier `generate.py --demo`
placeholder has unresolved questions and cannot be approved.

Check that a change invalidates approval (only changes the synthetic example):

```powershell
Add-Content "$reviewDir/product_spec.md" 'Changed after approval.'
py -3.12 -m agent.review consume $reviewDir --repo example/review-fixture --commit $baseCommit --allow-fixture
$LASTEXITCODE # expected: 1
py -3.12 -m agent.review feedback $reviewDir --repo example/review-fixture --commit $baseCommit --actor test-agent --decision REJECT --reason 'Spec changed after approval'
```

For real output, replace `$reviewDir` with `$draftDir`, supply the real repository
and expected commit, and omit `--allow-fixture`. Read the spec and context before
approving. Approval blocks unresolved questions, missing requirements/tests/tasks,
inconsistent handoffs, and stale review hashes. It does not judge factual support.
The bundle hash binds all four reviewed files; ACK rechecks approval and hashes.
The recorded approval is never overwritten. After edits, generate a fresh artifact
directory for a new review. Feedback records retain actor, reason, observed file
hashes, and approval hash when present. `REJECT` is allowed without approval.

This is a single-user local prototype: reviewer names are self-reported, records
are unsigned, and the filesystem is trusted. Hashes detect accidental changes;
they are not protection from someone who can rewrite all local files. Do not edit
artifacts concurrently with review commands. The consumer checks the repo/commit
provided on the command line; it does not inspect a checkout or run implementation
tests. ACK means the contract was accepted, not that implementation succeeded.

## What the MVP should demonstrate

The current agreed scope is one low-risk issue in one repository. Per the updated
user direction, review uses local artifacts; the separate coding agent owns PR creation:

1. A request starts a tracked run and pins repository context to a commit.
2. The agent retrieves relevant issue/docs/code evidence with traceable citations.
3. It generates a spec and structured handoff, checks them, and makes at most two
   correction passes; unresolved critical problems block handoff.
4. Local `product_spec.md` and `handoff.json` artifacts are available for review.
5. A human explicitly approves the correct artifact version/hash.
6. A coding-agent consumer checks the approved contract and records ACK/REJECT.
7. A run summary/dashboard reports elapsed time, cost, corrections, and outcome.

The spec should explain the problem, scope/non-goals, current/proposed behavior,
requirements, measurable acceptance tests, implementation sequence, API impact,
risks, rollout/rollback, and open questions. It should be useful to an engineer
without requiring them to reconstruct the original issue investigation.

Current prototype coverage:

| Capability | Current state |
| --- | --- |
| Issue intake and commit pinning | Implemented; live GitHub tested |
| Repository evidence | Implemented; files selected manually |
| Draft generation | Implemented; live model quality still needs testing |
| Structural checks | Implemented |
| Factual verification and corrections | Pending |
| Local approval and feedback | Implemented; synthetic end-to-end tested |
| Coding-agent handoff | Approved JSON export and optional task preparation; no launch |
| Implementation and PR creation | Owned by the separate coding agent; out of scope |
| Run reporting | JSON traces exist; summary/dashboard and cost calculation pending |

A successful demo is not evidence of general quality. The PRD proposes a later
15–20 issue pilot, with independent human review and comparison against an
issue-only prompt baseline. Targets include 90% requirement/acceptance coverage,
95% supported factual claims, and 25% less human planning time. These are targets,
not measured results. Autonomous implementation, merging, self-training, and
multi-agent coordination are outside this MVP.

## Next checkpoints (not implemented)

1. Verify factual support and completeness; add bounded corrections.
2. Add local run summaries (independent of live model testing).
3. Improve portable approved-artifact handoff; no coding-agent launch or PR creation.
4. Add the API/webhook if needed for the demo.

The implemented components collect context, generate drafts, and record local
approval/feedback. They do not publish to GitHub or dispatch a coding agent.
