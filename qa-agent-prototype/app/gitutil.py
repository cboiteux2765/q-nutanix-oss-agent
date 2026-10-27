"""Small, read-only Git helpers. QA never writes to the PR repository."""

from __future__ import annotations

import subprocess
from pathlib import Path

from .models import ChangedFile

GIT_TIMEOUT_SECONDS = 30


class GitError(RuntimeError):
    pass


def _git(repo: Path, *args: str) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitError(f"git {' '.join(args)} failed: {exc}") from exc
    if proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def resolve(repo: Path, ref: str) -> str:
    return _git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}").strip()


def changed_files(repo: Path, base: str, head: str) -> list[ChangedFile]:
    out = _git(repo, "diff", "--name-status", "--no-renames", f"{base}..{head}")
    files = []
    for line in out.splitlines():
        if not line.strip():
            continue
        status, _, path = line.partition("\t")
        files.append(ChangedFile(path=path.strip(), status=status.strip()[:1]))
    return files


def added_lines(repo: Path, base: str, head: str) -> dict[str, list[tuple[int, str]]]:
    """Map each changed file to the (line number, text) pairs the PR adds."""
    out = _git(repo, "diff", "--unified=0", "--no-color", "--no-renames", f"{base}..{head}")
    result: dict[str, list[tuple[int, str]]] = {}
    current: str | None = None
    line_no = 0
    for raw in out.splitlines():
        if raw.startswith("+++ "):
            target = raw[4:]
            current = target[2:] if target.startswith("b/") else None
            if current is not None:
                result.setdefault(current, [])
        elif raw.startswith("@@"):
            # @@ -a,b +c,d @@
            plus = raw.split("+", 1)[1].split(" ", 1)[0]
            line_no = int(plus.split(",")[0])
        elif raw.startswith("+") and not raw.startswith("+++") and current is not None:
            result[current].append((line_no, raw[1:]))
            line_no += 1
    return result


def read_file(repo: Path, rev: str, path: str) -> str | None:
    """Return a file's text at a revision, or None if it does not exist there."""
    try:
        return _git(repo, "show", f"{rev}:{path}")
    except GitError:
        return None
