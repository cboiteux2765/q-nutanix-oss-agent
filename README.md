# Test Stage — Prime Agent Tool Prototype

UCSB CMPSC 189A, Fall 2026 · Sponsored by Nutanix

## Project Overview

Verify a registered pull request against an approved specification and return
requirement-level results, failure diagnostics, and reproducible evidence.
Prime Agent owns agent sessions, scheduling, and SDLC handoffs. This branch
implements the Test-stage tools that a Harness-managed agent can call.

The current prototype checks pinned versions and prerequisites, runs real
pytest suites, compares failing PR tests with the base version, and records
checksummed evidence. A successful automated run becomes **READY_FOR_QA**;
a separately authorized human decision is still required.

See the [Test-stage README](test-agent-prototype/README.md) for the execution
model and [Prime integration guide](test-agent-prototype/docs/prime_test_tools.md)
for tool contracts, operator setup, and REPL examples.

## Offline Demo

Requirements: Python 3.11+ and Git on PATH. The latest recorded validation used
Windows, Python 3.12.10, and MCP Python SDK 2.3.0.

From the repository root:

```powershell
cd test-agent-prototype
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev,qa]"
python -m scripts.prime_repl_example
python demo_acceptance.py
```

On macOS/Linux, activate the environment with `source .venv/bin/activate`.

The REPL-style example starts a real stdio MCP server and stores preflight,
verification, manifest, and artifact responses as Python variables. It uses
a local client facade rather than a live Prime Host session.

The acceptance demo executes real Git commits and pytest subprocesses across
five scenarios:

1. A clean PR passes its mapped acceptance tests and records a separate QA decision.
2. A regression fails on the PR head and passes on the base; an explicitly enabled candidate is tested separately, then an incorporated fix receives fresh verification.
3. A missing environment prerequisite blocks testing and identifies the environment owner.
4. Conflicting requirements block testing and identify the specification owner.
5. Failed candidate attempts exhaust the budget, an identical tool retry reuses the run, and a newer commit invalidates old readiness.

The demo prints five PASS lines and saves a fresh directory under
`test-agent-prototype/demo-output/`, ignored by Git. Fixtures use deterministic
patch proposals; no paid model, GitHub write, cloud account, or image pull is
needed.

## Prime Agent Integration

The `evo-test` MCP connection exposes four tools:

| Tool | Purpose |
| --- | --- |
| `test_preflight` | Check registered versions, permissions, requirement mappings, and declared prerequisites. |
| `test_verify` | Execute bounded verification and return its terminal result. |
| `test_manifest` | Retrieve the authorized, version-bound evidence manifest. |
| `test_artifact` | Read a size-limited UTF-8 artifact from the manifest allowlist. |

The handoff pins the repository/PR, base and head SHA, approved specification,
test profile, environment, and policy versions. Tool arguments identify expected
registered inputs; they do not grant permissions or supply arbitrary commands.

An operator registers the source and configures absolute `EVO_TEST_DATA_DIR`
and `EVO_TEST_POLICY_FILE` paths. Start the trusted tool process from the package
directory with `python -m app.prime_mcp`, or use the installed `evo-test-tools`
command. Policy must be stored outside the agent-writable repository.

See the [integration guide](test-agent-prototype/docs/prime_test_tools.md),
[JSON schemas](test-agent-prototype/schemas/prime-test-tools-v1.json), and
[permissions manifest](test-agent-prototype/schemas/prime-test-permissions-v1.json).

## Prototype Layout

```text
test-agent-prototype/
  app/
    prime_mcp.py          Four-tool MCP entry point
    prime_tools.py        Pinned contracts, permissions, and tool results
    providers.py          Registered local Git mirrors and immutable snapshots
    preflight.py          Specification, environment, and profile checks
    test_runner.py        Bounded pytest execution and JUnit/log capture
    run_manager.py        Verification lifecycle, budgets, evidence, and QA
    rca_and_repair.py     Failure classification and optional candidate validation
    evolution_memory.py   Redacted run, review, and feedback events
    evolver_engine.py     Opt-in validated artifact evolution
    main.py               Optional human QA/evidence HTTP API
  tests/                  Unit, boundary, and real integration tests
  scripts/                MCP example, schema export, and audit utilities
  schemas/                Versioned contracts and permissions
  docs/                   Integration, governance, and audit notes
  demo_acceptance.py      Five executable tool acceptance scenarios
  benchmark_pilot.py      Fifteen seeded PR benchmark cases
  seed_demo.py            Trusted fixture registration and pinned handoff
```

## Tests and Validation

Run from `test-agent-prototype/` with the environment activated:

```powershell
python -m pytest -v
python -m scripts.check_execution_sandbox
```

The latest recorded validation, November 12, 2026, includes **213 passing
tests**, all **five acceptance scenarios**, and independent verification of
**seven evidence manifests and 44 artifact checksums**. The full regression
includes the seeded benchmark, evolver, MCP, and human QA checks.

Restricted Windows command sandboxes may prevent asyncio from creating its
local socket pair. The capability probe reports this separately from Docker
daemon availability. The full integration suite was run with approved execution
outside that restriction; 49 checks also passed inside the command sandbox.
See [VALIDATION.md](test-agent-prototype/VALIDATION.md) for commands, results,
and the limits of those observations.

## Benchmark and Learning

The [seeded benchmark](test-agent-prototype/benchmark_pilot.py) replays 15
synthetic PR snapshots covering clean passes, code regressions, missing
prerequisites, ambiguous specifications, flaky tests, and budget exhaustion.
It produces Markdown/JSON reports with integrity, requirement mapping,
classification, blocker, patch, runtime, cost, TGC, and APT metrics.
Independent RCA review remains pending until version-bound reviews are supplied.

The [evolver](test-agent-prototype/app/evolver_engine.py) is enabled separately.
It uses reviewed memory in a **Diagnose → Plan → Update → Verify** loop,
reflects on stale proposals when a worker is configured, and requires AST,
schema, and pinned holdout checks before committing versioned skill/prompt
artifacts. Ordinary verification does not automatically run evolution or
retrieve historical repair examples.

See the [advanced module guide](test-agent-prototype/docs/advanced_modules.md)
for replay commands, reflection adapters, holdout requirements, and metrics.

## Human QA and Execution Policy

`READY_FOR_QA` means the automated evidence is ready for review. It does not
approve, merge, or deploy a PR. A validated candidate cannot clear the original
PR; incorporation requires a fresh run against the new head.

Default tools have no repair provider or model-spend allowance. They return
regression evidence and a next responsible role to Prime. Candidate generation
requires explicit operator configuration and an authorized budget. The optional
human QA API serves existing results and review/feedback; the old standalone
webhook and HTTP execution queue have been removed.

Untrusted code requires the pinned Docker backend, with networking disabled
and resource limits. `local_trusted` is only for operator-reviewed fixtures and
runs on the host. An unavailable Docker backend never falls back to host pytest.

The prototype still binds static operator scope rather than signed per-caller
identity. Live Prime Host execution and real Docker container execution have
not been integration-validated. Production deployment needs trusted host
authentication and OS-level separation. The run store is single-process;
independent MCP and QA processes must not concurrently open the same directory.

## Team

- Clement Boiteux — Team Lead
- Mihir Srivastava — Scribe
- Shanqin Chen — Test-stage prototype on this branch
- Hanson Hu
- Timothy Nguyen

Nutanix Mentors: Srinath GS and Kostadis Roussos.

## Project Documents

- [Project Drive Folder](https://drive.google.com/drive/folders/1gU4mTCUmGwYahgBPCl9y5lf1rDZUrHXl)
- [Project Vision Draft](https://docs.google.com/document/d/1U8PigryM9BC_RDWqhW7rS_osOF0PKRiAjhZTcsWvtJ4/edit)
- [Team Journal](https://docs.google.com/document/d/1mD9hfMsIwTCFuCEDhGSnKR5YvTnjHzCAGTxGxDNCquI/edit)
