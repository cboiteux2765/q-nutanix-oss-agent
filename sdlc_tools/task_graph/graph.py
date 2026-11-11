"""Copy-on-write task graph with transactional, versioned mutations."""
import json
from datetime import datetime, timezone
from threading import RLock

from pydantic import ValidationError

from .errors import TaskGraphError
from .models import Dependency, GraphData, HistoryEntry, Task
from .validation import check_structure, order, validate_graph, validate_requirement_coverage


def parse_data(data) -> GraphData:
    try:
        # Validate in JSON mode so exported ISO timestamps round-trip in strict models.
        if isinstance(data, str):
            data = json.loads(data)
        data = json.dumps(data, allow_nan=False)
        parsed = GraphData.model_validate_json(data)
        check_structure(parsed)
        return parsed
    except (ValidationError, ValueError, TypeError) as exc:
        raise TaskGraphError("INVALID_GRAPH", "Invalid graph serialization.", reason=str(exc)) from exc


class TaskGraph:
    def __init__(self, *, metadata=None):
        self._lock = RLock()
        self._data = parse_data({"metadata": {} if metadata is None else metadata})

    @classmethod
    def load(cls, data):
        graph = cls()
        graph._data = parse_data(data)
        return graph

    def _snapshot(self):
        with self._lock:
            return self._data.model_copy(deep=True)

    @property
    def version(self):
        return self._snapshot().version

    @property
    def metadata(self):
        return self._snapshot().metadata

    @property
    def history(self):
        return self._snapshot().history

    @property
    def dependencies(self):
        return self._snapshot().dependencies

    def export(self):
        return self._snapshot().model_dump(mode="json")

    def to_json(self):
        return json.dumps(self.export(), indent=2, ensure_ascii=False, allow_nan=False)

    def clone(self):
        return type(self).load(self.export())

    def list_tasks(self):
        return self._snapshot().tasks

    def get_task(self, task_id):
        for task in self.list_tasks():
            if task.id == task_id:
                return task
        raise TaskGraphError("TASK_NOT_FOUND", "Task does not exist.", task_id=task_id)

    def apply_operations(self, operations, expected_version=None, *, actor=None):
        """Commit a nonempty batch once; every intermediate operation must be valid.

        Only successful commits enter history. Failed attempts raise structured
        errors and leave every part of the graph unchanged, including history.
        """
        with self._lock:
            if expected_version is not None and (type(expected_version) is not int or expected_version != self._data.version):
                raise TaskGraphError("VERSION_CONFLICT", "Graph version does not match.", expected=expected_version, actual=self._data.version)
            if not isinstance(operations, list):
                raise TaskGraphError("VALIDATION_FAILED", "Operations must be a list.")
            if not operations:
                return self.version
            proposed = self._data.model_dump(mode="python")
            affected = []
            names = []
            try:
                for operation in operations:
                    names.append(self._apply(proposed, operation, affected))
                    check_structure(GraphData.model_validate(proposed))
                version = self._data.version + 1
                proposed["version"] = version
                proposed["history"].append(HistoryEntry(
                    operation=names[0] if len(names) == 1 else "batch",
                    task_ids=list(dict.fromkeys(affected)), previous_version=self._data.version,
                    new_version=version, timestamp=datetime.now(timezone.utc), actor=actor).model_dump())
                candidate = GraphData.model_validate(proposed)
                # JSON validation also rejects non-serializable metadata and nonfinite numbers.
                candidate = parse_data(candidate.model_dump(mode="json"))
            except (ValidationError, ValueError, TypeError) as exc:
                raise TaskGraphError("VALIDATION_FAILED", "Invalid operation input.", reason=str(exc)) from exc
            self._data = candidate
            return version

    @staticmethod
    def _apply(data, op, affected):
        fields = {
            "add_task": {"task"}, "update_task": {"task_id", "changes"},
            "remove_task": {"task_id"},
            "add_dependency": {"prerequisite_id", "dependent_id"},
            "remove_dependency": {"prerequisite_id", "dependent_id"},
        }
        if not isinstance(op, dict) or not isinstance(op.get("operation"), str) or op["operation"] not in fields:
            raise TaskGraphError("VALIDATION_FAILED", "Unknown operation.")
        name = op["operation"]
        if set(op) != fields[name] | {"operation"}:
            raise TaskGraphError("VALIDATION_FAILED", "Unexpected or missing operation fields.", operation=name)
        tasks = {t["id"]: t for t in data["tasks"]}

        def require(id_):
            if not isinstance(id_, str) or id_ not in tasks:
                raise TaskGraphError("TASK_NOT_FOUND", "Task does not exist.", task_id=id_)

        if name == "add_task":
            raw = op["task"].model_dump() if isinstance(op["task"], Task) else op["task"]
            task = Task.model_validate(raw)
            if task.id in tasks:
                raise TaskGraphError("DUPLICATE_TASK", "Task ID already exists.", task_id=task.id)
            data["tasks"].append(task.model_dump())
            affected.append(task.id)
        elif name in {"update_task", "remove_task"}:
            id_ = op["task_id"]
            require(id_)
            affected.append(id_)
            if name == "update_task":
                changes = op["changes"]
                if not isinstance(changes, dict) or "id" in changes:
                    raise TaskGraphError("VALIDATION_FAILED", "Changes must be a dictionary; task IDs are immutable.")
                updated = Task.model_validate({**tasks[id_], **changes}).model_dump()
                data["tasks"] = [updated if t["id"] == id_ else t for t in data["tasks"]]
            else:
                data["tasks"] = [t for t in data["tasks"] if t["id"] != id_]
                for edge in data["dependencies"]:
                    if id_ in edge.values():
                        affected.extend(edge.values())
                data["dependencies"] = [e for e in data["dependencies"] if id_ not in e.values()]
        else:
            edge = Dependency.model_validate({k: op[k] for k in fields[name]}).model_dump()
            for id_ in edge.values():
                require(id_)
                affected.append(id_)
            if name == "add_dependency":
                data["dependencies"].append(edge)
            else:
                if edge not in data["dependencies"]:
                    raise TaskGraphError("INVALID_DEPENDENCY", "Dependency does not exist.")
                data["dependencies"].remove(edge)
        return name

    def add_task(self, task, **options):
        return self.apply_operations([{"operation": "add_task", "task": task}], **options)

    def update_task(self, task_id, changes, **options):
        return self.apply_operations([{"operation": "update_task", "task_id": task_id, "changes": changes}], **options)

    def remove_task(self, task_id, **options):
        return self.apply_operations([{"operation": "remove_task", "task_id": task_id}], **options)

    def add_dependency(self, prerequisite_id, dependent_id, **options):
        return self.apply_operations([dict(operation="add_dependency", prerequisite_id=prerequisite_id, dependent_id=dependent_id)], **options)

    def remove_dependency(self, prerequisite_id, dependent_id, **options):
        return self.apply_operations([dict(operation="remove_dependency", prerequisite_id=prerequisite_id, dependent_id=dependent_id)], **options)

    def _related(self, task_id, *, reverse=False, transitive=False):
        data = self._snapshot()
        if task_id not in {t.id for t in data.tasks}:
            raise TaskGraphError("TASK_NOT_FOUND", "Task does not exist.", task_id=task_id)
        adjacency = {t.id: [] for t in data.tasks}
        for e in data.dependencies:
            a, b = e.prerequisite_id, e.dependent_id
            if reverse:
                a, b = b, a
            adjacency[a].append(b)
        found, pending = set(), list(adjacency[task_id])
        while pending:
            current = pending.pop()
            if current not in found:
                found.add(current)
                if transitive:
                    pending.extend(adjacency[current])
        return [t.id for t in data.tasks if t.id in found]

    def get_dependencies(self, task_id):
        return self._related(task_id, reverse=True)

    def get_dependents(self, task_id):
        return self._related(task_id)

    def get_ancestors(self, task_id):
        return self._related(task_id, reverse=True, transitive=True)

    def get_descendants(self, task_id):
        return self._related(task_id, transitive=True)

    def topological_sort(self):
        return order(self._snapshot())

    def get_root_tasks(self):
        data = self._snapshot()
        dependent = {e.dependent_id for e in data.dependencies}
        return [t.id for t in data.tasks if t.id not in dependent]

    def get_leaf_tasks(self):
        data = self._snapshot()
        prerequisite = {e.prerequisite_id for e in data.dependencies}
        return [t.id for t in data.tasks if t.id not in prerequisite]

    def validate(self):
        return validate_graph(self)

    def validate_requirement_coverage(self, requirements):
        return validate_requirement_coverage(self, requirements)
