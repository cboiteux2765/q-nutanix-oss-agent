"""Host-side git operations. The implementer never gets git credentials.

Every git invocation disables hooks, fsmonitor, external diff and textconv, because the
implementer can write to .git/ inside its workspace and those settings execute code.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from .models import ChangedFile, ChangeType

_NO_HOOKS_DIR = tempfile.mkdtemp(prefix="evo-no-hooks-")
EXCLUDE_LINES = [".evo-artifacts/", "__pycache__/", "*.pyc", ".pytest_cache/"]
_SAFE_CONFIG = [
    f"core.hooksPath={_NO_HOOKS_DIR}",
    "core.fsmonitor=false",
    "core.autocrlf=false",
    "core.longpaths=true",
    "commit.gpgsign=false",
    "tag.gpgsign=false",
    "protocol.file.allow=always",
    "init.defaultBranch=main",
    "advice.detachedHead=false",
]


class GitError(Exception):
    def __init__(self, args: list[str], returncode: int, output: str):
        self.returncode = returncode
        self.output = output
        super().__init__(f"git {' '.join(args)} failed ({returncode}): {output.strip()[:500]}")


def git(cwd: Path | None, *args: str, env: dict[str, str] | None = None, timeout: float = 120,
        check: bool = True) -> str:
    command = ["git"]
    for item in _SAFE_CONFIG:
        command += ["-c", item]
    command += list(args)
    full_env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", **(env or {})}
    proc = subprocess.run(command, cwd=cwd, env=full_env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          encoding="utf-8", errors="replace", timeout=timeout)
    if check and proc.returncode != 0:
        raise GitError(list(args), proc.returncode, proc.stdout)
    return proc.stdout.strip()


def clone(source: str, dest: Path, env: dict[str, str] | None = None, branch: str | None = None) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    args = ["clone", "--no-tags", "--quiet"]
    if branch:
        args += ["--branch", branch]
    git(None, *args, source, str(dest), env=env, timeout=600)
    # The implementer must not be able to push anywhere.
    git(dest, "remote", "remove", "origin", check=False)


def commit_exists(repo: Path, sha: str) -> bool:
    return git(repo, "cat-file", "-t", sha, check=False) == "commit"


def checkout(repo: Path, sha: str, branch: str) -> None:
    git(repo, "checkout", "--quiet", "-B", branch, sha)


def head_sha(repo: Path) -> str:
    return git(repo, "rev-parse", "HEAD")


class GitSnapshot:
    """Saves .git/config and .git/info/exclude so implementer tampering can be undone."""

    def __init__(self, repo: Path):
        self.repo = repo
        git_dir = repo / ".git"
        exclude = git_dir / "info" / "exclude"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
        missing = [line for line in EXCLUDE_LINES if line not in existing.splitlines()]
        if missing:
            exclude.write_text(existing.rstrip("\n") + ("\n" if existing else "") + "\n".join(missing) + "\n",
                               encoding="utf-8")
        self._files = {p: p.read_bytes() for p in (git_dir / "config", exclude) if p.exists()}

    def restore(self) -> None:
        for path, content in self._files.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        hooks = self.repo / ".git" / "hooks"
        if hooks.is_dir():
            shutil.rmtree(hooks, ignore_errors=True)


def stage_changes(repo: Path, base: str, branch: str) -> None:
    """Undo any implementer commits/branch switches (keeping file changes) and stage everything."""
    git(repo, "symbolic-ref", "HEAD", f"refs/heads/{branch}")
    git(repo, "reset", "--quiet", "--soft", base)
    git(repo, "add", "--all")


def staged_changes(repo: Path, base: str, test_matcher=None) -> list[ChangedFile]:
    name_status = git(repo, "diff", "--cached", "--no-renames", "--no-ext-diff", "--no-textconv",
                      "--name-status", base)
    numstat = git(repo, "diff", "--cached", "--no-renames", "--no-ext-diff", "--no-textconv", "--numstat", base)
    counts: dict[str, tuple[int, int]] = {}
    for line in numstat.splitlines():
        added, deleted, path = line.split("\t", 2)
        counts[path] = (0 if added == "-" else int(added), 0 if deleted == "-" else int(deleted))
    changes = []
    for line in name_status.splitlines():
        status, path = line.split("\t", 1)
        kind = {"A": ChangeType.ADDED, "D": ChangeType.DELETED}.get(status[0], ChangeType.MODIFIED)
        add, delete = counts.get(path, (0, 0))
        changes.append(ChangedFile(path=path, change=kind, additions=add, deletions=delete,
                                   is_test=bool(test_matcher and test_matcher(path))))
    return changes


def staged_symlinks(repo: Path) -> list[str]:
    out = git(repo, "ls-files", "--stage")
    return [line.split("\t", 1)[1] for line in out.splitlines() if line.startswith("120000 ")]


def staged_added_lines(repo: Path, base: str) -> dict[str, list[str]]:
    diff = git(repo, "diff", "--cached", "--no-renames", "--no-ext-diff", "--no-textconv", "-U0", base)
    added: dict[str, list[str]] = {}
    current = None
    for line in diff.splitlines():
        if line.startswith("+++ "):
            current = line[6:] if line.startswith("+++ b/") else None
        elif current and line.startswith("+") and not line.startswith("+++"):
            added.setdefault(current, []).append(line[1:])
    return added


def staged_diff(repo: Path, base: str) -> str:
    return git(repo, "diff", "--cached", "--no-renames", "--no-ext-diff", "--no-textconv", "--binary", base)


def commit(repo: Path, message: str, author_name: str, author_email: str) -> str:
    env = {
        "GIT_AUTHOR_NAME": author_name, "GIT_AUTHOR_EMAIL": author_email,
        "GIT_COMMITTER_NAME": author_name, "GIT_COMMITTER_EMAIL": author_email,
    }
    git(repo, "commit", "--quiet", "--no-verify", "-m", message, env=env)
    return head_sha(repo)
