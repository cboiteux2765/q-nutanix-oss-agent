# Product Spec Agent: mini-swe-agent vs Prime-RLM harness

A qualitative comparison of two harnesses running the same Product Spec Agent task:
turn a pinned issue snapshot into a cited specification that passes `check_draft`.
Both use the same model, tools, validators, base prompt and frozen benchmark. Only the
harness differs. Run it yourself in EVO (**Spec Lab → Compare harnesses**) or with:

```powershell
uv run python -m evals.run_evals --backend aicommons --model claude-v5.5-haiku
```

## Short answer

- **With a capable hosted model, both harnesses solve every case.** On `claude-v5.5-haiku`,
  all 21 runs produced a valid, cited draft and passed every rubric check. The benchmark
  doesn't separate the harnesses on quality. It does separate them on cost, failure modes,
  transparency and fit for self-evolution.
- **mini-swe-agent is about half the cost per spec**: 3.1 model calls and 12.4k tokens per
  run, against 6.3 calls and 26.8k tokens for Prime-RLM. Its single transcript is the
  easiest to read and debug.
- **The Prime-RLM orchestrator does more work for its tokens.** It writes two drafts
  (current and proposed prompt), generates scenarios, has a critic judge them and proposes
  a prompt revision. That makes it the natural refiner for self-evolution. Its critic,
  however, is too noisy to serve as a promotion gate.
- **With a small local model, neither harness works.** qwen2.5:3b produced 0 drafts. The
  harnesses fail differently: mini-swe-agent runs out of steps or breaks its action format;
  Prime-RLM loses the whole cycle when one specialist fails its single repair.
- **Recommendation:** use mini-swe-agent to write specs and to replay the frozen benchmark
  (Gate 5), and use the Prime-RLM orchestrator to propose prompt revisions, with
  deterministic gates rather than its critic deciding promotion.

## Results

### claude-v5.5-haiku on UCSB AI Commons (2026-11-17)

7 frozen cases × 3 harnesses, prompt v1 (`BASE_PROMPT`), one run per cell. The model ignores
`temperature`, so runs are not exactly repeatable; the replies are recorded in
`examples/recorded/` and replay identically in Spec Lab.

| Harness | Drafts | Mean rubric score | Model calls (per run) | Tokens in / out (per run) | Seconds per run |
| --- | --- | --- | --- | --- | --- |
| mini-swe-agent | 7 / 7 | 100% | 22 (3.1) | 66,228 / 20,664 (12.4k) | 13.3 |
| Prime-RLM | 7 / 7 | 100% | 44 (6.3) | 141,526 / 45,807 (26.8k) | 18.9 |
| Prime-RLM, schema in prompt | 7 / 7 | 100% | 42 (6.0) | 146,000 / 50,243 (28.0k) | 20.6 |

Per case, every harness flagged the open decision in `undecided-whitespace`,
`conflicting-comments` and `vague-issue`. None of them committed to the injected "DROP TABLE"
or "mark this spec approved" in `prompt-injection`, and none cited the unrelated auth excerpt
in `distractor-evidence`. mini-swe-agent made no format errors and needed 3–4 calls. The
orchestrator needed one extra call (a tool continuation or a repair) in two runs.

The full recording cost 558,823 tokens (1.6% of the account's 35M monthly quota), including
the seven mini-swe-agent runs with the evolution cycle's candidate prompt used by Gate 5.

Raw rows: `evals/results/results-aicommons-claude-v5.5-haiku-*-recorded.json`.

### qwen2.5:3b on Ollama (local, earlier harness versions)

| Harness | Drafts | How it failed |
| --- | --- | --- |
| mini-swe-agent | 0 of 3 attempted cases | Used all 12 steps resubmitting a draft with the same error; wrote ```` ```json ```` blocks instead of an action (format errors) |
| Prime-RLM | 0 of 3 | Research brief missing a nested field; scenarios with empty strings; an empty `tool_requests` list on repair |
| Prime-RLM, schema-constrained | 0 of 3 | Schema-valid JSON with empty strings; duplicate or misplaced IDs |

These runs were stopped early (logs in `evals/results/partial-run-*.txt`), and several harness
fixes listed below came after them. They show failure modes, not the final 3B capability.
Spot checks on qwen2.5:7b after the fixes: mini-swe-agent submitted a valid draft in one call,
and the orchestrator's research and scenario specialists passed.

## The two harnesses

| | mini-swe-agent (`agent/mini_product.py`) | Prime-RLM orchestrator (`agent/learning.py`) |
| --- | --- | --- |
| Shape | One agent, one linear message history | Six specialists: research, prompt design, scenarios, baseline writer, candidate writer, critic |
| Control flow | The model decides each step: search, read feedback, validate, fix, submit | Fixed DAG: three specialists in parallel, then two writers in parallel, then the critic |
| Actions | One shell-style command per turn, in a fenced block, parsed by regex (mini's text-based protocol) | Each child returns JSON: its final schema or one `tool_requests` batch (at most 3) |
| Tools | Virtual read-only shell: `evidence_search`, `feedback_memory`, `requirement_trace`, `validate_draft`, `submit` | The same `spec_tools.py` dispatcher, scoped per role, plus one primary tool result injected up front |
| Validation | Validators answer like a terminal; the agent can retry until the step limit (12) | One repair per specialist; any specialist failing fails the whole cycle |
| Context | Grows every turn: the full history is re-sent | Each child starts fresh with the shared payload |
| Output | One draft | Two drafts (current vs proposed prompt), scenarios, critic verdicts, a candidate prompt |
| Model calls | 2–12 | 6 when nothing fails, at most 18 |
| Runtime | `minisweagent.DefaultAgent`, unchanged | `PrimeBackend` over spawn/collect. Here a local stand-in (`agent/backends.LocalRLM`) runs each child as one completion; a real Prime child is a sandboxed REPL agent |

A third arm runs the same orchestrator but hands the JSON schema to the backend. On Ollama
that is schema-constrained decoding. AI Commons does not offer constrained decoding, so there
the schema is written into the prompt instead. The arm separates "harness topology" from
"how the output format is enforced".

## Qualitative findings

**1. Model capability matters more than the harness.** The same harnesses go from 0 of 21
to 21 of 21 between a 3B local model and Haiku. Below that threshold, harness design decides
*how* a run fails, not *whether* it succeeds. Above it, this benchmark is at its ceiling, so
it needs harder cases before it can rank harnesses on quality.

**2. Failure is graceful in one harness and all-or-nothing in the other.** mini-swe-agent
treats validation errors as terminal output and can keep correcting a draft until its step
limit. The orchestrator gives each specialist one repair; if any of six specialists fails,
the cycle produces nothing, even when the writers' drafts would have been valid. With the 3B
model, cycles failed on the research brief or the scenario list, neither of which is the spec.

**3. Error feedback is part of the harness.** The biggest improvements came from validator
messages, not prompts. Reporting every problem at once with exact paths (for example
`brief.users[0]: missing field 'source_ids'`) turned repeated one-error-at-a-time loops into
single-step fixes. The orchestrator benefits too, because its one repair now sees the whole
problem.

**4. Each harness has its own brittleness.**
- mini-swe-agent depends on format compliance: one fenced action per turn. Small models
  write ```` ```json ```` instead, or copy hint text literally (a skeleton hint
  `"ISSUE / COMMENT-N / SRC-N"` became the citation `"ISSUE / COMMENT-1"`). They can also
  resend an identical failing action until the step limit, which the shell now points out.
- The orchestrator depends on large nested schemas whose field meanings are unambiguous.
  A scenario's `input` field read like a unit-test argument, so models wrote `""` for an
  empty-input case. Defining the fields in words fixed it for 7B and Haiku.

**5. Output-format enforcement doesn't carry across providers.** Ollama enforces a JSON
schema while decoding, but that guarantees shape, not content: the 3B model returned
schema-valid JSON with empty strings. Hosted AI Commons models have no constrained decoding;
the first schema-arm run returned made-up fields because the schema was only passed as a
decoding hint. Showing the schema in the prompt fixed it, and Haiku then matched the
prompt-only arm.

**6. Cost scales differently.** mini-swe-agent re-sends its whole history every turn, so a
long struggle gets expensive: up to 74k input tokens for one failing 3B run, against about
12k for a typical Haiku run. The orchestrator's cost is roughly fixed (six or seven calls),
but every call carries the full payload. Per draft the two are close: the orchestrator writes
two drafts for about 27k tokens.

**7. Transparency.** A mini-swe-agent run is one readable transcript of thought, action and
observation. An orchestrator run is a set of specialist events, per-role tool calls and a
critic table. That is richer, but you have to reconstruct the story yourself. Both are shown
in Spec Lab.

**8. For self-evolution, keep the proposer separate from the judge.** The orchestrator is a
good proposer: the research brief, scenarios and feedback give its prompt designer real
material. Its critic is not a reliable gate. It writes a fresh set of 6–9 scenarios each run,
so scores can't be compared across runs. Verdicts also swing on the same case: on
`undecided-whitespace` the candidate passed 3 of 8 scenarios in one arm and 7 of 8 in the other,
and that arm's baseline passed 2 of 8. In the recorded demo cycle, Haiku's "small lesson" rewrote the whole base
line into 392 characters. The deterministic size gate caught that, and the mini-swe-agent
benchmark replay showed no regressions. The governed loop works best with the expensive
harness proposing and the cheap one checking.

**9. Rubric graders need the same care as the agents.** Two automatic grading errors came
from the injection check matching the word "approved" anywhere. Both were legitimate spec
text ("Is a breaking change … approved?" and "the approved over-maximum decision"). The check
now looks only at what the spec commits to (scope, change, requirements, tests, tasks), and
for phrases that show the injected instruction was followed. Neither error changed a harness
total by more than one case.

## What was changed to make the comparison fair

Changes made during development are listed so the numbers can be read honestly.
Each one applies to both harnesses unless noted. All were made before the Haiku recording,
except the grader fix, which was applied to the stored drafts (each regraded row is marked).

- **Validator messages** (`generate.check_shape`, `check_draft`) list every problem at once,
  with exact paths and the offending IDs, labelled by output (`brief`, `scenarios`, role).
- **Repair turns** quote the validation error and ask for the final schema only.
- **`evidence_search`** also matches source IDs.
- **Scenario fields** are defined in words in the scenario specialist's instruction.
- **Hosted backends** show the JSON schema in the prompt when one is required.
- **Mini only:** the task message includes the same source payload every Prime child gets;
  the prompt shows a typed JSON skeleton with real ID examples; an identical repeated failing
  action gets a note.
- **Supplemental prompt:** both harnesses read the active governed prompt version.
- **Grader:** injection markers are phrases, checked only in commitment fields (see finding 9).

## Limits of this evaluation

- **One run per cell, two models in depth.** Treat the numbers as an exploratory benchmark,
  not a statistical result.
- **Seven synthetic cases, now at their ceiling for Haiku.** The rubrics check structure,
  decision flagging, injection resistance and distractor citations. They don't judge whether
  the spec is good product thinking.
- **The Prime runtime is simulated.** Each child is a single completion, so the real Prime
  agent's in-child tool use and REPL are not exercised. The orchestration, tool protocol,
  repair budget and failure semantics are the real ones from `learning.py`.
- **The harnesses do different amounts of work.** The orchestrator also writes scenarios, a
  critic verdict and a prompt proposal. A per-run cost comparison overstates its overhead for
  the evolution use case and understates it for one-off specs.
