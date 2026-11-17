"""Durable run artifacts and evidence-gated workflow transitions."""

from __future__ import annotations

from pathlib import Path
from threading import RLock
from uuid import uuid4

from .common import MAX_TEXT_BYTES
from .store import Store, require
from .workspaces import git, safe_path

_LOCK = RLock()
_PHASES = ("triage", "spec", "code", "test", "qa", "ready")
_TARGETS = set(_PHASES) | {"blocked"}


class ArtifactStore:
    def __init__(self, store: Store):
        self.store = store
        self.root = store.data_dir / "artifacts"

    def list(self, run_id: str) -> list[dict]:
        require(self.store, "runs", run_id)
        return self.store.list("artifacts", run_id=run_id)

    def write(self, run_id: str, agent_id: str | None, name: str, content: str,
              kind: str = "text", citations: list | None = None) -> dict:
        run = require(self.store, "runs", run_id)
        name = name.strip()
        if not name or len(name) > 180:
            raise ValueError("Provide an artifact name of up to 180 characters.")
        encoded = content.encode("utf-8")
        if len(encoded) > MAX_TEXT_BYTES:
            raise ValueError("Text artifacts must be 8 MiB or smaller.")
        artifact_id = uuid4().hex
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / artifact_id
        path.write_bytes(encoded)
        try:
            return self.store.put("artifacts", artifact_id, {
                "project_id": run["project_id"], "run_id": run_id, "agent_id": agent_id,
                "name": name, "kind": kind, "size": len(encoded), "citations": citations or [],
            })
        except Exception:
            path.unlink(missing_ok=True)
            raise

    def read(self, artifact_id: str) -> dict:
        record = require(self.store, "artifacts", artifact_id)
        with safe_path(self.root, artifact_id).open(encoding="utf-8", newline="") as artifact:
            return record | {"content": artifact.read()}


class Workflow:
    def __init__(self, store: Store):
        self.store = store

    def status(self, run_id: str) -> dict:
        run = require(self.store, "runs", run_id)
        return {key: run.get(key) for key in ("phase", "base_commit", "implementation_commit",
                                              "writer_agent_id")} | {
            "handoffs": self.store.list("workflow_handoffs", run_id=run_id),
            "findings": self.store.list("workflow_findings", run_id=run_id),
        }

    def _evidence(self, run_id: str, artifacts: list[str]) -> None:
        for artifact_id in artifacts:
            artifact = require(self.store, "artifacts", artifact_id)
            if artifact["run_id"] != run_id:
                raise ValueError("Workflow evidence must come from this run.")

    def handoff(self, agent_id: str, target: str, summary: str, artifacts: list[str] | None = None,
                commit: str | None = None, passed: bool | None = None) -> dict:
        agent = require(self.store, "agents", agent_id)
        role, run_id = agent["role"].lower(), agent["run_id"]
        if target not in _TARGETS or not summary.strip():
            raise ValueError("Provide a supported target phase and a handoff summary.")
        artifacts = list(dict.fromkeys(artifacts or []))
        self._evidence(run_id, artifacts)
        with _LOCK:
            run = require(self.store, "runs", run_id)
            if commit is not None and commit != run.get("implementation_commit"):
                raise ValueError("Handoff commit must match the published implementation commit.")
            if role in {"test", "qa"} and commit and commit != agent.get("workspace_commit"):
                raise ValueError("Verify the implementation commit in your own worktree.")
            if role in {"test", "qa"} and passed is True:
                workspace = agent.get("workdir")
                if not commit or not workspace or git(Path(workspace), "rev-parse", "HEAD") != commit:
                    raise ValueError("Passing verification requires checking out the published commit.")
                if git(Path(workspace), "status", "--porcelain"):
                    raise ValueError("Restore your verification worktree before marking the commit as passed.")
            handoff = self.store.put("workflow_handoffs", uuid4().hex, {
                "run_id": run_id, "agent_id": agent_id, "source": role, "target": target,
                "summary": summary, "artifacts": artifacts, "commit": commit, "passed": passed,
                "round": run.get("workflow_round", 1),
            })
            self.store.event(run_id, agent_id, "workflow.handoff", handoff)
        return handoff

    def advance(self, agent_id: str, phase: str, handoff_id: str | None = None) -> dict:
        agent = require(self.store, "agents", agent_id)
        with _LOCK:
            run = require(self.store, "runs", agent["run_id"])
            current = run.get("phase") or "triage"
            if current == phase:
                return self.status(run["id"])
            if phase not in _TARGETS:
                raise ValueError("Unknown workflow phase.")
            role = agent["role"].lower()
            root = run.get("root_agent_id") == agent_id
            if role != current and not root:
                raise PermissionError("Only the active role or root agent can advance the workflow.")
            if current == "ready":
                if not root or phase != "spec":
                    raise ValueError("The root agent can start a follow-up implementation at Spec.")
                self.store.put("runs", run["id"], {
                    "phase": "spec", "workflow_round": run.get("workflow_round", 1) + 1,
                    "tested_commit": None, "verified_commit": None,
                    "previous_implementation_commit": run.get("implementation_commit"),
                })
                self.store.event(run["id"], agent_id, "workflow.phase", {
                    "previous": current, "phase": phase,
                })
                return self.status(run["id"])
            evidence = require(self.store, "workflow_handoffs", handoff_id) if handoff_id else None
            if evidence and (evidence["run_id"] != run["id"] or evidence["target"] != phase
                             or evidence["source"] != current
                             or evidence.get("round", 1) != run.get("workflow_round", 1)):
                raise ValueError("Select a handoff from this run and current phase to the requested phase.")
            blocked = phase == "blocked" or current == "blocked"
            repair = phase == "code" and current in {"test", "qa"}
            forward = current in _PHASES and phase in _PHASES and (
                _PHASES.index(phase) == _PHASES.index(current) + 1
            )
            if not (blocked or repair or forward):
                raise ValueError(f"Workflow cannot advance directly from {current} to {phase}.")
            if current == "blocked" and (not root or phase != run.get("blocked_from")):
                raise ValueError("The root agent can resume the phase where the run was blocked.")
            if repair and (not evidence or evidence["passed"] is not False):
                raise ValueError("Returning to Code requires a failed verification handoff.")
            if forward and current == "spec" and (not evidence or not evidence["artifacts"]):
                raise ValueError("Specification handoff must attach a specification artifact.")
            commit = run.get("implementation_commit")
            if (forward and current in {"code", "test", "qa"}) or repair:
                if not commit or not evidence or evidence["commit"] != commit:
                    raise ValueError("Attach a handoff for the current published implementation commit.")
                if forward and current == "code" and commit == run.get("previous_implementation_commit"):
                    raise ValueError("Publish a new implementation commit for this revision.")
                if git(Path(run["workdir"]), "rev-parse", "HEAD") != commit or git(
                    Path(run["workdir"]), "status", "--porcelain"
                ):
                    raise ValueError("Publish the implementation worktree before advancing.")
            if forward and current in {"test", "qa"}:
                if not evidence or evidence["passed"] is not True or not evidence["artifacts"]:
                    raise ValueError("Verification must pass and include a verification artifact.")
                if current == "qa" and run.get("tested_commit") != commit:
                    raise ValueError("Test must pass on the same commit before QA can mark it ready.")
            updates = {"phase": phase}
            if phase == "blocked":
                updates["blocked_from"] = current
            if forward and current == "test":
                updates["tested_commit"] = commit
            if forward and current == "qa":
                updates["verified_commit"] = commit
            if repair:
                updates.update(tested_commit=None, verified_commit=None, previous_implementation_commit=commit)
            self.store.put("runs", run["id"], updates)
            self.store.event(run["id"], agent_id, "workflow.phase", {
                "previous": current, "phase": phase, "handoff_id": handoff_id, "commit": commit,
            })
        return self.status(run["id"])

    def finding(self, agent_id: str, title: str, details: str, severity: str = "medium",
                artifact_id: str | None = None) -> dict:
        agent = require(self.store, "agents", agent_id)
        run_id = agent["run_id"]
        if not title.strip() or not details.strip() or severity not in {"low", "medium", "high", "critical"}:
            raise ValueError("Provide a title, details, and severity (low, medium, high, or critical).")
        if artifact_id:
            self._evidence(run_id, [artifact_id])
        finding = self.store.put("workflow_findings", uuid4().hex, {
            "run_id": run_id, "agent_id": agent_id, "title": title, "details": details,
            "severity": severity, "artifact_id": artifact_id, "commit": agent.get("workspace_commit"),
        })
        self.store.event(run_id, agent_id, "workflow.finding", finding)
        return finding
