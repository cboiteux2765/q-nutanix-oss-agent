# EVO Test Agent Prototype

A runnable Python prototype of Phase 04, Verify and Test. It pins a local Git PR mirror and approved specification, verifies declared prerequisites, executes real pytest suites, diagnoses evidence-backed regressions, validates bounded candidate fixes, and produces evidence for named human QA review.

The offline acceptance demo uses actual Git commits and pytest subprocesses. Candidate proposals use a deterministic fixture adapter; no external GitHub, cloud, or model account is required. A passing candidate does not make the original PR ready. Only a new run on the incorporated PR head can do that.

## Quick start

Requirements: Python 3.11 or newer and Git available on PATH. Tested on Windows with Python 3.12. Create a project-local environment from this directory:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python demo_acceptance.py
python -m pytest -q
```

On macOS/Linux, activate with `source .venv/bin/activate`. `requirements.lock.txt` records the exact dependency versions used for verification; install it instead of `requirements.txt` for that environment. The suite and demo require local process/loopback permission for asyncio on Windows. They do not contact external services.

The demo prints five PASS lines and writes a new directory under `demo-output/` each time. It contains local Git fixture repositories, pinned snapshots, candidate branches, persisted state, all run artifacts, evolution events, and `acceptance_summary.json`. An explicit `--output PATH` must name a new directory, so existing evidence is not overwritten.

This package includes [validation results](VALIDATION.md) and a captured five-scenario run under `example-results/` for inspection. The captured artifacts use their original local run IDs; rerun the demo to create complete source snapshots and live API state on your own machine.

## Five acceptance scenarios

1. A clean PR passes both REQ-1 acceptance tests and receives a named, authorized human QA approval bound to the exact input and manifest hashes.
2. An empty-input regression fails on the head and passes on the base. A source-only candidate passes validation on a separate local branch. The original PR stays CHANGES_REQUIRED. An engineer incorporates the patch, then a fresh run on its new commit becomes READY_FOR_QA.
3. A missing declared sandbox prerequisite blocks before pytest or patching and routes to the environment owner.
4. Conflicting acceptance criteria block before execution and produce a targeted question for the spec owner.
5. Two failing patch attempts exhaust the repair allowance; a signed duplicate webhook returns the existing run; a later commit invalidates an earlier ready result and stale QA approval is rejected.

## Run the API locally

First register the included trusted fixture and generate the request payload:

```powershell
python seed_demo.py --data-dir .evo-data
$env:EVO_DATA_DIR = (Resolve-Path .evo-data).Path
$env:EVO_ENGINEER_TOKEN = python -c "import secrets; print(secrets.token_urlsafe(32))"
$env:EVO_QA_TOKEN = python -c "import secrets; print(secrets.token_urlsafe(32))"
$env:EVO_ENGINEER_NAME = "Local Engineer"
$env:EVO_QA_NAME = "Named QA Reviewer"
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
```

Start with one worker. The prototype's tasks, locks, and registry are process-local; it is not a distributed job service. Authentication fails closed when tokens are unset. Do not publish a development instance to the Internet.

In another terminal with the same token and data-dir variables:

```powershell
$headers = @{Authorization = "Bearer $env:EVO_ENGINEER_TOKEN"; "Idempotency-Key" = "manual-demo-1"}
$body = Get-Content .evo-data/create-request.json -Raw
$created = Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/v1/test-runs -Headers $headers -ContentType application/json -Body $body
$run = Invoke-RestMethod -Uri "http://127.0.0.1:8000$($created.status_uri)" -Headers $headers
$run
```

Poll the status URI until terminal. `/docs` contains the generated OpenAPI documentation. Copy `identity_hash` and `artifact_manifest_hash` from the completed result into a QA review request; authorize with the separate QA token. No merge or deployment endpoint exists.

The regular server starts with no configured model repair proposals. `--regression` seeds a failing PR for diagnosis; the offline demo injects a deterministic repair adapter explicitly. An operator must configure a cancellable async repair adapter before using an external model. Such an adapter declares an upper cost quote, observes its model-token ceiling, and never supplies a test verdict. The core reserves and conservatively charges that quote, including failed requests.

## Interfaces

| Route | Behavior |
| --- | --- |
| `POST /github/webhook` | HMAC verification, actor allowlist, delivery deduplication, eligible PR label/comment intake, registered-head stale invalidation. |
| `POST /v1/test-runs` | Authorized immutable input resolution and idempotent queueing; HTTP 202 returns run ID and status URI. |
| `GET /v1/test-runs/{id}` | Lifecycle, readiness, coverage, attempts, time, cost, reasons, and hashes. |
| `GET /v1/test-runs/{id}/artifacts` | Versioned evidence manifest. |
| `GET /v1/test-runs/{id}/artifacts/{name}` | Authorized manifest-allowlisted artifact with checksum verification, including nested JUnit/log paths. |
| `POST /v1/test-runs/{id}/cancel` | Cancels queued work or signals active test/model work; retains partial evidence. |
| `POST /v1/test-runs/{id}/review` | Authorized human APPROVED/REJECTED decision bound to the exact current identity and manifest. |
| `POST /v1/test-runs/{id}/feedback` | Code Agent receipt, RCA correction, and patch review annotations. |
| `GET /listenToSpecAndCodeAgents` | Registered context retrieval using query parameters; returns a caller-bound context ID. |
| `POST /verifyAndJudgeReadiness` | Compatibility adapter with required context ID, configured profile and bounded compute budget; returns the requested legacy fields plus status link and human-review requirement. |

Set `GITHUB_WEBHOOK_SECRET` and `EVO_WEBHOOK_SENDERS` for webhook testing. `EVO_ALLOWED_REPOS` scopes environment-token identities (comma separated); it does not register new repositories. Send `test:verify` labels or the exact `/test-agent run` PR comment. A synchronize event only invalidates earlier results after its head matches the operator-updated registered mirror. Payload URLs, commands, secrets, and specification text never become trusted configuration.

The legacy GET does not take a JSON request body. The POST requires `context_id` to prevent cross-user or cross-PR mixing; `pytest_suite` is an approved profile ID, not arbitrary shell text. Its boolean is exactly `readiness == READY_FOR_QA`, as requested. It expresses automated evidence readiness for human QA, not release approval. A QA rejection changes readiness to CHANGES_REQUIRED. Human QA is still mandatory even when the compatibility boolean is true.

## State, budgets, and evidence

The normal lifecycle follows QUEUED → PREFLIGHT → TESTING → DIAGNOSING → REPAIRING → COMPLETED. REPAIRING is an explicit no-op for a clean run. Blockers, cancellation, and service errors terminate early as BLOCKED, CANCELLED, and ERROR. Readiness is independent: READY_FOR_QA, CHANGES_REQUIRED, BLOCKED, INCONCLUSIVE, or STALE.

Pilot hard maxima are 2 repair attempts, 4 pytest invocations, 1,200 seconds of active work, and US$2.00 reserved model spend. Inputs may lower these limits. Initial, base, flaky-rerun, and candidate calls all count. A candidate cannot be proposed without a remaining validation slot. The last allowed attempt still receives its reserved validation run. Test deadlines terminate the process tree; cancellation races model calls and terminates active test jobs. Candidate Git commands have bounded timeouts and check the remaining deadline. Queue and phase timings are recorded separately. Local CI cost is left null because no hosted compute bill is available; fixture model spend is zero.

Every accepted run, including blocked and cancelled runs, persists:

- `verification.json`, validated against version 1.0 and the READY_FOR_QA invariants.
- `test_report.md`, with customer intent, scope, findings, coverage, original-PR totals, executed commands, RCA, candidate details, risks, next owner, and costs.
- `evidence_manifest.json`, with identities, SHA-256 checksums, JUnit/log references, local workflow/job IDs, and reproduction instructions.

The manifest covers immutable execution evidence and the current report. It excludes itself and the mutable verification/review envelope to avoid circular hashes. Serving or approving the verification envelope compares it with trusted persisted run state. Modified evidence is refused. When readiness becomes stale or QA rejects a version, prior envelopes and reports are retained under `audit/` before updated summaries are published. Checksums provide modification detection, not cryptographic attestation against a hostile operator with write access to all storage.

## Isolation and current boundaries

`local_trusted` executes real Python on the host and is allowed only for explicitly trusted operator-owned fixtures. It is not a security sandbox. Use the Docker backend for untrusted project execution. The implemented Docker command disables networking and pulls, pins an image digest, removes capabilities, runs without root, and caps CPU, memory, PIDs, and runtime. Prebuild the image with Python, pytest and required dependencies. On Linux, provision scratch/evidence mounts writable by UID 65534. Docker execution was not integration tested in this environment.

The operator trust flag also represents reviewed test provenance. Container isolation does not prove that adversarial PR tests are honest: production needs an independently approved test harness or test attestation. Untrusted results remain INCONCLUSIVE. The runner never receives service credentials, rejects unsafe paths and source links, checks JUnit consistency, and redacts logs. These controls do not replace OS/container isolation or a production threat review.

GitHub integration in this prototype is an authenticated webhook/API boundary over an operator-registered local Git mirror. It does not clone remote repositories, push fixes, create actual GitHub draft PRs, or merge/deploy. Candidate commits and branches are real local Git objects with `LOCAL_DRAFT_ONLY` publication status. A production GitHub App publisher and remote artifact resolver remain extension work. Prerequisite availability is operator-provided declared state; live cloud probes require their own read-only adapter. Snapshot, source-collection, and artifact-persistence overhead is not a hard real-time service guarantee.

Restart recovery preserves terminal runs and marks interrupted work ERROR/INCONCLUSIVE; explicit retry creates a new run with `parent_run_id`. No worker resumes an interrupted repair invisibly. `state.json` and artifact storage are operator-controlled local files; multi-process transactions, remote queues, authenticated durable storage, retention cleanup, and signed audit logging are outside the prototype.

## Non-parametric learning and research

`evolution_memory.py` records redacted run, review, and feedback events. It exports reviewed examples for external offline GEPA evaluation and can produce a reviewed promotion plan requiring held-out evidence, a named approver, a canary percentage and rollback version. It never trains a model, runs GEPA, modifies SKILL.md, or activates a policy. A-EVOLVE, FlashEvolve, AlphaEvolve, and the EVO vision inform the separation of proposal, deterministic evaluation, versioning, and persistent feedback; their published speedups are not claimed for this prototype. See [research notes](docs/research_notes.md).

The proposed 12–20 hours of weekly savings remains a product hypothesis, not a demonstrated benchmark. This package demonstrates functional behavior and control checks, not measured productivity or deployment readiness.

## Repository structure

```text
app/main.py                 authenticated API and legacy adapters
app/models.py               versioned contracts and invariant validation
app/providers.py            operator registry, immutable Git snapshots, demo fixtures
app/run_manager.py          lifecycle, budgets, cancellation, evidence, QA, recovery
app/preflight.py            deterministic spec, environment, and profile checks
app/test_runner.py          bounded pytest execution and evidence mapping
app/rca_and_repair.py        deterministic classification and proposal-only interfaces
app/evolution_memory.py     redacted feedback and offline promotion plans
demo_acceptance.py          five executable end-to-end acceptance scenarios
seed_demo.py                optional local API seed
tests/                      unit, security-boundary, and real integration checks
```
