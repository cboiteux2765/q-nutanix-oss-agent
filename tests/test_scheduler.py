import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest

from evo.budget import BudgetExceeded, Reservation
from evo.common import NotFound
from evo.runtime import Runtime
from evo.scheduler import WorkflowScheduler


class QueueRuntime:
    """Keep the scheduler and its persistence real while controlling when each run yields its slot."""

    def __init__(self, store):
        self.store = store
        self.tasks = {}
        self.maintenance = {}
        self.responder = SimpleNamespace(tasks={})
        self.listeners = []
        self.closing = False
        self.dispatched = []

    def _run_status(self, run_id, status, **fields):
        run = self.store.put("runs", run_id, {"status": status, **fields})
        self.store.event(run_id, run["root_agent_id"], "run.status", {"status": status, **fields})
        return run

    async def start_run(self, project_id, message, *, kind, source, defer):
        assert defer and kind == "workflow"
        await asyncio.sleep(0)
        run_id, root_id = uuid4().hex, uuid4().hex
        run = self.store.put("runs", run_id, {
            "project_id": project_id, "kind": kind, "title": message, "source": source,
            "branch": f"run/{run_id}", "worktree": run_id, "root_agent_id": root_id,
            "status": "queued", "queue_pending": True,
        })
        self.store.put("agents", root_id, {"run_id": run_id, "status": "queued"})
        event = self.store.event(run_id, root_id, "message", {"role": "user", "text": message})
        return run | {"message_event_id": event["id"]}

    async def resume(self, run_id, *, dispatch):
        assert dispatch and self.store.get("runs", run_id)["queue_pending"]
        self.dispatched.append(run_id)
        return self._run_status(run_id, "running", queue_pending=False)

    async def steer(self, run_id, message, *, defer, routing):
        run = self.store.get("runs", run_id)
        if run["status"] != "running":
            assert defer
        event = self.store.event(run_id, run["root_agent_id"], "message", {"text": message, "routing": routing})
        return run | {"message_event_id": event["id"]}

    async def pause(self, run_id):
        return self._run_status(run_id, "paused", queue_pending=False)

    async def cancel(self, run_id):
        return self._run_status(run_id, "cancelled", queue_pending=False)

    def finish(self, run_id, status):
        run = self._run_status(run_id, status)
        for listener in self.listeners:
            listener("turn_finished", run, {"status": status})


async def wait_for_drain(scheduler, project_id):
    await asyncio.sleep(0)
    if task := scheduler.drainers.get(project_id):
        await task


def test_simultaneous_starts_admit_the_limit_and_release_in_queue_order(store):
    for project_id in ("first", "second"):
        store.put("projects", project_id, {"name": project_id})

    async def scenario():
        runtime = QueueRuntime(store)
        scheduler = WorkflowScheduler(runtime)
        assert scheduler.settings("first")["max_concurrent_workflows"] == 3
        scheduler.configure("first", {"max_concurrent_workflows": 2})
        await scheduler.initialize()
        try:
            runs = await asyncio.gather(*(scheduler.start("first", name) for name in "abcd"))
            assert [run["status"] for run in runs] == ["running", "running", "queued", "queued"]
            assert [run["queue_pending"] for run in runs] == [False, False, True, True]
            other = await scheduler.start("second", "No shared slot")
            assert other["status"] == "running"

            runtime.finish(runs[1]["id"], "completed")
            await wait_for_drain(scheduler, "first")
            assert runtime.dispatched == [runs[0]["id"], runs[1]["id"], other["id"], runs[2]["id"]]
            scheduler.configure("first", {"max_concurrent_workflows": 3})
            await wait_for_drain(scheduler, "first")
            assert runtime.dispatched[-1] == runs[3]["id"]
        finally:
            await scheduler.close()

    asyncio.run(scenario())


def test_pause_and_resume_preserve_branches_and_targeted_messages(store):
    store.put("projects", "p", {"name": "project"})
    store.put("projects", "other", {"name": "other"})
    store.put("runs", "supervisor", {"project_id": "p", "kind": "supervisor", "status": "running"})

    async def scenario():
        runtime = QueueRuntime(store)
        scheduler = WorkflowScheduler(runtime)
        scheduler.configure("p", {"max_concurrent_workflows": 1})
        await scheduler.initialize()
        try:
            original = await scheduler.start("p", "original", source={"source_event_id": "source"})
            queued = await scheduler.start("p", "queued")
            assert original["source"] == {"source_event_id": "source"}
            assert original["message_event_id"] == store.events(original["id"])[0]["id"]
            assert (await scheduler.pause("p", original["id"]))["status"] == "paused"
            assert store.get("runs", queued["id"])["status"] == "running"
            routing = {"source_event_id": "new-message"}
            updated = await scheduler.steer("p", original["id"], "answer", routing=routing)
            assert updated["status"] == "paused" and updated["branch"] == original["branch"]
            assert store.events(original["id"])[-1]["id"] == updated["message_event_id"]
            assert store.events(original["id"])[-1]["data"]["routing"] == routing
            resumed = await scheduler.resume("p", original["id"])
            assert resumed["status"] == "queued" and resumed["queue_pending"]

            with pytest.raises(NotFound):
                await scheduler.cancel("other", queued["id"])
            with pytest.raises(NotFound):
                await scheduler.steer("p", "supervisor", "Not a workflow")
            cancelled = await scheduler.cancel("p", queued["id"])
            assert cancelled["status"] == "cancelled" and cancelled["branch"] == queued["branch"]
            assert store.get("runs", original["id"])["status"] == "running"
        finally:
            await scheduler.close()

    asyncio.run(scenario())


def test_waiting_releases_the_slot_after_live_tasks_leave(store):
    store.put("projects", "p", {"name": "project"})

    async def scenario():
        runtime = QueueRuntime(store)
        scheduler = WorkflowScheduler(runtime)
        scheduler.configure("p", {"max_concurrent_workflows": 1})
        await scheduler.initialize()
        after_reply = asyncio.Event()
        try:
            a = await scheduler.start("p", "first")
            b = await scheduler.start("p", "second")
            responder = asyncio.create_task(after_reply.wait())
            runtime.responder.tasks[a["id"]] = responder
            runtime.finish(a["id"], "waiting")
            await wait_for_drain(scheduler, "p")
            assert runtime.dispatched == [a["id"]]

            after_reply.set()
            await responder
            await wait_for_drain(scheduler, "p")
            assert runtime.dispatched == [a["id"], b["id"]]
            routed = await scheduler.steer("p", a["id"], "continue while full")
            assert routed["status"] == "queued" and routed["queue_pending"]
        finally:
            after_reply.set()
            await scheduler.close()

    asyncio.run(scenario())


def test_restart_drains_pending_work_but_requires_explicit_resume_for_interrupted(store):
    store.put("projects", "p", {"name": "project"})

    async def scenario():
        scheduler = WorkflowScheduler(QueueRuntime(store))
        scheduler.configure("p", {"max_concurrent_workflows": 1})
        await scheduler.initialize()
        a = await scheduler.start("p", "interrupted")
        b = await scheduler.start("p", "pending")
        c = await scheduler.start("p", "paused")
        await scheduler.pause("p", c["id"])
        await scheduler.close()
        store.put("runs", a["id"], {"status": "interrupted"})

        runtime = QueueRuntime(store)
        scheduler = WorkflowScheduler(runtime)
        await scheduler.initialize()
        try:
            assert runtime.dispatched == [b["id"]]
            assert store.get("runs", a["id"])["status"] == "interrupted"
            assert store.get("runs", c["id"])["status"] == "paused"
            assert (await scheduler.resume("p", a["id"]))["queue_pending"]
            runtime.finish(b["id"], "failed")
            await wait_for_drain(scheduler, "p")
            assert runtime.dispatched == [b["id"], a["id"]]
        finally:
            await scheduler.close()

    asyncio.run(scenario())


def test_project_budget_admits_against_all_production_usage_and_holds(store):
    for project_id in ("p", "other"):
        store.put("projects", project_id, {"name": project_id})
    settings = {"max_run_tokens": 1_000, "max_output_tokens": 10, "input_price": 1_000_000}
    for run_id, project_id, extra in (
        ("first", "p", {"token_usage": {"input": 8, "output": 2, "unresolved": 5,
                                        "cost_usd": 2, "unresolved_cost_usd": 3}}),
        ("second", "p", {}),
        ("supervisor", "p", {"kind": "supervisor", "token_usage": {"input": 5, "output": 5, "cost_usd": 5}}),
        ("eval", "p", {"evaluation": True, "token_usage": {"input": 1000, "cost_usd": 200}}),
        ("foreign", "other", {"token_usage": {"input": 1_000, "cost_usd": 200}}),
    ):
        store.put("runs", run_id, {"project_id": project_id, "settings_snapshot": settings, **extra})

    runtime = Runtime(store)
    scheduler = WorkflowScheduler(runtime)
    scheduler.configure("p", {"max_project_tokens": 70, "max_project_cost_usd": 35})
    with pytest.raises(ValueError):
        scheduler.configure("p", {"max_concurrent_workflows": 21})
    first = {"id": "a", "run_id": "first"}
    second = {"id": "b", "run_id": "second"}
    assert runtime._admit(first, settings, 10) == Reservation(20, 10.0)
    with pytest.raises(BudgetExceeded, match="Project reached its token budget"):
        runtime._admit(second, settings, 20)
    scheduler.configure("p", {"max_project_tokens": None})
    with pytest.raises(BudgetExceeded, match="Project reached its spending limit"):
        runtime._admit(second, settings, 20)
    with pytest.raises(BudgetExceeded, match="Configure model prices"):
        runtime._admit(second, settings | {"input_price": 0}, 10)
    assert scheduler.usage("p") == {
        "tokens": 20, "unresolved_tokens": 5, "reserved_tokens": 20,
        "cost_usd": 7, "unresolved_cost_usd": 3, "reserved_cost_usd": 10,
    }

    restarted = Runtime(store)
    restarted.reconcile_reservations()
    scheduler = WorkflowScheduler(restarted)
    with pytest.raises(BudgetExceeded, match="Project reached its spending limit"):
        restarted._admit(second, settings, 20)
    assert scheduler.usage("p")["reserved_tokens"] == 0
    assert scheduler.usage("p")["unresolved_tokens"] == 25
    assert scheduler.usage("p")["unresolved_cost_usd"] == 13
    scheduler.configure("p", {"max_project_cost_usd": None})
    assert restarted._admit(second, settings, 20) == Reservation(30, 20.0)
