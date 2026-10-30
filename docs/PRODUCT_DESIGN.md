# Product discovery checkpoint — Q* PRD v1 / SRS / user stories

The supplied Q* PRD describes the complete EVO system: Spec -> Coding -> Test ->
QA -> human merge. This repository implements the Spec phase only, respecting the
user's boundary: no coding-agent launch or PR creation.

This checkpoint adds a product-analysis pass before the existing bounded draft
loop. Use `--product` to enable it; legacy commands and their fixtures still work.

## Demonstrate the new behavior without a model download

```powershell
$draftDir = .venv/Scripts/python.exe -m agent.mini_spec examples/evidence.json --allowed-repo example/review-fixture --product --replay examples/product-replay.json --log runs/product-events.jsonl
if ($LASTEXITCODE -ne 0) { throw 'Product run failed' }
Get-Content "$draftDir/product_brief.json" -Raw -Encoding UTF8
Get-Content "$draftDir/product_spec.md" -Raw -Encoding UTF8
Get-Content "$draftDir/trace.json" -Raw
Get-ChildItem specifications -Filter '*.md'
```

Expected: product analysis -> draft -> failed test-reference check -> one correction
-> artifacts -> BLOCKED. The synthetic brief asks the product reviewer to clarify
caller compatibility. That decision survives drafting and blocks approval even
when the draft is structurally valid. A completed artifact write is not spec approval.
Replay responses are recorded fixtures, not a measurement of model quality.

Every successful run also writes an exact Markdown copy under `specifications/` in
a folder named from the issue number and title. Repeated generations become
`product_spec-v1.md`, `product_spec-v2.md`, and so on. Open one to show the generated
spec without navigating JSON and trace files. The matching `runs/<run-id>/` folder
remains the source of truth for review, hashes, and handoff. Use
`--specifications PATH` to choose another viewing folder.

## Real local generation and additional sources

With Ollama running and a local model installed, omit `--replay`:

```powershell
.venv/Scripts/python.exe -m agent.mini_spec examples/evidence.json --allowed-repo example/review-fixture --product --model qwen2.5:7b --log runs/product-events.jsonl
```

For a real issue use its evidence JSON and repository name. Add `--source path/to/product-goals.md`
or repeat it for up to three UTF-8 product documents (8 KB each). These should be
documents about the product being changed, not automatically the PRD describing EVO.
Documents receive DOC-N IDs, SHA-256 hashes, local provenance, and text snapshots
in context.json. Repository evidence still comes from the pinned commit. PDFs must
first be converted into focused UTF-8 excerpts; this flag does not parse PDFs.
The existing 24 KB per-call context guard still applies; adding sources can exceed it.

## Design mapping

| Q* Spec-phase need | Implementation in this checkpoint |
| --- | --- |
| Users and outcomes | Cited personas, needs, outcomes, success measures, user stories |
| Source-backed current state | Exact source-quote checks and explicit evidence limitations |
| Product judgment | Alternatives, tradeoffs, recommendation and concise rationale |
| Architecture and feasibility | Constraint, dependency and nonfunctional-requirement sections |
| Missing business choices | Owned open decisions with next actions; deterministic approval blocker |
| Traceable handoff | Product brief in Markdown, context, JSON handoff and standalone artifact |
| Progress | Product-analysis/source/readiness events, counts, risk, next owner and status |

The brief is model-authored analysis, not hidden chain-of-thought or verified fact.
Exact quotes prove source presence, not whether a claim follows from the quote.
The live model's factual accuracy and product quality still need human evaluation.
Risk tiers are model assessments, not an agreed project risk policy. All approval
remains human-controlled. Metadata-only logging is preserved (no request payloads).

Budget: one product-analysis call, then one draft and at most two corrections.
Product-analysis validation failure stops the run and records a failure trace;
there is no unbounded analysis retry. Missing decisions yield BLOCKED artifact
traces, with a DRAFT handoff and product reviewer as next owner. Existing artifacts
remain compatible; the full shared EVO cross-phase schema is not implemented yet.

Next checkpoints after user testing: autonomous relevant-source selection;
independent factual/completeness verification; full shared handoff schema alignment.
Coding, execution tests, QA, learning-policy updates and PR creation stay outside
this product-agent checkpoint.

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
