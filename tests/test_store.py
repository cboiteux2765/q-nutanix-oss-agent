import pytest

from evo.store import Store


def test_put_merges_and_keeps_created_at(store: Store):
    first = store.put("things", "a", {"x": 1, "y": 2})
    second = store.put("things", "a", {"y": 3})
    assert second == first | {"y": 3, "updated_at": second["updated_at"]}
    assert store.get("things", "a")["created_at"] == first["created_at"]


def test_list_filters_by_fields(store: Store):
    store.put("agents", "a1", {"run_id": "r1", "status": "running", "parent_id": None})
    store.put("agents", "a2", {"run_id": "r1", "status": "completed", "parent_id": "a1"})
    store.put("agents", "a3", {"run_id": "r2", "status": "running", "parent_id": "a1"})
    assert [a["id"] for a in store.list("agents")] == ["a1", "a2", "a3"]
    assert [a["id"] for a in store.list("agents", run_id="r1")] == ["a1", "a2"]
    assert [a["id"] for a in store.list("agents", run_id="r1", status="running")] == ["a1"]
    assert [a["id"] for a in store.list("agents", parent_id="a1")] == ["a2", "a3"]
    assert [a["id"] for a in store.list("agents", parent_id=None)] == ["a1"]
    assert store.list("agents", run_id="missing") == []


def test_list_filters_booleans_and_numbers(store: Store):
    store.put("things", "a", {"flag": True, "n": 2})
    store.put("things", "b", {"flag": False, "n": 3})
    assert [t["id"] for t in store.list("things", flag=True)] == ["a"]
    assert [t["id"] for t in store.list("things", n=3)] == ["b"]


def test_events_and_project_messages(store: Store):
    store.put("runs", "r1", {"project_id": "p1", "root_agent_id": "root"})
    store.event("r1", "root", "message", {"role": "user", "text": "hi", "visible": True})
    store.event("r1", "root", "message", {"role": "assistant", "text": "thinking", "visible": False})
    store.event("r1", "child", "message", {"role": "assistant", "text": "child", "visible": True})
    store.event("r1", "root", "message", {"role": "assistant", "text": "hello", "visible": True})
    assert [e["data"]["text"] for e in store.project_messages("p1")] == ["hi", "hello"]
    assert len(store.events("r1")) == 4
    assert len(store.events("r1", agent_id="child")) == 1


def test_shared_values_are_versioned(store: Store):
    assert store.shared_set("run:r1", "k", 1)["version"] == 1
    assert store.shared_set("run:r1", "k", 2, expected_version=1)["version"] == 2
    with pytest.raises(ValueError, match="conflict"):
        store.shared_set("run:r1", "k", 3, expected_version=1)
    assert store.shared_get("run:r1", "k")["value"] == 2


def test_search_history(store: Store):
    store.put("runs", "r1", {"project_id": "p1"})
    store.event("r1", "a", "message", {"text": "The Needle is here"})
    store.event("r1", "a", "message", {"text": "nothing"})
    assert [e["data"]["text"] for e in store.search_history("needle", project_id="p1")] == ["The Needle is here"]
