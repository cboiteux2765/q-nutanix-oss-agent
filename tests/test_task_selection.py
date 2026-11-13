"""Task selection rules: chronological held-out boundary, fixed splits, round-robin search splits."""

import pytest

from evo.evolve.select_tasks import assign_splits


def task(task_id, as_of, merged):
    return {"id": task_id, "as_of": as_of, "merged_at": merged, "source_files": []}


def test_heldout_is_newest_and_every_search_fix_predates_it():
    tasks = [task(f"t{i}", f"2026-0{i}-01T00:00:00Z", f"2026-0{i}-10T00:00:00Z") for i in range(1, 9)]
    # t5's fix lands after t6 was opened, so the boundary cannot fall between them.
    tasks[4]["merged_at"] = "2026-06-15T00:00:00Z"
    assign_splits(tasks, heldout_target=3)
    split = {t["id"]: t["split"] for t in tasks}
    heldout = [t for t in tasks if t["split"] == "heldout"]
    search = [t for t in tasks if t["split"] != "heldout"]
    assert max(t["merged_at"] for t in search) < min(t["as_of"] for t in heldout)
    assert split["t5"] != "heldout" or split["t6"] == "heldout"
    assert len(heldout) in {2, 3, 4}
    # Search tasks alternate between reflection and validation in time order.
    assert [t["split"] for t in search][:4] == ["train", "validation", "train", "validation"]


def test_exposed_tasks_keep_their_role():
    tasks = [task("pytest-14392", "2026-04-01T00:00:00Z", "2026-04-02T00:00:00Z"),
             task("new-a", "2026-04-05T00:00:00Z", "2026-04-06T00:00:00Z"),
             task("pytest-14483", "2026-05-01T00:00:00Z", "2026-05-02T00:00:00Z"),
             task("new-b", "2026-06-01T00:00:00Z", "2026-06-02T00:00:00Z"),
             task("pytest-15132", "2026-10-01T00:00:00Z", "2026-10-02T00:00:00Z")]
    assign_splits(tasks, heldout_target=2)
    split = {t["id"]: t["split"] for t in tasks}
    # A task an experiment searched stays in search; a held-out one stays held out.
    assert split["pytest-14483"] in {"train", "validation"} and split["pytest-15132"] == "heldout"


def test_no_valid_boundary_is_an_error():
    # A fixed search task newer than a fixed held-out task cannot be separated.
    tasks = [task("pytest-15132", "2026-04-01T00:00:00Z", "2026-04-02T00:00:00Z"),
             task("pytest-14483", "2026-05-01T00:00:00Z", "2026-05-02T00:00:00Z")]
    with pytest.raises(RuntimeError):
        assign_splits(tasks, heldout_target=1)
