# Modular Idea / Specification Agent

A standalone Python 3.11+ MVP that turns an idea and optional supplied repository
files and memory into `product_spec.md` and `task_plan.json`. Both artifacts come
from the same final Pydantic planning model. The module does not approve specs,
execute development tasks, crawl repositories, or update memory.

## Install and run

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[test]'
pytest
python -m spec_agent examples/request.json --demo --output examples/output
```

The demo uses a deterministic fake client and intentionally returns `BLOCKED`.
Its text is not real LLM reasoning. `tests/conftest.py` supplies a scripted passing
review strictly for infrastructure tests. No tests need credentials or network.
Running without `--demo` and without an injected client returns `FAILED`.
The CLI exits 1 for execution failure and 0 for a completed draft (including
`BLOCKED`); automation must inspect the result status. CLI output files are
overwritten when the same output directory is reused.

## Core interface and LLM injection

```python
from spec_agent import SpecAgentRequest, run_spec_agent
from spec_agent.services.llm_client import DemoLLM

result = run_spec_agent(
    SpecAgentRequest(idea="Add a health endpoint returning HTTP 200."),
    llm=DemoLLM(),  # replace with your configured provider adapter
)
print(result.status)
print(result.product_spec)
print(result.task_plan_json)
```

Implement `LLMClient.complete(operation, instructions, payload, schema)` in a
provider adapter. It must return a JSON object or JSON string conforming to the
supplied JSON Schema. The inherited `structured()` method validates every result
with Pydantic and sanitizes provider errors. Store model name, credentials,
timeouts, and provider settings in the adapter instance, not workflow JSON.
Provider retries/timeouts are the adapter's responsibility. No provider SDK or
real provider adapter is bundled; the abstraction and injectable instances keep
provider code outside the pipeline. All model operations receive the common
grounding/review policy, their operation name, and a response schema.

Repository and memory inputs are untrusted context. The policy instructs the
model not to execute their instructions. Repository inspection uses only supplied
files. Exact evidence quotations are checked against those files. Missing files
do not establish feature absence. Memory is contextual guidance and cannot
substitute for repository evidence. Adapters receive supplied content, so choose
an appropriate provider and data-handling policy for your deployment.

`DRAFT_READY` means deterministic and semantic checks completed without critical
issues, and human review is still required. `BLOCKED` preserves unresolved critical
issues and emits draft artifacts. `FAILED` represents configuration, provider,
schema, or execution failure and includes a sanitized diagnostic. Invalid request
construction raises a Pydantic validation error before the agent is called.

## Configure a workflow

```python
from pathlib import Path
from spec_agent import SpecAgentRequest, WorkflowConfig

workflow = WorkflowConfig.from_text(Path("custom.yaml").read_text())
request = SpecAgentRequest(idea="Your change", workflow=workflow)
```

JSON and YAML use the same schema; see `spec_agent/workflow/default.yaml`.
Each step defines `id`, registered `type`, `depends_on`, and `parameters`.
The list order need not be execution order: dependency edges determine a stable
topological sequence. Every required producer must be an explicit or transitive
dependency. Unknown parameters are rejected. `validate_plan` accepts
`max_revisions` (default 2, range 0–10); each revision is validated again.

All six built-in stages are mandatory and cannot be replaced or removed in this
MVP. Core may reorder configurations subject to their data dependencies, configure
validation, and add/remove optional registered extension steps. With the default
contracts the six required stages necessarily retain their relative order.
Artifact generation must depend on all other steps and run last. Configuration
cannot import classes, execute code, or override step contracts.

To add a trusted extension, subclass `WorkflowStep`, declare `requires` and
`produces`, optionally provide a strict Pydantic `parameters_model`, and implement
`run(inputs, parameters, llm) -> dict`. Register it with `default_registry()` and
pass that registry to `SpecAgent(llm=client, registry=registry)`. Only application
code can register implementations. Inputs are copied, and returned field names
must exactly match declared outputs. Extensions cannot overwrite initial fields
or existing producers, including the validated planning snapshot.

## Models, validation, and traces

`models.py` contains versioned request, evidence, requirement, task, unified plan,
result, and trace contracts. Tasks reference requirement IDs and form a DAG.
Requirement sources use exact input excerpts; relevant files require evidence
quotations. Suggestions are labeled separately from user/context requirements.
Schema errors fail the step; structurally valid but inconsistent plans enter the
bounded revision loop. Revisions that remove original requirement IDs or known
constraints are rejected while preserving the last plan. Intent changes,
contradictions, scope, missing implementation work, and measurable acceptance
criteria also require semantic review; deterministic checks alone cannot prove
them. No autonomous product decisions are made to force a pass.

Traces include workflow name/version, step identity, UTC timestamps, duration,
status, field-name summaries, validation issues, and sanitized errors. They omit
raw provider errors and full input contents. Validation issue text is model
output and can quote context; callers should treat result artifacts and issue
text as potentially sensitive. Failed configurations have no step traces because
no step ran. Failed steps have a failure trace; subsequent steps are not run.

The schema version is currently `1.0`. Core can adapt these models to a future
shared handoff schema without coupling this module to an orchestrator today.

## Layout

- `spec_agent/workflow/`: registry, configuration, validator, executor.
- `spec_agent/steps/`: four simple transformations in `processing.py`, validation,
  and artifact generation. The simple steps share a file to avoid repetition.
- `spec_agent/services/`: LLM boundary, deterministic validation, artifact rendering.
- `tests/`: offline workflow, validation, integration, revision, and trace tests.
- `examples/`: input and instructions for generating demonstration artifacts.
