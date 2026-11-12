# EVO Product Spec Learning Lab

The existing EVO interface now includes specialist activity, prompt comparisons,
scenario verdicts, feedback memory, promotion/rejection, rollback, run history,
and downloadable specifications and run packages. The original issue demo remains
available at `issue-demo.html`.

## Run locally

From the repository root, start the recorded demo (standard library only):

```powershell
.venv/Scripts/python.exe -m agent.learning_server
```

Open http://127.0.0.1:8765. For live inference through an already configured Ollama:

```powershell
.venv/Scripts/python.exe -m agent.learning_server --live --model qwen2.5:7b
```

Live mode requires an uploaded `EVIDENCE_COLLECTED` JSON snapshot from the existing
intake/evidence pipeline and its allowed repository. See [LOCAL_DEMO.md](LOCAL_DEMO.md)
for evidence collection. Additional product documents can be embedded using the
existing `agent.product.add_documents` function. Repository content stays pinned
to the snapshot commit. The server only binds to loopback; it is not a multi-user
or authenticated production backend. Reviewer names are attribution, not identity verification.

Demo and live stores are separate: `runs/learning-demo` and `runs/learning-live`.
Do not share one `--data` directory across server processes or modes. State is
persisted with atomic replacement; interrupted runs are marked failed on restart.

## End-to-end workflow

1. Import idea/issue evidence and validate the repository and commit.
2. The orchestrator runs three independent specialists: product research,
   prompt design using recent feedback, and scenario generation.
3. Two spec agents use the same evidence and product brief: one with the active
   prompt, one with the proposed prompt. Fixed evidence rules apply to both.
4. An independent critic judges both drafts against the same scenarios. Structural
   validators check citations, quoted evidence, requirement/test links, and ordering.
5. Save the candidate prompt, both drafts, scenarios, verdicts, trace, product
   specification, and draft handoff. Failed responses receive one targeted repair;
   there are at most eighteen model calls and three concurrent specialists per run, including tool continuations and repairs.
6. A human adds feedback, rejects the candidate, or promotes it. Promotion requires
   all candidate scenarios to pass, no regressions, nonempty requirements, no
   assumptions/open decisions, an unchanged prompt hash, and the same baseline.
7. The next cycle uses the promoted prompt and the last twenty feedback records.
   Rollback restores a previously active prompt while retaining history.
8. Specification approval is separate from prompt promotion. Use the existing
   [review and handoff workflow](HANDOFF.md) on the generated artifact directory
   before handing a live specification to the coding agent. No coding agent or
   PR is launched by this workflow.

Feedback proposes changes; it is not automatically treated as verified product
evidence. To resolve a product decision, supply the owner's decision in the next
evidence snapshot. This prevents a prompt edit from silently erasing blockers.

## Prime Agent RLM baseline

Reference: [PrimeIntellect-ai/prime-agent RLM](https://github.com/PrimeIntellect-ai/prime-agent/tree/main/prime-agent-runtime/src/rlm).
Inspected `__init__.py` blob `f2994dec45d1aaf062b197a98178fb20b5252f91`.
The runtime exposes async `spawn(prompt, name=...)` and `collect(handle,
timeout_ms=...)` with settled child results. The EVO `PrimeBackend` uses these
actual calls; the local Ollama backend uses isolated structured model requests.
No upstream source was copied or vendored.

Inside an existing Prime Agent REPL with its host bridge configured:

```python
import json
from pathlib import Path
from agent.learning import Workspace, Orchestrator, PrimeBackend

evidence = json.loads(Path("path/to/evidence.json").read_text())
orchestrator = Orchestrator(Workspace("runs/learning-prime"), PrimeBackend())
run_id = orchestrator.create(evidence, "owner/repo")
await orchestrator.execute(run_id, evidence, "owner/repo")
```

Use a live evidence snapshot for this example. The adapter requires the Prime
host, not just `pip install rlm`. It was contract-tested with a fake host, not
run against a connected Prime installation. Collection has a 180-second deadline;
an unsettled child fails the run and may still require cancellation in Prime's UI.
Children are instructed not to spawn recursively or call native host tools; actual Prime tool
permissions must be constrained by the host. Both backends can return structured product-tool
requests through the same Python dispatcher. This is an application protocol, not a native Prime
MCP registration. Each continuation uses a new child with the same role and accumulated context.

## What learning and evaluation mean here

Learning is durable prompt revision from human feedback, with comparison and
explicit promotion. It does not train model weights or claim autonomous general
improvement. Evaluations are model judgments on a single evidence snapshot plus
deterministic structural checks. They do not execute product tests or verify
factual claims. Scenarios are generated fresh per run, so cross-run scores are
not a benchmark. Equal scores demonstrate no measured improvement.

The private hosted Site is a clearly labeled browser demo with recorded verdicts
and localStorage persistence. It never contacts a local model or backend. Feedback
edits there affect the next displayed prompt but are not scored by live inference.
Use Export workspace package to retain browser records; they are not shared across
devices. The Python server uses the same interface with real persisted API data.

## Validation and fixture generation

```powershell
.venv/Scripts/python.exe -m unittest discover -v
.venv/Scripts/python.exe -m scripts.build_learning_demo
node --check site/product-spec-demo/dist/learning.js
```

Tests cover child failure/repair limits, repository allowlisting, scenario result
completeness, regression gates, tampered prompt hashes, stale baselines, feedback
reuse, persistence, rollback, and the Prime spawn/collect contract.

## Product-requirement tools

The deliberately small `agent/spec_tools.py` dispatcher exposes four read-only tools:

| Tool | Specialists | Result |
| --- | --- | --- |
| `evidence_search` | Research, scenarios, spec writers, critic | Literal search over pinned excerpts with source IDs; eight results, 2,000 characters each, explicit truncation |
| `feedback_memory` | Prompt designer | Last twenty reviewer suggestions, explicitly unverified |
| `decision_register` | Spec writers, critic | Open product decisions from the research brief |
| `requirement_trace` | Critic | Both drafts' requirement ? source ? acceptance-test maps |

Each role gets one initial tool result. It may return this envelope instead of its final answer:

```json
{"tool_requests": [{"name": "evidence_search", "query": "empty"}]}
```

The orchestrator validates the request and role, records the results, and asks for the final
structured output. There is one optional batch of up to three reads per role, plus one repair.
Requests cannot invoke arbitrary Python, shell commands, approval, or file writes. The dispatcher
is not a sandbox for the Prime host itself. The Specialist tools tab and exported run package
show the trace. The recorded demo executes the initial Python tools when fixtures are built;
browser playback does not execute Python or spawn agents.

## Present the core prototype in three minutes

1. Start a cycle from the bundled issue snapshot; watch the six focused roles.
2. Open Specialist tools to inspect source lookup and requirement traceability.
3. Compare prompt versions and the four scenario categories; inspect the draft spec.
4. Add feedback such as ?Ask the product owner before changing whitespace behavior.?
5. Promote an eligible candidate, then run another cycle to see the saved suggestion
   in the proposed prompt. Reject or restore a previous baseline to demonstrate control.

This is a standard-library learning path (`learning.py`, `spec_tools.py`, and
`learning_server.py`) with buildless HTML/CSS/JS. No training pipeline, vector database,
agent framework dependency, code-generation agent, or deployment agent is needed.
Existing issue intake and artifact validators are reused rather than duplicated.
Equal fixture scores are intentional: the demo shows the mechanism, not invented improvement.
