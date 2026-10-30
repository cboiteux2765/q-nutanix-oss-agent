# EVO Coding Agent

The coding phase of EVO (Spec → **Code** → Test → QA). It takes an approved spec handoff, makes the change in an
isolated checkout, tests it, and opens a **draft** PR for the Test Agent. It never merges.

## Run the prototype

```powershell
cd coding-agent
uv sync --extra mini-swe              # Python 3.11+, Git on PATH (Git Bash on Windows)
uv run evo-code demo --out demo-out   # 6 pilot issues, offline, no API key needed
uv run pytest                         # full test suite
```

The demo uses a scripted implementer and a local git repo standing in for GitHub. Each `PASS` line means the agent
reached the expected outcome:

```
PASS 01-median-even: r0:TESTING                  # draft PR opened
PASS 02-percentile: r0:TESTING -> r1:TESTING     # PR, then a fix commit after Test Agent feedback
PASS 03-missing-prereq: r0:BLOCKED               # environment owner must act
PASS 04-ambiguous-mode: r0:BLOCKED               # spec reviewer must decide
PASS 05-out-of-scope: r0:BLOCKED                 # edited CI/README, so it was blocked
PASS 06-deletes-test: r0:BLOCKED                 # deleted a test, so it was blocked
```

## Run it on a real handoff

```powershell
$env:OPENAI_API_KEY = "..."; $env:EVO_GITHUB_TOKEN = "..."
uv run evo-code run handoff.json -c evo.toml          # exit 0 = PR opened, 3 = blocked
uv run evo-code revise RUN_ID verification.json -c evo.toml   # after Test/QA says CHANGES_REQUIRED
uv run evo-code spend -c evo.toml                     # total $ and tokens across runs
```

`evo.toml` (full example in `examples/evo-coding.example.toml`):

```toml
[implementer]
kind = "mini_swe"          # mini_swe | native | rlm | pi | scripted
[llm]
base_url = "https://api.openai.com/v1"   # any OpenAI-compatible endpoint
model = "gpt-6-luna"       # default; Responses API, reasoning_effort = "high"
[policy]
lifetime_max_usd = 5.0     # total cap across all runs in work_dir
[publisher]
kind = "github"
```

From Python (how the orchestrator calls it): `run_coding_task(handoff_path, config)` and
`revise_coding_task(run_id, request, config)`.

## Output

Each attempt writes `.evo-coding/runs/<run_id>/r<N>/artifacts/`:
- `coding_result.json`: status, PR, head commit, changed files, criteria coverage, tests and blockers, plus
  `test_agent_request` for the Test Agent.
- `work_report.md`: the same information for humans.
- Also the trace, diff, JUnit and logs.

## Notes

- **Code map:** start at `src/evo_coding/agent.py`, which is the whole pipeline. Contracts are in `models.py`.
  Pilot scenarios are one TOML file each in `pilot/scenarios/`.
- **Handoff schema:** provisional (v0.1) until the team settles it. `evo-code schema` exports it.
- **litellm** (used by mini-SWE) is pinned to 1.104.0, from before the Oct 2026 PyPI compromise. Don't bump it
  without checking. `native` and `rlm` don't need it.
- **pi** must be installed separately. It runs on the local backend only.
- **Docker backend:** build the image first with
  `docker build -t evo-coding-sandbox:py312 -f docker/sandbox.Dockerfile docker`.
- **Cost:** priced from the built-in table in `llm.py` (gpt-6-luna: $0.10 in / $0.01 cached / $0.50 out per
  1M tokens, higher above 272K input), or from prices set in `[llm]`. Every attempt is appended to
  `work_dir/spend_ledger.jsonl`. A pilot fix costs about $0.001. Tests use fake models unless you set
  `EVO_E2E_OPENAI=1`.
- **Test Agent gap:** Shanqin's Test Agent can't fetch a remote PR head yet.
