# Main agent Test-stage tools

The main Python tool broker now exposes the existing Test-stage verifier directly.
The adapter does not spawn agents or duplicate verification logic. The upstream
registry, preflight, pytest runner, RunManager, RCA, budgets and evidence validators
live in [`evo.test_stage`](../src/evo/test_stage/PROVENANCE.md). Main's Runtime still
owns all agent sessions and orchestration.

## Agent API

These are synchronous functions inside main's persistent Python REPL:

```python
t = test.target()                     # exact operator-approved request + budget
p = test.preflight(t)                 # readiness, blockers and next owner
v = test.verify(t)                    # terminal result, requirements, RCA and costs
m = test.manifest(v["run_id"])         # bound manifest, SHA-256 and execution ledger
a = test.artifact(v["run_id"], "initial/junit.xml")
e = artifacts.read(v["main_artifact_id"])
```

`test.verify` returns the strict upstream `VerifyOutput` plus `main_artifact_id`.
That main artifact contains the verification result, complete manifest, main run
ID and the approved Spec artifact ID. Original evidence remains in the verifier's
store; `test.artifact` validates the allowlist, size and checksum before returning
UTF-8 content. Reading historical evidence never makes an old SHA current.

The Test agent attaches `main_artifact_id`, `reviewed_head_sha` and `passed=True`
to its existing `workflow.handoff('qa', ...)`. Main's Workflow checks the original
evidence, imported bundle hash, requirement readiness and current pins before it
records `tested_commit`. Failed and blocked results remain available to the main
agent with their next responsible role. They cannot advance to QA as passing.

`READY_FOR_QA` is not approval. QA remains a separate stage. For a bound run,
transition to Ready additionally requires the existing verifier's named,
hash-bound **human** review; no approval tool is exposed to model Python. A team
deployment must connect its authenticated human review surface to that host API.

## Host setup

Bindings are installed after an approved Spec handoff and Code publication.
They cannot be created by `shared.set`, an agent role string or an `approved`
argument. All target fields and budget must exactly match the host binding.

```python
from evo.runtime import Runtime
from evo.test_stage.prime_tools import load_operator_tools
from evo.test_stage_bridge import RunBinding, TestStageBridge

# Operator-owned paths outside registered source checkouts. The registry already
# holds approved PR, spec, environment and test profile records.
tools = load_operator_tools(data_dir, policy_file)
bridge = TestStageBridge(tools)
bridge.bind(RunBinding(
    project_id=project_id, run_id=run_id,
    spec_artifact_id=approved_spec_artifact_id,
    request=pinned_verify_input,  # correlation_id must be "evo:" + run_id
))
runtime = Runtime(store, test_stage=bridge)
```

For CLI/server startup, `EVO_TEST_STAGE_CONFIG` can name an absolute, operator-owned
JSON file with `data_dir`, `policy_file`, and a `bindings` array of the same
`RunBinding` objects. All three locations must be outside the registered checkout.
The policy file contains the original `ToolPolicy` schema. Missing configuration
leaves the main application usable, but every Test tool call fails closed. Changing
Code, spec, profile, environment, policy or revision requires a new host-approved
binding; registration and approval are deliberately absent from agent tools.

The approved spec artifact must be a complete `SpecDocument` JSON, canonically
hash-identical to the registry and included in the current round's Spec -> Code
handoff. The registered source and main project must share the same Git common
directory, and the registered branch must resolve to the published main Code SHA.
The implementation checkout must be clean. One main host process owns a verifier
data directory; do not open it from an independent MCP or QA server concurrently.

## Permissions and deployment limits

The [permissions manifest](../schemas/test-stage-permissions-v1.json) describes
the enforced RPC rules. Caller identity comes from Runtime's host-bound RPC
closure and stored agent, run and project records. It is not supplied by the model.
Only the root coordinator and recorded Test/QA roles may call these tools.
Evidence is additionally scoped to the owning main run. Bound Test/QA roles cannot
call `shell.run`, repository write/patch/commit, or draft publishing through the
broker. The Test tools expose no shell, GitHub writes, merge, deploy or orchestration.

This is **not** a security sandbox for main's Python kernel. Main currently executes
model Python on the local account; code with that account's filesystem access can
bypass tool RPC. A hostile kernel needs a separate OS identity/process deployment
and protected host stores. RPC identity is host-bound, not a cryptographic per-call
credential. Do not treat this adaptation as solving main's unrestricted execution.

Untrusted repository tests require the original Docker backend: a prebuilt image
pinned by digest, no pulls or network, non-root user and resource limits. It fails
closed if unavailable. `local_trusted` is only for operator-reviewed trusted
fixtures and has no network sandbox. No new Docker runtime was exercised here.

Verifier compute/model budgets are separate from the existing main model API
budget. Repair and verifier model spend default to zero. Candidate validation
never marks the original PR approved. The optional evolver is not auto-activated.

## Reproduce verification

```powershell
uv sync --frozen
uv run pytest -v
uv run python scripts/test_stage_acceptance.py --output .evo/acceptance-unique
# Billable real main-model loop. Requires the team's valid provider configuration.
uv run python scripts/smoke_test_stage.py --live --output .evo/test-stage-live
```

The smoke script creates a tiny trusted repository, publishes Spec and Code via
main's workflow, then schedules the **actual root agent** with the actual provider.
It checks tool events, persistent REPL variables, two passing pytest cases, bound
manifest checksums and main artifact retrieval. It does not advance or approve QA.
Use `EVO_BASE_URL`, `EVO_API_STYLE` and `EVO_TEST_SMOKE_MODEL` for another provider.
Do not put credentials in CLI arguments or reports.

The unit integration test uses scripted model replies with the real Runtime,
REPL subprocess, RPC, pytest and artifact stores. This provides repeatable coverage
without conflating it with successful live API authentication. See
[`test_test_stage.py`](../tests/test_test_stage.py) for adversarial and handoff checks.

The five acceptance scenarios are the upstream harness with only imports and its
default output directory adapted. They cover passing verification and named human
review, separate candidate validation and fresh-head retest, environment/spec
blockers, budget exhaustion, idempotent retry and stale invalidation.
