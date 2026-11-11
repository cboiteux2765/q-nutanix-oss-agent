# SDLC Task Graph Tools

Python 3.11+ deterministic planning tools for a Prime Agent and dynamically chosen
subagents. Agents decide what to build; the library maintains task nodes and a
DAG of prerequisite edges. No model provider, agent initialization, credentials,
network, scheduler, or database is required.

## Install and test

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[test]'
pytest
python examples/task_graph_demo.py
```

## Python API

```python
from sdlc_tools.task_graph import Task, TaskGraph, load_graph

graph = TaskGraph(metadata={"requirements": [{"id": "REQ-001"}]})
graph.add_task(Task(
    id="TASK-001", title="Inspect authentication",
    description="Review existing authentication interfaces.",
    requirement_ids=["REQ-001"],
    acceptance_criteria=["Authentication interfaces are documented"],
))
graph.add_task(Task(
    id="TASK-002", title="Implement password reset",
    description="Implement the approved reset requirements.",
    requirement_ids=["REQ-001"],
    acceptance_criteria=["Approved password reset tests pass"],
))
graph.add_dependency("TASK-001", "TASK-002")
issues = graph.validate() + graph.validate_requirement_coverage(["REQ-001"])
assert not issues
snapshot = graph.export()       # JSON-compatible dictionary
restored = load_graph(graph.to_json())
assert restored.export() == snapshot
```

`A → B` means A must finish before B begins. Tasks have no dependency fields.
`get_task` and `list_tasks` return detached Task models. Dependency, ancestor,
descendant, root, leaf, and topological queries return task IDs. Query ordering
is deterministic for a given insertion order; topological order respects edges.
`remove_task` also removes incident edges. Task IDs cannot be updated.

Both object methods and functions are available: `create_graph`, `load_graph`,
`export_graph`, `clone_graph`, `add_task`, `update_task`, `remove_task`, `get_task`,
`list_tasks`, `add_dependency`, `remove_dependency`, `get_dependencies`,
`get_dependents`, `topological_sort`, `get_root_tasks`, `get_leaf_tasks`,
`get_ancestors`, `get_descendants`, `validate_graph`,
`validate_requirement_coverage`, and `apply_operations`.
Functions take the graph as their first argument, except create/load.

## Atomic changes and validation

```python
from sdlc_tools.task_graph import TaskGraphError

try:
    graph.apply_operations([
        {"operation": "update_task", "task_id": "TASK-002",
         "changes": {"title": "Implement and verify password reset"}},
        {"operation": "remove_dependency", "prerequisite_id": "TASK-001",
         "dependent_id": "TASK-002"},
    ], expected_version=graph.version, actor="planner")
except TaskGraphError as error:
    print(error.to_dict())  # code, message, details
```

Mutations return the new version. Each successful mutation or nonempty batch
increments the version once and appends a UTC history record with operation,
affected IDs, old/new versions, actor, and success. Empty batches do not change
anything. Failed operations leave the whole graph unchanged, including history.
Valid updates that repeat existing field values still count as mutations.
Duplicate edges and removing nonexistent edges are errors.

Batches apply in order on a private snapshot and commit once. Each intermediate
state must be structurally valid: create nodes before edges, remove old edges
before introducing a conflicting replacement. `expected_version` and an in-memory
lock prevent stale simultaneous updates on the same graph object. Clones are
independent; this is not distributed synchronization. Metadata is supplied at
creation/load and returned by copy; edit a snapshot and load a new graph to create
a separately revised metadata artifact.

Invalid fields, duplicate IDs, missing edge endpoints, self-dependencies, and
cycles are rejected on mutation/import with `TaskGraphError`. Invalid direct
`Task(...)` construction raises Pydantic's `ValidationError`, before a tool is
called. Tool dictionaries are validated at the boundary. Unknown fields and
schema versions are rejected; metadata must be JSON-compatible.

Incomplete draft tasks are allowed. `validate()` reports missing acceptance
criteria and requirement references. Coverage validation accepts a list of IDs,
requirement dictionaries, existing `Requirement` models, or `Requirements`, and
reports unknown references, duplicate requirements, and uncovered requirements.
It uses the explicitly supplied requirement list, not graph metadata. These
checks cannot establish semantic completeness or user approval.

## Planning skill and integration

Use [the task-planning skill](skills/task_planning/SKILL.md) to guide requirement
analysis, repository investigation, decomposition, validation, semantic review,
and handoff. It is a runtime-independent document, not a workflow engine.

`task_graph.metadata.TOOL_DEFINITIONS` describes each tool's input/output types,
permissions, and side effects. `PermissionChecker` is a minimal host integration
protocol. Metadata does **not** enforce permissions or create a security boundary;
the trusted host must enforce authorization before invoking tools. No arbitrary
Python executor or filesystem access is provided.

## Migration and preserved components

The old six-stage workflow mixed planning decisions with validation and artifact
production. The new primary interface is `sdlc_tools.task_graph`; the old fixed
pipeline remains available for compatibility and emits `DeprecationWarning` on
`SpecAgent` construction. Its existing behavior and tests are retained. See the
[legacy interface documentation](docs/legacy-spec-agent.md).

- Retained: `spec_agent.models` requirements, repository/evidence contracts,
  planning and trace models; existing evidence checks and Markdown/JSON renderer.
- Added: `sdlc_tools.specification` exports those reusable contracts,
  `validate_findings`, `render`, and `from_task_plan` without initializing agents.
- Refactored boundary: `from_task_plan(old_plan)` converts old `depends_on` lists
  into graph edges, preserves evidence in task metadata, and preserves requirement,
  constraint, assumption, risk, and status data in `metadata.legacy_plan`. The new
  graph starts at version 0; legacy description supplies its initial task title.
- Deprecated: the fixed pipeline entry point; its registry, executor, LLM adapter
  boundary, and stages remain compatibility code, not dependencies of graph tools.
- Separate schemas: the graph schema is not the legacy `task_plan.json` schema.
  Import old plans through the explicit adapter. The old renderer still accepts
  `PlanningModel`; a graph alone cannot reconstruct its idea/repository context.

Repository inspection previously used an injected model over supplied files; it
was not a standalone crawler. The deterministic evidence validator remains a
separate utility, and the planning agent chooses its own investigation tools.

## Layout

- `sdlc_tools/task_graph/`: contracts, transactional graph, validation, public
  functions, errors, discovery metadata and permission interface.
- `sdlc_tools/specification/`: retained utility exports and legacy plan adapter.
- `skills/task_planning/`: agent planning instructions.
- `spec_agent/`: preserved legacy implementation.
- `tests/`: offline graph tests and preserved legacy tests.
