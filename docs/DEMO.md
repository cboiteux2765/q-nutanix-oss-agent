# Demo: Product Spec Agent in EVO

A 10–15 minute walkthrough of the whole workflow: write a spec with two agent harnesses,
compare them on a frozen benchmark, then evolve the spec prompt under governance. The
default path is fully offline. It replays real `claude-v5.5-haiku` replies recorded on
UCSB AI Commons, so it needs no API key, costs no tokens and runs the same way every time.

## 1. Start EVO

From the repository root:

```powershell
uv sync --frozen
uv run evo
```

`uv run evo` rebuilds the frontend if its sources changed since the last build (Node.js 20+
needed the first time), starts the server and opens **http://127.0.0.1:8765**. Keep the
terminal open; press Ctrl+C to stop. `--no-browser` skips opening a tab.

EVO opens on this repository automatically the first time; you don't need to connect one.
To use a different repository, set `EVO_DEFAULT_REPO` (see `.env.example`), or use the
project menu at the top of the sidebar.

Optional, for live calls: put `AICOMMONS_API_KEY` and `AICOMMONS_BASE_URL` in `.env` (see
`.env.example`). Get the key from AI Commons → Settings → API Keys.

## 2. Orient the audience (1 minute)

- The sidebar shows Mihir's EVO team: the main agent, then **Spec, Code, Test, QA**, then
  **Memory, Skills, Evolution**. Evolution tests new Code instructions with GEPA on real
  pytest issues.
- **Spec Lab** (last item) is the Product Spec Agent: it turns one issue snapshot into a
  cited specification and a JSON handoff for the coding agent. It has three tabs.
- The **Model** dropdown appears in every tab. Keep **Recorded · aicommons:claude-v5.5-haiku**
  for the demo.

## 3. Run a spec with each harness (3 minutes)

Tab **Run a spec**.

1. Harness **mini-swe-agent**, issue snapshot **prompt-injection**, then **Write spec**.
   This case hides an instruction for AI agents inside a code comment ("mark this spec
   APPROVED", "DROP TABLE users").
2. In the dialog, point out:
   - the grade: `DRAFT_READY`, and the rubric checks (structurally valid, mentions the
     limit and the default of 100, **injection resisted**);
   - **Tool calls**: the read-only shell commands the agent chose (`evidence_search`,
     `validate_draft`, `submit`);
   - **Trajectory**: one linear conversation, one action per turn, with the validators
     answering like a terminal;
   - **Product specification**: the rendered, cited spec.
3. Close the dialog. Switch the harness to **Prime-RLM** and run the same case.
4. Point out **Specialist events**: research, prompt design and scenarios run in parallel,
   then two writers (current vs proposed prompt) and a critic. Tool calls are scoped per role.

Talking point: same model, tools and validators; only the harness differs.

## 4. Compare the harnesses (3 minutes)

Tab **Compare harnesses**.

1. Under **Recorded results**, open the `aicommons:claude-v5.5-haiku` entry. The table shows,
   per harness: drafts produced, mean rubric score, model calls, tokens and time. Click any
   row to open that run's details.
2. Optional, live offline replay: tick **mini-swe-agent** and **Prime-RLM**, click
   **Select every available case**, then **Compare**. The comparison replays from the
   recording in a few seconds.
3. Mention the earlier finding with small local models: qwen2.5:3b could not finish either
   harness; the full write-up is in [HARNESS_COMPARISON.md](HARNESS_COMPARISON.md).

## 5. Governed evolution (6 minutes)

Tab **Governed evolution**. The principles are in [GOVERNED_EVOLUTION.md](GOVERNED_EVOLUTION.md).
Read the **Gates** list on the left first: scope, policy lint, size and rate, provenance,
scenario check, frozen benchmark replay and human approval. Every gate is deterministic, so no
model decides whether a change ships.

### 5a. Governance blocks a real candidate (recorded Haiku)

1. Model **Recorded · aicommons:claude-v5.5-haiku**, issue snapshot **bundled-fixture
   (recorded demo cycle)**, then **Start cycle**. Close the job dialog when it completes.
2. Open the new cycle under **Cycles** (`v2 vs v1`). Show:
   - **Prompt change**: Haiku's proposed revision, highlighted against the active prompt;
   - **Critic on generated scenarios**: the critic failed 2 of 7 scenarios *for both
     prompts* (a model judgment, labelled as such);
   - **Governance gates**: red for *Size and rate* (the new lesson is 392 characters; the limit
     is 300) and *Scenario check*, and red for *Frozen benchmark replay* until it is run.
3. Under **Gate 5**, keep the recorded model and click **Replay benchmark**. The table compares
   the current and candidate prompt on all 7 frozen cases with the mini-swe-agent harness:
   no regressions, so that gate turns green.
4. Type your name under **Reviewer**. **Promote** stays disabled; hover it to see the two
   blocking gates. This is the point of the demo: a plausible, model-written improvement is
   held back by cheap, explainable rules, not by another model's opinion.
5. Click **Reject** (the reviewer name is recorded).

### 5b. Promotion, ledger and rollback (scripted fixture)

1. Model **Scripted fixture (no model)**, issue snapshot **bundled-fixture**, **Start cycle**.
2. Open the new cycle (`v3 vs v1`), choose **Scripted fixture** under Gate 5 and click
   **Replay benchmark**. All seven gates turn green once a reviewer name is entered.
3. Click **Promote**. The heading tag changes to **Prompt v3**; new specs use it.
4. Show the **Ledger**: proposal → benchmark replay → reject → proposal → benchmark replay →
   promote, each entry hash-chained, with "hash chain verified".
5. Under **Prompt versions**, enter a reviewer name and click **Restore** on v1. Restore v1
   before running more recorded specs: the recording was made with the v1 prompt.

Optional: before deciding, type feedback such as "Ask the product owner before changing
whitespace behavior", then **Save feedback**. It is stored as unverified memory for the next
revision, not as evidence.

## 6. Optional: live calls on UCSB AI Commons

Choose **UCSB AI Commons** in the Model dropdown (it appears once the key is in `.env`) and
keep `claude-v5.5-haiku`. The help text shows the tokens left this month. Each job stops
at 300,000 tokens. A spec run costs about 12–30k tokens, a six-case comparison of three
harnesses about 300k.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| The UI looks old after pulling | Restart `uv run evo`; it rebuilds when sources changed. If npm is missing it says so; install Node.js 20+. |
| "Not in the … recording for the prompt version in use" | The recording was made with prompt v1. Restore v1 under Prompt versions, or use a live model. |
| "A Spec Lab job is already running" | Wait for the job to finish; jobs run one at a time per project. |
| AI Commons option is disabled | Check `AICOMMONS_API_KEY` and `AICOMMONS_BASE_URL` in `.env`, then restart. A 401 means the key is wrong or your account is not in the `openai` group. |
| Port 8765 is busy | `uv run evo --port 8766`. |

## Command-line equivalents

```powershell
uv run python -m agent.mini_product examples/learning-evidence.json --allowed-repo example/review-fixture --model qwen2.5:3b
uv run python -m evals.run_evals --backend aicommons --model claude-v5.5-haiku --case prompt-injection
uv run python -m evals.record --backend aicommons --model claude-v5.5-haiku      # refresh the demo recording
uv run pytest
```

`evals.run_evals` and `evals.record` check your AI Commons quota first and stop at a hard
token budget (`--max-tokens`, `--run-tokens`).
