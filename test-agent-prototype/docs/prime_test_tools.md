# Prime Agent Test-stage tools

The Python REPL calls four `evo-test` MCP tools: `test_preflight`,
`test_verify`, `test_manifest`, and `test_artifact`. The MCP server runs as a
separate operator-owned process. It reuses the local registered PR mirror,
approved spec, fixed test profile, preflight, `RunManager`, and checksum
verification. It never creates an agent or makes a QA decision.

**Caller-authentication limit:** the current stdio server binds a static
operator policy at startup; it does not verify a signed or Host-issued caller
identity on each request. A model can construct a matching `Principal` if it
can execute in the privileged process or reach its data with the same OS
permissions. Rejecting `role` or `approved` tool arguments does not repair
that gap. See the [strict audit](prime_tool_security_audit_20261110.md).

The interface is defined by [Pydantic models](../app/prime_tools.py), exported
[JSON Schemas](../schemas/prime-test-tools-v1.json) and the machine-readable
[permission manifest](../schemas/prime-test-permissions-v1.json).

## Operator setup

Install the tool package in the trusted process:

```powershell
python -m pip install -e .
```

MCP is a core dependency. The `qa` extra adds the optional human QA HTTP API;
the `dev` extra installs the full test dependencies. The old `[prime]` extra
remains an installation alias. Tool imports do not load the HTTP module.

Register an approved repository, specification, profile and environment with
the existing operator-only `SourceRegistry`. For the offline fixture, run
`python seed_demo.py --data-dir .evo-data`; the pinned input is written to
`test-target.json`. Live GitHub cloning and remote
registration are not part of this adapter.

Create an **operator-owned policy file outside the agent-writable repository**.
For a reviewed local demo fixture only, its contents can be:

```json
{
  "schema_version": "1.0",
  "operator_name": "evo-test-service",
  "allowed_repos": ["org/service"],
  "allowed_backends": ["local_trusted"],
  "max_runtime_seconds": 120,
  "max_test_executions": 4,
  "max_debug_iterations": 0,
  "max_model_spend_usd": 0,
  "max_artifact_bytes": 2097152
}
```

For real untrusted PRs, configure only `docker` and provision an operator-pinned
image and independent test provenance. The existing runner has no network in
Docker and refuses image pulls. `local_trusted` runs on the host and can use its
network; the example policy is appropriate only for the operator-owned demo.
The existing verification model marks untrusted test provenance INCONCLUSIVE
even if the containerized tests pass.

Set `EVO_TEST_DATA_DIR` and `EVO_TEST_POLICY_FILE` to absolute paths in the
operator's MCP process configuration. Start it with
`python -m app.prime_mcp` (or the installed `evo-test-tools` command). The process
fails closed if either setting or a valid operator policy is absent. Register
this process as Prime Agent's `evo-test` stdio MCP connection; its working
directory and Python environment must contain the installed prototype package.
Keep policy/data ACLs outside the Prime kernel's writable area. A same-user
Python REPL can import modules or read files directly: a Python API alone
cannot enforce a security boundary against it. Use a separate OS identity or
container with appropriate filesystem and network controls for a hostile agent
or repository.

## Pinned handoff

The upstream Code/Spec stages provide `target` with the registered repo and PR,
expected base/head Git SHA, approved spec ID/hash, profile ID/hash, environment
manifest ID/fingerprint, policy version and a correlation ID. These are
**expectations**, never sources of configuration. The server resolves its own
operator registry and rejects a mismatch before testing. `test_verify` also
accepts a bounded `budget`; the server intersects it with operator maxima.
The default tool budget proposes no repair and spends no model funds.

In a Prime Agent Python REPL, after the operator configures the connection:

```python
from rlm import mcp  # Prime Agent's MCP client; the plain `mcp` SDK is different

# handoff["test_target"] is the structured output of the approved prior stage.
target = handoff["test_target"]
check = await mcp.call_tool("evo-test", "test_preflight", {"target": target})
if check.structured_content["ready"]:
    run = await mcp.call_tool("evo-test", "test_verify", {
        "request": {
            **target,
            "budget": {
                "max_debug_iterations": 0,
                "max_test_executions": 4,
                "max_runtime_seconds": 120,
                "max_llm_cost_usd": 0
            }
        }
    })
    result = run.structured_content
    evidence = await mcp.call_tool("evo-test", "test_manifest",
                                   {"run_id": result["run_id"]})
    # Optional: fetch an allowlisted UTF-8 evidence file by manifest path.
    # await mcp.call_tool("evo-test", "test_artifact",
    #                     {"run_id": result["run_id"], "name": "initial/junit.xml"})
else:
    result = check.structured_content  # BLOCKED: send findings to the named owner.
```

The MCP response shape should be checked against the installed Prime/MCP SDK
when wiring the team environment. No model-supplied name, role, token, file
path, Git URL, command, test target, approval or backend appears in tool
arguments. `READY_FOR_QA` only routes evidence to human QA; a validated
candidate fix never approves the original PR. Evidence is copied out only
after manifest and per-file checksum verification, with a 2 MiB transfer cap.

`TestStageTools` is the trusted-side implementation for tests and other
operator-owned adapters. Do not instantiate it inside the model's REPL: the
model could construct a fake `Principal` in the same Python process. The
separate MCP process binds its identity at startup and exposes only the four
methods above. The service is single-process, consistent with the current
`RunManager` persistence and locking design. Never open the same store in
independent concurrent MCP and HTTP processes. The standalone human QA API
uses `uvicorn app.main:create_app --factory`; it provides no execution intake.

## Migration from the standalone Test Agent

Webhook intake, HTTP task creation/queuing/cancellation, the two legacy
Spec/Code context endpoints, PM notification callbacks and unused agent
adapter placeholders were removed. These orchestration responsibilities
belong to the Harness. The four tool names and pinned contracts remain stable.
Default `RunManager` instances have no repair provider. Regressions return a
`code_agent` next owner; optional candidate adapters require operator injection
and a nonzero authorized attempt budget. The offline demo enables its fixture
adapter explicitly, and keeps candidate validation separate from original-PR QA.

Input models revalidate existing instances, including objects produced through
Pydantic's unchecked `model_copy(update=...)`, before accessing the registry.
Old run stores remain readable; obsolete webhook delivery mappings are ignored.
Harness retries with the same correlation, versions and budget reuse the run,
including after a restart.

For an offline Python smoke test without Prime installed, run
`python -m scripts.prime_repl_example`. It executes the tool flow as
top-level-await cells in a persistent namespace against a real stdio MCP
subprocess. This checks protocol and variable flow, but it is not a live
Prime Agent session or proof of host-verified caller identity.
