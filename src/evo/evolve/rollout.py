"""One evaluation rollout: the real EVO harness on one task, fully isolated from the user's data.

Each rollout gets its own data directory, SQLite store, runtime and copy of the task snapshot, plus
the frozen memory and skills captured when the experiment started. Tools with outside effects are
disabled (web, GitHub, memory writes, skill proposals, talking to the user, delegation). Nothing is
written to the user's store; evaluation runtimes never feed the reflection queue.

The evaluated unit is one role (Code by default) working alone from the task statement to a committed
change. The score comes from executable hidden checks, not from a model judge.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import time
from pathlib import Path
from uuid import uuid4

from ..runtime import Runtime
from ..store import Store
from .tasks import run_pytest, score_checks

DENY = ["web.*", "github.*", "memory.write", "skills.propose", "chat.*", "agents.spawn", "agents.send"]
# Paths to evaluator-only data or the full-history mirror. Web and GitHub tools are disabled outright.
LEAK_MARKERS = ("capstone", "repo.git", "history.db", "evolution/tasks", "evolution\\tasks", "hidden/checks",
                "hidden\\checks", "checks.json")
# File contents written by these tools are not scanned, only the paths they touch.
CONTENT_TOOLS = {"repo.write", "repo.apply_patch", "artifacts.write", "repo.commit"}
EVAL_PROJECT = "evaluation"


def task_prompt(task: dict) -> str:
    return (
        f"Fix this issue in the repository snapshot (taken {task['as_of']}). The issue text is also in ISSUE.md.\n\n"
        f"{task['statement']}\n"
        "Read AGENTS.md first. Make the fix with a regression test, run the relevant tests with "
        "`python run_checks.py ...`, and commit with repo.commit. This evaluation has no Test or QA stage: "
        "finish with a short summary of the change and the checks you ran."
    )


def _git(cwd: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=300)
    if check and result.returncode:
        raise RuntimeError(result.stderr.strip() or "git failed")
    return result.stdout


def leakage_audit(events: list[dict], extra_markers: tuple[str, ...] = ()) -> list[dict]:
    """Flag tool calls that reference evaluator-only data or the future (detected, not prevented)."""
    markers = tuple(m.lower() for m in (*LEAK_MARKERS, *extra_markers) if m)
    flagged = []
    for event in events:
        if event["kind"] != "tool.started":
            continue
        arguments = event["data"].get("arguments")
        if event["data"].get("name") in CONTENT_TOOLS and isinstance(arguments, dict):
            arguments = (arguments.get("args") or [None])[:1]
        text = json.dumps(arguments, ensure_ascii=False).lower().replace("\\\\", "\\")
        hits = [marker for marker in markers if marker in text]
        if hits:
            flagged.append({"event_id": event["id"], "tool": event["data"].get("name"), "markers": hits})
    return flagged


async def run_rollout(*, root: Path, task: dict, hidden: dict, role: str, candidate: dict | None,
                      frozen: dict, provider_settings: dict, limits: dict, python: str,
                      cancel: asyncio.Event | None = None, rollout_id: str | None = None) -> dict:
    """Run one rollout and return its record (never raises for agent failures)."""
    rollout_id = rollout_id or uuid4().hex[:12]
    folder = Path(root) / rollout_id
    folder.mkdir(parents=True)
    started = time.monotonic()
    record = {"id": rollout_id, "task_id": task["id"], "split": task.get("split"), "role": role,
              "candidate_version": (candidate or {}).get("version", "baseline"), "outcome": "error",
              "score": 0.0, "resolved": False, "tokens": 0, "cost_usd": 0.0, "data_dir": str(folder)}
    store = runtime = None
    try:
        repo = folder / "repo"
        _git(folder, "clone", "-q", str(Path(task["dir"]) / "snapshot"), str(repo))
        store = Store(folder / "data")
        overrides = {
            "max_run_tokens": int(limits["rollout_tokens"]), "max_agents": 1, "max_depth": 0,
            "max_steps": int(limits.get("max_steps", 60)), "runtime_preset": "custom",
            "command_timeout": int(limits.get("command_timeout", 300)),
            # The experiment's remaining dollars for this rollout (None: no dollar limit).
            "max_run_cost_usd": limits.get("max_run_cost_usd"),
        }
        store.put("settings", "provider", {**provider_settings, **overrides})
        store.put("projects", EVAL_PROJECT, {"name": "pytest", "path": str(repo),
                                             "description": "Evaluation snapshot of pytest."})
        if frozen.get("wiki_dir") and frozen.get("wiki_revision"):
            wiki = store.data_dir / "knowledge" / EVAL_PROJECT
            _git(store.data_dir, "clone", "-q", frozen["wiki_dir"], str(wiki))
            _git(wiki, "checkout", "-q", "-B", "main", frozen["wiki_revision"])
        for skill in frozen.get("skills", []):
            store.put("skills", skill["id"], {**{k: v for k, v in skill.items() if k not in {"created_at", "updated_at"}},
                                              "project_id": EVAL_PROJECT})
        runtime = Runtime(store, evaluation={"rollout_id": rollout_id, "task_id": task["id"]})
        # The experiment's frozen provider settings win over anything in the environment.
        run = await runtime.start_evaluation_run(
            EVAL_PROJECT, role, task_prompt(task), candidate=candidate,
            settings_overrides={**provider_settings, **overrides}, tool_policy={"deny": DENY})
        record["run_id"] = run["id"]
        used = run["settings_snapshot"]
        drift = [name for name in ("model", "api_style", "base_url", "reasoning_effort")
                 if name in provider_settings and used.get(name) != provider_settings[name]]
        if drift:
            await runtime.cancel(run["id"])
            raise RuntimeError(f"rollout settings differ from the experiment's ({', '.join(drift)}).")
        root_id = run["root_agent_id"]
        deadline = started + float(limits.get("rollout_seconds", 1200))
        outcome = None
        while root_id in runtime.tasks:
            if cancel is not None and cancel.is_set():
                outcome = "cancelled"
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                outcome = "timeout"
                break
            waiters = [runtime.tasks[root_id]]
            if cancel is not None:
                waiters.append(asyncio.ensure_future(cancel.wait()))
            done, pending = await asyncio.wait(waiters, timeout=min(remaining, 5.0),
                                               return_when=asyncio.FIRST_COMPLETED)
            for waiter in pending:
                if waiter is not runtime.tasks.get(root_id):
                    waiter.cancel()
        if outcome:
            await runtime.cancel(run["id"])
        run = store.get("runs", run["id"])
        if outcome is None:
            error = run.get("error") or ""
            # Stopped by the experiment's spending limit: not a property of the agent, never scored.
            outcome = ("spending_limit" if "spending limit" in error
                       else "budget" if "token budget" in error or "step limit" in error
                       else "error" if run["status"] == "failed" else "completed")
        usage = run.get("token_usage", {})
        events = store.events(run["id"], limit=100_000)
        root_agent = store.get("agents", root_id)
        record.update(
            # Unresolved: reservations kept for requests that may have been billed without usage.
            outcome=outcome, error=run.get("error"),
            tokens=usage.get("input", 0) + usage.get("output", 0) + usage.get("unresolved", 0),
            unresolved_tokens=usage.get("unresolved", 0), cache_write_tokens=usage.get("cache_write", 0),
            input_tokens=usage.get("input", 0), output_tokens=usage.get("output", 0),
            cached_tokens=usage.get("cached", 0),
            cost_usd=round((usage.get("cost_usd", 0) or 0) + (usage.get("unresolved_cost_usd", 0) or 0), 6),
            unresolved_cost_usd=round(usage.get("unresolved_cost_usd", 0) or 0, 6),
            model=run["settings_snapshot"].get("model"), api_style=run["settings_snapshot"].get("api_style"),
            steps=sum(1 for e in events if e["kind"] == "message" and e["data"].get("role") == "assistant"),
            tool_calls=sum(1 for e in events if e["kind"] == "tool.started" and "call_id" not in e["data"]),
            commands=[json.dumps(e["data"].get("arguments"), ensure_ascii=False)[:300] for e in events
                      if e["kind"] == "tool.started" and e["data"].get("name") == "shell.run"][:25],
            final_message=str((root_agent or {}).get("result") or "")[:4000],
            leakage=leakage_audit(events, (str(Path(task["dir"]).parent),)),
        )
        if outcome == "cancelled":
            return record
        # Score whatever the agent left in its worktree, committed or not.
        workdir = Path(run["workdir"])
        _git(workdir, "add", "--all")
        diff = _git(workdir, "diff", "--cached", run["base_commit"], "--", ".", ":(exclude).evo-junit.xml")
        record["diff"] = diff[:40_000]
        record["changed_files"] = [line for line in _git(
            workdir, "diff", "--cached", "--name-only", run["base_commit"]).splitlines() if line]
        hidden_dir = Path(task["dir"]) / "hidden"
        for name in hidden["test_files"]:
            target = workdir / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(hidden_dir / name, target)
        checks = await asyncio.to_thread(run_pytest, python, workdir, hidden["test_files"])
        if checks.get("timed_out"):
            record.update(checks_error="Hidden checks timed out.", score=0.0)
        else:
            record.update(score_checks(hidden, checks["outcomes"]))
            record["checks_output"] = checks["output"][-1500:]
    except asyncio.CancelledError:
        record["outcome"] = "cancelled"
        raise
    except Exception as exc:  # harness problems are recorded, not raised
        record.update(outcome="error", error=f"Evaluation harness: {exc}")
    finally:
        record["latency_s"] = round(time.monotonic() - started, 1)
        if runtime:
            await runtime.close()
        if store:
            store.close()
        # Keep the rollout's event log for inspection; drop the bulky worktrees.
        for bulky in (folder / "repo", folder / "data" / "workspaces"):
            shutil.rmtree(bulky, ignore_errors=True)
    return record
