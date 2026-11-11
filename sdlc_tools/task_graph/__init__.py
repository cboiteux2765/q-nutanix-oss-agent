"""Public Python tools; all graph queries return detached values.

Relation and ordering queries return task IDs. Mutations return the new version.
"""
from .errors import TaskGraphError
from .graph import TaskGraph
from .models import Task, Dependency, HistoryEntry, ValidationIssue
from .validation import validate_graph, validate_requirement_coverage


def create_graph(*, metadata=None):
    return TaskGraph(metadata=metadata)


def load_graph(data):
    return TaskGraph.load(data)


def export_graph(graph):
    return graph.export()


def clone_graph(graph):
    return graph.clone()


# Unbound methods provide the same explicit signatures as the object API.
add_task = TaskGraph.add_task
update_task = TaskGraph.update_task
remove_task = TaskGraph.remove_task
get_task = TaskGraph.get_task
list_tasks = TaskGraph.list_tasks
add_dependency = TaskGraph.add_dependency
remove_dependency = TaskGraph.remove_dependency
get_dependencies = TaskGraph.get_dependencies
get_dependents = TaskGraph.get_dependents
topological_sort = TaskGraph.topological_sort
get_root_tasks = TaskGraph.get_root_tasks
get_leaf_tasks = TaskGraph.get_leaf_tasks
get_ancestors = TaskGraph.get_ancestors
get_descendants = TaskGraph.get_descendants
apply_operations = TaskGraph.apply_operations

__all__ = [
    "TaskGraph", "Task", "Dependency", "HistoryEntry", "ValidationIssue", "TaskGraphError",
    "create_graph", "load_graph", "export_graph", "clone_graph", "add_task", "update_task",
    "remove_task", "get_task", "list_tasks", "add_dependency", "remove_dependency",
    "get_dependencies", "get_dependents", "topological_sort", "get_root_tasks",
    "get_leaf_tasks", "get_ancestors", "get_descendants", "validate_graph",
    "validate_requirement_coverage", "apply_operations",
]
