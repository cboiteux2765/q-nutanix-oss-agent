"""Offline example: python examples/task_graph_demo.py after installing the package."""
from sdlc_tools.task_graph import Task, TaskGraph


def main():
    graph = TaskGraph(metadata={"requirements": [{"id": "R1", "description": "Health endpoint"}]})
    graph.apply_operations([
        {"operation": "add_task", "task": Task(
            id="T1", title="Implement health endpoint", description="Add GET /health using existing routing.",
            requirement_ids=["R1"], acceptance_criteria=["GET /health returns 200"])},
        {"operation": "add_task", "task": Task(
            id="T2", title="Verify health endpoint", description="Add an integration test.",
            requirement_ids=["R1"], acceptance_criteria=["Endpoint integration test passes"])},
        {"operation": "add_dependency", "prerequisite_id": "T1", "dependent_id": "T2"},
    ], expected_version=0, actor="example")
    assert not graph.validate()
    assert not graph.validate_requirement_coverage(["R1"])
    print(graph.to_json())


if __name__ == "__main__":
    main()
