# Evolution: testing new instructions before agents use them

EVO improves by changing its instructions, skills and memory, not the model. Evolution makes that evidence-based. A candidate set of instructions for a role is scored by running the real EVO harness on real historical issues, checked by executable tests the agent never sees.

## What is optimized

One component per experiment: the **project-specific instructions for the Code role**. They're appended to the built-in role instructions exactly as an approved version is in a normal run. The built-in prompt, the tool manifest, the evaluator and the checks are not optimizable.

## The optimizer: GEPA

Experiments use the official [`gepa`](https://github.com/gepa-ai/gepa) package (v0.1.4; Agrawal et al., 2025, *GEPA: Reflective Prompt Evolution Can Outperform Reinforcement Learning*) through an adapter (`src/evo/evolve/adapter.py`):

- `evaluate` runs one EVO rollout per task and returns its hidden-test score.
- `make_reflective_dataset` gives GEPA's reflection model the task, the agent's diff, commands and final message, and feedback from the hidden tests. This covers **reflection (train) tasks only**.
- GEPA runs its own loop:
  - Pareto candidate selection;
  - minibatch reflection;
  - acceptance only on improvement;
  - the baseline is kept as candidate 0 throughout.
- Limits stop it: GEPA proposals, metric calls (rollouts), the search token pool (see **Budget**), a spending cap (when prices are set in Settings), wall time, and cancellation.

The reflection prompt includes your objective and recorded events from the source runs you select. It asks for general guidance and forbids mentioning specific issues, hidden tests or evaluator files.

## Tasks: reproducible, no future leakage

`src/evo/evolve/pytest_tasks.json` lists 27 real pytest issues, each fixed by a small merged PR. They come from the capstone replay mirror (`history.db` + `repo.git`, mirrored from 2026-04-01).

### How tasks were selected

`src/evo/evolve/select_tasks.py` applies fixed criteria. They were written before any agent ran on the new tasks, and selection never looks at agent results. A candidate must meet all of these:

1. The PR was merged into `main` and opened inside the mirrored window.
2. It closes exactly one issue, which was opened inside the window (so its comments were mirrored) and before the PR.
3. The fix is small:
   - 1–3 Python files under `src/` and at most 80 changed source lines;
   - at least one changed test under `testing/`;
   - nothing changed outside `src/`, `testing/`, `changelog/`, `doc/` and `AUTHORS`.
4. **Reference verification**, run twice. When the bundle is built, the fix's tests must fail on the snapshot and pass with the fix. Then, independently, the scorer rollouts use must give the untouched snapshot a score below 1 (unresolved) and resolve the reference fix.
5. One task per issue (the earliest PR wins).

Rerunning `python -m evo.evolve.select_tasks --data-dir <dir>` rebuilds the catalog and `pytest_selection.json`. That file is the full report: every candidate, why it was excluded, verification counts, and pairs of tasks that touch the same source file.

**Result: 116 merged PRs closed an issue, and 27 passed.** Main exclusions:

| Reason | Count |
|---|---|
| Issue opened before the window, so its comments weren't mirrored | 42 |
| Backport to a release branch | 18 |
| Reference verification failed | 9 |
| Closes several issues | 6 |
| No source change | 5 |
| Fix too large | 4 |
| Packaging or CI files changed | 3 |
| Other | 2 |

The 9 verification failures:
- the fix's patch doesn't apply to the as-of snapshot (main had moved): 14377, 14412, 14675, 14683, 14828;
- the fix's tests already pass before the fix: 14606 (a typing-only change), 14700, 15172 (about Python 3.15's `frozendict`; the task interpreter is 3.12, so its tests are skipped), 15284.

No two selected PRs close the same issue.

### Splits

| Split | Tasks | Snapshots |
|---|---|---|
| Reflection | 9 | April–July |
| Validation | 8 | April–June |
| Held-out | 10 | 2026-07-31 to 2026-10-29 |

- **Held-out tasks are the newest.** Every reflection and validation fix was merged (latest 2026-07-23) before the earliest held-out snapshot (2026-07-31), so nothing the search sees postdates any held-out task.
- **Reflection and validation alternate in time order.** They aren't chronologically separated from each other; both feed the search.
- **Held-out has 10 tasks rather than the target 8.** The next earlier boundary would put a search fix after a held-out snapshot.
- **Earlier exposure is kept.** The only experiment so far used 14492 and 14483 in search and held out 15132. They stay that way.

### Bundles

For each task (`src/evo/evolve/tasks.py`):

- **As-of time** is one second before the fix PR was opened.
- **The snapshot** is the last main-branch commit before that time, exported with `git archive` into a fresh **single-commit** repository. It contains no history and no future objects. Alongside it go the issue text and the comments that existed at the as-of time (filtered by `created_at`), plus `AGENTS.md` and `run_checks.py`.
- **Hidden checks**, readable only by the evaluator, are the fix's test files plus:
  - FAIL_TO_PASS tests, which fail before the fix and pass after it;
  - PASS_TO_PASS tests, which pass both before and after.

Score = (FAIL_TO_PASS passing fraction) × (PASS_TO_PASS passing fraction). A task is *resolved* only when every check passes. No model judge is involved.

### Limitations

- **Issue text.** The mirror stores each issue's and comment's *final* text, so edits made after the as-of time can't be undone. Comments are filtered by creation time, so comments posted later are excluded. Labels are never shown.
- **Short descriptions.** Some issues are terse; 15052's is 380 characters.
- **Long discussions.** Others carry long pre-fix discussions; 14635 and 14808 run to over 8,000 characters. Those can include a maintainer's diagnosis, which is legitimate historical information.
- **Related tasks.** 24 pairs of selected tasks touch the same source file, 13 of them across the search/held-out boundary. Most share `config/__init__.py`, `main.py` or `terminal.py`. None duplicate each other, but a lesson learned on one can transfer to the other. The pairs are listed in the report.
- **Duplicates.** Issue 14635 (reflection) has `marked_as_duplicate` events. The mirror doesn't record their target, so this is reported, not resolved.
- **Platform.** Checks were computed on Windows with the task interpreter. Tests skipped there never become checks. 14864 is a Windows-specific bug.
- **Environment.** Bundles need `EVO_REPLAY_DATA` (default `D:/capstone/data/pytest`) and `EVO_REPLAY_PYTHON`, an interpreter with pytest's dependencies. The first experiment builds missing bundles, which takes about a minute per task.

## Rollouts: the real harness, isolated

Each rollout (`src/evo/evolve/rollout.py`) gets its own:
- data directory;
- SQLite store;
- runtime;
- clone of the task snapshot;
- frozen copy of the project's memory (at the revision captured when the experiment started) and approved skills.

The Code role works alone from the issue to a committed change.

Disabled in rollouts:
- `web.*`, `github.*`;
- `memory.write`, `skills.propose`;
- `chat.*`;
- `agents.spawn`, `agents.send`.

Nothing is written to your store. Evaluation runtimes never feed the reflection queue.

Each rollout records:
- the outcome: completed, timeout, budget, error or cancelled;
- the score and resolved flag;
- check counts and the diff;
- commands and the final message;
- tokens and latency;
- a leakage audit, which flags any tool call that references the mirror or evaluator files.

There is no sandbox, so the audit *detects* such access; it can't prevent it. Any flag blocks the gate.

**Frozen configuration.** An experiment records the full effective provider configuration when it is created (model, API style, base URL, reasoning effort, output limits). Every rollout and every reflection call uses that configuration, including after a resume, and environment overrides such as `EVO_MODEL` can't change it. A rollout whose run settings differ from the frozen ones fails instead of being scored.

**Reuse.** A finished rollout is reused only when all of these match:
- role, instructions and task;
- the task version (snapshot, issue text and hidden checks);
- the frozen provider configuration;
- the effective execution limits (tokens per rollout, steps, rollout timeout, command timeout);
- skills and memory revision;
- its phase type (held-out or search).

Only agent outcomes are reused: completed, out of budget, or timed out. Harness errors, provider failures, hidden-check timeouts, cancellations and budget skips always run again. Raising a timeout or token cap therefore re-runs the task.

## Budget

The token budget is enforced **before** each model call, not after the fact.

- **Every call is admitted against its worst case.** That covers agent steps, context compaction, the quick-reply responder and GEPA's reflection calls. The worst case is an upper bound on the call's input plus the configured output limit (`max_output_tokens`, which the provider enforces; reasoning tokens are billed as output and count against it). A call that might not fit isn't made, and a rollout that stops this way is recorded as *out of budget*.
- **The input bound.** Byte-level tokenizers never produce more tokens than the request has bytes. So the bound is the UTF-8 size of the messages and tool schemas, plus a fixed allowance per message. When the previous call in the same conversation reported its exact input count and the earlier messages are unchanged, the bound is that count plus the bytes added since, which is much tighter. Calls already in flight in the same run hold their reservation, so concurrent agents can't overshoot together.
- **Unknown usage is charged, not ignored.** Each in-flight reservation is saved. Some requests end without reporting usage even though the provider may have processed and billed them. Those keep their full reservation as an *unresolved* charge:
  - a dropped connection, or a timeout;
  - any server or gateway error (5xx, 408), since a gateway can fail after the model ran;
  - a stream that ends early, or a malformed reply;
  - a request cancelled mid-call;
  - any unexpected error once the request may have been submitted, such as a stream event that fails to parse or a failing streaming callback.

  Only rejections known to happen before the model runs release the reservation: 400, 401, 403, 404, 409, 413, 415, 422 and 429. A retry after an uncertain failure is admitted as a new call, so retries can't run under one reservation. A reply whose usage is missing or incomplete (an absent count isn't treated as zero) is charged what it reported plus the rest of its reservation.
- **Settlement is atomic.** Recording a call's usage and removing its reservation happen in one database transaction, so a crash can neither lose the usage nor count it twice. The same applies to unresolved charges, and to a rollout's settlement: the experiment's spending, the release of its reservation and the rollout's *settled* mark are committed together, with the settled check inside the transaction.
- **Restarts.** At startup, reservations saved by the previous process become unresolved charges. Each rollout is also recorded with its reservation before it starts. If EVO stops mid-rollout, the experiment is charged the usage the rollout recorded in its own store, including its unresolved and in-flight tokens. If that can't be read, it is charged the full reservation. Settlement happens once (recovery and resume both reconcile first, and repeating it changes nothing). An experiment stopped by shutdown is marked *interrupted*.
- **Two pools per experiment.** At creation, the experiment reserves enough for the final paired comparison: 2 arms × held-out tasks × tokens per rollout (skill comparisons also reserve validation). The search (GEPA rollouts and reflection) can only spend the rest. A reflection call is admitted only if it fits *together with* the child rollouts needed to test its proposal. An experiment whose budget can't cover the reserve plus one testable proposal is refused, with the numbers.
- **Rollouts always get the full per-rollout cap.** A rollout that would not fit is skipped rather than run with less, so results stay comparable. The pool is charged actual usage, so the number of search rollouts isn't fixed. "N rollouts" in a plan means N affordable at the full cap; more fit when rollouts finish below it.
- **Spending limit (dollars).** With prices set (Settings → Runtime settings), the spending limit is a hard dollar limit.
  - Each call is admitted against its worst-case cost: its input bound at the higher of the input and cache-write prices (no cache discount is assumed) plus the full output limit at the output price.
  - The limit is split the same way as tokens: a reserve of *reserve per held-out rollout* (default $0.50) × final rollouts, with the rest for the search.
  - Each rollout runs with a hard allowance: what's left in its pool, up to its token cap at the output price. Its runtime admits every call against that allowance.
  - A rollout stopped by the spending limit is recorded as *spending_limit*. That's not an agent outcome, so it's never scored or reused, and a held-out comparison containing one is incomplete rather than a pass.
  - Unresolved charges carry their worst-case cost too. Actual spending is usually far below the limit because most input is cached.
- **Price tier.** Some models charge more per token when a request's input is over a threshold: `gpt-6.1-terra` charges 2× input and 1.5× output above 272K input tokens. When the input budget (`context_tokens`) is at or below such a threshold, every request is checked before it is sent. The byte bound settles most requests. Above the threshold, the outgoing request is counted with the provider's counting endpoint, and a request that is over the threshold or can't be counted is refused. An input budget above the threshold is treated as choosing the long-context rate.

What it can't cover:
- providers that ignore the output limit;
- tokenizers that aren't byte-level, for which the byte bound isn't guaranteed.

"Draft only" and the text-response check are single user-triggered calls and aren't part of an experiment's budget.

## Held-out evaluation and the gate

After GEPA finishes, the baseline and the best validation candidate each run once on the held-out tasks. Held-out results are never shown to GEPA.

**Exposure is tracked across experiments.** Rollout results are shared, so a task's role is fixed once used:
- A task that was ever held out can't later be used for reflection or validation.
- A task the search has seen (reflection or validation) can't later be held out.

The Evolution page marks these tasks, and the server refuses such assignments.

A candidate **passes the gate** when held-out evaluation:
- has a scored result for every held-out task in **both** arms. A harness error, provider failure, check timeout, cancellation or skip in either arm makes the comparison invalid; it never counts as evidence for the candidate;
- shows no regression (a task the baseline resolved but the candidate doesn't);
- has a candidate score at least the baseline's;
- uses no more than 1.5× the baseline's tokens;
- has no leakage flags.

The gate also reports **paired outcomes** per held-out task: solved by both, only by the candidate, only by the baseline, or by neither. With 10 held-out tasks this is an exploratory benchmark. A pass means the candidate did no worse on these tasks, not a statistically established improvement.

## Activation and rollback

- **Manual by default.** A candidate that passed can be activated. One that didn't (or was never evaluated) can only be activated with an explicit override, and it's labelled *activated without passing evaluation* or *activated without evaluation*.
- **Running assignments keep their version.** Runs snapshot their prompt IDs at start; activation affects only new assignments.
- **Rollback** restores an earlier version or returns the role to the built-in instructions.

## Background evolution

Completed and failed assignments and failed verifications are added to a per-project **reflection queue**. Plain conversation turns and evaluation runs aren't added.

- **Manual mode** (the default): the queue only collects runs. You choose whether to start an experiment from them.
- **Automatic mode** (opt-in): when enough runs are queued, EVO starts one bounded experiment, with at most N per day. It runs one experiment at a time, separate from foreground work, and nothing runs at startup. After a restart, unfinished experiments are marked *interrupted* and resume only when you ask.
- **Automatic activation** (a second opt-in) activates a candidate only if it passes every gate *and* improves held-out results. Otherwise it waits for review.

## Skills and memory

- Skills that agents propose carry **evidence** (validated event, artifact, commit or URL citations, each labelled *observed* or *inferred*) and stay *proposed* until a person approves them. An experiment of kind `skill` compares the baseline against baseline-plus-skill on validation and held-out tasks. Nothing approves skills automatically, and generated executable tools are never promoted automatically.
- Memory citations (`memory.write`) are checked against real events and artifacts in the project.

## Using it

Open **Evolution** in the sidebar:
1. Select one to eight source runs (or add queued runs) and write an objective.
2. Choose how each task is used: Reflection, Validation, Held-out or Skip.
3. Adjust the limits if needed. The form shows the worst-case number of rollouts and the token cap.
4. **Start experiment.** It runs in the background, and the sidebar shows a small dot while it runs.
5. Open the experiment to see:
   - its stage and spend;
   - GEPA's candidates with ancestry and validation scores;
   - every rollout (diff, commands, outcome);
   - the held-out table and the gate.
6. Open a candidate to see its diff against the active version and its evidence, then activate it or roll it back.

**Draft only** makes one model call over the selected runs and creates an unevaluated draft. The older **text-response check** (a model-judged comparison of written answers) is still available on each version, labelled as not running code.

## What the first live experiment showed (2026-11-12)

The run used `gpt-6.1-terra` (Responses API) with one reflection task (14492), one validation task (14483) and one held-out task (15132). Limits: 4 rollouts, 1 GEPA proposal, 200k tokens per rollout, 1.2M in total.

**Result:** 3 rollouts plus 1 reflection call, about 647k tokens, about 5 minutes. Every score was 0, and nothing was activated.

- **14483, baseline:** stopped at the 200k-token rollout cap after 17 steps. It had edited only a test file.
- **14492, baseline:** completed and committed a plausible fix with its own passing regression test (18/18). However, it counted the variadic flags correctly but missed keyword-only arguments, so the hidden upstream test still failed (0/1 target, 16/16 kept). The executable checks caught what the agent's self-check and any model judge would likely have missed.
- **14492, GEPA's candidate** (recorded under a "validation" label because of a labelling bug, since fixed): hit the token cap and scored 0. GEPA requires strict improvement on the minibatch, so it rejected the candidate. With no candidate better than the baseline on validation, held-out evaluation was skipped and the gate reported "No candidate beat the baseline on validation."

Takeaways:
- 200k tokens per rollout was too tight for this model on pytest. The 400k default is the next thing to try, not a demonstrated sufficient budget.
- One-task splits can't separate candidates.
- A meaningful experiment needs several tasks per split and a budget of a few million tokens.
- This run shows that execution and scoring work end to end. It does not show that evolution improves task performance.

## Two runs on the 27-task set (2026-11-13)

Both runs used `gpt-6.1-terra`, the 9/8/10 splits above, the objective "reproduce the issue with a failing test first, make the smallest change that fixes it, and run the relevant tests before committing", and a $25 spending limit. Costs are at the configured prices, so check the provider's billing for the actual charge.

| | Pilot | Run 2 |
| --- | --- | --- |
| Per rollout | 400k tokens, 60 steps | 1M tokens, 100 steps |
| Proposals, minibatch | 3, 2 | 6, 3 |
| Rollouts, reflection calls | 18, 2 | 32, 5 |
| Tokens, cost, time | 5.6M, $1.21, 44 min | 17.4M, $3.21, 2 h 23 min |
| Baseline on validation | 5/8 (0.625) | 4/8 (0.50) |
| Rollouts stopped by the token cap | 9 of 18 | 4 of 32 |
| Proposals accepted | 0 of 2 | 0 of 5 |

Neither run produced a candidate, so the held-out tasks were never run and nothing was activated.

- **Ties.** In run 2, three of the five proposals tied their parent on the minibatch (2.0 vs 2.0, 2.6 vs 2.6, 2.6 vs 2.6), and two scored slightly lower. GEPA accepts only strict improvements. In each run, one iteration was skipped because the parent already solved its whole minibatch.
- **Too little headroom.** The baseline solves about 80% of the reflection tasks. The failures it does have (14389 at 0, 14462 and 14371 partial) were failed the same way by every proposal, so a minibatch of three rarely leaves room to win.
- **Noise.** Baseline validation went from 5/8 to 4/8 between runs. In run 2, 14434 and 14638 failed even with the larger cap, and 14474 ended in a provider error: one response hit the output-token limit (`incomplete:max_output_tokens`) and was scored 0. One task changes the validation score by 0.125, which is as large as any effect these instruction changes could plausibly have.
- **Takeaway.** On this task set, generic test-first instructions don't measurably change outcomes for this model. A useful next experiment needs reflection tasks the baseline actually fails, more than one rollout per task to average out noise, and a decision on whether provider errors should be retried rather than scored. Running the same configuration again would mostly spend money on ties.
