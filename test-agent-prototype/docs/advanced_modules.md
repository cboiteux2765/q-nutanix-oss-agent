# Seeded pilot benchmark and explicit evolver

## Replay and review

From the project root, with Git on PATH and the project's Python environment active:

```powershell
python benchmark_pilot.py --output benchmark-output/my-pilot
```

The output directory must be new. The harness creates 15 synthetic historical PR snapshots: three clean passes, three genuine source regressions, three missing prerequisites, two conflicting specifications, two scheduled flaky suites, and two exhausted repair budgets. Base/head commits, approved specs, pytest subprocesses, JUnit and candidate commits are real local evidence. Seed labels are fixed before execution. These are reproducible fixtures rather than customer PR history. The flaky fixture deliberately depends on the recorded invocation directory: head and base fail, and the same-head diagnostic rerun passes. This avoids an unreliable random test or mutable source tree.

The output includes benchmark_report.md, benchmark_results.json, per-case repositories and evidence, cases.json, and independent_review_template.json. Reports record:

- Complete pilot coverage; bound input and evidence identities; checksums; JUnit consistency.
- Correct requirement-to-test mapping and original-head outcomes, targeting 90%.
- Seed-label RCA accuracy separately from independently reviewed RCA accuracy, targeting 80%.
- Zero patch attempts or executions for environment/spec blockers, with named owner roles.
- Original-head versus base regression evidence and source-only candidate validation.
- p50/p95 active time, queue time, wall time, and quoted model spend in USD.
- TGC: fraction meeting the seeded scenario goal, including correct blocker escalation. This is not deployment readiness.
- APT: mean original-head passed tests across all 15 cases, counting blocked cases as zero. Baseline/candidate passes are excluded.

Integrity checks detect inconsistencies and local modifications; they do not provide signed attestation against an operator who can rewrite all storage. Model spend is zero for the deterministic repair fixtures. The original PR remains CHANGES_REQUIRED even when a candidate passes.

Have an independent reviewer inspect source snapshots, original/base/rerun JUnit, manifests and findings. Copy independent_review_template.json to a separate reviews.json, fill each reviewer, classification and rationale, and retain the exact run/identity/manifest hashes. The allowed classifications are NONE, CODE_REGRESSION, ENVIRONMENT_BLOCKED, SPEC_AMBIGUOUS, TEST_FLAKY and UNKNOWN. Then run:

```powershell
python benchmark_pilot.py --rescore benchmark-output/my-pilot --reviews reviews.json
```

Rescoring checks persisted evidence again without rerunning tests. Duplicate, missing, stale, mismatched or self-attributed seed reviews are rejected. Until all cases have independent reviews, the RCA gate is PENDING_REVIEW and overall success is false. Importing reviewer names is an operator assertion, not remote identity authentication. No independent reviews or PRD source were supplied with the implementation task; the implemented thresholds are those explicitly listed in that task.

## Evolver API

[app/evolver_engine.py](../app/evolver_engine.py) is an explicit opt-in engine. The existing FastAPI service and RunManager continue to use frozen policies and never automatically activate an evolved pool. The engine accepts the existing [EvolutionMemory](../app/evolution_memory.py), an approved HoldoutGate, an optional async reflection worker, and a dedicated artifact pool directory.

The persistent state corresponds to pi_S = {K, T, V}:

- knowledge: reviewed transferable principles and supporting event IDs (K).
- prompts: GEPA-oriented instructions and Python examples (T).
- validation: pinned holdout identity, source tree hash and mandatory coverage policy (V).

Proposals may add principles and prompt material. They cannot supply paths, replace existing principles, alter tests, change validators, issue commands or grant readiness. V remains operator-owned. Each successful version writes its own SKILL.md, gepa_prompts.json, state.json and checksummed commit.json under pool/versions/VERSION. An atomic current.json pointer selects the committed version. Installed skills and service policies remain unchanged; a separate operator integration may load a verified version later.

The four stages are:

1. Diagnose: select the latest run traces with version-matching named reviews or authenticated-actor feedback; honor the caller's repository scope. Redact data before planning.
2. Plan: obtain a version/hash-bound EvolutionProposal, either explicitly or from an injected LLM worker.
3. Update: append reviewed transferable principles and prompts in a trial directory. Reject duplicate IDs, missing provenance, obvious task identifiers, secrets and oversized input.
4. Verify: validate Pydantic schemas, parse Python examples and Python fenced snippets with ast.parse, run the pinned holdout on both current and proposed artifacts, require every mapped acceptance node to pass, and recheck artifacts before compare-and-swap commit.

If the pool advances while a proposal is pending, the reflection worker receives the stale proposal, latest state, reviewed diagnosis and every intervening hash-linked commit. It must return a new proposal bound to the latest version, discarding case-specific noise and redundant principles. Reflection retries and quoted model costs are bounded. A second pool advance during validation triggers another rebase and full validation; stale evidence cannot authorize a commit. Missing workers, invalid reflection, failed holdouts, timeouts and cancellation leave the committed pointer unchanged.

### Provider and holdout integration

An operator can use the following pattern from trusted application code:

```python
from pathlib import Path
from app.evolver_engine import EvolverEngine, HoldoutGate, LLMReflectionWorker

# holdout_context is resolved from a separately approved operator registration.
# principal is supplied by trusted authentication and includes the evolver role.
worker = LLMReflectionWorker(
    complete=provider_complete,  # async provider adapter supplied by the operator
    quote_usd=0.10,             # conservative upper quote per request
    max_output_tokens=3000,
)
engine = EvolverEngine(
    Path(".evolver-pool"), manager.memory, HoldoutGate(holdout_context),
    worker=worker, max_runtime_seconds=120,
    max_model_spend_usd=2, max_reflections=2,
)
result = await engine.evolve(principal)
```

The completion callable receives system, payload, response_schema and max_output_tokens keyword arguments and returns a JSON string or object matching EvolutionProposal. The provider owns credentials and must honor cancellation and the declared quote/token ceiling. The engine conservatively charges the full quote on failed calls as well. Without a configured provider there are no external model calls; explicitly supplied fresh proposals can still be evaluated offline.

HoldoutGate requires a trusted, approved ResolvedContext and pins the source tree and spec hash. Its tests execute in disposable copies with candidate data at evolution_artifacts/SKILL.md, evolution_artifacts/gepa_prompts.json and evolution_artifacts/state.json. Independent holdout tests must read and evaluate those artifacts; ordinary PR tests that ignore them cannot measure prompt behavior or improvement. [tests/test_evolver.py](../tests/test_evolver.py) demonstrates a real holdout that checks artifact consistency and rejects QA-bypass instructions. Local host execution is restricted to reviewed fixtures, exactly as in the MVP; Docker remains an alternative and was not integration tested here.

Passing these checks establishes compatibility with the configured holdout, not universal prompt safety or measured LLM improvement. The engine does not train weights or run the GEPA optimizer. Validation-policy changes require an explicitly configured new pool. Commits use a nonblocking filesystem lock and atomic pointer replacement; a crash leaving a lock or orphan version fails closed and needs operator inspection rather than automatic overwrite.

Artifact JSON and Markdown are UTF-8. Holdout tests and integrations should use
`read_text(encoding="utf-8")`, including on Windows, so non-ASCII principles,
prompts and reviewer names round-trip without depending on the system locale.
Cancellation during holdout execution returns CANCELLED and leaves the pool
version unchanged.

### Evolution scaling metrics

Every attempted step appends a redacted promotion_proposal event to the existing JSONL memory. It records stage order and timings, total runtime, reviewed-event count, staleness distance, reflection count, holdout invocation count, quoted model spend, accepted updates, artifact version and principle count. Pool-scoped cumulative step/spend/accepted-update counters allow comparison across evolution steps. These observations do not claim FlashEvolve throughput or A-Evolve benchmark gains.

## Tests and coverage

```powershell
python -m pytest -q
python demo_acceptance.py
python -m pip install -e ".[dev]"
python -m coverage run --source=benchmark_pilot,app.evolver_engine -m pytest -q
python -m coverage report -m
```

Coverage is a development dependency. MCP is now a core dependency; the human QA API is optional. Tests use real local Git and holdout pytest alongside deterministic async provider fixtures. No paid LLM, GitHub write, merge or deployment is required.
