# Product Spec Agent — MVP Prototype

UCSB CMPSC 189A, Fall 2026 · Sponsored by Nutanix

## Project Overview

Turn one GitHub issue into a cited product specification and an approved JSON handoff. The separate coding agent owns implementation, tests, and PR creation. This project does not launch that agent or create PRs.

See [PROTOTYPE.md](docs/PROTOTYPE.md) for implementation checkpoints and end-to-end test commands.

The current prototype collects selected repository evidence, drafts requirements and acceptance criteria, checks structure, retries failed drafts, and records approval against artifact hashes. Automatic evidence selection and factual verification remain future checkpoints.

## Interactive demo

The private demo Site shows a complete, repeatable Product Spec Agent run: issue
intake, repository research, product analysis, drafting, independent validation,
targeted correction, automatic handoff clearance, Markdown preview/download, and
the JSON coding-agent contract.

https://evo-product-spec-demo-20261029.civilspark13.chatgpt.site

The hosted experience uses an explicitly labeled fixture. It demonstrates the
workflow without GitHub credentials, Ollama, code changes, or PR creation.

## Prototype layout

```text
agent/       Python package: intake, evidence, generation, review, mini agent
docs/        Setup, MVP checkpoints, and local demo instructions
tests/       Unit and integration tests
examples/    Synthetic evidence and recorded model responses
specifications/ Generated Markdown specs for easy viewing
runs/        Generated artifacts and logs (ignored by Git)
```

Run commands from the repository root using `python -m agent.<module>`:

```powershell
.venv/Scripts/python.exe -m unittest -v
.venv/Scripts/python.exe -m agent.mini_spec examples/evidence.json --allowed-repo example/review-fixture --replay examples/replay.json --verbose
```

See [the local demo guide](docs/LOCAL_DEMO.md) for real Ollama inference.
See [the handoff guide](docs/HANDOFF.md) to retrieve approved JSON and prepare a mini-SWE-agent task.
See [product discovery](docs/PRODUCT_DESIGN.md) for the Q* PRD-aligned product brief, sources, and blocked-decision demo.

## Team

- Clement Boiteux — Team Lead
- Mihir Srivastava — Scribe
- Shanqin Chen
- Hanson Hu
- Timothy Nguyen



Nutanix Mentors: Srinath GS and Kostadis Roussos.

## Project Documents

- [Project Drive Folder](https://drive.google.com/drive/folders/1gU4mTCUmGwYahgBPCl9y5lf1rDZUrHXl)
- [Project Vision Draft](https://docs.google.com/document/d/1U8PigryM9BC_RDWqhW7rS_osOF0PKRiAjhZTcsWvtJ4/edit)
- [Team Journal](https://docs.google.com/document/d/1mD9hfMsIwTCFuCEDhGSnKR5YvTnjHzCAGTxGxDNCquI/edit)

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
