# EVO Test-stage tools

Prime Agent owns sessions, spawning, SDLC handoffs, retries and scheduling. This
package provides four bounded tools over an operator-registered PR mirror:

| Tool | Result |
| --- | --- |
| `test_preflight` | Checks exact base/head SHA, approved spec, environment, profile and prerequisites. |
| `test_verify` | Runs the registered pytest profile within operator budget/backend limits and returns a terminal result. |
| `test_manifest` | Retrieves the version-bound, checksum-verified evidence manifest. |
| `test_artifact` | Retrieves bounded UTF-8 evidence from the manifest allowlist. |

See the [Prime integration guide](docs/prime_test_tools.md), [schemas](schemas/prime-test-tools-v1.json)
and [permissions](schemas/prime-test-permissions-v1.json). The primary entry point
is `python -m app.prime_mcp` (or `evo-test-tools` after package installation).
The trusted service implementation is not instantiated in the agent's REPL.

## Quick start

Python 3.11+ and Git on PATH are required. Verification here uses Python 3.12
on Windows and MCP SDK 2.3.0.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev,qa]"
python -m scripts.prime_repl_example
python demo_acceptance.py
python -m pytest -v
```

On macOS/Linux use `source .venv/bin/activate`. `requirements.txt` lists the
tool runtime; `requirements.lock.txt` pins this Windows verification environment.
The `qa` extra adds the optional human API, and `dev` supplies
the complete test dependencies. [Validation](VALIDATION.md) distinguishes
historical results from the current architecture.

The smoke example runs a real stdio MCP server and retains results in Python
REPL-style variables. It does not certify a live Prime Host or host-issued
caller identity. `python seed_demo.py --data-dir .evo-data` registers a trusted
fixture and creates `test-target.json` for the pinned handoff. Configure absolute
`EVO_TEST_DATA_DIR` and `EVO_TEST_POLICY_FILE` paths in the trusted server.
Policy must live outside the agent-writable project; see the integration guide.

## Architecture and migration

```text
Prime Harness / REPL
        |
        v
four MCP tools (operator policy + pinned Pydantic contracts)
        |
        v
SourceRegistry -> preflight -> RunManager -> bounded pytest runner
                                   |
                                   v
                      RCA + requirement results + evidence + memory
                                   |
                                   v
                    next_owner_role returned to the Harness
```

The old webhook receiver, HTTP execution queue, cancellation endpoint,
`listenToSpecAndCodeAgents`, `verifyAndJudgeReadiness`, PM notification callback
and unused hypothesis/judge agent adapters were removed. RunManager is a
verification lifecycle/evidence component, not an agent manager. Retried calls
with the same pinned identity, correlation and budget reuse their run. Existing
run stores remain readable; the obsolete webhook ledger is ignored.

The default manager has no repair provider. A regression returns evidence to
the Harness-managed Code stage. An operator may explicitly inject a bounded
proposal adapter for candidate experiments and offline benchmarks. Candidates
are executed in separate workspaces, cannot change tests/protected paths, and
never approve the original PR. A new PR head needs fresh verification.
The [benchmark and evolver](docs/advanced_modules.md) remain explicit offline
utilities, outside the tool execution path.

## Five acceptance scenarios

All five scenarios use the tool preflight/verify/manifest/artifact flow with
real Git commits and pytest subprocesses:

1. Clean PR passes mapped requirements; a separate named QA approves exact identity/evidence hashes.
2. Head fails and base passes; an explicitly enabled fixture candidate passes separately; incorporation requires a fresh head run.
3. Missing environment prerequisites block execution and patching, naming the environment owner.
4. Conflicting requirements block execution and name the spec owner.
5. Two failing candidates exhaust the budget; an idempotent tool retry reuses evidence; a later commit invalidates readiness and rejects stale approval.

The fifth scenario replaces the retired duplicate-webhook scenario with a
Harness retry. The demo creates a fresh `demo-output/` directory and never
overwrites earlier evidence. Inspect it with
`python -m scripts.audit_acceptance_manifests PATH`.

## Optional human QA API

The optional [HTTP module](app/main.py) exposes only existing results and human
review/feedback. It does not accept new verification tasks.

| Route | Purpose |
| --- | --- |
| `GET /v1/test-runs/{id}` | Authenticated status and identity. |
| `GET /v1/test-runs/{id}/artifacts` | Evidence manifest. |
| `GET /v1/test-runs/{id}/artifacts/{name}` | Allowlisted, checksum-validated artifact. |
| `POST /v1/test-runs/{id}/review` | Human QA decision bound to exact hashes. |
| `POST /v1/test-runs/{id}/feedback` | Authenticated feedback annotation. |

Start with `python -m uvicorn app.main:create_app --factory --host 127.0.0.1 --workers 1`.
Configure `EVO_DATA_DIR`, distinct `EVO_ENGINEER_TOKEN` / `EVO_QA_TOKEN`,
reviewer names and `EVO_ALLOWED_REPOS` on the trusted side. No credentials
means no access. Importing the module does not create storage.

RunManager storage is single-process. Trusted adapters may share one manager;
do not run independent MCP/QA processes concurrently against the same store.
For the standalone QA demo, stop MCP, review the persisted run, then restart MCP.
Multi-user deployment requires an authenticated host/broker and durable shared
storage integration.

## Evidence, budgets and isolation

The lifecycle retains QUEUED -> PREFLIGHT -> TESTING -> DIAGNOSING -> REPAIRING
-> COMPLETED, with early BLOCKED, CANCELLED or ERROR results. Readiness is
separate: READY_FOR_QA, CHANGES_REQUIRED, BLOCKED, INCONCLUSIVE or STALE.
REPAIRING can be a no-op; it does not create an agent.

Hard pilot ceilings are two candidate attempts, four pytest invocations,
1,200 active seconds and US$2 in quoted model spend; operator policy can be
stricter. Default tool repair attempts and spend are zero. Initial/base/rerun/
candidate tests all count. Cancellation and deadlines terminate active test
processes; partial, missing or skipped evidence cannot establish a pass.

Every run persists verification JSON, a Markdown report and an evidence manifest.
Logs/JUnit are redacted. Execution evidence is hashed and checked before export;
later stale/QA refreshes cannot silently rehash tampered files. JSONL memory
records version-bound run/review/feedback events. The evolver can consume reviewed
records separately; ordinary repair has no automatic historical retrieval.

For untrusted source, only an operator-pinned Docker backend is permitted.
It disables networking and image pulls, removes capabilities, runs without root,
and limits CPU, memory, PIDs and runtime. Provision a prebuilt Python/pytest image
by digest and writable scratch/evidence mounts for UID 65534. Missing Docker
never falls back to host execution. Test provenance must be independently
approved; container isolation alone cannot make adversarial test results trusted.

`local_trusted` is an explicit opt-in for operator-reviewed fixtures and executes
Python on the host. It is not a sandbox. Inspect host capabilities with
`python -m scripts.check_execution_sandbox`; this probe has a five-second
asyncio deadline and does not pull images or run repository code.

The stdio service binds static operator policy. It does not implement signed
per-caller assertions. A same-user REPL able to edit service code/policy/data
can bypass a Python-only boundary; production needs OS isolation and trusted
host authentication. No merge, deployment, GitHub write or automatic QA
approval capability is exposed.

## Source map

| File | Responsibility |
| --- | --- |
| [prime_mcp.py](app/prime_mcp.py), [prime_tools.py](app/prime_tools.py) | Four-tool interface, contracts and permissions. |
| [providers.py](app/providers.py), [preflight.py](app/preflight.py) | Trusted registry, pinned snapshots and prerequisite checks. |
| [run_manager.py](app/run_manager.py), [test_runner.py](app/test_runner.py) | Bounded verification lifecycle, execution and evidence. |
| [rca_and_repair.py](app/rca_and_repair.py) | Evidence classification and optional candidate validation policy. |
| [evolution_memory.py](app/evolution_memory.py), [evolver_engine.py](app/evolver_engine.py) | Experience events and opt-in validated artifact evolution. |
| [main.py](app/main.py) | Optional human QA/evidence API. |
| [demo_acceptance.py](demo_acceptance.py), [benchmark_pilot.py](benchmark_pilot.py) | Five tool scenarios and 15 seeded PR benchmark fixtures. |
