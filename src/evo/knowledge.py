"""Project wiki backed by a dedicated local Git repository."""

import json
from pathlib import Path
import re
from threading import RLock

from .common import Conflict
from .store import Store, require
from .workspaces import git, safe_path

_LOCK = RLock()
_CITATIONS = "\n\n<!-- EVO citations: "


class KnowledgeBase:
    def __init__(self, store: Store):
        self.store = store
        self.root = store.data_dir / "knowledge"

    def _repository(self, project_id: str) -> Path:
        require(self.store, "projects", project_id)
        root = safe_path(self.root, project_id)
        if root == self.root:
            raise ValueError("Invalid project ID.")
        with _LOCK:
            root.mkdir(parents=True, exist_ok=True)
            if not (root / ".git").is_dir():
                git(root, "init", "-b", "main")
            if not git(root, "rev-parse", "--verify", "HEAD", check=False):
                git(root, "-c", "user.name=EVO", "-c", "user.email=evo@localhost",
                    "commit", "--allow-empty", "-m", "Initialize project knowledge")
        return root

    @staticmethod
    def _path(root: Path, path: str) -> Path:
        page = safe_path(root, path)
        relative = page.relative_to(root)
        if page.suffix != ".md" or any(part.startswith(".") for part in relative.parts):
            raise ValueError("Wiki pages use relative .md paths.")
        return page

    @staticmethod
    def _title(path: Path, content: str) -> str:
        return next(
            (line[2:].strip() for line in content.splitlines() if line.startswith("# ") and line[2:].strip()),
            path.stem.replace("-", " "),
        )

    def revision(self, project_id: str) -> str:
        root = self._repository(project_id)
        with _LOCK:
            return git(root, "rev-parse", "HEAD")

    @staticmethod
    def _tree(root: Path, revision: str | None) -> str:
        if revision is None:
            return "HEAD"
        if not re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", revision):
            raise ValueError("Use a full wiki revision ID.")
        if not git(root, "rev-parse", "--verify", revision + "^{commit}", check=False):
            raise KeyError(f"Unknown wiki revision {revision!r}")
        return revision

    def list_pages(self, project_id: str, revision: str | None = None) -> list[dict]:
        root = self._repository(project_id)
        with _LOCK:
            tree = self._tree(root, revision)
            pages = git(root, "ls-tree", "-r", "-z", "--name-only", tree).split("\0")
            return [
                {key: value for key, value in self.read_page(project_id, path, tree if revision else None).items()
                 if key in {"path", "title", "revision", "updated_at"}}
                for path in pages if path.endswith(".md")
            ]

    def read_page(self, project_id: str, path: str, revision: str | None = None) -> dict:
        root = self._repository(project_id)
        with _LOCK:
            page = self._path(root, path)
            relative = page.relative_to(root).as_posix()
            tree = self._tree(root, revision)
            if not git(root, "rev-parse", "--verify", f"{tree}:{relative}", check=False):
                raise KeyError(f"No wiki page {path!r}")
            log = git(root, "--literal-pathspecs", "log", "-1", "--format=%H%x00%cI", tree, "--", relative)
            revision, date = log.split("\0")
            content = git(root, "cat-file", "blob", f"{tree}:{relative}", trim=False)
            citations = []
            if _CITATIONS in content and content.endswith(" -->\n"):
                content, encoded = content.rsplit(_CITATIONS, 1)
                citations = json.loads(encoded[:-5])
            return {"path": relative, "title": self._title(page, content), "content": content,
                    "revision": revision, "updated_at": date, "citations": citations}

    def write_page(self, project_id: str, path: str, content: str, citations: list | None = None,
                   agent_id: str | None = None, expected_revision: str | None = None) -> dict:
        root = self._repository(project_id)
        if len(content.encode("utf-8")) > 512 * 1024:
            raise ValueError("Wiki pages must be 512 KiB or smaller.")
        with _LOCK:
            page = self._path(root, path)
            relative = page.relative_to(root).as_posix()
            revision = git(root, "--literal-pathspecs", "log", "-1", "--format=%H", "--", relative)
            if expected_revision is not None and revision != expected_revision:
                raise Conflict(f"Wiki page changed. Current revision is {revision or 'empty'}.")
            page.parent.mkdir(parents=True, exist_ok=True)
            encoded = json.dumps(citations or [], ensure_ascii=False).replace("--", "\\u002d\\u002d")
            prior = page.read_bytes() if page.exists() else None
            updated = (content + _CITATIONS + encoded + " -->\n").encode("utf-8")
            if updated != prior:
                try:
                    page.write_bytes(updated)
                    git(root, "--literal-pathspecs", "add", "--", relative)
                    author = f" by agent {agent_id}" if agent_id else ""
                    git(root, "--literal-pathspecs", "-c", "user.name=EVO", "-c", "user.email=evo@localhost",
                        "commit", "-m", f"Update {relative}{author}", "--", relative)
                except Exception:
                    if prior is None:
                        page.unlink(missing_ok=True)
                    else:
                        page.write_bytes(prior)
                    git(root, "--literal-pathspecs", "reset", "HEAD", "--", relative, check=False)
                    raise
            return self.read_page(project_id, relative)

    def history(self, project_id: str, path: str, revision: str | None = None) -> list[dict]:
        root = self._repository(project_id)
        relative = self._path(root, path).relative_to(root).as_posix()
        with _LOCK:
            tree = self._tree(root, revision)
            lines = git(root, "--literal-pathspecs", "log", "-100", "--format=%H%x00%cI%x00%s",
                        tree, "--", relative)
        return [
            dict(zip(("revision", "updated_at", "message"), line.split("\0"), strict=True))
            for line in lines.splitlines()
        ]
