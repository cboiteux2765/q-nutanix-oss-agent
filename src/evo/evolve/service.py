"""Evolution experiments, the reflection queue, evidence gates, activation and rollback.

An experiment optimizes one role's project-specific instructions with GEPA, scoring candidates by
running the real EVO harness on reproducible historical tasks (see rollout.py). Validation scores
drive GEPA's search; held-out tasks are run once, after the search, for the baseline and the best
candidate only. A candidate passes the gate when held-out results show no regression and its cost
stays within bounds. Activation is manual unless the project opts into automatic activation.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import threading
import time
from datetime import datetime, UTC
from pathlib import Path
from uuid import uuid4

import sqlite3

from ..budget import (BudgetExceeded, confirm_price_tier, cost_bound, input_bound, output_limit, prices_set,
                      usage_complete)
from ..providers import ProviderError
from ..config import get_api_key, get_settings
from ..providers import ModelProvider
from ..roles import ROLES
from ..store import Store, require
from .adapter import REFLECTION_TEMPLATE, SEED_PLACEHOLDER, EvoAdapter
from .rollout import run_rollout
from .tasks import TaskBundles, task_python

FINAL = {"completed", "failed", "cancelled", "interrupted", "stopped"}
DEFAULT_LIMITS = {
    "max_metric_calls": 8,        # rollouts GEPA may spend on train + validation
    "max_iterations": 3,          # GEPA proposals
    "reflection_minibatch_size": 1,
    "rollout_tokens": 400_000,    # per rollout
    "experiment_tokens": 3_000_000,
    "max_cost_usd": 10.0,         # hard spending limit; enforced when prices are configured
    "rollout_cost_usd": 0.5,      # dollars reserved per held-out rollout and needed to start any rollout
    "rollout_seconds": 1200,
    "wall_seconds": 4 * 3600,
    "max_steps": 60,
}
LIMIT_CAPS = {"max_metric_calls": 40, "max_iterations": 10, "reflection_minibatch_size": 3,
              "rollout_tokens": 2_000_000, "experiment_tokens": 100_000_000, "max_cost_usd": 200.0,
              "rollout_cost_usd": 20.0,
              "rollout_seconds": 3600, "wall_seconds": 12 * 3600, "max_steps": 200}
DEFAULT_SETTINGS = {
    "mode": "manual",             # manual: the queue only suggests; auto: experiments start themselves
    "auto_activate": False,       # auto mode may activate a candidate that passes every gate
    "min_queued_runs": 2,
    "max_auto_per_day": 1,
    "role": "code",
    "limits": {**DEFAULT_LIMITS, "max_metric_calls": 6},
}
GATE = {"min_heldout_delta": 0.0, "max_cost_ratio": 1.5}
# Outcomes that are a property of the agent under a given configuration, so a finished rollout with
# the same identity can be reused. Harness errors, provider failures, check timeouts, cancellations
# and budget skips are retried instead.
REUSABLE = {"completed", "budget", "timeout"}
# Settings that do not change what a rollout does.
NOT_BEHAVIORAL = {"input_price", "cached_input_price", "cache_write_price", "output_price"}


def budget_plan(limits: dict, splits: dict, provider: dict, kind: str = "prompt") -> dict:
    """Split the experiment budget into a search pool and a reserve for the final paired evaluation.

    The reserve covers both arms on every final task at the full per-rollout cap, so the search can
    never consume the tokens the held-out comparison needs. When prices are configured, the spending
    limit is a hard dollar limit with the same split: every model call is admitted against its
    worst-case cost, and the held-out reserve is rollout_cost_usd per final rollout.
    Rollouts always get the full per-rollout token cap (a trimmed rollout would not be comparable);
    one that does not fit is skipped instead."""
    per_rollout = int(limits["rollout_tokens"])
    total = int(limits["experiment_tokens"])
    priced = prices_set(provider) and bool(limits.get("max_cost_usd"))
    final_tasks = len(splits.get("heldout", [])) + (len(splits.get("validation", [])) if kind == "skill" else 0)
    reserve = 2 * final_tasks * per_rollout
    search = max(0, total - reserve) if kind == "prompt" else 0
    validation = len(splits.get("validation", []))
    minibatch = min(int(limits.get("reflection_minibatch_size", 1)), max(1, len(splits.get("train", []))))
    proposals = int(limits.get("max_iterations", 1)) if kind == "prompt" else 0
    total_usd = float(limits["max_cost_usd"]) if priced else None
    reserve_usd = 2 * final_tasks * float(limits.get("rollout_cost_usd", 0.5)) if priced else None
    return {
        "total_tokens": total, "per_rollout_tokens": per_rollout, "heldout_reserve_tokens": reserve,
        "search_tokens": search, "final_rollouts": 2 * final_tasks,
        "total_usd": total_usd, "heldout_reserve_usd": reserve_usd,
        "search_usd": (max(0.0, total_usd - reserve_usd) if kind == "prompt" else 0.0) if priced else None,
        "rollout_cost_usd": float(limits.get("rollout_cost_usd", 0.5)) if priced else None,
        # GEPA evaluates the seed on the full validation set; each proposal then costs a parent
        # minibatch, a child minibatch and, only if the child improves on it, the full validation set.
        "search_rollouts": {
            "all_rejected": validation + proposals * 2 * minibatch if kind == "prompt" else 0,
            "all_accepted": validation + proposals * (2 * minibatch + validation) if kind == "prompt" else 0,
            "fit_in_search_tokens": search // per_rollout if per_rollout else 0,
            "max_metric_calls": int(limits.get("max_metric_calls", 0)),
        },
        "reflection_output_limit": output_limit(provider),
        "not_covered": ("Every model call is admitted only if its worst case (an input bound plus the output "
                        "limit, priced with no cache discount) fits, and requests whose usage is unknown are "
                        "charged that worst case. Not covered: providers that ignore the output limit, "
                        "tokenizers that are not byte-level, and prices that differ from the configured ones."),
    }


def _spent_after(spent: dict, tokens: int, cost: float, *, rollout=False, reflection=False, pool="search",
                 release: str | None = None, released: int = 0, released_cost: float = 0.0,
                 unresolved: int = 0, unresolved_cost: float = 0.0) -> dict:
    """The ledger after a charge. `cost` includes any unresolved cost; `release` names the
    reservation (search, heldout or reflection) that the charge replaces."""
    spent = dict(spent)
    spent["tokens"] += int(tokens)
    side = "heldout" if pool == "heldout" else "search"
    spent[f"{side}_tokens"] = spent.get(f"{side}_tokens", 0) + int(tokens)
    spent["cost_usd"] = round(spent["cost_usd"] + float(cost or 0), 6)
    spent[f"{side}_cost_usd"] = round(spent.get(f"{side}_cost_usd", 0.0) + float(cost or 0), 6)
    spent["rollouts"] += int(rollout)
    spent["reflection_calls"] += int(reflection)
    spent["unresolved_tokens"] = spent.get("unresolved_tokens", 0) + int(unresolved)
    spent["unresolved_cost_usd"] = round(spent.get("unresolved_cost_usd", 0.0) + float(unresolved_cost), 6)
    if release:
        spent[f"reserved_{release}"] = max(0, spent.get(f"reserved_{release}", 0) - int(released))
        spent[f"reserved_cost_{release}"] = round(max(0.0, spent.get(f"reserved_cost_{release}", 0.0)
                                                      - float(released_cost)), 6)
    return spent


def scored(record: dict) -> bool:
    """A rollout that ran to an agent outcome and was scored by the hidden checks."""
    return (record.get("outcome") in REUSABLE and "fail_to_pass" in record and not record.get("checks_error")
            and not str(record.get("error") or "").startswith("Evaluation harness"))


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _hash(*parts) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:24]


def _mean(values):
    values = list(values)
    return round(sum(values) / len(values), 4) if values else None


def _per_task_best(value):
    items = value.items() if isinstance(value, dict) else enumerate(value or [])
    return {str(key): sorted(best) for key, best in items}


def _clean_limits(limits: dict | None, base: dict | None = None) -> dict:
    merged = {**(base or DEFAULT_LIMITS)}
    for key, value in (limits or {}).items():
        if key not in DEFAULT_LIMITS:
            raise ValueError(f"Unknown limit {key!r}.")
        number = float(value)
        if not 0 < number <= LIMIT_CAPS[key]:
            raise ValueError(f"{key} must be between 0 and {LIMIT_CAPS[key]:,}.")
        merged[key] = number if key.endswith("_usd") else int(number)
    return merged


class ExperimentService:
    def __init__(self, store: Store, runtime):
        self.store = store
        self.runtime = runtime
        self.root = store.data_dir / "evolution"
        self.tasks = TaskBundles(store.data_dir)
        self.jobs: dict[str, asyncio.Task] = {}
        self.cancel_flags: dict[str, threading.Event] = {}
        self.cancel_events: dict[str, asyncio.Event] = {}
        self.search_exhausted: dict[str, bool] = {}
        self.closing = False
        self.slot = asyncio.Semaphore(1)  # one experiment at a time, separate from foreground work
        self.queue_task: asyncio.Task | None = None
        runtime.listeners.append(self.on_run_event)

    # ------------------------------------------------------------------ settings and startup
    def recover(self) -> None:
        """After a restart, settle interrupted work and mark unfinished experiments interrupted;
        never resume paid work by itself."""
        self.reconcile()
        for experiment in self.store.list("evolution_experiments"):
            if experiment["status"] not in FINAL:
                self.store.put("evolution_experiments", experiment["id"], {
                    "status": "interrupted", "stage": "interrupted",
                    "note": "EVO restarted. Resume to continue; finished rollouts are reused, not repeated."})

    def settings(self, project_id: str) -> dict:
        require(self.store, "projects", project_id)
        saved = self.store.get("evolution_settings", project_id) or {}
        return {**DEFAULT_SETTINGS, **{k: v for k, v in saved.items() if k in DEFAULT_SETTINGS},
                "limits": _clean_limits(saved.get("limits"), DEFAULT_SETTINGS["limits"])}

    def update_settings(self, project_id: str, changes: dict) -> dict:
        current = self.settings(project_id)
        if "mode" in changes and changes["mode"] not in {"manual", "auto"}:
            raise ValueError("Mode is manual or auto.")
        if "role" in changes and (changes["role"] not in ROLES or changes["role"] == "orchestrator"):
            raise ValueError("Choose Spec, Code, Test, QA, or Helper.")
        for key in ("min_queued_runs", "max_auto_per_day"):
            if key in changes and not 1 <= int(changes[key]) <= 20:
                raise ValueError(f"{key} must be between 1 and 20.")
        updated = {**current, **{k: v for k, v in changes.items() if k in DEFAULT_SETTINGS and k != "limits"}}
        updated["limits"] = _clean_limits(changes.get("limits"), current["limits"])
        updated["auto_activate"] = bool(updated["auto_activate"])
        self.store.put("evolution_settings", project_id, updated)
        return updated

    # ------------------------------------------------------------------ reflection queue
    def on_run_event(self, kind: str, run: dict, detail: dict) -> None:
        """Runtime listener: completed or failed turns and failed verification feed the queue."""
        if run.get("evaluation") or self.runtime.evaluation is not None:
            return  # evaluation runs never trigger evolution
        status = detail.get("status")
        if kind == "turn_finished" and status not in {"completed", "failed"}:
            return  # a conversation turn or a pause, nothing to learn from yet
        reason = {"turn_finished": f"run {status}", "verification_failed": "verification failed"}[kind]
        item_id = f"{run['id']}:{kind}:{status or detail.get('handoff_id')}"
        if self.store.get("evolution_queue", item_id):
            return
        self.store.put("evolution_queue", item_id, {
            "project_id": run["project_id"], "run_id": run["id"], "reason": reason, "status": "queued",
            "title": run.get("title")})
        if self.settings(run["project_id"])["mode"] == "auto":
            self._kick_queue(run["project_id"])

    def queue(self, project_id: str) -> list[dict]:
        return [item for item in self.store.list("evolution_queue", project_id=project_id)
                if item["status"] == "queued"]

    def dismiss(self, project_id: str, item_id: str) -> dict:
        item = require(self.store, "evolution_queue", item_id)
        if item["project_id"] != project_id:
            raise KeyError("Queue item not found.")
        return self.store.put("evolution_queue", item_id, {"status": "dismissed"})

    def _kick_queue(self, project_id: str) -> None:
        if self.queue_task and not self.queue_task.done():
            return
        self.queue_task = asyncio.get_running_loop().create_task(self._auto_start(project_id))

    async def _auto_start(self, project_id: str) -> None:
        settings = self.settings(project_id)
        items = self.queue(project_id)
        if settings["mode"] != "auto" or len(items) < settings["min_queued_runs"]:
            return
        if any(e["status"] not in FINAL for e in self.store.list("evolution_experiments")):
            return
        today = datetime.now(UTC).date().isoformat()
        started_today = [e for e in self.store.list("evolution_experiments", project_id=project_id)
                         if e.get("origin") == "auto" and e["created_at"][:10] == today]
        if len(started_today) >= settings["max_auto_per_day"]:
            return
        run_ids = list(dict.fromkeys(item["run_id"] for item in items))[-4:]
        try:
            experiment = self.create(project_id, role=settings["role"], run_ids=run_ids, limits=settings["limits"],
                                     objective="Reduce the failures and rework seen in the queued runs.",
                                     origin="auto")
        except (ValueError, KeyError, RuntimeError) as exc:
            # Leave the runs queued and say why nothing started (for example, a budget too small).
            for item in items:
                self.store.put("evolution_queue", item["id"], {"note": f"Automatic experiment not started: {exc}"})
            return
        for item in items:
            self.store.put("evolution_queue", item["id"], {"status": "used", "experiment_id": experiment["id"]})

    # ------------------------------------------------------------------ experiments
    def list(self, project_id: str) -> list[dict]:
        require(self.store, "projects", project_id)
        return sorted(self.store.list("evolution_experiments", project_id=project_id),
                      key=lambda e: e["created_at"], reverse=True)

    def get(self, experiment_id: str) -> dict:
        experiment = require(self.store, "evolution_experiments", experiment_id)
        rollouts = self.store.list("evolution_rollouts", experiment_id=experiment_id)
        prompts = [p for p in self.store.list("prompts", project_id=experiment["project_id"])
                   if p.get("experiment_id") == experiment_id]
        return experiment | {"rollouts": sorted(rollouts, key=lambda r: r["created_at"]), "candidates": prompts}

    def create(self, project_id: str, *, role: str = "code", objective: str = "", run_ids: list[str] | None = None,
               train: list[str] | None = None, validation: list[str] | None = None,
               heldout: list[str] | None = None, limits: dict | None = None, kind: str = "prompt",
               skill_id: str | None = None, origin: str = "manual") -> dict:
        require(self.store, "projects", project_id)
        role = role.lower()
        if role not in ROLES or role == "orchestrator":
            raise ValueError("Experiments evaluate Spec, Code, Test, QA, or Helper working alone.")
        if role != "code":
            raise ValueError("The available evaluation tasks score code changes; choose the Code role.")
        run_ids = list(dict.fromkeys(run_ids or []))
        if kind == "prompt" and not 1 <= len(run_ids) <= 8:
            raise ValueError("Select between one and eight source runs.")
        for run_id in run_ids:
            if require(self.store, "runs", run_id)["project_id"] != project_id:
                raise KeyError(f"No run {run_id!r} in this project.")
        catalog = {task["id"]: task for task in self.tasks.list()}
        defaults = {split: [t for t, task in catalog.items() if task["split"] == split]
                    for split in ("train", "validation", "heldout")}
        train = train if train is not None else defaults["train"]
        validation = validation if validation is not None else defaults["validation"]
        heldout = heldout if heldout is not None else defaults["heldout"]
        for task_id in (*train, *validation, *heldout):
            if task_id not in catalog:
                raise KeyError(f"No evaluation task {task_id!r}.")
        if set(heldout) & (set(train) | set(validation)) or set(train) & set(validation):
            raise ValueError("Reflection, validation and held-out tasks must not overlap.")
        if not heldout or not validation or (kind == "prompt" and not train):
            raise ValueError("Choose at least one reflection, one validation and one held-out task.")
        exposure = self.exposure()
        reused = sorted(t for t in heldout if exposure.get(t, {}).get("optimization"))
        if reused:
            raise ValueError(f"{', '.join(reused)} already informed an earlier experiment's search, so it cannot "
                             "give a fresh held-out result. Choose tasks that have only been held out.")
        reserved = sorted(t for t in (*train, *validation) if exposure.get(t, {}).get("heldout"))
        if reserved:
            raise ValueError(f"{', '.join(reserved)} is reserved as held-out by an earlier experiment; using it for "
                             "reflection or validation would expose its results to the optimizer.")
        if kind == "skill":
            skill = require(self.store, "skills", skill_id or "")
            if skill["project_id"] != project_id or skill["status"] != "candidate":
                raise ValueError("Choose a proposed skill from this project.")
        if not task_python():
            raise RuntimeError("Evaluation needs a Python with pytest's dependencies (set EVO_REPLAY_PYTHON).")
        limits = _clean_limits(limits)
        baseline = self._active_prompt(project_id, role)
        # One effective provider configuration for every rollout and reflection call, including resumes.
        settings = get_settings(self.store)
        splits = {"train": train, "validation": validation, "heldout": heldout}
        budget = budget_plan(limits, splits, settings, kind)
        if budget["total_usd"] is not None and budget["total_usd"] < budget["heldout_reserve_usd"]:
            raise ValueError(
                f"The spending limit (${budget['total_usd']:.2f}) cannot cover the final paired evaluation: "
                f"{budget['final_rollouts']} rollouts at ${budget['rollout_cost_usd']:.2f} need "
                f"${budget['heldout_reserve_usd']:.2f}.")
        if budget["total_tokens"] < budget["heldout_reserve_tokens"]:
            raise ValueError(
                f"The budget ({budget['total_tokens']:,} tokens) cannot cover the final paired evaluation: "
                f"{budget['final_rollouts']} rollouts at {budget['per_rollout_tokens']:,} tokens need "
                f"{budget['heldout_reserve_tokens']:,}. Raise the total or use fewer held-out tasks.")
        if kind == "prompt":
            minibatch = min(limits["reflection_minibatch_size"], len(train))
            needed = (len(validation) + 2 * minibatch) * limits["rollout_tokens"] + output_limit(settings)
            if budget["search_usd"] is not None:
                needed_usd = (len(validation) + 2 * minibatch) * budget["rollout_cost_usd"]
                if budget["search_usd"] < needed_usd:
                    raise ValueError(
                        f"After reserving ${budget['heldout_reserve_usd']:.2f} for held-out evaluation, the search "
                        f"has ${budget['search_usd']:.2f}; testing one proposal needs ${needed_usd:.2f} "
                        f"({len(validation) + 2 * minibatch} rollouts at ${budget['rollout_cost_usd']:.2f}).")
            if budget["search_tokens"] < needed:
                raise ValueError(
                    f"After reserving {budget['heldout_reserve_tokens']:,} tokens for held-out evaluation, the "
                    f"search has {budget['search_tokens']:,}; testing one proposal can take {needed:,} (the "
                    "baseline on validation, parent and child on a minibatch, and a reflection call).")
        experiment = self.store.put("evolution_experiments", uuid4().hex, {
            "provider": settings,
            "project_id": project_id, "role": role, "kind": kind, "skill_id": skill_id, "origin": origin,
            "objective": objective.strip()[:4000], "source_run_ids": run_ids,
            "splits": splits, "limits": limits, "budget": budget, "status": "queued", "stage": "queued",
            "baseline_prompt_id": baseline["id"] if baseline else None,
            "baseline_version": baseline["version"] if baseline else None,
            "model": {"model": settings["model"], "api_style": settings["api_style"],
                      "base_url": settings["base_url"], "reasoning_effort": settings.get("reasoning_effort")},
            "optimizer": "gepa 0.1.4 (reflective prompt evolution, Pareto candidate selection)"
            if kind == "prompt" else "A/B comparison (no optimization)",
            "spent": {"tokens": 0, "cost_usd": 0.0, "rollouts": 0, "reflection_calls": 0,
                      "search_tokens": 0, "heldout_tokens": 0},
        })
        self.start(experiment["id"])
        return experiment

    def start(self, experiment_id: str) -> None:
        if experiment_id in self.jobs and not self.jobs[experiment_id].done():
            raise ValueError("This experiment is already running.")
        self.cancel_flags[experiment_id] = threading.Event()
        self.search_exhausted.pop(experiment_id, None)
        self.cancel_events[experiment_id] = asyncio.Event()
        self.jobs[experiment_id] = asyncio.get_running_loop().create_task(
            self._job(experiment_id), name=f"evo-experiment-{experiment_id}")

    def resume(self, experiment_id: str) -> dict:
        experiment = require(self.store, "evolution_experiments", experiment_id)
        if experiment["status"] not in {"interrupted", "cancelled", "failed", "stopped"}:
            raise ValueError("Only a stopped experiment can be resumed.")
        self.reconcile(experiment_id)  # usage from interrupted work counts before anything new starts
        experiment = self.store.put("evolution_experiments", experiment_id,
                                    {"status": "queued", "stage": "queued", "note": None, "error": None})
        self.start(experiment_id)
        return experiment

    def cancel(self, experiment_id: str) -> dict:
        experiment = require(self.store, "evolution_experiments", experiment_id)
        if experiment["status"] in FINAL:
            return experiment
        if flag := self.cancel_flags.get(experiment_id):
            flag.set()
        if event := self.cancel_events.get(experiment_id):
            event.set()
        return self.store.put("evolution_experiments", experiment_id, {"stage": "cancelling"})

    async def close(self) -> None:
        self.closing = True  # experiments stopped by shutdown are interrupted, not cancelled
        tasks = [task for task in [*self.jobs.values(), self.queue_task] if task and not task.done()]
        for experiment_id in list(self.jobs):
            if flag := self.cancel_flags.get(experiment_id):
                flag.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def exposure(self) -> dict[str, dict[str, list[str]]]:
        """Which experiments used each task to guide the search (reflection or validation) or held it out.
        Shared across projects because finished rollouts are shared."""
        found: dict[str, dict[str, list[str]]] = {}
        for experiment in self.store.list("evolution_experiments"):
            splits = experiment.get("splits") or {}
            for split, use in (("train", "optimization"), ("validation", "optimization"), ("heldout", "heldout")):
                for task_id in splits.get(split, []):
                    found.setdefault(task_id, {"optimization": [], "heldout": []})[use].append(experiment["id"])
        return found

    def _update(self, experiment_id: str, **fields) -> dict:
        return self.store.put("evolution_experiments", experiment_id, fields)

    def _active_prompt(self, project_id: str, role: str) -> dict | None:
        return next((p for p in self.store.list("prompts", project_id=project_id, status="active")
                     if p["role"] == role), None)

    def _frozen_state(self, project_id: str) -> dict:
        wiki = self.store.data_dir / "knowledge" / project_id
        revision = self.runtime.knowledge.revision(project_id)
        return {"wiki_dir": str(wiki) if (wiki / ".git").exists() else None, "wiki_revision": revision,
                "skills": self.store.list("skills", project_id=project_id, status="active")}

    def _evidence(self, experiment: dict) -> str:
        """Recorded events from the source runs for the reflection prompt."""
        lines = []
        for run_id in experiment["source_run_ids"]:
            run = self.store.get("runs", run_id)
            if not run:
                continue
            agents = self.store.list("agents", run_id=run_id)
            role_ids = [a["id"] for a in agents if a["role"] == experiment["role"]] or [a["id"] for a in agents]
            lines.append(f"Run {run_id} ({run.get('title')}, phase {run.get('phase')}, status {run.get('status')}):")
            for event in self.store.recent_events(run_id, role_ids, 40):
                if event["kind"] in {"agent.delta", "agent.response", "usage"}:
                    continue
                lines.append(f"  {event['id']} {event['kind']} "
                             f"{json.dumps(event['data'], ensure_ascii=False)[:300]}")
        return "\n".join(lines)[-12_000:] or "(none)"

    # ------------------------------------------------------------------ the job
    async def _job(self, experiment_id: str) -> None:
        async with self.slot:
            experiment = require(self.store, "evolution_experiments", experiment_id)
            loop = asyncio.get_running_loop()
            started = time.monotonic()
            try:
                self._update(experiment_id, status="running", stage="preparing tasks", started_at=_now())
                split_ids = experiment["splits"]
                tasks = {}
                for split in ("train", "validation", "heldout"):
                    for task_id in split_ids[split]:
                        # The experiment's assignment, not the catalog default, decides a task's split.
                        tasks[task_id] = {**await asyncio.to_thread(self.tasks.load, task_id), "split": split}
                provider = experiment.get("provider")
                if not provider:
                    raise RuntimeError("This experiment predates frozen provider settings. Start a new one.")
                context = {
                    "frozen": experiment.get("frozen") or await asyncio.to_thread(
                        self._frozen_state, experiment["project_id"]),
                    "provider": provider, "python": task_python(), "started": started,
                }
                self._update(experiment_id, frozen=context["frozen"])
                component = f"role:{experiment['role']}"
                baseline = self.store.get("prompts", experiment["baseline_prompt_id"]) \
                    if experiment.get("baseline_prompt_id") else None
                seed_text = baseline["content"] if baseline else SEED_PLACEHOLDER

                if experiment["kind"] == "skill":
                    best_text, candidate_id = seed_text, None
                else:
                    self._update(experiment_id, stage="optimizing")
                    best_text, candidate_id = await asyncio.to_thread(
                        self._optimize, experiment_id, tasks, seed_text, component, context, loop)
                if self.cancel_flags[experiment_id].is_set():
                    raise asyncio.CancelledError
                self._update(experiment_id, stage="held-out evaluation")
                summary = await self._heldout(experiment_id, tasks, seed_text, best_text, candidate_id, context)
                status = "stopped" if summary.get("incomplete") else "completed"
                self._update(experiment_id, status=status, stage=status, finished_at=_now(), summary=summary)
                await self._maybe_auto_activate(experiment_id)
            except asyncio.CancelledError:
                status = "interrupted" if self.closing else "cancelled"
                self._update(experiment_id, status=status, stage=status, finished_at=_now())
            except Exception as exc:
                self._update(experiment_id, status="failed", stage="failed", error=str(exc)[:2000],
                             finished_at=_now())

    def _budget(self, experiment: dict) -> dict:
        return experiment.get("budget") or budget_plan(experiment["limits"], experiment["splits"],
                                                       experiment.get("provider") or {}, experiment["kind"])

    @staticmethod
    def _committed(spent: dict, pool: str, unit: str = "tokens") -> float:
        """Spent plus reserved for work whose usage is not settled yet, in tokens or dollars."""
        name = "reserved_" if unit == "tokens" else "reserved_cost_"
        reserved = spent.get(f"{name}search", 0) + spent.get(f"{name}reflection", 0)
        total_key, search_key = ("tokens", "search_tokens") if unit == "tokens" else ("cost_usd", "search_cost_usd")
        if pool == "heldout":
            return spent[total_key] + reserved + spent.get(f"{name}heldout", 0)
        return spent.get(search_key, spent[total_key]) + reserved

    def _dollars_left(self, experiment: dict, pool: str) -> float | None:
        """Dollars this pool can still commit, or None when there is no dollar limit."""
        budget = self._budget(experiment)
        if budget.get("total_usd") is None:
            return None
        limit = budget["total_usd"] if pool == "heldout" else budget["search_usd"]
        return limit - self._committed(experiment["spent"], pool, "usd")

    def _budget_reason(self, experiment_id: str, started: float, *, pool: str = "search") -> str | None:
        """Why the next full-cap rollout in this pool cannot start, or None."""
        experiment = require(self.store, "evolution_experiments", experiment_id)
        spent, limits, budget = experiment["spent"], experiment["limits"], self._budget(experiment)
        if self.cancel_flags[experiment_id].is_set():
            return "cancelled"
        need = budget["per_rollout_tokens"]
        if pool == "heldout":
            if self._committed(spent, "heldout") + need > budget["total_tokens"]:
                return "token budget reached"
        elif (self.search_exhausted.get(experiment_id)
              or self._committed(spent, "search") + need > budget["search_tokens"]):
            return "search budget reached"
        dollars = self._dollars_left(experiment, pool)
        if dollars is not None and dollars < budget["rollout_cost_usd"]:
            return "spending limit reached"
        if time.monotonic() - started >= limits["wall_seconds"]:
            return "wall-time limit reached"
        return None

    def _spend(self, experiment_id: str, tokens: int, cost: float, **ledger) -> None:
        """Record spending, and release a reservation, in one write."""
        experiment = require(self.store, "evolution_experiments", experiment_id)
        self._update(experiment_id, spent=_spent_after(experiment["spent"], tokens, cost, **ledger))

    def _reserve(self, experiment_id: str, pool: str, amount: int, cost: float = 0.0) -> None:
        experiment = require(self.store, "evolution_experiments", experiment_id)
        spent = dict(experiment["spent"])
        spent[f"reserved_{pool}"] = spent.get(f"reserved_{pool}", 0) + int(amount)
        spent[f"reserved_cost_{pool}"] = round(spent.get(f"reserved_cost_{pool}", 0.0) + float(cost), 6)
        self._update(experiment_id, spent=spent)

    @staticmethod
    def _usage_on_disk(data_dir: str) -> tuple[int, float] | None:
        """Tokens and dollars a rollout recorded in its own store, including requests whose usage is
        unknown (unresolved and still-reserved amounts). None when it cannot be read."""
        db = Path(data_dir) / "data" / "evo.db"
        if not db.exists():
            return None
        try:
            connection = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
            try:
                rows = connection.execute("SELECT data FROM records WHERE collection='runs'").fetchall()
            finally:
                connection.close()
        except sqlite3.Error:
            return None
        tokens, cost = 0, 0.0
        for (data,) in rows:
            run = json.loads(data)
            usage = run.get("token_usage") or {}
            tokens += (usage.get("input", 0) + usage.get("output", 0) + usage.get("unresolved", 0)
                       + int(run.get("reserved_tokens") or 0))
            cost += (float(usage.get("cost_usd") or 0) + float(usage.get("unresolved_cost_usd") or 0)
                     + float(run.get("reserved_cost_usd") or 0))
        return tokens, cost

    def _settle(self, rollout_id: str, record: dict | None) -> dict:
        """Charge a rollout's usage to its experiment exactly once and release its reservation.
        Without a readable record, the usage on disk is charged; if that is unreadable too, the full
        reservation is."""
        pending = require(self.store, "evolution_rollouts", rollout_id)
        if pending.get("settled", True):
            return pending
        reserved = int(pending.get("reserved_tokens") or 0)
        reserved_cost = float(pending.get("reserved_cost_usd") or 0)
        if record is None:
            used = self._usage_on_disk(pending["data_dir"])
            note = ("Interrupted; usage recovered from the rollout's own records." if used is not None else
                    "Interrupted; usage could not be read, so the full reservation was charged.")
            tokens, cost = used if used is not None else (reserved, reserved_cost)
            record = {"outcome": "interrupted", "error": note, "tokens": tokens, "cost_usd": cost,
                      "unresolved_tokens": reserved if used is None else 0,
                      "unresolved_cost_usd": reserved_cost if used is None else 0.0}

        def change(tx):
            # Checked again inside the transaction: settlement happens exactly once.
            current = tx.get("evolution_rollouts", rollout_id)
            if current.get("settled", True):
                return current
            experiment = tx.get("evolution_experiments", current["experiment_id"])
            tx.put("evolution_experiments", experiment["id"], {"spent": _spent_after(
                experiment["spent"], int(record.get("tokens", 0)), record.get("cost_usd", 0), rollout=True,
                pool=current["pool"], release=current["pool"], released=reserved, released_cost=reserved_cost,
                unresolved=int(record.get("unresolved_tokens") or 0),
                unresolved_cost=float(record.get("unresolved_cost_usd") or 0))})
            return tx.put("evolution_rollouts", rollout_id, {**record, "settled": True})

        return self.store.transact(change)

    def reconcile(self, experiment_id: str | None = None) -> None:
        """Settle rollouts and reflection calls left unfinished by a restart (idempotent)."""
        for pending in self.store.list("evolution_rollouts"):
            if pending.get("settled", True) or (experiment_id and pending["experiment_id"] != experiment_id):
                continue
            if self.store.get("evolution_experiments", pending["experiment_id"]):
                self._settle(pending["id"], None)
        for experiment in self.store.list("evolution_experiments"):
            if experiment_id and experiment["id"] != experiment_id:
                continue
            spent = experiment.get("spent") or {}
            leftover = int(spent.get("reserved_reflection", 0))
            leftover_cost = float(spent.get("reserved_cost_reflection", 0) or 0)
            if leftover or leftover_cost:
                # A reflection call was in flight: its usage is unknown, so its reservation is charged.
                self._spend(experiment["id"], leftover, leftover_cost, release="reflection", released=leftover,
                            released_cost=leftover_cost, unresolved=leftover, unresolved_cost=leftover_cost)

    async def rollout(self, experiment_id: str, task: dict, text: str | None, context: dict, *, phase: str,
                      candidate_label: str, extra_skill: dict | None = None) -> dict:
        """Run one rollout, or reuse a finished one with the same task, candidate and configuration."""
        experiment = require(self.store, "evolution_experiments", experiment_id)
        frozen = context["frozen"]
        if extra_skill:
            frozen = {**frozen, "skills": [*frozen["skills"], {**extra_skill, "status": "active"}]}
        # Always the full per-rollout cap: a trimmed rollout would not be comparable to the others.
        limits = {**experiment["limits"]}
        execution = {name: limits[name] for name in ("rollout_tokens", "max_steps", "rollout_seconds")}
        execution["command_timeout"] = int(limits.get("command_timeout", 300))
        provider = {k: v for k, v in context["provider"].items() if k not in NOT_BEHAVIORAL}
        key = _hash(experiment["role"], text, task["id"], task.get("version"), provider, execution,
                    [s["id"] for s in frozen["skills"]], frozen["wiki_revision"])
        # Held-out results never feed the search, and search results never stand in for held-out ones.
        heldout_phase = phase == "heldout"
        cached = next((r for r in self.store.list("evolution_rollouts", cache_key=key)
                       if scored(r) and (r.get("phase") == "heldout") == heldout_phase), None)
        if cached:
            if cached["experiment_id"] != experiment_id:
                cached = self.store.put("evolution_rollouts", uuid4().hex, {
                    **{k: v for k, v in cached.items() if k not in {"id", "created_at", "updated_at"}},
                    "experiment_id": experiment_id, "phase": phase, "reused_from": cached["id"]})
            return cached | {"statement": task["statement"], "task_title": task["title"]}
        # Final-comparison rollouts draw on the reserve; search rollouts on the search pool.
        pool = "heldout" if heldout_phase or experiment["kind"] == "skill" else "search"
        if reason := self._budget_reason(experiment_id, context["started"], pool=pool):
            return {"task_id": task["id"], "outcome": "skipped", "score": 0.0, "error": reason,
                    "statement": task["statement"], "task_title": task["title"]}
        candidate = None if text in (None, SEED_PLACEHOLDER) else {"version": candidate_label, "content": text}
        self._update(experiment_id, stage=f"{phase}: {task['id']} ({candidate_label})")
        # Reserve the full cap and record the rollout before it starts, so a restart mid-rollout can
        # settle its real usage instead of forgetting it.
        folder_id = uuid4().hex[:12]
        cap = int(limits["rollout_tokens"])
        # Dollars: the rollout may spend what is left in its pool, up to its token cap at the
        # worst-case price; its runtime admits every call against that allowance.
        dollars = self._dollars_left(require(self.store, "evolution_experiments", experiment_id), pool)
        allowance = 0.0 if dollars is None else round(min(dollars, cost_bound(context["provider"], 0, cap)), 6)
        if dollars is not None:
            limits["max_run_cost_usd"] = allowance

        def open_rollout(tx):
            # The pending record and its reservation are written together.
            current = tx.get("evolution_experiments", experiment_id)
            spent = dict(current["spent"])
            spent[f"reserved_{pool}"] = spent.get(f"reserved_{pool}", 0) + cap
            spent[f"reserved_cost_{pool}"] = round(spent.get(f"reserved_cost_{pool}", 0.0) + allowance, 6)
            tx.put("evolution_experiments", experiment_id, {"spent": spent})
            return tx.put("evolution_rollouts", uuid4().hex, {
                "experiment_id": experiment_id, "task_id": task["id"], "outcome": "running", "settled": False,
                "pool": pool, "reserved_tokens": cap, "reserved_cost_usd": allowance,
                "data_dir": str(self.root / "rollouts" / folder_id),
                "cache_key": key, "phase": phase, "candidate_label": candidate_label,
                "candidate_hash": _hash(text), "split": task["split"], "task_title": task["title"]})

        pending = self.store.transact(open_rollout)
        try:
            record = await run_rollout(
                root=self.root / "rollouts", task=task, hidden=self.tasks.hidden_checks(task["id"]),
                role=experiment["role"], candidate=candidate, frozen=frozen,
                provider_settings=context["provider"], limits=limits, python=context["python"],
                cancel=self.cancel_events.get(experiment_id), rollout_id=folder_id)
        except BaseException:
            self._settle(pending["id"], None)
            raise
        record.pop("id", None)
        saved = self._settle(pending["id"], record)
        return saved | {"statement": task["statement"], "task_title": task["title"]}

    def _optimize(self, experiment_id, tasks, seed_text, component, context, loop):
        """Run GEPA in a worker thread; rollouts and reflection calls run on the event loop."""
        import gepa
        from gepa.utils.stop_condition import MaxCandidateProposalsStopper

        experiment = require(self.store, "evolution_experiments", experiment_id)
        limits = experiment["limits"]
        labels = {_hash(seed_text): "baseline"}

        def runner(task, text):
            label = labels.setdefault(_hash(text), f"candidate {len(labels)}")
            phase = "reflection" if task["split"] == "train" else "validation"
            future = asyncio.run_coroutine_threadsafe(
                self.rollout(experiment_id, task, text, context, phase=phase, candidate_label=label), loop)
            return future.result()

        def reflect(prompt):
            messages = prompt if isinstance(prompt, list) else [{"role": "user", "content": prompt}]
            return asyncio.run_coroutine_threadsafe(self._reflect(experiment_id, messages), loop).result()

        def stop(_state):
            return self._budget_reason(experiment_id, context["started"]) is not None

        template = REFLECTION_TEMPLATE.format(role=ROLES[experiment["role"]][0],
                                              objective=experiment["objective"] or "(none given)",
                                              evidence=self._evidence(experiment))
        service = self

        class Log:
            """GEPA's progress lines, kept on the experiment record for the UI."""

            def log(self, message):
                lines = [*require(service.store, "evolution_experiments", experiment_id).get("log", []),
                         str(message).splitlines()[0][:300] if str(message).strip() else ""]
                service._update(experiment_id, log=[line for line in lines if line][-60:])

        run_dir = self.root / "experiments" / experiment_id / "gepa"
        run_dir.mkdir(parents=True, exist_ok=True)
        train = [tasks[t] for t in experiment["splits"]["train"]]
        validation = [tasks[t] for t in experiment["splits"]["validation"]]
        result = gepa.optimize(
            seed_candidate={component: seed_text}, trainset=train, valset=validation,
            adapter=EvoAdapter(runner, component), reflection_lm=reflect,
            reflection_prompt_template=template, candidate_selection_strategy="pareto",
            reflection_minibatch_size=min(limits["reflection_minibatch_size"], len(train)),
            max_metric_calls=limits["max_metric_calls"],
            stop_callbacks=[stop, MaxCandidateProposalsStopper(limits["max_iterations"])],
            run_dir=str(run_dir), seed=0, raise_on_exception=False, display_progress_bar=False, logger=Log(),
            skip_perfect_score=True)
        return self._record_candidates(experiment_id, result, component)

    async def _reflect(self, experiment_id: str, messages: list[dict]) -> str:
        experiment = require(self.store, "evolution_experiments", experiment_id)
        settings = experiment["provider"]
        budget = self._budget(experiment)
        bound = input_bound(messages)
        need = bound + output_limit(settings)
        need_cost = cost_bound(settings, bound, output_limit(settings)) if budget.get("total_usd") is not None else 0.0
        # Admit the call only if its worst case, plus the child minibatch rollout(s) needed to test
        # the proposal, fits in what is left of the search pool.
        follow_up = min(experiment["limits"]["reflection_minibatch_size"],
                        max(1, len(experiment["splits"]["train"]))) * budget["per_rollout_tokens"]

        follow_up_cost = (follow_up // max(1, budget["per_rollout_tokens"])) * (budget.get("rollout_cost_usd") or 0)

        def admit():
            current = require(self.store, "evolution_experiments", experiment_id)
            spent = current["spent"]
            if self._committed(spent, "search") + need + follow_up > budget["search_tokens"]:
                self.search_exhausted[experiment_id] = True
                raise BudgetExceeded(f"Search budget reached: a reflection call could use up to {need:,} tokens, "
                                     f"and testing its proposal needs up to {follow_up:,} more.")
            dollars = self._dollars_left(current, "search")
            if dollars is not None and need_cost + follow_up_cost > dollars:
                self.search_exhausted[experiment_id] = True
                raise BudgetExceeded(f"Search spending limit reached: a reflection call could cost up to "
                                     f"${need_cost:.4f}, and testing its proposal needs ${follow_up_cost:.2f} more.")
            self._reserve(experiment_id, "reflection", need, need_cost)

        def charge_reservation(reason_unresolved=True, reflection=False):
            self._spend(experiment_id, need, need_cost, reflection=reflection, release="reflection",
                        released=need, released_cost=need_cost, unresolved=need if reason_unresolved else 0,
                        unresolved_cost=need_cost if reason_unresolved else 0.0)

        client = ModelProvider(settings, get_api_key(settings))
        try:
            await confirm_price_tier(client, settings, messages, None, bound)
            admit()

            held = {"reserved": True}

            def before_retry(uncertain):
                if uncertain:  # the failed attempt may be billed: keep its reservation, admit again
                    held["reserved"] = False
                    charge_reservation()
                    admit()
                    held["reserved"] = True

            client.before_retry = before_retry
            try:
                response = await client.complete(messages)
            except ProviderError as exc:
                if not held["reserved"]:
                    pass
                elif exc.billing_uncertain:
                    charge_reservation()
                else:  # a known rejection before the model ran
                    self._spend(experiment_id, 0, 0, release="reflection", released=need, released_cost=need_cost)
                raise
            except BaseException:
                # Cancellation, a malformed stream or anything unexpected may follow submission:
                # charge the reservation still held (none if the budget refused a retry).
                if held["reserved"]:
                    charge_reservation()
                raise
        finally:
            await client.close()
        usage = response.usage or {}
        tokens = int(usage.get("input") or 0) + int(usage.get("output") or 0)
        # Missing or incomplete usage keeps the worst case for whatever was not reported.
        complete = usage_complete(usage)
        unresolved = 0 if complete else max(0, need - tokens)
        cost = float(usage.get("cost_usd") or 0)
        unresolved_cost = 0.0 if complete else max(0.0, need_cost - cost)
        self._spend(experiment_id, tokens + unresolved, cost + unresolved_cost, reflection=True,
                    release="reflection", released=need, released_cost=need_cost, unresolved=unresolved,
                    unresolved_cost=unresolved_cost)
        # Keep every proposal, including ones GEPA rejects, so the search can be inspected.
        proposals = require(self.store, "evolution_experiments", experiment_id).get("proposals", [])
        self._update(experiment_id, proposals=[*proposals, {"at": _now(), "text": (response.text or "")[:6000]}][-20:])
        return response.text or ""

    def _record_candidates(self, experiment_id, result, component):
        """Store GEPA's candidates as prompt versions with their ancestry and validation scores."""
        experiment = require(self.store, "evolution_experiments", experiment_id)
        project_id, role = experiment["project_id"], experiment["role"]
        ids: dict[int, str | None] = {0: experiment.get("baseline_prompt_id")}
        versions = [p["version"] for p in self.store.list("prompts", project_id=project_id) if p["role"] == role]
        candidates = []
        for index, candidate in enumerate(result.candidates):
            score = result.val_aggregate_scores[index]
            parents = [p for p in (result.parents[index] or []) if p is not None]
            entry = {"index": index, "validation_score": score, "parents": parents,
                     "subscores": result.val_subscores[index] if result.val_subscores else None}
            if index > 0:
                existing = next((p for p in self.store.list("prompts", project_id=project_id)
                                 if p.get("experiment_id") == experiment_id and p.get("gepa_index") == index), None)
                prompt = existing or self.store.put("prompts", uuid4().hex, {
                    "project_id": project_id, "role": role, "status": "candidate",
                    "version": max(versions, default=0) + 1, "content": candidate[component].strip(),
                    "experiment_id": experiment_id, "gepa_index": index,
                    "parent_prompt_ids": [ids.get(p) for p in parents], "validation_score": score,
                    "source_run_ids": experiment["source_run_ids"],
                    "rationale": f"GEPA candidate {index} from experiment {experiment_id[:8]}.",
                })
                versions.append(prompt["version"])
                ids[index] = prompt["id"]
                entry["prompt_id"] = prompt["id"]
            candidates.append(entry)
        best = result.best_idx
        self._update(experiment_id, gepa={
            "candidates": candidates, "best_index": best, "total_metric_calls": result.total_metric_calls,
            "per_task_best": _per_task_best(result.per_val_instance_best_candidates)})
        return result.candidates[best][component], ids.get(best) if best else None

    async def _heldout(self, experiment_id, tasks, seed_text, best_text, candidate_id, context) -> dict:
        experiment = require(self.store, "evolution_experiments", experiment_id)
        extra = None
        if experiment["kind"] == "skill":
            extra = require(self.store, "skills", experiment["skill_id"])
            arms = {"baseline": (seed_text, None), "candidate": (seed_text, extra)}
            evaluation_splits = ["validation", "heldout"]
        else:
            if candidate_id is None:
                # GEPA scores a proposal on validation only after it beats its parent on a minibatch.
                scored = len((experiment.get("gepa") or {}).get("candidates") or []) > 1
                tried = len(experiment.get("proposals") or [])
                reason = ("No candidate beat the baseline on validation." if scored else
                          f"All {tried} proposals were rejected on their reflection minibatch, so none was "
                          "scored on validation." if tried else "The optimizer proposed no candidate.")
                return {"gate": {"passed": False, "complete": False, "improved": False, "reasons": [reason]},
                        "candidate_prompt_id": None, "metrics": self._metrics(experiment_id)}
            arms = {"baseline": (seed_text, None), "candidate": (best_text, None)}
            evaluation_splits = ["heldout"]
        results = {arm: [] for arm in arms}
        for split in evaluation_splits:
            for task_id in experiment["splits"][split]:
                for arm, (text, skill) in arms.items():
                    label = "baseline" if arm == "baseline" else (
                        "skill" if skill else f"candidate v{require(self.store, 'prompts', candidate_id)['version']}")
                    record = await self.rollout(experiment_id, tasks[task_id], text, context,
                                                phase=split if split == "heldout" else "validation",
                                                candidate_label=label, extra_skill=skill)
                    results[arm].append({**record, "split": split})
        gate = self._gate(results, experiment["splits"]["heldout"])
        summary = {"candidate_prompt_id": candidate_id, "metrics": self._metrics(experiment_id), "gate": gate,
                   "incomplete": not gate["complete"]}
        if candidate_id:
            self.store.put("prompts", candidate_id, {"gate": summary["gate"], "heldout_experiment_id": experiment_id})
        if extra:
            self.store.put("skills", extra["id"], {"evaluation": {"experiment_id": experiment_id, **summary["gate"]}})
        return summary

    @staticmethod
    def _gate(results: dict, heldout_ids: list[str]) -> dict:
        """Compare the arms on held-out tasks. Each arm must have a scored result for every task;
        a failed evaluation in either arm invalidates the comparison instead of counting as evidence."""
        arms = {arm: {r["task_id"]: r for r in results[arm] if r.get("split") == "heldout"}
                for arm in ("baseline", "candidate")}
        reasons, invalid = [], []
        for arm, records in arms.items():
            for task_id in heldout_ids:
                record = records.get(task_id) or {}
                if not scored(record):
                    detail = record.get("checks_error") or record.get("error") or record.get("outcome") or "missing"
                    invalid.append(f"{arm} on {task_id} ({str(detail)[:80]})")
        complete = bool(heldout_ids) and not invalid
        if invalid:
            reasons.append("Held-out evaluation is not comparable: " + "; ".join(invalid) + ".")
        baseline, candidate = arms["baseline"], arms["candidate"]
        base_score = _mean(baseline[t].get("score", 0) for t in heldout_ids) if complete else None
        cand_score = _mean(candidate[t].get("score", 0) for t in heldout_ids) if complete else None
        regressions = [t for t in heldout_ids if complete and baseline[t].get("resolved")
                       and not candidate[t].get("resolved")]
        if regressions:
            reasons.append(f"Regressed on {', '.join(regressions)}.")
        if complete and cand_score - base_score < GATE["min_heldout_delta"]:
            reasons.append(f"Held-out score {cand_score} is below the baseline's {base_score}.")
        base_tokens = _mean(r.get("tokens", 0) for r in baseline.values()) or 0
        cand_tokens = _mean(r.get("tokens", 0) for r in candidate.values()) or 0
        if complete and base_tokens and cand_tokens > GATE["max_cost_ratio"] * base_tokens:
            reasons.append(f"Uses {cand_tokens / base_tokens:.1f}x the baseline's tokens.")
        # Paired outcomes per held-out task: the evidence behind the means, reported as is.
        paired = None
        if complete:
            outcomes = [(bool(baseline[t].get("resolved")), bool(candidate[t].get("resolved"))) for t in heldout_ids]
            paired = {"both": outcomes.count((True, True)), "baseline_only": outcomes.count((True, False)),
                      "candidate_only": outcomes.count((False, True)), "neither": outcomes.count((False, False)),
                      "tasks": {t: {"baseline": baseline[t].get("score"), "candidate": candidate[t].get("score")}
                                for t in heldout_ids}}
        leaks = [r["task_id"] for r in [*baseline.values(), *candidate.values()] if r.get("leakage")]
        if leaks:
            reasons.append(f"Possible evaluator leakage flagged on {', '.join(sorted(set(leaks)))}.")
        return {"passed": complete and not reasons, "complete": complete, "reasons": reasons,
                "improved": complete and cand_score > base_score,
                "heldout": {"tasks": len(heldout_ids), "baseline_score": base_score, "candidate_score": cand_score,
                            "baseline_resolved": sum(bool(r.get("resolved")) for r in baseline.values()),
                            "candidate_resolved": sum(bool(r.get("resolved")) for r in candidate.values()),
                            "baseline_tokens": base_tokens, "candidate_tokens": cand_tokens},
                "paired": paired, "regressions": regressions, "policy": GATE}

    def _metrics(self, experiment_id: str) -> dict:
        metrics = {}
        for rollout in self.store.list("evolution_rollouts", experiment_id=experiment_id):
            bucket = metrics.setdefault(rollout["phase"], {}).setdefault(rollout["candidate_label"], {
                "rollouts": 0, "resolved": 0, "score": 0.0, "tokens": 0, "cost_usd": 0.0, "outcomes": {}})
            bucket["rollouts"] += 1
            bucket["resolved"] += int(bool(rollout.get("resolved")))
            bucket["score"] += float(rollout.get("score") or 0)
            bucket["tokens"] += int(rollout.get("tokens") or 0)
            bucket["cost_usd"] += float(rollout.get("cost_usd") or 0)
            bucket["outcomes"][rollout["outcome"]] = bucket["outcomes"].get(rollout["outcome"], 0) + 1
        for phase in metrics.values():
            for bucket in phase.values():
                bucket["mean_score"] = round(bucket.pop("score") / bucket["rollouts"], 4)
        return metrics

    async def _maybe_auto_activate(self, experiment_id: str) -> None:
        experiment = require(self.store, "evolution_experiments", experiment_id)
        settings = self.settings(experiment["project_id"])
        summary = experiment.get("summary") or {}
        gate = summary.get("gate", {})
        if (experiment.get("origin") == "auto" and settings["auto_activate"] and settings["mode"] == "auto"
                and gate.get("complete") and gate.get("passed") and gate.get("improved")
                and summary.get("candidate_prompt_id")):
            self.activate(experiment["project_id"], summary["candidate_prompt_id"], actor="auto")

    # ------------------------------------------------------------------ promotion
    def activate(self, project_id: str, prompt_id: str, *, force: bool = False, actor: str = "user") -> dict:
        prompt = require(self.store, "prompts", prompt_id)
        if prompt["project_id"] != project_id:
            raise KeyError(f"No prompt {prompt_id!r} in this project.")
        gate = prompt.get("gate")
        if not (gate and gate.get("passed")) and not force:
            raise PermissionError("This version has not passed held-out evaluation. Activate it anyway to "
                                  "override; it will be labelled as activated without passing evaluation.")
        previous = self._active_prompt(project_id, prompt["role"])
        if previous and previous["id"] != prompt_id:
            self.store.put("prompts", previous["id"], {"status": "superseded"})
        label = ("passed held-out evaluation" if gate and gate.get("passed")
                 else "activated without passing evaluation" if gate else "activated without evaluation")
        history = [*prompt.get("activations", []), {"at": _now(), "by": actor, "label": label,
                                                    "replaced_prompt_id": previous["id"] if previous else None}]
        return self.store.put("prompts", prompt_id, {"status": "active", "activation_label": label,
                                                     "activations": history})

    def rollback(self, project_id: str, role: str, prompt_id: str | None = None) -> dict:
        """Return a role to an earlier version, or to the built-in instructions when prompt_id is None."""
        current = self._active_prompt(project_id, role)
        if prompt_id:
            target = require(self.store, "prompts", prompt_id)
            if target["project_id"] != project_id or target["role"] != role:
                raise KeyError("Choose a version of this role.")
        if current:
            self.store.put("prompts", current["id"], {"status": "rolled_back", "rolled_back_at": _now()})
        if not prompt_id:
            return {"role": role, "active": None, "rolled_back": current["id"] if current else None}
        history = [*target.get("activations", []), {"at": _now(), "by": "user", "label": "rollback",
                                                    "replaced_prompt_id": current["id"] if current else None}]
        restored = self.store.put("prompts", prompt_id, {"status": "active", "activations": history})
        return {"role": role, "active": restored, "rolled_back": current["id"] if current else None}

    def cleanup_rollout_dirs(self, keep_days: int = 30) -> None:
        cutoff = time.time() - keep_days * 86400
        for folder in (self.root / "rollouts").glob("*"):
            if folder.is_dir() and folder.stat().st_mtime < cutoff:
                shutil.rmtree(folder, ignore_errors=True)
