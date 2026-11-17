"""Manage isolated implementation and verification worktrees."""

import os
from pathlib import Path
import re
import shutil
import subprocess
from tempfile import TemporaryFile
from threading import RLock

from .common import Conflict, FINISHED
from .store import Store, require

_LOCK = RLock()
_IGNORED = {".git", ".evo", ".venv", "node_modules", "__pycache__", ".env"}
_SNAPSHOT_MAX_BYTES = 512 * 1024 * 1024
_SNAPSHOT_MAX_FILES = 20_000
_ALLOWED_ENV = {
    "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP",
    "HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA",
    "LANG", "LC_ALL", "TERM", "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMDATA",
}


def local_env() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if key.upper() in _ALLOWED_ENV}


def safe_path(root: Path, path: str) -> Path:
    relative = Path(path)
    if relative.anchor or any(part == ".." or ":" in part for part in relative.parts):
        raise ValueError("Use a relative path within the workspace.")
    resolved = (root / relative).resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise ValueError("Path escapes the workspace.")
    return resolved


def git(cwd: Path, *args: str, check: bool = True, timeout: int = 60, trim: bool = True) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout,
        env=local_env() | {"GIT_TERMINAL_PROMPT": "0"},
    )
    if check and result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "Git command failed.")
    return result.stdout.rstrip("\r\n") if trim else result.stdout


def _copy_source(source: Path, destination: Path, data_dir: Path) -> None:
    total = 0
    files = 0
    for directory, dirs, names in os.walk(source):
        current = Path(directory)
        dirs[:] = [
            name for name in dirs
            if name.lower() not in _IGNORED and not (current / name).is_symlink()
            and (current / name).resolve() not in {destination.parent.resolve(), data_dir}
            and not (current / name).is_junction()
        ]
        target = destination / current.relative_to(source)
        target.mkdir(parents=True, exist_ok=True)
        for name in names:
            path = current / name
            normalized = name.lower()
            if normalized in _IGNORED or normalized.startswith(".env.") and normalized != ".env.example":
                continue
            if path.is_symlink() or not path.is_file():
                continue
            total += path.stat().st_size
            files += 1
            if total > _SNAPSHOT_MAX_BYTES or files > _SNAPSHOT_MAX_FILES:
                raise ValueError("Project exceeds the non-Git snapshot limit of 512 MiB or 20,000 files.")
            shutil.copy2(path, target / name)


def _working_tree_patch(source: Path, destination: Path, data_dir: Path) -> None:
    common = ["diff", "--binary", "--no-ext-diff", "--no-textconv", "--ignore-submodules=all", "HEAD", "--"]
    exclusions = [f":(exclude,icase,glob)**/{name}/**" for name in sorted(_IGNORED) if name != ".env"]
    if data_dir != source and data_dir.is_relative_to(source):
        exclusions.append(":(exclude,literal)" + data_dir.relative_to(source).as_posix())
    local_env_files = [":(exclude,icase,glob)**/.env", ":(exclude,icase,glob)**/.env.*"]
    # Include tracked environment templates while keeping local environment files out of the snapshot.
    changes = ([".", *exclusions, *local_env_files],
               [":(icase,glob)**/.env.example", *exclusions])
    env = local_env() | {"GIT_TERMINAL_PROMPT": "0"}
    with TemporaryFile() as patch:
        for pathspecs in changes:
            generated = subprocess.run(
                ["git", "-C", str(source), *common, *pathspecs], stdout=patch, stderr=subprocess.PIPE,
                env=env, timeout=60,
            )
            if generated.returncode:
                raise RuntimeError(generated.stderr.decode("utf-8", errors="replace").strip())
            if patch.tell() > 128 * 1024 * 1024:
                raise ValueError("The tracked changes exceed the 128 MiB workspace patch limit.")
        if patch.tell():
            patch.seek(0)
            applied = subprocess.run(
                ["git", "-C", str(destination), "apply", "--binary", "--whitespace=nowarn", "-"],
                stdin=patch, capture_output=True, env=env, timeout=60,
            )
            if applied.returncode:
                details = applied.stderr.decode("utf-8", errors="replace").strip()
                raise RuntimeError(f"Could not snapshot the working-tree changes: {details}")


def _copy_untracked(source: Path, destination: Path, data_dir: Path) -> None:
    files = git(source, "ls-files", "-z", "--others", "--exclude-standard").split("\0")
    count = 0
    total = 0
    for name in files:
        if not name:
            continue
        relative = Path(name)
        parts = [part.lower() for part in relative.parts]
        if any(part in _IGNORED or (part.startswith(".env.") and part != ".env.example") for part in parts):
            continue
        raw_path = source / relative
        if raw_path.is_symlink() or raw_path.is_junction():
            raise ValueError(f"Commit or ignore the untracked link {name!r} before creating a workspace.")
        path = safe_path(source, name)
        if path.is_relative_to(data_dir) or not path.is_file():
            continue
        target = safe_path(destination, name)
        if target.exists():
            raise Conflict(f"Untracked file {name!r} conflicts with the isolated checkout.")
        count += 1
        total += path.stat().st_size
        if total > _SNAPSHOT_MAX_BYTES or count > _SNAPSHOT_MAX_FILES:
            raise ValueError("Untracked files exceed the snapshot limit of 512 MiB or 20,000 files.")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)


class WorkspaceManager:
    def __init__(self, store: Store):
        self.store = store
        self.root = store.data_dir / "workspaces"

    def create_run(self, project_id: str, run_id: str) -> dict:
        project = require(self.store, "projects", project_id)
        source = Path(project["path"]).expanduser().resolve(strict=True)
        if not source.is_dir():
            raise ValueError("Project path must be a directory.")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", run_id):
            raise ValueError("Invalid run ID.")
        workdir = safe_path(self.root, f"{run_id}/implementation")
        branch = f"evo/{run_id[:12]}"
        with _LOCK:
            workdir.parent.mkdir(parents=True, exist_ok=True)
            if workdir.exists():
                raise ValueError(f"Workspace already exists for run {run_id}.")
            top = git(source, "rev-parse", "--show-toplevel", check=False)
            if top:
                if Path(top).resolve() != source:
                    raise ValueError("Select the repository root as the project path.")
                if git(source, "ls-files", "--unmerged"):
                    raise Conflict("Resolve the source repository's merge conflicts before creating a workspace.")
                source_commit = base = git(source, "rev-parse", "--verify", "HEAD")
                git(source, "worktree", "add", "-b", branch, str(workdir), base)
                try:
                    _working_tree_patch(source, workdir, self.store.data_dir)
                    _copy_untracked(source, workdir, self.store.data_dir)
                    if git(workdir, "status", "--porcelain"):
                        git(workdir, "add", "--all")
                        git(workdir, "-c", "user.name=EVO", "-c", "user.email=evo@localhost",
                            "commit", "-m", "Snapshot local project changes for EVO")
                        base = git(workdir, "rev-parse", "HEAD")
                except Exception:
                    git(source, "worktree", "remove", "--force", str(workdir), check=False)
                    git(source, "branch", "-D", branch, check=False)
                    raise
            else:
                source_commit = None
                try:
                    _copy_source(source, workdir, self.store.data_dir)
                    git(workdir, "init", "-b", branch)
                    git(workdir, "add", "--all")
                    git(workdir, "-c", "user.name=EVO", "-c", "user.email=evo@localhost",
                        "commit", "--allow-empty", "-m", "Snapshot project for EVO")
                    base = git(workdir, "rev-parse", "HEAD")
                except Exception:
                    if workdir.exists():
                        shutil.rmtree(workdir)
                    raise
        return {"workdir": str(workdir), "branch": branch, "base_commit": base,
                "source_commit": source_commit}

    def prepare_agent(self, agent_id: str, commit: str | None = None) -> dict:
        agent = require(self.store, "agents", agent_id)
        run = require(self.store, "runs", agent["run_id"])
        workdir = Path(run["workdir"])
        role = agent["role"].lower()
        if role == "code":
            return {"workdir": str(workdir), "branch": run["branch"],
                    "base_commit": run["base_commit"]}
        if role not in {"triage", "orchestrator", "spec", "test", "qa", "helper"}:
            raise ValueError("Unknown workspace role.")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", agent_id):
            raise ValueError("Invalid agent ID.")
        commit = commit or run.get("implementation_commit") or run["base_commit"]
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit):
            raise ValueError("Use a full Git commit ID.")
        verification = workdir.parent / f"{role}-{agent_id}"
        with _LOCK:
            if verification.exists():
                old_base = agent.get("workspace_commit") or git(verification, "rev-parse", "HEAD")
                if old_base != commit:
                    if git(verification, "status", "--porcelain"):
                        raise ValueError("Preserve your changes before changing verification commits.")
                    git(verification, "checkout", "--detach", commit)
            else:
                git(workdir, "worktree", "add", "--detach", str(verification), commit)
            self.store.put("agents", agent_id, {
                "workdir": str(verification), "workspace_commit": commit,
            })
        return {"workdir": str(verification), "branch": None, "base_commit": commit}

    def acquire_writer(self, agent_id: str, *, publishing: bool = False) -> dict:
        agent = require(self.store, "agents", agent_id)
        if agent["role"].lower() != "code":
            raise PermissionError("Only Code can write the implementation worktree.")
        with _LOCK:
            run = require(self.store, "runs", agent["run_id"])
            if not publishing and run.get("phase") != "code":
                raise PermissionError("Implementation writes are available during the Code phase.")
            owner = run.get("writer_agent_id")
            if owner and owner != agent_id:
                previous = self.store.get("agents", owner)
                if previous and previous["status"] not in FINISHED:
                    raise PermissionError("Another Code agent owns this run's worktree.")
            return self.store.put("runs", run["id"], {"writer_agent_id": agent_id})

    def commit(self, agent_id: str, message: str) -> dict:
        with _LOCK:
            run = self.acquire_writer(agent_id)
            workdir = Path(run["workdir"])
            if git(workdir, "branch", "--show-current") != run["branch"]:
                raise ValueError("Switch back to the run branch before publishing a commit.")
            if not message.strip():
                raise ValueError("A commit message is required.")
            git(workdir, "add", "--all")
            if git(workdir, "status", "--porcelain"):
                git(workdir, "-c", "user.name=EVO", "-c", "user.email=evo@localhost",
                    "commit", "-m", message)
            head = git(workdir, "rev-parse", "HEAD")
            if head == run["base_commit"]:
                raise ValueError("There is no implementation commit to publish.")
            self.store.put("runs", run["id"], {"implementation_commit": head})
            return {"commit": head, "branch": run["branch"]}
