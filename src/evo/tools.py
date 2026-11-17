"""Host tool routing and project-scoped operations for the Python REPL.

Every public `_Scope` method named `<namespace>_<name>` is a tool the agent calls as
`<namespace>.<name>(...)`. Its signature and docstring become the tool's manifest entry, which is
shown to the model verbatim, so keep docstrings to one line written for the agent.
"""

import asyncio
import codecs
import inspect
import json
import os
from pathlib import Path
import re
import shutil
from tempfile import TemporaryFile
from threading import RLock
from uuid import uuid4

from .common import INLINE_RESULT_CHARS, MAX_TEXT_BYTES, StreamRedactor
from .config import get_api_key
from . import procs
from .procs import bounded, kill_tree, new_group
from .store import require
from .webtools import fetch, search
from .workspaces import git, local_env, safe_path

_WIKI_VIEW_LOCK = RLock()
_SHELL_PREVIEW_BYTES = 8 * 1024
_SHELL_RETENTION_BYTES = 64 * 1024 * 1024
_SHELL_CHUNK_BYTES = 1024 * 1024


def _shell_log(stream, label: str, scope: "_Scope", command_id: str, secret: str) -> dict:
    size = stream.seek(0, os.SEEK_END)
    stream.seek(0)
    redactor = StreamRedactor(secret)
    if size <= _SHELL_PREVIEW_BYTES:
        text = stream.read().decode("utf-8", errors="replace")
        return {label: redactor.feed(text) + redactor.flush()}

    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    retained = min(size, _SHELL_RETENTION_BYTES)
    remaining = retained
    fragments = []
    preview = ""
    while remaining:
        raw = stream.read(min(_SHELL_CHUNK_BYTES, remaining))
        remaining -= len(raw)
        if not raw:
            raise RuntimeError("Command log ended before its recorded length.")
        text = redactor.feed(decoder.decode(raw, final=remaining == 0))
        if not remaining:
            text += redactor.flush()
        if not text:
            continue
        if len(preview) < _SHELL_PREVIEW_BYTES:
            preview += text[:_SHELL_PREVIEW_BYTES - len(preview)]
        artifact = scope.artifacts_write(
            f"Shell {command_id} {label} {len(fragments) + 1}", text, "shell_log",
        )
        fragments.append(artifact["id"])
    return {
        label: preview + "\n[Preview. Read the ordered log artifacts for retained output.]",
        f"{label}_artifact_ids": fragments,
        f"{label}_omitted_bytes": size - retained,
        f"{label}_bytes": size,
    }


def _collect_shell_logs(stdout, stderr, scope: "_Scope") -> dict:
    command_id = uuid4().hex[:12]
    secret = get_api_key(scope.run.get("settings_snapshot", {}))
    return (_shell_log(stdout, "stdout", scope, command_id, secret)
            | _shell_log(stderr, "stderr", scope, command_id, secret))


async def _shell_command(argv: list[str], cwd: Path, timeout: float, scope: "_Scope") -> dict:
    with TemporaryFile() as stdout, TemporaryFile() as stderr:
        process = await asyncio.create_subprocess_exec(
            *argv, cwd=cwd, env=local_env(), stdin=asyncio.subprocess.DEVNULL,
            stdout=stdout, stderr=stderr, **new_group(),
        )
        timed_out = False
        interrupted = None
        try:
            await asyncio.wait_for(process.wait(), timeout)
        except (asyncio.CancelledError, TimeoutError) as error:
            timed_out = isinstance(error, TimeoutError)
            interrupted = error if not timed_out else None
            if process.returncode is None:
                await kill_tree(process)
                await process.wait()
        capture = asyncio.create_task(asyncio.to_thread(_collect_shell_logs, stdout, stderr, scope))
        try:
            output = await asyncio.shield(capture)
        except asyncio.CancelledError as error:
            interrupted = error
            output = await capture
        if interrupted:
            scope.store.event(scope.run_id, scope.agent_id, "shell.interrupted_output", output)
            raise interrupted
    return output | {"exit_code": None if timed_out else process.returncode, "timed_out": timed_out}


def _repo_path(workdir: Path, path: str) -> Path:
    target = safe_path(workdir, path)
    if any(part.lower() in {".git", ".evo"} for part in target.relative_to(workdir).parts):
        raise PermissionError("Repository metadata is managed by the host.")
    return target


class ToolBroker:
    def __init__(self, runtime):
        self.runtime = runtime
        self.store = runtime.store
        self.workspaces = runtime.workspaces
        self.knowledge = runtime.knowledge
        self.skills = runtime.skills
        self.workflow = runtime.workflow
        self.artifacts = runtime.artifacts

    async def dispatch(self, agent_id: str, name: str, args: list, kwargs: dict):
        if name not in TOOL_MANIFEST:
            raise ValueError(f"Unknown tool {name!r}.")
        scope = _Scope(self, agent_id)
        # Evaluation runs deny tools with outside effects (publishing, web, durable memory).
        denied = scope.run.get("tool_policy", {}).get("deny", [])
        if any(name == rule or (rule.endswith(".*") and name.startswith(rule[:-1])) for rule in denied):
            raise PermissionError(f"{name} is disabled in this run.")
        target = getattr(scope, name.replace(".", "_", 1))
        inspect.signature(target).bind(*args, **kwargs)
        if inspect.iscoroutinefunction(target):
            return await target(*args, **kwargs)
        return await asyncio.to_thread(target, *args, **kwargs)


class _Scope:
    def __init__(self, broker: ToolBroker, agent_id: str):
        self.broker = broker
        self.store = broker.store
        self.agent_id = agent_id
        self.agent = require(self.store, "agents", agent_id)
        self.run_id = self.agent["run_id"]
        self.run = require(self.store, "runs", self.run_id)
        self.project_id = self.run["project_id"]
        self.role = self.agent["role"].lower()

    def _workspace(self, write: bool = False) -> tuple[Path, str]:
        if self.role == "code":
            if write:
                self.broker.workspaces.acquire_writer(self.agent_id)
            return Path(self.run["workdir"]), self.run["base_commit"]
        if write and self.role not in {"test", "qa"}:
            raise PermissionError("Only Code, Test, and QA have writable workspaces through the broker.")
        view = self.broker.workspaces.prepare_agent(self.agent_id, self.agent.get("workspace_commit"))
        return Path(view["workdir"]), view["base_commit"]

    def _repo_files(self, workdir: Path, path: str) -> list[str]:
        relative = _repo_path(workdir, path).relative_to(workdir).as_posix()
        listing = git(workdir, "--literal-pathspecs", "ls-files", "-z", "--cached", "--others", "--exclude-standard",
                      "--", relative)
        return list(dict.fromkeys(file for file in listing.split("\0") if file))

    def repo_list(self, path: str = ".", limit: int = 200) -> dict:
        """lists tracked and visible untracked files."""
        workdir, _ = self._workspace()
        files = self._repo_files(workdir, path)
        return {"files": files[:max(1, min(limit, 1000))], "total": len(files)}

    def repo_read(self, path: str, start: int = 1, lines: int = 200) -> dict:
        """reads UTF-8 lines with 1-based offsets."""
        workdir, _ = self._workspace()
        file = _repo_path(workdir, path)
        if start < 1 or lines < 1:
            raise ValueError("Use positive line offsets and counts.")
        if not file.is_file():
            raise KeyError(f"File {path!r} does not exist.")
        if file.stat().st_size > MAX_TEXT_BYTES:
            raise ValueError("File exceeds the 8 MiB reading limit.")
        content = file.read_text(encoding="utf-8").splitlines()
        selected = content[start - 1:start - 1 + min(lines, 1000)]
        return {"path": path, "start": start, "total_lines": len(content),
                "content": bounded("\n".join(selected))}

    def repo_search(self, query: str, path: str = ".", limit: int = 50, regex: bool = False) -> list[dict]:
        """returns matching file lines."""
        if not query or len(query) > 1000:
            raise ValueError("Use a non-empty search pattern of at most 1,000 characters.")
        pattern = re.compile(query) if regex else None
        workdir, _ = self._workspace()
        matches = []
        for name in self._repo_files(workdir, path)[:20_000]:
            try:
                file = _repo_path(workdir, name)
                if not file.is_file() or file.stat().st_size > 2 * 1024 * 1024:
                    continue
                content = file.read_text(encoding="utf-8")
            except (OSError, UnicodeError, ValueError, PermissionError):
                continue
            if "\0" in content:
                continue
            for number, line in enumerate(content.splitlines(), 1):
                found = pattern.search(line[:5000]) if pattern else query in line
                if found:
                    matches.append({"path": name, "line": number, "text": bounded(line, 1500)})
                    if len(matches) >= max(1, min(limit, 250)):
                        return matches
        return matches

    def repo_status(self) -> dict:
        """returns branch, HEAD, verification base and porcelain status."""
        workdir, base = self._workspace()
        return {"branch": git(workdir, "branch", "--show-current") or None,
                "head": git(workdir, "rev-parse", "HEAD"), "base_commit": base,
                "status": bounded(git(workdir, "status", "--porcelain"))}

    def repo_refresh(self) -> dict:
        """updates an Orchestrator, Spec, or Helper snapshot to the latest published run commit."""
        if self.role not in {"orchestrator", "triage", "spec", "helper"}:
            raise PermissionError("This role's checkout is managed by its assignment.")
        return self.broker.workspaces.prepare_agent(self.agent_id)

    def repo_diff(self, base: str | None = None) -> str:
        """shows unstaged/staged changes against the worktree base."""
        workdir, default = self._workspace()
        ref = base or default
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/~^+-]{0,199}", ref):
            raise ValueError("Use a Git reference or commit SHA.")
        return bounded(git(workdir, "diff", "--no-ext-diff", ref, "--"))

    def repo_write(self, path: str, content: str) -> dict:
        """writes a UTF-8 file in your writable worktree."""
        workdir, _ = self._workspace(write=True)
        file = _repo_path(workdir, path)
        encoded = content.encode("utf-8")
        if len(encoded) > MAX_TEXT_BYTES:
            raise ValueError("Write at most 8 MiB at a time.")
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(encoded)
        return {"path": path, "size": len(encoded)}

    def repo_apply_patch(self, patch: str) -> dict:
        """applies a Git unified diff in your writable worktree."""
        workdir, _ = self._workspace(write=True)
        encoded = patch.encode("utf-8")
        if len(encoded) > MAX_TEXT_BYTES:
            raise ValueError("Apply patches no larger than 8 MiB.")
        result = procs.run(["git", "apply", "--whitespace=nowarn", "-"], workdir, 30, local_env(), input=encoded)
        if result["timed_out"]:
            raise ValueError("git apply timed out.")
        if result["exit_code"]:
            raise ValueError(result["stderr"])
        return self.repo_status()

    def repo_commit(self, message: str) -> dict:
        """publishes the Code agent's implementation commit for this run."""
        return self.broker.workspaces.commit(self.agent_id, message)

    async def shell_run(self, command: str | list[str], cwd: str = ".", timeout: float | None = None) -> dict:
        """runs a shell string or argv list for Code, Test, or QA. Local trusted execution. Long output includes ordered log artifact IDs and any bytes omitted above the per-stream 64 MiB retention limit."""
        workdir, _ = await asyncio.to_thread(self._workspace, write=True)
        directory = _repo_path(workdir, cwd)
        maximum = min(max(float(self.run.get("settings_snapshot", {}).get("command_timeout", 120)), 1), 600)
        duration = maximum if timeout is None else min(max(float(timeout), 1), maximum)
        if isinstance(command, str):
            if os.name == "nt":
                shell = shutil.which("pwsh") or shutil.which("powershell")
                if not shell:
                    raise RuntimeError("PowerShell is required to run a shell string on Windows.")
                argv = [shell, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command]
            else:
                argv = ["/bin/sh", "-c", command]
        else:
            argv = command
        if not argv:
            raise ValueError("Provide a command to run.")
        return await _shell_command(argv, directory, duration, self)

    def _github(self, args: list[str]) -> str:
        gh = shutil.which("gh")
        if not gh:
            raise RuntimeError("Install the GitHub CLI and run gh auth login to use GitHub tools.")
        workdir, _ = self._workspace()
        env = local_env() | {"GH_PROMPT_DISABLED": "1", "GH_NO_UPDATE_NOTIFIER": "1"}
        result = procs.run([gh, *args], workdir, 45, env)
        if result["exit_code"] != 0:
            raise RuntimeError(result["stderr"] or "GitHub CLI request failed or timed out.")
        return result["stdout"].strip()

    def _github_view(self, kind: str, number: int, repo: str | None, fields: str) -> dict:
        if int(number) < 1:
            raise ValueError("Use a positive issue or pull request number.")
        args = [kind, "view", str(int(number)), "--json", fields]
        if repo:
            args.extend(["--repo", repo])
        output = self._github(args)
        try:
            return json.loads(output)
        except json.JSONDecodeError:
            return {"output": output, "truncated": True}

    def github_issue(self, number: int, repo: str | None = None) -> dict:
        """reads an issue via the signed-in gh CLI."""
        return self._github_view("issue", number, repo, "number,title,body,state,url,labels")

    def github_pr(self, number: int, repo: str | None = None) -> dict:
        """reads a pull request via the signed-in gh CLI."""
        return self._github_view("pr", number, repo, "number,title,body,state,url,headRefOid,isDraft")

    def github_create_draft(self, title: str, body: str, base: str | None = None) -> dict:
        """pushes this verified Code branch to origin and opens a draft PR when requested."""
        run = self.broker.workspaces.acquire_writer(self.agent_id, publishing=True)
        commit = run.get("implementation_commit")
        if run.get("phase") != "ready" or not commit or run.get("verified_commit") != commit:
            raise ValueError("Complete verification of this implementation commit before opening the draft.")
        workdir = Path(run["workdir"])
        branch = run["branch"]
        if (git(workdir, "rev-parse", "HEAD") != commit
                or git(workdir, "branch", "--show-current") != branch
                or git(workdir, "status", "--porcelain")):
            raise ValueError("The workspace must match the verified commit.")
        if not title.strip():
            raise ValueError("Provide a pull request title.")
        if run.get("draft_pr_url") and run.get("draft_pr_commit") == commit:
            return {"url": run["draft_pr_url"], "commit": commit, "branch": branch}
        if not shutil.which("gh"):
            raise RuntimeError("Install the GitHub CLI and run gh auth login before publishing a draft.")
        self._github(["auth", "status"])
        git(workdir, "push", "-u", "origin", f"HEAD:refs/heads/{branch}")
        if run.get("draft_pr_url"):
            self.store.put("runs", self.run_id, {"draft_pr_commit": commit})
            return {"url": run["draft_pr_url"], "commit": commit, "branch": branch}
        args = ["pr", "create", "--draft", "--head", branch, "--title", title, "--body", body]
        if base:
            args.extend(["--base", base])
        output = self._github(args)
        url = next((line for line in reversed(output.splitlines()) if line.startswith("https://")), output)
        self.store.put("runs", self.run_id, {"draft_pr_url": url, "draft_pr_commit": commit})
        return {"url": url, "commit": commit, "branch": branch}

    def web_search(self, query: str, limit: int = 5) -> list[dict]:
        """returns public search results with source URLs."""
        return search(query, limit)

    def web_fetch(self, url: str, max_chars: int = 16000) -> dict:
        """fetches public HTTP text with the final citation URL."""
        return fetch(url, max_chars)

    async def agents_spawn(self, role: str, task: str) -> dict:
        """delegates scoped work to a child and returns its record."""
        return await self.broker.runtime.spawn(self.agent_id, role, task)

    async def agents_send(self, target_id: str, message: str) -> dict:
        """sends a message to an agent in this run."""
        return await self.broker.runtime.send(self.agent_id, target_id, message)

    async def agents_wait(self, ids: list[str], timeout: float | None = None) -> list[dict]:
        """waits for the named agents and returns their records."""
        return await self.broker.runtime.wait(self.agent_id, ids, timeout)

    def agents_list(self) -> list[dict]:
        """lists agents in this run."""
        return self.broker.runtime.list_agents(self.agent_id)

    def chat_reply(self, text: str) -> dict:
        """main agent only: answers the user now, while the team keeps working. The text appears in the conversation."""
        return self.broker.runtime.post_chat(self.agent_id, text, channel="reply")

    def chat_notify(self, text: str, kind: str = "progress") -> dict:
        """main agent only: tells the user about a milestone, blocker or decision they need to make (kind: progress, blocker, question). Not for routine "still working" notes."""
        if kind not in {"progress", "blocker", "question"}:
            raise ValueError("Use kind progress, blocker, or question.")
        return self.broker.runtime.post_chat(self.agent_id, text, channel="update", kind=kind)

    def _project_run(self, run_id: str) -> dict:
        run = require(self.store, "runs", run_id)
        if run["project_id"] != self.project_id:
            raise KeyError(f"No run {run_id!r} in this project.")
        return run

    def history_search(self, query: str, run_id: str | None = None, agent_id: str | None = None,
                       limit: int = 20) -> list[dict]:
        """searches events in this project."""
        if run_id:
            self._project_run(run_id)
        return self.store.search_history(
            query, run_id, agent_id, self.project_id, max(1, min(limit, 100)),
        )

    def history_read(self, event_id: str, run_id: str | None = None) -> dict:
        """retrieves an exact original event in this project."""
        return self.history_trace([event_id], run_id)[0]

    def history_trace(self, event_ids: list[str], run_id: str | None = None) -> list[dict]:
        """retrieves original events cited by a summary."""
        if not event_ids or len(event_ids) > 100:
            raise ValueError("Provide between one and 100 event IDs.")
        if run_id:
            self._project_run(run_id)
        found = self.store.events_by_id(list(event_ids), self.project_id, run_id)
        if missing := sorted(set(event_ids) - found.keys()):
            raise KeyError(f"Events not found in this project: {', '.join(missing)}")
        return [found[event_id] for event_id in event_ids]

    def history_expand(self, event_id: str, offset: int = 0, limit: int = 20) -> dict:
        """retrieves a page of original events behind a compaction event."""
        event = self.history_read(event_id)
        if event["kind"] != "compaction":
            raise ValueError("Expand a compaction event, or use history.read for an individual event.")
        if offset < 0 or limit < 1:
            raise ValueError("Use a nonnegative offset and a positive limit.")
        refs = event["data"].get("event_ids", [])
        end = offset + min(limit, 50)
        page = refs[offset:end]
        return {
            "event_id": event_id, "event_count": len(refs),
            "events": self.history_trace(page, event["run_id"]) if page else [],
            "next_offset": end if end < len(refs) else None,
        }

    def _shared_scope(self, scope: str, key: str) -> str:
        if not key.strip() or len(key) > 200:
            raise ValueError("Provide a key of up to 200 characters.")
        if scope == "run":
            return f"run:{self.run_id}"
        if scope == "project":
            return f"project:{self.project_id}"
        raise ValueError("Shared scope must be run or project.")

    def shared_get(self, key: str, scope: str = "run") -> dict | None:
        """reads a versioned value from this run or project."""
        return self.store.shared_get(self._shared_scope(scope, key), key)

    def shared_set(self, key: str, value, scope: str = "run", expected_version: int | None = None) -> dict:
        """writes versioned shared state."""
        scope_id = self._shared_scope(scope, key)
        encoded = json.dumps(value, ensure_ascii=False)
        if len(encoded.encode("utf-8")) > INLINE_RESULT_CHARS:
            artifact = self.artifacts_write(f"Shared value {key}", encoded, "json")
            value = {"artifact_id": artifact["id"], "format": "json"}
        return self.store.shared_set(scope_id, key, value, expected_version)

    def _wiki_revision(self) -> str:
        run = require(self.store, "runs", self.run_id)
        return run.get("wiki_revision") or self.memory_refresh()["revision"]

    def memory_list(self) -> list[dict]:
        """lists pages at the run's wiki revision."""
        return self.broker.knowledge.list_pages(self.project_id, self._wiki_revision())

    def memory_read(self, path: str, revision: str | None = None) -> dict:
        """reads a Markdown page and its citations at the run or specified revision."""
        return self.broker.knowledge.read_page(self.project_id, path, revision or self._wiki_revision())

    def memory_write(self, path: str, content: str, citations: list | None = None,
                     expected_revision: str | None = None) -> dict:
        """commits a Markdown page using the run's observed page revision by default. Cite evidence as [{"event_id"|"artifact_id"|"commit"|"url": ..., "kind": "observed"|"inferred"}]."""
        citations = self._validated_citations(citations)
        with _WIKI_VIEW_LOCK:
            if expected_revision is None:
                try:
                    expected_revision = self.memory_read(path)["revision"]
                except KeyError:
                    expected_revision = ""
            result = self.broker.knowledge.write_page(
                self.project_id, path, content, citations, self.agent_id, expected_revision,
            )
            self.memory_refresh()
            return result

    def _validated_citations(self, citations) -> list[dict]:
        """Citations must point at real evidence in this project, labelled observed or inferred."""
        result = []
        for citation in citations or []:
            if isinstance(citation, str):
                citation = {"event_id": citation, "kind": "observed"}
            if not isinstance(citation, dict):
                raise ValueError("Each citation must be an object such as {'event_id': ..., 'kind': 'observed'}.")
            citation = dict(citation)
            citation.setdefault("kind", "observed")
            if citation["kind"] not in {"observed", "inferred"}:
                raise ValueError("Citation kind must be observed or inferred.")
            refs = [key for key in ("event_id", "artifact_id", "commit", "url") if citation.get(key)]
            if not refs:
                raise ValueError("A citation needs an event_id, artifact_id, commit, or url.")
            if citation.get("event_id"):
                self.history_trace([citation["event_id"]])
            if citation.get("artifact_id"):
                record = self.store.get("artifacts", citation["artifact_id"])
                if not record or record.get("project_id") != self.project_id:
                    raise KeyError(f"No artifact {citation['artifact_id']!r} in this project.")
            if citation.get("url") and not str(citation["url"]).startswith(("http://", "https://")):
                raise ValueError("Citation URLs must use http or https.")
            result.append(citation)
        return result

    def memory_history(self, path: str) -> list[dict]:
        """lists page revisions visible to the run."""
        return self.broker.knowledge.history(self.project_id, path, self._wiki_revision())

    def memory_refresh(self) -> dict:
        """updates this run's wiki view to the latest project revision."""
        with _WIKI_VIEW_LOCK:
            revision = self.broker.knowledge.revision(self.project_id)
            self.store.put("runs", self.run_id, {"wiki_revision": revision})
        return {"revision": revision}

    def skills_list(self) -> list[dict]:
        """lists skill versions active when the run began."""
        ids = self.run.get("skill_ids")
        if ids is None:
            return self.broker.skills.list(self.project_id, active_only=True)
        return [self.broker.skills.read(self.project_id, skill_id) for skill_id in ids]

    def skills_read(self, skill_id: str) -> dict:
        """reads a skill version available to this run."""
        skill = self.broker.skills.read(self.project_id, skill_id)
        ids = self.run.get("skill_ids")
        available = skill_id in ids if ids is not None else skill["status"] == "active"
        if not available:
            raise PermissionError("This skill version is unavailable to the current run.")
        return skill

    def skills_propose(self, name: str, content: str, description: str = "", evidence: list | None = None) -> dict:
        """records an inactive skill proposal for human review. Cite the events or artifacts that motivated it as evidence (same format as memory citations)."""
        return self.broker.skills.propose(
            self.project_id, name, content, self.agent_id, description=description,
            evidence=self._validated_citations(evidence), run_id=self.run_id,
        )

    def workflow_status(self) -> dict:
        """returns phase, commit, handoffs and findings."""
        return self.broker.workflow.status(self.run_id)

    def workflow_handoff(self, target: str, summary: str, artifacts: list[str] | None = None,
                         commit: str | None = None, passed: bool | None = None) -> dict:
        """records evidence for the target phase."""
        return self.broker.workflow.handoff(self.agent_id, target, summary, artifacts, commit, passed)

    def workflow_advance(self, phase: str, handoff_id: str | None = None) -> dict:
        """advances the current phase. Triage -> Spec -> Code -> Test -> QA -> ready."""
        return self.broker.workflow.advance(self.agent_id, phase, handoff_id)

    def workflow_finding(self, title: str, details: str, severity: str = "medium",
                         artifact_id: str | None = None) -> dict:
        """records a concrete defect."""
        return self.broker.workflow.finding(self.agent_id, title, details, severity, artifact_id)

    def artifacts_list(self) -> list[dict]:
        """lists this run's artifacts."""
        return self.broker.artifacts.list(self.run_id)

    def artifacts_read(self, artifact_id: str, offset: int = 0, limit: int = INLINE_RESULT_CHARS) -> dict:
        """reads a portion of a text artifact in this project."""
        record = require(self.store, "artifacts", artifact_id)
        if record["project_id"] != self.project_id:
            raise KeyError(f"No artifact {artifact_id!r} in this project.")
        record = self.broker.artifacts.read(artifact_id)
        content = record["content"]
        if offset < 0 or limit < 1:
            raise ValueError("Use a nonnegative offset and positive limit.")
        end = offset + min(limit, 100_000)
        return record | {"content": content[offset:end], "offset": offset,
                         "next_offset": end if end < len(content) else None}

    def artifacts_write(self, name: str, content: str, kind: str = "text",
                        citations: list | None = None) -> dict:
        """persists text and returns its reference."""
        return self.broker.artifacts.write(self.run_id, self.agent_id, name, content, kind, citations)


def _manifest_entry(name: str, method) -> str:
    parameters = list(inspect.signature(method).parameters.values())[1:]
    arguments = ", ".join(p.name if p.default is p.empty else f"{p.name}={p.default!r}" for p in parameters)
    return f"{name}({arguments}) {inspect.getdoc(method)}"


TOOL_MANIFEST = {
    attribute.replace("_", ".", 1): _manifest_entry(attribute.replace("_", ".", 1), method)
    for attribute, method in vars(_Scope).items()
    if not attribute.startswith("_") and callable(method)
}
