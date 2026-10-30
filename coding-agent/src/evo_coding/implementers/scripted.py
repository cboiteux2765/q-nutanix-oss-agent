"""Deterministic implementer that replays a recorded edit script.

Used for tests, the offline pilot demo, and replaying historical fixes. No model calls.

Script format (JSON, or the [implementation] table of a pilot scenario .toml):
    {"revisions": {"0": {...}, "1": {...}}}     or a single revision object:
    {"actions": [{"write": "path", "content": "..."},
                 {"replace": "path", "old": "...", "new": "...", "count": 1},
                 {"delete": "path"},
                 {"symlink": "path", "target": "..."},
                 {"run": "shell command"}],
     "notes": {"summary": "...", "criteria": {"AC-1": {"status": "IMPLEMENTED", "notes": "..."}}},
     "cost_usd_per_action": 0.0}
"""
from __future__ import annotations

import json
import os
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..config import BudgetTracker, CancelToken
from ..workspace import Workspace
from .base import NOTES_PATH, ImplementationOutcome, Implementer, TaskBrief


class ScriptError(Exception):
    pass


def _safe_path(root: Path, relative: str) -> Path:
    target = (root / relative).resolve()
    if target != root and root not in target.parents:
        raise ScriptError(f"script path escapes the workspace: {relative}")
    return target


class ScriptedImplementer(Implementer):
    name = "scripted"
    version = "1"

    def __init__(self, script: dict[str, Any] | Path, hooks: dict[str, Callable[[], None]] | None = None):
        if isinstance(script, (str, Path)):
            text = Path(script).read_text(encoding="utf-8")
            if Path(script).suffix == ".toml":  # a pilot scenario file: use its [implementation] table
                script = tomllib.loads(text)["implementation"]
            else:
                script = json.loads(text)
        self.script = script
        self.hooks = hooks or {}

    def _revision_script(self, revision: int) -> dict[str, Any]:
        if "revisions" in self.script:
            revisions = self.script["revisions"]
            if str(revision) not in revisions:
                raise ScriptError(f"script has no entry for revision {revision}")
            return revisions[str(revision)]
        if revision:
            raise ScriptError("script has no revision entries")
        return self.script

    def implement(self, brief: TaskBrief, workspace: Workspace, budget: BudgetTracker,
                  cancel: CancelToken) -> ImplementationOutcome:
        script = self._revision_script(brief.revision)
        root = workspace.root
        transcript: list[dict[str, Any]] = []
        cost = float(script.get("cost_usd_per_action", 0.0))
        for index, action in enumerate(script.get("actions", [])):
            cancel.raise_if_cancelled()
            budget.charge_step()
            if cost:
                budget.charge_llm(cost)
            budget.check()
            entry: dict[str, Any] = {"step": index, "action": action}
            if "write" in action:
                path = _safe_path(root, action["write"])
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(action["content"], encoding="utf-8", newline="\n")
            elif "replace" in action:
                path = _safe_path(root, action["replace"])
                text = path.read_text(encoding="utf-8")
                if action["old"] not in text:
                    raise ScriptError(f"replace target not found in {action['replace']}")
                path.write_text(text.replace(action["old"], action["new"], action.get("count", 1)),
                                encoding="utf-8", newline="\n")
            elif "delete" in action:
                _safe_path(root, action["delete"]).unlink()
            elif "symlink" in action:
                os.symlink(action["target"], _safe_path(root, action["symlink"]))
            elif "run" in action:
                result = workspace.run(action["run"])
                entry["returncode"] = result.returncode
                entry["output"] = result.output[-4000:]
            elif "hook" in action:
                self.hooks[action["hook"]]()
            else:
                raise ScriptError(f"unknown script action: {action}")
            transcript.append(entry)
        notes = script.get("notes")
        if notes is not None:
            notes_path = root / NOTES_PATH
            notes_path.parent.mkdir(parents=True, exist_ok=True)
            notes_path.write_text(json.dumps(notes, indent=2), encoding="utf-8")
        return ImplementationOutcome(exit_status=script.get("exit_status", "Submitted"),
                                     steps=len(transcript), transcript=transcript)
