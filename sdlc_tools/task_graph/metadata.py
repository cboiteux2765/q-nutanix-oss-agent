"""Discovery metadata only, NOT a security boundary.

Hosts must enforce permissions before invoking Python tools. The protocol is an
integration point, not an executor or sandbox. Tools never access the filesystem.
"""
from typing import Protocol
from pydantic import Field
from .models import Model


class ToolDefinition(Model):
    name: str
    description: str
    input_types: dict[str, str]
    output_type: str
    required_permissions: list[str] = Field(default_factory=list)
    side_effects: bool = False


class PermissionChecker(Protocol):
    def require(self, actor: str, permissions: list[str]) -> None:
        """Raise before invocation if the trusted host denies permission."""
        ...


_SPECS = {
    "create_graph": ("Create an empty graph.", {"metadata": "dict | None"}, "TaskGraph"),
    "load_graph": ("Validate and load graph data.", {"data": "dict | str"}, "TaskGraph"),
    "export_graph": ("Export a detached JSON-compatible snapshot.", {}, "dict"),
    "clone_graph": ("Clone a graph including version and history.", {}, "TaskGraph"),
    "add_task": ("Add a unique task.", {"task": "Task | dict"}, "int"),
    "update_task": ("Update fields of an existing task.", {"task_id": "str", "changes": "dict"}, "int"),
    "remove_task": ("Remove a task and its incident edges.", {"task_id": "str"}, "int"),
    "get_task": ("Get a detached task.", {"task_id": "str"}, "Task"),
    "list_tasks": ("List detached tasks in insertion order.", {}, "list[Task]"),
    "add_dependency": ("Add a prerequisite edge without introducing a cycle.", {"prerequisite_id": "str", "dependent_id": "str"}, "int"),
    "remove_dependency": ("Remove an existing prerequisite edge.", {"prerequisite_id": "str", "dependent_id": "str"}, "int"),
    "get_dependencies": ("Get immediate prerequisite IDs.", {"task_id": "str"}, "list[str]"),
    "get_dependents": ("Get immediate dependent IDs.", {"task_id": "str"}, "list[str]"),
    "get_ancestors": ("Get transitive prerequisite IDs.", {"task_id": "str"}, "list[str]"),
    "get_descendants": ("Get transitive dependent IDs.", {"task_id": "str"}, "list[str]"),
    "topological_sort": ("Order prerequisites before dependents.", {}, "list[str]"),
    "get_root_tasks": ("Get IDs with no prerequisites.", {}, "list[str]"),
    "get_leaf_tasks": ("Get IDs with no dependents.", {}, "list[str]"),
    "validate_graph": ("Check structure and planning completeness.", {}, "list[ValidationIssue]"),
    "validate_requirement_coverage": ("Check task requirement references and coverage.", {"requirements": "list[str | Requirement | dict] | Requirements"}, "list[ValidationIssue]"),
    "apply_operations": ("Commit an ordered batch atomically.", {"operations": "list[dict]"}, "int"),
}
_MUTATIONS = {"add_task", "update_task", "remove_task", "add_dependency", "remove_dependency", "apply_operations"}
TOOL_DEFINITIONS = tuple(
    ToolDefinition(
        name=f"task_graph.{name}", description=description,
        input_types={**({"graph": "TaskGraph"} if name not in {"create_graph", "load_graph"} else {}),
                     **inputs, **({"expected_version": "int | None", "actor": "str | None"} if name in _MUTATIONS else {})},
        output_type=output, required_permissions=["task_graph.write" if name in _MUTATIONS else "task_graph.read"],
        side_effects=name in _MUTATIONS,
    ) for name, (description, inputs, output) in _SPECS.items()
)
