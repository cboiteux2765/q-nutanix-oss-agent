"""Read and normalize activity from the selected GitHub repository."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx

from .common import redact_all
from .workspaces import git, local_env

API_HOST = "api.github.com"
REPOSITORY = re.compile(r"[A-Za-z0-9-]{1,39}/[A-Za-z0-9_.-]{1,100}")
SHA = re.compile(r"[a-f0-9]{40,64}", re.IGNORECASE)
LINK = re.compile(r'<([^>]+)>\s*;\s*rel="next"')
FAILURES = frozenset({"action_required", "failure", "startup_failure", "timed_out"})


def timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        result = datetime.fromisoformat(value)
        return result.astimezone(UTC) if result.tzinfo else None
    except (TypeError, ValueError):
        return None


def digest(*parts) -> str:
    data = json.dumps(parts, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def repository_name(value: str) -> str:
    if not REPOSITORY.fullmatch(value) or value.split("/", 1)[1] in {".", ".."}:
        raise ValueError("Use a GitHub repository in owner/name format.")
    return value.lower()


def origin_repository(project_path: str) -> str:
    try:
        origin = git(Path(project_path), "config", "--local", "--get", "remote.origin.url",
                     check=False, timeout=5)
    except (OSError, RuntimeError, TimeoutError):
        origin = ""
    if origin.startswith("git@github.com:"):
        path = origin.removeprefix("git@github.com:")
    else:
        parsed = urlsplit(origin)
        if parsed.scheme not in {"http", "https", "ssh", "git"} or parsed.hostname != "github.com":
            raise ValueError("Connect a GitHub origin or select a repository in owner/name format.")
        path = parsed.path.strip("/")
    return repository_name(path.removesuffix(".git"))


def route(repository: str, endpoint: str = "", **query) -> str:
    return str(httpx.URL(path=f"/repos/{repository}{endpoint}", params=query))


def continuation(current: str, candidate: str | None, repository: str, repository_id: int) -> str | None:
    """Follow pagination on the same endpoint, without forwarding credentials to a new host."""
    if not candidate:
        return None
    target = urlsplit(candidate)
    source = urlsplit(current)
    if target.scheme or target.netloc:
        if target.scheme != "https" or target.netloc.lower() != API_HOST:
            raise GitHubError("GitHub returned an invalid pagination link.")
    # GitHub frequently paginates via /repositories/<numeric ID> in place of /repos/owner/name.
    repo_path = f"/repos/{repository}"
    numeric_path = f"/repositories/{repository_id}"
    expected = source.path.removeprefix(repo_path).removeprefix(numeric_path)
    if target.path not in {repo_path + expected, numeric_path + expected}:
        raise GitHubError("GitHub returned an invalid pagination link.")
    if target.fragment or not target.query:
        raise GitHubError("GitHub returned an invalid pagination link.")
    return target.path + "?" + target.query


class GitHubError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None,
                 retry_at: datetime | None = None):
        super().__init__(message)
        self.status = status
        self.retry_at = retry_at


@dataclass(frozen=True)
class GitHubPage:
    status: int
    data: dict | list | None = None
    headers: dict[str, str] = field(default_factory=dict)
    rate_limited: bool = False

    def __post_init__(self):
        object.__setattr__(self, "headers", {key.lower(): value for key, value in self.headers.items()})

    @property
    def next_link(self) -> str | None:
        match = LINK.search(self.headers.get("link", ""))
        return match[1] if match else None


class GitHubReader:
    def __init__(self, *, transport=None):
        self.client = httpx.AsyncClient(
            base_url=f"https://{API_HOST}",
            headers={"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"},
            follow_redirects=False,
            timeout=12,
            transport=transport,
        )
        self._token = ""

    async def _credential(self) -> str:
        env_token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        if env_token and env_token.strip():
            return env_token.strip()
        if self._token:
            return self._token
        executable = shutil.which("gh")
        if not executable:
            raise GitHubError("Install the GitHub CLI and run gh auth login to enable monitoring.")
        try:
            process = await asyncio.create_subprocess_exec(
                executable, "auth", "token", "--hostname", "github.com",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                env=local_env() | {"GH_PROMPT_DISABLED": "1", "GH_NO_UPDATE_NOTIFIER": "1"},
            )
        except OSError as exc:
            raise GitHubError("GitHub CLI authentication is unavailable.") from exc
        try:
            output, _ = await asyncio.wait_for(process.communicate(), 8)
        except (TimeoutError, asyncio.CancelledError):
            if process.returncode is None:
                process.kill()
            await process.wait()
            raise
        token = output.decode("utf-8", errors="replace").strip()
        if process.returncode or not token:
            raise GitHubError("Run gh auth login to enable GitHub monitoring.")
        self._token = token
        return token

    async def get(self, target: str, *, etag: str | None = None) -> GitHubPage:
        url = urlsplit(target)
        if url.scheme or url.netloc or not url.path.startswith(("/repos/", "/repositories/")):
            raise GitHubError("Invalid GitHub activity endpoint.")
        token = await self._credential()
        headers = {"Authorization": f"Bearer {token}"}
        if etag:
            headers["If-None-Match"] = etag
        try:
            response = await self.client.get(target, headers=headers)
        except (httpx.HTTPError, ValueError) as exc:
            raise GitHubError("Could not read GitHub activity.") from exc
        if response.status_code == 401:
            self._token = ""
        data = None
        if response.status_code == 200:
            try:
                data = redact_all(response.json(), token)
            except ValueError as exc:
                raise GitHubError("GitHub returned invalid activity data.") from exc
        limited = False
        if response.status_code == 403:
            try:
                message = str(response.json().get("message", "")).casefold()
                limited = "rate limit" in message
            except (AttributeError, ValueError):
                pass
        # Never surface the provider's raw error output or request headers.
        headers = {key: value for key, value in response.headers.items()
                   if key in {"etag", "link", "retry-after", "x-poll-interval",
                              "x-ratelimit-reset", "x-ratelimit-remaining"} and token not in value}
        return GitHubPage(response.status_code, data, headers, limited)

    async def close(self) -> None:
        self._token = ""
        await self.client.aclose()


@dataclass(frozen=True)
class Activity:
    object_id: str
    kind: str
    subject: str
    number: int | None
    title: str
    url: str
    occurred_at: str
    opened_at: str | None
    actor: str | None
    bot: bool
    facts: dict
    revision: dict


def _excerpt(value, limit=480) -> str:
    return " ".join(str(value or "").split())[:limit]


def _actor(value) -> tuple[str | None, bool]:
    user = value or {}
    actor = _excerpt(user.get("login"), 80) or None
    return actor, user.get("type") == "Bot" or bool(actor and actor.casefold().endswith("[bot]"))


def _source_url(repository: str, value, fallback: str) -> str:
    url = urlsplit(str(value or ""))
    if (url.scheme == "https" and url.netloc.lower() == "github.com"
            and url.path.lower().startswith(f"/{repository}/")):
        return str(value)
    return f"https://github.com/{repository}/{fallback}"


def issue_activity(repository: str, item: dict) -> Activity:
    number = int(item["number"])
    pull = item.get("pull_request")
    kind = "pull_request" if pull is not None else "issue"
    endpoint = "pull" if kind == "pull_request" else "issues"
    state = "merged" if (pull or {}).get("merged_at") else str(item["state"])
    actor, bot = _actor(item.get("user"))
    title = _excerpt(item.get("title"), 180)
    facts = {
        "state": state,
        "labels": sorted({_excerpt(label.get("name"), 80) for label in item.get("labels") or []}),
        "assignees": sorted({_excerpt(user.get("login"), 80) for user in item.get("assignees") or []}),
        "excerpt": _excerpt(item.get("body")),
    }
    return Activity(str(item["id"]), kind, f"thread:{number}", number, title,
                    _source_url(repository, item.get("html_url"), f"{endpoint}/{number}"),
                    item["updated_at"], item.get("created_at"), actor, bot, facts,
                    (facts | {"title": digest(item.get("title")), "body": digest(item.get("body"))}))


def comment_activity(repository: str, item: dict, *, review: bool = False) -> Activity:
    subject_url = item["pull_request_url" if review else "issue_url"]
    number = int(urlsplit(subject_url).path.rsplit("/", 1)[-1])
    actor, bot = _actor(item.get("user"))
    facts = {"excerpt": _excerpt(item.get("body"))}
    if review:
        facts |= {"path": _excerpt(item.get("path"), 240), "line": item.get("line")}
    fallback = f"pull/{number}#discussion_r{item['id']}" if review else f"issues/{number}#issuecomment-{item['id']}"
    return Activity(str(item["id"]), "review_comment" if review else "comment", f"thread:{number}",
                    number, f"Review comment on #{number}" if review else f"Comment on #{number}",
                    _source_url(repository, item.get("html_url"), fallback),
                    item["updated_at"], item.get("created_at"), actor, bot, facts,
                    facts | {"body": digest(item.get("body"))})


def review_activity(repository: str, number: int, item: dict) -> Activity | None:
    state = str(item["state"]).lower()
    if state == "pending" or not item.get("submitted_at"):
        return None
    actor, bot = _actor(item.get("user"))
    facts = {"state": state, "excerpt": _excerpt(item.get("body")), "sha": item.get("commit_id")}
    return Activity(str(item["id"]), "review", f"thread:{number}", number, f"Review on PR #{number}",
                    _source_url(repository, item.get("html_url"), f"pull/{number}#pullrequestreview-{item['id']}"),
                    item.get("updated_at") or item["submitted_at"], item["submitted_at"], actor, bot,
                    facts, facts | {"body": digest(item.get("body"))})


def pull_head_activity(repository: str, item: dict, now: str) -> Activity:
    number = int(item["number"])
    actor, bot = _actor(item.get("user"))
    head = item["head"]
    facts = {"sha": head["sha"], "ref": _excerpt(head.get("ref"), 240)}
    return Activity(str(number), "pull_head", f"thread:{number}", number,
                    _excerpt(item.get("title"), 180),
                    _source_url(repository, item.get("html_url"), f"pull/{number}"),
                    item.get("updated_at") or now, item.get("created_at"), actor, bot, facts, facts)


def branch_activity(repository: str, branch: str, item: dict, now: str) -> Activity:
    actor, bot = _actor(item.get("committer") or item.get("author"))
    sha = item["sha"]
    facts = {"ref": branch, "sha": sha, "commit_title": _excerpt(item["commit"]["message"].split("\n", 1)[0], 180)}
    return Activity(branch, "branch_head", f"branch:{branch}", None, f"Branch {branch}",
                    _source_url(repository, item.get("html_url"), f"commit/{quote(sha)}"),
                    now, None, actor, bot, facts, {"sha": sha})


def commit_activity(repository: str, branch: str, item: dict, now: str) -> Activity:
    actor, bot = _actor(item.get("committer") or item.get("author"))
    sha = item["sha"]
    title = _excerpt(item["commit"]["message"].split("\n", 1)[0], 180)
    facts = {"ref": branch, "sha": sha, "commit_title": title,
             "committed_at": (item["commit"].get("committer") or {}).get("date")}
    return Activity(sha, "commit", f"branch:{branch}", None, title,
                    _source_url(repository, item.get("html_url"), f"commit/{quote(sha)}"),
                    now, None, actor, bot, facts, {"sha": sha})


def check_activity(repository: str, subject: str, sha: str, item: dict) -> Activity | None:
    if not item.get("completed_at") or item.get("status") != "completed" or not item.get("conclusion"):
        return None
    number = int(subject.split(":")[1]) if subject.startswith("thread:") else None
    name = _excerpt(item.get("name"), 180)
    facts = {"name": name, "conclusion": item["conclusion"], "sha": sha}
    return Activity(str(item["id"]), "check", subject, number, name,
                    _source_url(repository, item.get("html_url"), f"commit/{quote(sha)}"),
                    item["completed_at"], item.get("started_at"), None, False, facts, facts)
