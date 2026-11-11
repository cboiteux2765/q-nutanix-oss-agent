import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

from sdlc_tools.task_graph import Task, TaskGraph, TaskGraphError, create_graph, load_graph
from sdlc_tools.task_graph.metadata import TOOL_DEFINITIONS
from sdlc_tools.specification import from_task_plan


def task(id_):
    return Task(id=id_, title=id_, description="Implement " + id_,
                requirement_ids=["R1"], acceptance_criteria=["Tests pass"], metadata={"nested": []})


def graph():
    g = TaskGraph()
    for id_ in "ABCD":
        g.add_task(task(id_))
    g.add_dependency("A", "B")
    g.add_dependency("B", "C")
    return g


def test_empty():
    g = create_graph()
    assert g.version == 0
    assert g.list_tasks() == g.dependencies == g.history == g.validate() == []
    assert g.topological_sort() == g.get_root_tasks() == g.get_leaf_tasks() == []


def test_crud_and_detached_values():
    g = TaskGraph(metadata={"nested": []})
    original = task("A")
    g.add_task(original, actor="planner", expected_version=0)
    original.metadata["nested"].append("external")
    g.get_task("A").metadata["nested"].append("external")
    g.metadata["nested"].append("external")
    assert g.get_task("A").metadata == {"nested": []}
    assert g.metadata == {"nested": []}
    g.update_task("A", {"title": "New"})
    assert g.get_task("A").title == "New"
    assert g.version == 2
    assert g.history[0].actor == "planner"
    assert g.history[0].timestamp.tzinfo is not None
    g.remove_task("A")
    assert not g.list_tasks()
    assert g.version == 3


def test_queries_and_edge_removal():
    g = graph()
    assert g.get_dependencies("C") == ["B"]
    assert g.get_dependents("A") == ["B"]
    assert g.get_ancestors("C") == ["A", "B"]
    assert g.get_descendants("A") == ["B", "C"]
    assert g.get_root_tasks() == ["A", "D"]
    assert g.get_leaf_tasks() == ["C", "D"]
    ordered = g.topological_sort()
    assert ordered.index("A") < ordered.index("B") < ordered.index("C")
    g.remove_dependency("A", "B")
    assert g.get_dependencies("B") == []
    g.remove_task("B")
    assert g.dependencies == []
    assert set(g.history[-1].task_ids) == {"B", "C"}


@pytest.mark.parametrize("operation,code", [
    (lambda g: g.add_task(task("A")), "DUPLICATE_TASK"),
    (lambda g: g.add_dependency("A", "missing"), "TASK_NOT_FOUND"),
    (lambda g: g.add_dependency("A", "A"), "INVALID_DEPENDENCY"),
    (lambda g: g.add_dependency("A", "B"), "INVALID_DEPENDENCY"),
    (lambda g: g.add_dependency("C", "A"), "CYCLE_DETECTED"),
    (lambda g: g.remove_dependency("A", "D"), "INVALID_DEPENDENCY"),
    (lambda g: g.update_task("A", {"id": "other"}), "VALIDATION_FAILED"),
    (lambda g: g.update_task("A", {"title": " "}), "VALIDATION_FAILED"),
    (lambda g: g.update_task("A", {"unknown": True}), "VALIDATION_FAILED"),
    (lambda g: g.update_task("A", {"acceptance_criteria": [1]}), "VALIDATION_FAILED"),
    (lambda g: g.remove_task("missing"), "TASK_NOT_FOUND"),
    (lambda g: g.add_task({"id": "E"}), "VALIDATION_FAILED"),
    (lambda g: g.add_task(task("E"), actor=" "), "VALIDATION_FAILED"),
    (lambda g: g.add_task(task("E"), expected_version=0), "VERSION_CONFLICT"),
    (lambda g: g.apply_operations([{"operation": "execute_python"}]), "VALIDATION_FAILED"),
    (lambda g: g.apply_operations([{"operation": "add_task"}]), "VALIDATION_FAILED"),
    (lambda g: g.apply_operations("wrong"), "VALIDATION_FAILED"),
])
def test_failed_operations_atomic(operation, code):
    g = graph()
    before = g.export()
    with pytest.raises(TaskGraphError) as exc:
        operation(g)
    assert exc.value.to_dict()["code"] == code
    assert g.export() == before


def test_batches():
    g = TaskGraph()
    ops = [{"operation": "add_task", "task": task(id_)} for id_ in "AB"]
    ops.append(dict(operation="add_dependency", prerequisite_id="A", dependent_id="B"))
    assert g.apply_operations(ops, expected_version=0) == 1
    assert g.history[0].operation == "batch"
    assert g.history[0].task_ids == ["A", "B"]
    before = g.export()
    with pytest.raises(TaskGraphError):
        g.apply_operations([{"operation": "update_task", "task_id": "A", "changes": {"title": "Changed"}},
                            dict(operation="add_dependency", prerequisite_id="B", dependent_id="A")])
    assert g.export() == before
    assert g.apply_operations([]) == 1
    assert g.export() == before


def test_concurrent_version_check():
    g = TaskGraph()
    def add(id_):
        try:
            g.add_task(task(id_), expected_version=0)
            return "ok"
        except TaskGraphError as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(add, "AB")) == ["VERSION_CONFLICT", "ok"]
    assert g.version == 1


def test_validation_and_coverage():
    g = TaskGraph()
    g.add_task(Task(id="A", title="A", description="A"))
    assert {i.code for i in g.validate()} == {"MISSING_ACCEPTANCE_CRITERIA", "MISSING_REQUIREMENT_REFERENCE"}
    g.update_task("A", {"requirement_ids": ["R1", "unknown"]})
    issues = g.validate_requirement_coverage([{"id": "R1"}, {"id": "R2"}, {"id": "R2"}])
    assert {i.code for i in issues} == {"UNKNOWN_REQUIREMENT", "MISSING_COVERAGE", "DUPLICATE_REQUIREMENT"}
    g.update_task("A", {"requirement_ids": ["R1"], "acceptance_criteria": ["Verified"]})
    assert g.validate() == g.validate_requirement_coverage(["R1"]) == []


@pytest.mark.parametrize("requirements", ["R1", {}, [None], [" "], [{"id": 1}]])
def test_invalid_requirements(requirements):
    with pytest.raises(TaskGraphError, match="Requirement"):
        TaskGraph().validate_requirement_coverage(requirements)


def test_serialization_clone_and_version():
    g = graph()
    assert load_graph(g.to_json()).export() == load_graph(g.export()).export() == g.export()
    clone = g.clone()
    clone.remove_task("A")
    assert g.get_task("A")
    assert clone.version == g.version + 1
    assert json.loads(g.to_json()) == g.export()


@pytest.mark.parametrize("data,code", [
    ("not json", "INVALID_GRAPH"), ([], "INVALID_GRAPH"),
    ({"version": -1}, "INVALID_GRAPH"), ({"version": "1"}, "INVALID_GRAPH"),
    ({"schema_version": "2.0"}, "INVALID_GRAPH"), ({"surprise": True}, "INVALID_GRAPH"),
    ({"metadata": {"nan": float("nan")}}, "INVALID_GRAPH"),
    ('{"metadata": {"number": NaN}}', "INVALID_GRAPH"),
    ('{"metadata": {"number": Infinity}}', "INVALID_GRAPH"),
    ('{"metadata": {"number": 1e999}}', "INVALID_GRAPH"),
    ({"tasks": [task("A").model_dump()] * 2}, "DUPLICATE_TASK"),
    ({"dependencies": [{"prerequisite_id": "A", "dependent_id": "B"}]}, "TASK_NOT_FOUND"),
])
def test_invalid_serialization(data, code):
    with pytest.raises(TaskGraphError) as exc:
        load_graph(data)
    assert exc.value.code == code


def test_invalid_serialized_cycle_and_history():
    data = graph().export()
    data["dependencies"].append(dict(prerequisite_id="C", dependent_id="A"))
    with pytest.raises(TaskGraphError) as exc:
        load_graph(data)
    assert exc.value.code == "CYCLE_DETECTED"
    data = graph().export()
    data["history"][0]["new_version"] = 50
    with pytest.raises(TaskGraphError) as exc:
        load_graph(data)
    assert exc.value.code == "INVALID_GRAPH"


def test_missing_queries():
    for method in ["get_task", "get_dependencies", "get_dependents", "get_ancestors", "get_descendants"]:
        with pytest.raises(TaskGraphError) as exc:
            getattr(TaskGraph(), method)("unknown")
        assert exc.value.code == "TASK_NOT_FOUND"


def test_legacy_conversion():
    from spec_agent.models import Requirement, Task as LegacyTask, TaskPlan
    req = Requirement(id="R1", description="Feature", origin="user", source="Feature", acceptance_criteria=["Works"])
    plan = TaskPlan(requirements=[req], tasks=[
        LegacyTask(id="A", description="First", requirement_ids=["R1"], acceptance_criteria=["Works"]),
        LegacyTask(id="B", description="Second", requirement_ids=["R1"], acceptance_criteria=["Works"], depends_on=["A"]),
    ], constraints=["Keep API"])
    g = from_task_plan(plan)
    assert g.get_dependencies("B") == ["A"]
    assert "depends_on" not in g.get_task("B").model_dump()
    assert g.metadata["legacy_plan"]["constraints"] == ["Keep API"]
    assert g.validate_requirement_coverage(plan.requirements) == []


def test_standalone_import_and_tool_catalog():
    subprocess.run([sys.executable, "-c", "import sys; from sdlc_tools.task_graph import TaskGraph; assert TaskGraph().version == 0; assert 'spec_agent' not in sys.modules"], check=True)
    import sdlc_tools.task_graph as api
    for definition in TOOL_DEFINITIONS:
        assert callable(getattr(api, definition.name.split(".")[1]))
        assert definition.input_types is not None
        assert definition.required_permissions
