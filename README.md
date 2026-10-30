# EVO — Self-Evolving OSS Project Manager Agent

UCSB CMPSC 189A, Fall 2026 · Sponsored by Nutanix

**Team:** Clement Boiteux (Team Lead), Mihir Srivastava (Scribe), Shanqin Chen, Hanson Hu, Timothy Nguyen
**Nutanix mentors:** Srinath GS, Kostadis Roussos
**Documents:** [Drive Folder](https://drive.google.com/drive/folders/1gU4mTCUmGwYahgBPCl9y5lf1rDZUrHXl) · [Project Vision](https://docs.google.com/document/d/1U8PigryM9BC_RDWqhW7rS_osOF0PKRiAjhZTcsWvtJ4/edit) · [Team Journal](https://docs.google.com/document/d/1mD9hfMsIwTCFuCEDhGSnKR5YvTnjHzCAGTxGxDNCquI/edit)

## Demo

https://github.com/user-attachments/assets/7e693055-7d61-4603-9232-5e06c9e0200f



*A real 47-minute run on pytest issue #14442, edited to 2½ minutes. Click to watch.*

## What it is

An AI agent that helps open-source maintainers. It improves by updating its memory, tools and workflows, not by retraining the model.

EVO is a local app where you chat with a main agent about a repository. For coding tasks, it hands the work to a team: **Spec**, **Code**, **Test** and **QA**. Test and QA check Code's work independently before it's marked ready.

## Quick start

You'll need Python 3.12+, [uv](https://docs.astral.sh/uv/), Node.js 20.19+ and Git.

```powershell
uv sync --frozen
npm --prefix frontend ci
npm --prefix frontend run build
uv run evo            # opens http://127.0.0.1:8765  (Windows: ./start.ps1 does all of this)
```

Set `OPENAI_API_KEY` in your environment or in a `.env` file (see `.env.example`), or paste a key in **Settings**. Other OpenAI-compatible providers work through **Settings → Chat Completions**.

## Using it

1. Connect a local repository.
2. Ask a question or describe a task.
3. Watch the team work from the sidebar, or open **Work** for the full timeline. You can keep talking while it works: questions get a quick answer from the recorded state, and instructions go to the main agent, which confirms when it has applied them ([details](docs/main-agent.md)).
4. Use **Memory**, **Skills** and **Evolution** to see and shape what the project has learned. Evolution tests new Code instructions with [GEPA](https://github.com/gepa-ai/gepa) on real past pytest issues, scored by hidden tests, before you activate them ([details](docs/evolution.md)).

EVO creates a separate working copy for each assignment. Its workflow keeps changes local and only publishes when you request it.

> **Note:** agents run Python and shell commands with your account's permissions, with no sandbox. The workflow rules above are conventions, not enforced limits, so only connect repositories you trust and keep EVO on localhost.

## Development

```powershell
uv run evo --no-browser            # API server
npm --prefix frontend run dev      # frontend with hot reload at http://127.0.0.1:5173
uv run pytest                      # tests (scripted model, no API calls)
```

Python backend in `src/evo`, React frontend in `frontend/src`, tests in `tests/`. Local data is stored in `.evo/`; set `EVO_DATA_DIR` to change the location. The earlier prototype is on the [`mihir-coding` branch](https://github.com/cboiteux2765/q-nutanix-oss-agent/tree/mihir-coding/coding-agent).

## Product Spec Agent (prototype)

Turns one GitHub issue into a cited product specification and a JSON handoff for the
coding agent. Code lives in `agent/`; it does not launch a coding agent or create PRs.
See [PROTOTYPE.md](docs/PROTOTYPE.md), [the local demo guide](docs/LOCAL_DEMO.md),
[the handoff guide](docs/HANDOFF.md) and [product discovery](docs/PRODUCT_DESIGN.md).

```powershell
.venv/Scripts/python.exe -m unittest discover -v
.venv/Scripts/python.exe -m agent.mini_spec examples/evidence.json --allowed-repo example/review-fixture --replay examples/replay.json --verbose
```

**Review policy.** Product-mode runs auto-clear a live spec for handoff only when
structural checks pass, product context and quoted evidence are present, assessed risk
is low, and there are no assumptions or unresolved decisions; otherwise they report
NEEDS_REVIEW with reasons. Replay runs never auto-clear. AUTO_CLEARED is not human
APPROVED and does not establish factual correctness. Hash checks invalidate changed artifacts.
