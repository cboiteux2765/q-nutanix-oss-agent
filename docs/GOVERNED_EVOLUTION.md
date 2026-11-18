# Governed self-evolution

How the Product Spec Agent improves itself without drifting out of control.
Baseline: Prime Agent's *Continual Harness* and `/refine`
([PrimeIntellect-ai/prime-agent](https://github.com/PrimeIntellect-ai/prime-agent), MIT).
Prime Agent never rewrites its base system prompt. It makes small, evidence-backed
updates to supplemental state (prompts, memories, skill descriptions, subagent specs),
and it keeps refinement history and rollback. We keep that model and add explicit gates.

**Demo:** open EVO (`uv run evo`), connect any repository, then **Spec Lab → Governed
evolution**. **Recorded** mode replays real qwen2.5:7b replies (see below) and needs no model or
API key. The older standalone page
`site/simple-spec-demo/index.html` still opens directly with no server.

## Five parts

| Part | Contents | Who can change it | Code |
| --- | --- | --- | --- |
| **Immutable core** | Base prompt, evidence/citation rules, output schema, tool allowlist, validators, budgets | Humans, via code PR only | `generate.py` (`INSTRUCTIONS`, `SCHEMA`, `check_draft`), `spec_tools.py`, `review.py`, fixed rules in `learning.py` |
| **Evolvable harness** | Lessons (supplemental prompts), memories (unverified feedback), skill cards (Markdown, no code), subagent role specs | The refiner proposes; gates and humans decide | Prompt versions in `learning.Workspace`; both harnesses read the active version |
| **Refiner** | Reads a run's trajectory, validator failures and feedback. Emits **one small diff** that cites run and feedback IDs | Model | Prompt-designer specialist in `learning.Orchestrator` |
| **Gates** | See below | Policy, written in the core | `agent/governance.py` (`check_gates`) |
| **Ledger** | Append-only, hash-chained proposals, evaluations, decisions and approvers. Snapshots for rollback | Append only | `governance.seal` / `verify_ledger` over the workspace audit log |

## Gates (in order)

All gates are deterministic. No model is asked whether a change is safe.

1. **Scope:** the diff touches only harness files, never the core. Checked by path or key
   allowlist, not by the model. *Implemented:* a candidate version may only carry the
   allowlisted keys, and its prompt hash must match the hash recorded at evaluation.
2. **Policy lint:** reject text that would weaken core rules ("skip citations", "assume",
   "auto-approve", "ignore validator"). *Implemented:* regex list over the added lines.
3. **Size and rate:** at most about 300 characters per lesson, a cap on active lessons,
   and at most N promotions per day. *Implemented:* 300 characters, 8 lessons, 3 per day.
4. **Provenance:** the proposal must name the run and feedback that justify it.
   *Implemented:* the run ID and rationale are required; cited feedback is counted.
5. **Frozen benchmark replay:** run the current and candidate harness on the same pinned
   evidence snapshots. Zero regressions are allowed. Include "must stay blocked" cases so
   a lesson can't erase open decisions. *Implemented:* the mini-swe-agent harness replays
   the frozen benchmark (`evals/cases.py`) with both prompts. Any case whose score drops,
   or any must-stay-blocked case that stops flagging its decision, blocks promotion.
   The orchestrator's own critic verdict on freshly generated scenarios is shown as a
   separate **Scenario check** gate. It is a model judgment, so it doesn't replace this one.
6. **Risk-tiered approval:**
   - Memory: stored automatically, marked unverified, expires after a while, and never counts as evidence.
   - Lesson or skill card: needs one human approval. *Implemented: a named reviewer.*
   - New subagent role or tool-use change: needs canary/shadow runs and two approvals. *Not evolvable yet.*
   - Core or model weights: outside self-evolution.
7. **Post-promotion monitoring:** roll back automatically if spec approval rate or
   validator pass rate drops below the previous version. *Today rollback is manual (Spec Lab
   → prompt versions → Restore). Automatic rollback is the next step.*

## Frozen benchmark (was the gap to close first)

Scenarios generated inside a cycle change every run, so they can't be compared across
runs. Gate 5 therefore replays a **frozen benchmark**: pinned evidence snapshots with
expected outcomes, graded by rubrics rather than a model judge. `evals/cases.py` holds 7
cases: a happy path, an undecided product choice, prompt injection inside a code
excerpt, a distractor source, conflicting stakeholder comments, a vague issue, and the
bundled fixture. Three are marked *must stay blocked*. The original target of 10–20 still
stands.

**Offline demo data.** `python -m evals.record` runs every case through every harness on a
live model and stores each specialist's replies in `examples/recorded/`. It also records one
complete evolution cycle and the mini-swe-agent runs with that cycle's candidate prompt, so
propose → Gate 5 replay → promote works with no model. A replay feeds the recorded replies to
the real harness, tools, validators and gates. A combination that wasn't recorded (for example a
cycle after you promote a new prompt) is refused with a message instead of being made up.

## Relation to EVO's Evolution page

Mihir's **Evolution** page (GEPA on real pytest issues, hidden-test scoring, held-out
gate, manual activation, rollback) governs the **Code** role's instructions. The Spec Lab
applies the same discipline to the **Product Spec Agent's** prompt:

| | Evolution (Code role) | Spec Lab (Product Spec Agent) |
| --- | --- | --- |
| Optimizer | GEPA, reflective, Pareto | One refiner proposal per cycle, from reviewer feedback |
| Score | Hidden FAIL_TO_PASS × PASS_TO_PASS tests | Rubric checks on frozen snapshots plus validators |
| Gate | Held-out: no regression, score ≥ baseline, ≤ 1.5× tokens, no leakage | Gates 1–6 above, zero benchmark regressions |
| Activation | Manual (or opt-in automatic when it passes and improves) | Manual, named reviewer, every gate must pass |
| Rollback | Restore an earlier version or built-in | Restore any retired version |

A natural next step is to run the Spec Lab's benchmark replay through the Evolution
page's budget admission, so spec cycles share the same token and spending limits.

## Later: weights

Accepted specs, reviewer verdicts and coding-agent outcomes form a labeled trajectory
dataset. They could feed a Verifiers environment and PRIME-RL training. Any resulting
checkpoint would be a *core* change: it gets the same benchmark and human sign-off and
never promotes itself.
