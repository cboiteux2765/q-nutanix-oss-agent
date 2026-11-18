# Approved JSON to mini-SWE-agent

Run from the repository root. This offline checkpoint exercises approval, export,
JSON retrieval, validation, and preparation of the separate coding agent's task.
It does not launch a coding agent or alter application code.

```powershell
$reviewDir = .venv/Scripts/python.exe -m agent.review fixture
$report = .venv/Scripts/python.exe -m agent.review inspect $reviewDir | ConvertFrom-Json
Get-Content "$reviewDir/product_spec.md" -Raw -Encoding UTF8
.venv/Scripts/python.exe -m agent.review approve $reviewDir --reviewer "$env:USERNAME" --expected-hash $report.bundle_hash --allow-fixture
$sha = $report.handoff.base_commit
$packet = .venv/Scripts/python.exe -m agent.handoff $reviewDir --repo example/review-fixture --commit $sha --allow-fixture
if ($LASTEXITCODE -ne 0) { throw 'Export failed' }
$taskFile = .venv/Scripts/python.exe -m agent.handoff $reviewDir --repo example/review-fixture --commit $sha --allow-fixture --read $packet --mini-task
if ($LASTEXITCODE -ne 0) { throw 'Task preparation failed' }
Get-Content $taskFile -Raw -Encoding UTF8
```

The `coding-input-*.json` includes the approved handoff, complete Markdown spec,
source context, and approval provenance. The adapter verifies approval, repository,
commit, source context, and spec hash before emitting `mini-task-*.txt`.
No ACK is automatically asserted: preparing a task is not agent acceptance.
For real artifacts, use your draft directory, real repo/commit, and omit
`--allow-fixture`. Never use the synthetic fixture to implement real repository changes.

The independent coding workflow can call `agent.handoff.load_coding_input(...)`
and pass its returned spec/context to mini's `agent.run(task)`. Or, after configuring
mini's coding environment and local model in a real terminal, pass the prepared
text through mini's `--task` argument and save its `--output` trajectory. Mini's
stock coding configuration uses bash; configure a suitable isolated checkout and
execution environment separately. This checkpoint does not configure that runtime
or claim actual code implementation has been tested.

The adapter expects access to the original approved artifact directory; it is not
a signed remote handoff protocol. Local approval identities remain self-reported.
The Markdown keeps its original DRAFT label to preserve approved bytes; the
approval record and exported contract provide the authoritative APPROVED status.

## Action logs

```powershell
Get-Content runs/actions.jsonl -Tail 20 -Wait
```

Intake, GitHub HTTP statuses/endpoints, evidence, review commands, and handoff events
write to this shared file. Each command has a session ID; artifact/run IDs and
paths link stages. To put the mini specification loop in the same file:

```powershell
.venv/Scripts/python.exe -m agent.mini_spec examples/evidence.json --allowed-repo example/review-fixture --replay examples/replay.json --log runs/actions.jsonl --verbose
```

Operational logs contain stage metadata, HTTP status, endpoint, timing, and counts.
Request/response bodies and authorization headers are omitted. Full content stays
in spec/context/transcript artifacts; previously written logs are not modified.
They record observable application events, not hidden reasoning, external agent
actions, or Ollama internals. The legacy OpenAI generator still uses its artifact
trace rather than this shared event log. Direct Python calls to review functions
do not emit CLI events. Failed calls are logged with their errors.

Inspect the task, then record actual downstream feedback using `agent.review feedback`
with `--decision ACK` or `REJECT`, an actor, and a specific reason. Code execution
and its trajectory belong to the independent coding workflow.

The Product Spec Agent ends at approved artifact export. The independent coding
agent owns implementation, test execution, and PR creation. Preparing a task file
is optional; no agent launch or PR publication is planned in this product.

## Optional human review (current policy)

Product-mode runs automatically clear a live-generated spec for handoff when
structural checks pass, product context and quoted evidence are present, assessed
risk is low, and there are no assumptions or unresolved decisions. Other runs
report NEEDS_REVIEW with reasons. Demo/replay runs never auto-clear.

AUTO_CLEARED is explicitly different from human APPROVED. This policy uses the
model's risk assessment and deterministic checks; it does not establish factual
correctness or guarantee low risk. The independent coding workflow can require
human approval if its policy does not accept automatic clearance. No coding
agent is launched. Hash checks still invalidate changed artifacts.

This supersedes earlier statements that every handoff requires human approval.
The product command applies the policy automatically; manual approval remains
available. Terminal progress uses plain English. --verbose shows structured
metadata instead, still without request/response payloads.
