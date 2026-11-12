# Prototype validation

## Current: Prime-owned orchestration migration — November 12, 2026

Prime MCP is the primary execution entry point. The former HTTP task creation,
webhook queue, cancellation endpoint and two Spec/Code legacy endpoints were
removed. HTTP now serves existing evidence and authenticated human QA/feedback
only. PM notification and unused hypothesis/judge adapters were removed.
RunManager keeps verification budgets, cancellation, recovery, evidence and QA;
its default repair provider is now absent. Existing stores remain readable,
and old webhook ledgers are ignored. Candidate proposals require explicit
operator injection and an authorized budget.

The five acceptance scenarios now exercise tool preflight, verification,
manifest and artifact retrieval. Scenario five replaces duplicate webhook
delivery with an idempotent Harness retry. The retired endpoint tests were
replaced with absence/no-execution tests; these are migrated contracts, not
a claim that the removed HTTP routes are still compatible.

An adversarial regression reproduced unchecked Pydantic `model_copy` input
reaching registry lookup, including a boolean PR number converted to `1` by
serialization. Target and nested-budget models now revalidate instances
before registry access. Wrong spec hashes, boolean PR numbers and unchecked
over-limit budgets all fail validation.

Verified on Windows, Python 3.12.10, MCP 2.3.0:

| Executed check | Result | Execution environment |
| --- | --- | --- |
| `python -m pytest -q tests/test_architecture.py tests/test_repair_memory.py` | **49 passed in 1.37s** | Ordinary restricted command sandbox |
| `python -m pytest -v --junitxml=demo-output/tool-migration-20261112/pytest.xml` | **213 passed in 226.26s**, no failure/error/warning summary | Approved execution outside the command sandbox |
| `python demo_acceptance.py --output demo-output/tool-migration-acceptance-20261112` | **5 passed**, all through the tool interface | Approved execution outside the command sandbox |
| `python -m scripts.audit_acceptance_manifests demo-output/tool-migration-acceptance-20261112` | **7 bound manifests, 44 verified artifacts** | Ordinary command sandbox |
| `python -m scripts.prime_repl_example` | **PASS**, READY_FOR_QA, 2 fixture tests; preflight → verify → manifest → artifact | Real stdio MCP subprocess outside the command sandbox |
| `python seed_demo.py --data-dir demo-output/tool-migration-seed-20261112` | Registered fixture and generated pinned `test-target.json` | Ordinary command sandbox |
| `python -m pytest -q tests/test_prime_tools.py -k checked_in_contracts` | **1 passed**, runtime schemas/permission manifest agree | Ordinary command sandbox |
| `python -m pip check` | No broken requirements found | Ordinary command sandbox |
| Editor diagnostics and `git diff --check` | Passed | Local source checks |

- [Full regression JUnit](demo-output/tool-migration-20261112/pytest.xml)
- [Tool acceptance summary](demo-output/tool-migration-acceptance-20261112/acceptance_summary.json)
- [Sandbox capability probe](scripts/check_execution_sandbox.py)
- [Architecture isolation tests](tests/test_architecture.py)
- [Current tool and migration guide](docs/prime_test_tools.md)

The bounded sandbox probe reproduced asyncio event-loop construction timing
out after five seconds in the restricted command sandbox. The same probe
outside that sandbox returned `ASYNCIO_OK`. No sandbox configuration was
changed. Both environments report the Docker Desktop Linux daemon pipe is
absent. Therefore no live container test is claimed. A dedicated regression
does verify the pinned no-network Docker launch arguments and confirms that
an unavailable backend cannot fall back to host pytest; that check uses a
simulated launch failure, not a real container.

The service still uses static operator scope, not cryptographically verified
per-call host identity. The permissions manifest states this explicitly.
No Prime Host executable is installed, so the stdio smoke test does not
certify a live Harness session. Host authentication and protected OS deployment
remain integration work. The local run store supports one owning process;
independent MCP/QA servers must not concurrently open the same directory.

## Historical validation records

Validated on October 22, 2026 with Windows, Python 3.12, Git, and the dependency versions in `requirements.lock.txt`.

The sections below record earlier architectures and their original results.
The lock file now reflects the November 12 tool/QA/dev verification environment;
earlier version lists remain available in Git history.

## Results

| Check | Result |
| --- | --- |
| API, authorization, webhook and real integration tests | 21 passed |
| RCA, patch policy and evolution memory tests | 42 passed |
| Real pytest runner, preflight and process cancellation tests | 18 passed |
| Budget, async proposal cancellation, QA and stale-result tests | 14 passed |
| Complete pytest suite | **95 passed** |
| Five executable acceptance scenarios | **5 passed** |
| Dependency consistency (`pip check`) | Passed |
| Local API seeding and OpenAPI generation | Passed |

The five scenarios execute real local Git and pytest operations. See `example-results/acceptance_summary.json` and each scenario's run directory for validated verification JSON, reports, JUnit, logs and checksummed manifests. Recreate the complete fixture repositories and source snapshots by running `python demo_acceptance.py`; the package includes evidence rather than the temporary Git repositories or virtual environment.

## Reproduce

From the project directory, install `requirements.lock.txt` in a local virtual environment, then run:

```text
python -m pytest -q
python demo_acceptance.py
```

In a restricted Windows execution environment, asyncio may require permission to use local loopback. This is an execution-host restriction; the offline demo performs no external network calls. The final suite emitted one non-failing Starlette/httpx deprecation warning.

## What these checks establish

The tests cover deterministic observations, no automatic approval of candidate branches, version-bound named QA decisions, hash/ACL checks for evidence, persistence recovery, immutable inputs, bounded attempts and invocation reservations, missing prerequisites, spec conflicts, cancellation of spawned test processes and model proposals, invalid patch paths, secret redaction, and stale invalidation.

No paid model calls, real GitHub write actions, cloud probes, GEPA optimization, SKILL.md modification, merge or deployment were performed. Docker execution is implemented but was not integration tested. Local fixtures are trusted; host subprocess mode is not containment for arbitrary code. See README for the local-mirror integration boundary and production extensions.

## Advanced modules verification - October 27, 2026

The historical 95-test baseline above was rerun successfully before implementation. The final extension was verified on Windows with Python 3.12.10 and the installed environment (including FastAPI 0.162.0, Pydantic 2.14.1, pytest 9.1.1 and coverage 7.17.0); these installed versions are not represented as an exact replay of the older lock file.

| Check | Result |
| --- | --- |
| Existing plus advanced pytest suite | **153 passed** (95 existing + 58 new) |
| New benchmark tests | 15 passed |
| New evolver tests | 43 passed |
| Original end-to-end acceptance scenarios | **5 passed** |
| Seeded historical PR pilot | **15 / 15 task goals met**, all six categories |
| Requirement mappings and original-head outcomes | **30 / 30** correct against predefined fixture labels |
| Version / evidence integrity | **15 / 15** audited, zero observed inconsistencies |
| Blocker handling | **5 / 5**, no testing or patching before prerequisites/spec resolution |
| Published candidate validity | **3 / 3** independently executed candidates |
| Original-head / base regression evidence | **5 / 5** |
| RCA against seed labels | **15 / 15**; not independent review |
| Independent RCA review | **PENDING_REVIEW**, no reviews supplied |
| New-module line coverage | **94% combined** (589 / 627 executable statements) |
| Evolver line coverage | **95%** (340 / 358) |
| Benchmark line coverage | **93%** (249 / 269), including actual CLI execution |

Coverage was measured by running the two advanced test modules with coverage, then appending the actual benchmark CLI execution. The complete 153-test suite was also run separately. The CLI does not import Evolver, so coverage emitted an informational module-not-imported warning for that append operation; combined data retained the independently measured Evolver coverage.

The pilot uses synthetic seeded PR history, scheduled deterministic flakiness, actual Git commits and actual pytest subprocesses. It reports active and queue times, wall time, model cost, TGC and APT with explicit denominators. All deterministic pilot checks passed. Overall pilot approval remains false until independent version-bound RCA reviews are imported. No human review or live-model judgment was fabricated.

- [Pilot Markdown report](benchmark-output/pilot-20261027-verified/benchmark_report.md)
- [Pilot JSON results](benchmark-output/pilot-20261027-verified/benchmark_results.json)
- [Independent review template](benchmark-output/pilot-20261027-verified/independent_review_template.json)
- [Original acceptance rerun](demo-output/advanced-20261027/acceptance_summary.json)
- [Advanced usage and contracts](docs/advanced_modules.md)

The Evolver tests exercised actual AST/schema rejection, source and artifact mutation, missing holdout nodes, requirement-bound execution, exact version hashes, named reviewed evidence, repository scoping, async reflection over intervening history, concurrent compare-and-swap, no-op reflection, model spend limits, timeout and cancellation, immutable validation policy and pool-scoped scaling metrics. Versioned SKILL.md and GEPA prompt artifacts were generated only in isolated test pools. No installed agent skill, production policy, model weight, GitHub repository, merge or deployment was modified. No paid model or Docker integration was exercised.

## Project bug audit - October 28, 2026

Reviewed the application contracts, API, registry, preflight, execution, diagnosis,
repair, persistence, evolution memory, explicit evolver, benchmark, demo and tests.
The original 153-test suite passed before fixes. Additional reproductions exposed
the following defects, now covered by regression tests:

| Priority | Defect and effect | Fix |
| --- | --- | --- |
| High | A STALE/report refresh recalculated hashes for modified execution evidence and candidate patches, or dropped missing files, allowing damaged evidence to become allowlisted again. | Preserve the recorded hashes and missing entries for completed executions, published patches and audit artifacts; validate the previous manifest before republishing. |
| High | RCA helpers ignored `blocked_reason`, so an incomplete head/base/rerun observation could authorize regression repair or a flaky classification. | Both passing and failing observations must be free of runner blockers. |
| Medium | Repeated QA rejection overwrote the original `before-qa-rejection` archive. | Allocate a separate archive directory for each later review event. |
| Medium | Cancellation of the last base/rerun execution could finish as COMPLETED; cancelled evolver holdouts became REJECTED. | Propagate cancellation to the orchestrator and evolver, retain CANCELLED, and leave the artifact pool unchanged. |
| Medium | Evolver artifact reads and benchmark rescoring depended on the platform's default encoding, failing on non-ASCII content under a non-UTF-8 locale. | Read persisted JSON explicitly as UTF-8; cover Chinese principles, prompts, reviews and real holdout execution. |

Final verification used Windows and Python 3.12.10:

| Check | Result |
| --- | --- |
| Complete regression suite | **167 passed**, 0 failures, 0 errors, 0 skipped (153 existing + 14 audit regressions) |
| Five end-to-end acceptance scenarios | **5 passed** on the final code |
| Seeded benchmark replay | All 15 cases and six categories exercised by the regression suite |
| Line coverage, `app` and `benchmark_pilot` | **88%** (2,178 / 2,480 statements) |
| Explicit evolver line coverage | **95%** (342 / 360 statements) |
| Dependency consistency (`pip check`) | Passed |
| Editor diagnostics and `git diff --check` | Passed |

The full suite was run with `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` and Git on PATH:

```powershell
python -m coverage run --source=app,benchmark_pilot -m pytest -q --junitxml=demo-output/project-audit-20261028/pytest.xml
python -m coverage report
python demo_acceptance.py --output demo-output/audit-20261028-final
```

- [Saved regression results](demo-output/project-audit-20261028/pytest.xml)
- [Final acceptance evidence](demo-output/audit-20261028-final/acceptance_summary.json)
- [Evidence and cancellation regressions](tests/test_run_manager.py)
- [RCA blocker regressions](tests/test_repair_memory.py)
- [Evolver Unicode and cancellation regressions](tests/test_evolver.py)
- [Benchmark Unicode rescore regression](tests/test_benchmark.py)

Ordinary sandbox shell commands worked, but the Windows asyncio loopback setup
stalled inside the execution sandbox. Its captured stack was in
`socket._fallback_socketpair`, before the first API test body. Runtime tests were
therefore rerun with approved execution outside that sandbox. This host restriction
was kept separate from the project defects. No sandbox configuration was changed
by the audit. Docker, paid LLM providers and live GitHub integration remain outside
this verification; coverage and passing fixtures do not establish absence of all bugs.

## Prime Agent Test-stage tool integration - November 10, 2026

The optional stdio MCP adapter binds an operator-owned Test-stage identity and
policy before exposing `test_preflight`, `test_verify`, `test_manifest` and
`test_artifact`. Inputs pin the approved spec, Git base/head, test profile,
environment and policy versions. The tool process rejects out-of-scope
repositories/backends, excess budgets, model-supplied roles or commands, stale
handoffs, and evidence outside the checksum-verified manifest. Candidate patch
validation never grants QA approval. The default operator policy permits only
Docker and no model repair; a reviewed local fixture needs explicit opt-in.

| Check | Result |
| --- | --- |
| Complete existing and new pytest suite | **203 passed**, 0 failed, 0 skipped |
| Newly added Prime tool checks | **36 passed** within the complete suite |
| Original five end-to-end acceptance scenarios | **5 passed** |
| Real stdio MCP handshake and four-tool call chain | Passed with MCP Python SDK 2.3.0 |
| Source line coverage (`app` plus `benchmark_pilot`) | **89%** (2,455 / 2,766 statements) |
| Test-stage tool line coverage | **96%** (239 / 250 statements) |
| Dependency consistency and editor diagnostics | Passed |

The new evidence-path test first reproduced an internal Windows junction that
was accepted after path resolution. The underlying `RunManager.artifact_path`
now rejects linked files or ancestor directories before resolving the path.
The targeted regression passed after the fix. One later test-only check also
confirmed that a policy file inside the agent-writable project is refused.

- [Prime tool interface and deployment guide](docs/prime_test_tools.md)
- [Versioned input/output JSON Schemas](schemas/prime-test-tools-v1.json)
- [Machine-readable permissions](schemas/prime-test-permissions-v1.json)
- [Saved full-suite JUnit](demo-output/prime-tools-20261110/pytest.xml)
- [Saved acceptance summary](demo-output/prime-tools-acceptance-20261110/acceptance_summary.json)

The tests used real local Git commits, pytest subprocesses and an actual stdio
MCP server/client exchange. No Prime Agent executable was available on this
machine, so a live Prime session was not exercised. The MCP server must be
deployed under operator-controlled OS/file permissions separate from a
model-writable REPL; stdio with the same OS user alone is not a hostile-model
security boundary. Docker, live GitHub, paid model adapters and remote caller
authentication were not exercised. The Windows asyncio suite was run with
approved execution outside the restricted command sandbox, which otherwise
stalled while creating its local socket pair.

The subsequent [strict security audit](docs/prime_tool_security_audit_20261110.md)
confirmed 203 passing tests with no emitted pytest warnings, all five
acceptance scenarios, eight bound manifests and 57 artifact checksums. It
also identified an incomplete control: the stdio server's static operator
policy is not cryptographic or Host-verified per-caller authentication. A
direct REPL-style MCP smoke test passed, but no Prime Agent executable was
installed for a live Harness session. These two claims are marked FAIL and
PARTIAL in the audit report.
