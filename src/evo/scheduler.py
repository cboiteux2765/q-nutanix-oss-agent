"""Durable, per-project admission for parallel workflows and model spending."""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Mapping
from sys import float_info

from .budget import BudgetExceeded, Reservation
from .common import ACTIVE, RESUMABLE, NotFound
from .store import require, utc_now

DEFAULT_WORKFLOW_SETTINGS = {
    "max_concurrent_workflows": 3,
    "max_project_tokens": None,
    "max_project_cost_usd": None,
}
MAX_CONCURRENT_WORKFLOWS = 20

logger = logging.getLogger(__name__)


def is_workflow(run: dict) -> bool:
    return run.get("kind", "workflow") == "workflow" and not run.get("evaluation")


class WorkflowScheduler:
    """Serialize changes to each project's queue while its admitted workflows run independently."""

    def __init__(self, runtime):
        self.runtime = runtime
        self.store = runtime.store
        self.locks: dict[str, asyncio.Lock] = {}
        self.drainers: dict[str, asyncio.Task] = {}
        self.drain_requested: set[str] = set()
        self.watched_tasks: set[asyncio.Task] = set()
        self.initialized = False
        self.closed = False
        runtime.scheduler = self

    def settings(self, project_id: str) -> dict:
        project = require(self.store, "projects", project_id)
        return DEFAULT_WORKFLOW_SETTINGS | (project.get("workflow_settings") or {})

    def configure(self, project_id: str, patch: Mapping) -> dict:
        if set(patch) - DEFAULT_WORKFLOW_SETTINGS.keys():
            raise ValueError("Unknown workflow setting.")

        def update(tx):
            project = tx.get("projects", project_id)
            if project is None:
                raise NotFound(f"Unknown project {project_id!r}.")
            settings = DEFAULT_WORKFLOW_SETTINGS | (project.get("workflow_settings") or {}) | dict(patch)
            concurrency = settings["max_concurrent_workflows"]
            if type(concurrency) is not int or not 1 <= concurrency <= MAX_CONCURRENT_WORKFLOWS:
                raise ValueError(f"Set concurrent workflows to an integer from 1 to {MAX_CONCURRENT_WORKFLOWS}.")
            tokens = settings["max_project_tokens"]
            if tokens is not None and (type(tokens) is not int or tokens <= 0):
                raise ValueError("Set the project token budget to a positive integer or leave it blank.")
            cost = settings["max_project_cost_usd"]
            if cost is not None:
                if type(cost) not in (int, float) or not 0 < cost <= float_info.max:
                    raise ValueError("Set the project spending limit to a positive amount or leave it blank.")
                settings["max_project_cost_usd"] = float(cost)
            tx.put("projects", project_id, {"workflow_settings": settings})
            return settings

        settings = self.store.transact(update)
        self._request_drain(project_id)
        return settings

    def has_cost_cap(self, project_id: str) -> bool:
        return self.settings(project_id)["max_project_cost_usd"] is not None

    def usage(self, project_id: str) -> dict:
        require(self.store, "projects", project_id)
        return self._usage(project_id)

    def _usage(self, project_id: str) -> dict:
        totals = {"tokens": 0, "unresolved_tokens": 0, "reserved_tokens": 0}
        costs = {"cost_usd": [], "unresolved_cost_usd": [], "reserved_cost_usd": []}
        for run in self.store.list("runs", project_id=project_id):
            if run.get("evaluation"):
                continue
            usage = run.get("token_usage") or {}
            totals["tokens"] += (usage.get("input") or 0) + (usage.get("output") or 0)
            totals["unresolved_tokens"] += usage.get("unresolved") or 0
            totals["reserved_tokens"] += run.get("reserved_tokens") or 0
            costs["cost_usd"].append(usage.get("cost_usd") or 0)
            costs["unresolved_cost_usd"].append(usage.get("unresolved_cost_usd") or 0)
            costs["reserved_cost_usd"].append(run.get("reserved_cost_usd") or 0)
        return totals | {key: math.fsum(values) for key, values in costs.items()}

    def check_budget(self, project_id: str, need: Reservation) -> None:
        """Check the project caps in the same synchronous admission step that persists a hold."""
        settings = self.settings(project_id)
        token_cap = settings["max_project_tokens"]
        cost_cap = settings["max_project_cost_usd"]
        if token_cap is None and cost_cap is None:
            return

        usage = self._usage(project_id)
        tokens = usage["tokens"] + usage["unresolved_tokens"] + usage["reserved_tokens"]
        if token_cap is not None and tokens + need.tokens > token_cap:
            raise BudgetExceeded(
                f"Project reached its token budget: the next model call could use up to {need.tokens:,} tokens "
                f"and {max(0, token_cap - tokens):,} of {token_cap:,} remain."
            )
        if cost_cap is not None:
            spent = math.fsum((usage["cost_usd"], usage["unresolved_cost_usd"], usage["reserved_cost_usd"]))
            if spent + need.cost > cost_cap:
                raise BudgetExceeded(
                    f"Project reached its spending limit: the next model call could cost up to ${need.cost:.4f} "
                    f"and ${max(0, cost_cap - spent):.4f} of ${cost_cap:.2f} remain."
                )

    async def initialize(self):
        """Dispatch saved queue entries after the runtime recovers interrupted work."""
        if self.initialized or self.closed:
            return
        self.initialized = True
        self.runtime.listeners.append(self._on_run_event)
        for run in self.store.list("runs", queue_pending=True):
            if self._pending(run):
                self._request_drain(run["project_id"])
        await asyncio.gather(*self.drainers.values())

    async def close(self):
        self.closed = True
        if self.initialized:
            self.runtime.listeners.remove(self._on_run_event)
        self.initialized = False
        drainers = list(self.drainers.values())
        for task in drainers:
            task.cancel()
        await asyncio.gather(*drainers, return_exceptions=True)
        self.drainers.clear()
        self.drain_requested.clear()
        self.watched_tasks.clear()

    async def start(self, project_id: str, message: str, source=None) -> dict:
        async with self._lock(project_id):
            run = await self.runtime.start_run(project_id, message, kind="workflow", source=source, defer=True)
            run_id = run["id"]
            self.runtime._run_status(run_id, "queued", queue_pending=True)
            await self._drain_locked(project_id)
            updated = self._workflow(project_id, run_id)
            if event_id := run.get("message_event_id"):
                updated["message_event_id"] = event_id
            return updated

    async def steer(self, project_id: str, run_id: str, message: str, *, routing=None) -> dict:
        async with self._lock(project_id):
            run = self._workflow(project_id, run_id)
            defer = run["id"] not in self._active_ids([run]) or run["status"] == "paused"
            delivered = await self.runtime.steer(run_id, message, defer=defer, routing=routing)
            if defer and run["status"] != "paused":
                self._queue(run)
                await self._drain_locked(project_id)
            updated = self._workflow(project_id, run_id)
            if event_id := delivered.get("message_event_id"):
                updated["message_event_id"] = event_id
            return updated

    async def pause(self, project_id: str, run_id: str) -> dict:
        async with self._lock(project_id):
            run = self._workflow(project_id, run_id)
            if run["status"] == "paused":
                return run
            if run["status"] not in ACTIVE | {"waiting"}:
                raise ValueError("Only a queued workflow, a running workflow, or a workflow waiting for input can pause.")
            paused = await self.runtime.pause(run_id)
            await self._drain_locked(project_id)
            return paused

    async def resume(self, project_id: str, run_id: str) -> dict:
        async with self._lock(project_id):
            run = self._workflow(project_id, run_id)
            if run["status"] not in RESUMABLE | ACTIVE | {"paused"}:
                raise ValueError("This workflow is finished. Send a message to continue the conversation.")
            if run["status"] not in ACTIVE or self._pending(run):
                self._queue(run)
                await self._drain_locked(project_id)
            return self._workflow(project_id, run_id)

    async def cancel(self, project_id: str, run_id: str) -> dict:
        async with self._lock(project_id):
            self._workflow(project_id, run_id)
            cancelled = await self.runtime.cancel(run_id)
            await self._drain_locked(project_id)
            return cancelled

    async def drain(self, project_id: str) -> None:
        async with self._lock(project_id):
            await self._drain_locked(project_id)

    def _lock(self, project_id: str) -> asyncio.Lock:
        return self.locks.setdefault(project_id, asyncio.Lock())

    def _workflow(self, project_id: str, run_id: str) -> dict:
        require(self.store, "projects", project_id)
        run = require(self.store, "runs", run_id)
        if run["project_id"] != project_id or not is_workflow(run):
            raise NotFound(f"Unknown workflow {run_id!r} in this project.")
        return run

    @staticmethod
    def _pending(run: dict) -> bool:
        return is_workflow(run) and run["status"] == "queued" and bool(run.get("queue_pending"))

    def _queue(self, run: dict) -> None:
        if not self._pending(run):
            self.runtime._run_status(run["id"], "queued", queue_pending=True, queued_at=utc_now(), error=None)

    def _active_ids(self, workflows: list[dict]) -> set[str]:
        active = {run["id"] for run in workflows if run["status"] in ACTIVE and not self._pending(run)}
        projects = {run["id"]: run["project_id"] for run in workflows}

        def account(run_id, task):
            # A terminal status can be saved before a task releases its provider or worktree.
            if run_id in projects and not task.done():
                active.add(run_id)
                self._watch_task(projects[run_id], task)

        for run_id, task in self.runtime.responder.tasks.items():
            account(run_id, task)
        for registry in (self.runtime.tasks, self.runtime.maintenance):
            for agent_id, task in registry.items():
                agent = self.store.get("agents", agent_id)
                if agent:
                    account(agent["run_id"], task)
        return active

    async def _drain_locked(self, project_id: str) -> None:
        if self.closed or self.runtime.closing:
            return
        workflows = [run for run in self.store.list("runs", project_id=project_id) if is_workflow(run)]
        cap = self.settings(project_id)["max_concurrent_workflows"]
        active = self._active_ids(workflows)
        pending = sorted((run for run in workflows if self._pending(run)),
                         key=lambda run: (run.get("queued_at") or run["created_at"], run["id"]))
        for run in pending:
            if self.closed or self.runtime.closing:
                return
            run_id = run["id"]
            if run.get("root_agent_id") in self.runtime.tasks:
                continue
            if run_id not in active and len(active) >= cap:
                continue
            try:
                await self.runtime.resume(run_id, dispatch=True)
            except Exception:
                logger.exception("Could not dispatch workflow %s", run_id)
                self.runtime._run_status(run_id, "failed", queue_pending=False, error="Could not start this workflow.")
            else:
                active.add(run_id)

    def _watch_task(self, project_id: str, task: asyncio.Task) -> None:
        if task in self.watched_tasks:
            return
        self.watched_tasks.add(task)

        def finished(task):
            self.watched_tasks.discard(task)
            self._request_drain(project_id)

        task.add_done_callback(finished)

    def _on_run_event(self, kind, run, _detail):
        if kind in {"turn_finished", "agent_finished"} and is_workflow(run):
            self._request_drain(run["project_id"])

    def _request_drain(self, project_id: str) -> None:
        if self.closed:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self.drain_requested.add(project_id)
        if project_id in self.drainers:
            return

        async def drain():
            try:
                while project_id in self.drain_requested:
                    self.drain_requested.discard(project_id)
                    await self.drain(project_id)
            finally:
                self.drainers.pop(project_id, None)

        self.drainers[project_id] = loop.create_task(drain(), name=f"evo-workflow-queue-{project_id}")
