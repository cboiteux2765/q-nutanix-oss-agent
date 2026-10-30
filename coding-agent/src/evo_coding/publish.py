"""Publishing a branch and a draft pull request: a local git remote (tests, offline demo) or
GitHub REST. Publishers can never merge or force-push.
"""
from __future__ import annotations

import base64
import json
import os
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from . import gitops
from .config import CodingConfig
from .models import utc_now


class PublishError(Exception):
    pass


class PublishConflict(PublishError):
    """The remote branch is not where we expected it (someone else pushed, or a stale run)."""


@dataclass
class PRRequest:
    repository: str
    branch: str
    base: str
    title: str
    body: str
    issue_id: int
    labels: list[str] = field(default_factory=list)


@dataclass
class PullRequestRef:
    number: int
    url: str
    branch: str
    base: str
    draft: bool
    created: bool


class Publisher(ABC):
    kind: str = "abstract"

    @abstractmethod
    def git_url(self, repository: str) -> str: ...

    def git_env(self) -> dict[str, str]:
        return {}

    def secrets(self) -> list[str]:
        return []

    def clone(self, repository: str, dest: Path, branch: str | None = None) -> None:
        gitops.clone(self.git_url(repository), dest, env=self.git_env(), branch=branch)

    def remote_branch_sha(self, repository: str, branch: str) -> str | None:
        out = gitops.git(None, "ls-remote", self.git_url(repository), f"refs/heads/{branch}", env=self.git_env())
        return out.split()[0] if out else None

    def fetch(self, repo_dir: Path, repository: str, ref: str) -> None:
        gitops.git(repo_dir, "fetch", "--quiet", "--no-tags", self.git_url(repository), ref, env=self.git_env(),
                   timeout=600)

    def push(self, repo_dir: Path, repository: str, branch: str, expected_remote_sha: str | None) -> None:
        """Fast-forward-only push of HEAD to refs/heads/<branch>, retried once on transient failure."""
        if branch in ("main", "master") or not branch:
            raise PublishError(f"refusing to push to protected branch {branch!r}")
        head = gitops.head_sha(repo_dir)
        current = self.remote_branch_sha(repository, branch)
        if current == head:
            return  # idempotent re-publish
        if current != expected_remote_sha:
            raise PublishConflict(f"remote {branch} is at {current}, expected {expected_remote_sha}")
        args = ["push", "--quiet", "--no-verify", self.git_url(repository), f"HEAD:refs/heads/{branch}"]
        try:
            gitops.git(repo_dir, *args, env=self.git_env(), timeout=600)
        except gitops.GitError:
            if self.remote_branch_sha(repository, branch) == head:
                return
            gitops.git(repo_dir, *args, env=self.git_env(), timeout=600)

    @abstractmethod
    def open_or_get_pr(self, request: PRRequest) -> PullRequestRef: ...

    @abstractmethod
    def comment(self, repository: str, number: int, body: str, *, on: str = "pull") -> None:
        """Comment on a pull request (on="pull") or an issue (on="issue")."""


class LocalGitPublisher(Publisher):
    kind = "local"
    _lock = threading.Lock()

    def __init__(self, remote: Path, state_path: Path | None = None):
        self.remote = Path(remote).resolve()
        self.state_path = state_path or self.remote.parent / f"{self.remote.name}.evo-prs.json"

    def git_url(self, repository: str) -> str:
        return str(self.remote)

    def _load(self) -> dict:
        if self.state_path.exists():
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        return {"next_number": 1, "pulls": [], "issue_comments": []}

    def _save(self, state: dict) -> None:
        self.state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")

    def pulls(self) -> list[dict]:
        return self._load()["pulls"]

    def open_or_get_pr(self, request: PRRequest) -> PullRequestRef:
        with self._lock:
            state = self._load()
            for pull in state["pulls"]:
                if pull["head"] == request.branch and pull["state"] == "open":
                    return PullRequestRef(pull["number"], pull["url"], request.branch, pull["base"], True, False)
            number = state["next_number"]
            state["next_number"] += 1
            url = f"local://{request.repository}/pull/{number}"
            state["pulls"].append({
                "number": number, "url": url, "head": request.branch, "base": request.base, "title": request.title,
                "body": request.body, "labels": request.labels, "draft": True, "state": "open",
                "issue_id": request.issue_id, "comments": [], "created_at": utc_now().isoformat(),
            })
            self._save(state)
            return PullRequestRef(number, url, request.branch, request.base, True, True)

    def comment(self, repository: str, number: int, body: str, *, on: str = "pull") -> None:
        with self._lock:
            state = self._load()
            if on == "issue":
                state["issue_comments"].append({"issue": number, "body": body})
            else:
                pull = next((p for p in state["pulls"] if p["number"] == number), None)
                if pull is None:
                    raise PublishError(f"no local pull request #{number}")
                pull["comments"].append(body)
            self._save(state)


class GitHubPublisher(Publisher):
    kind = "github"

    def __init__(self, token: str, api_url: str = "https://api.github.com", git_url_override: str | None = None,
                 transport: httpx.BaseTransport | None = None, retry_backoff_seconds: float = 2.0):
        if not token:
            raise PublishError("no GitHub token configured")
        self._token = token
        self._git_url_override = git_url_override
        self._backoff = retry_backoff_seconds
        self._http = httpx.Client(
            base_url=api_url.rstrip("/") + "/",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "evo-coding-agent"},
            timeout=30,
            transport=transport,
        )

    @classmethod
    def from_env(cls, token_env: str, **kwargs) -> GitHubPublisher:
        token = os.environ.get(token_env) or os.environ.get("GITHUB_TOKEN") or ""
        return cls(token, **kwargs)

    def secrets(self) -> list[str]:
        return [self._token, base64.b64encode(f"x-access-token:{self._token}".encode()).decode()]

    def git_url(self, repository: str) -> str:
        return self._git_url_override or f"https://github.com/{repository}.git"

    def git_env(self) -> dict[str, str]:
        # Passed through GIT_CONFIG_* env vars so the token never appears in argv.
        basic = base64.b64encode(f"x-access-token:{self._token}".encode()).decode()
        return {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.extraheader",
                "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {basic}"}

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        for attempt in (1, 2):
            try:
                response = self._http.request(method, path, **kwargs)
            except httpx.TransportError:
                if attempt == 1:
                    time.sleep(self._backoff)
                    continue
                raise
            if response.status_code >= 500 and attempt == 1:
                time.sleep(self._backoff)
                continue
            return response
        raise AssertionError("unreachable")

    def _check(self, response: httpx.Response, action: str) -> dict | list:
        if response.status_code >= 400:
            raise PublishError(f"GitHub {action} failed ({response.status_code}): {response.text[:300]}")
        return response.json() if response.content else {}

    def open_or_get_pr(self, request: PRRequest) -> PullRequestRef:
        owner = request.repository.split("/")[0]
        existing = self._check(self._request("GET", f"repos/{request.repository}/pulls",
                                             params={"head": f"{owner}:{request.branch}", "state": "open"}),
                               "list pulls")
        if existing:
            pull = existing[0]
            return PullRequestRef(pull["number"], pull["html_url"], request.branch, pull["base"]["ref"],
                                  bool(pull.get("draft")), False)
        body = {"title": request.title, "head": request.branch, "base": request.base, "body": request.body,
                "draft": True}
        response = self._request("POST", f"repos/{request.repository}/pulls", json=body)
        if response.status_code == 422 and "draft" in response.text.lower():
            # Draft PRs are unavailable on some plans; fall back to a regular PR clearly labeled as draft.
            body["draft"] = False
            body["title"] = f"[Draft] {request.title}"
            response = self._request("POST", f"repos/{request.repository}/pulls", json=body)
        pull = self._check(response, "create pull")
        if request.labels:
            self._check(self._request("POST", f"repos/{request.repository}/issues/{pull['number']}/labels",
                                      json={"labels": request.labels}), "add labels")
        return PullRequestRef(pull["number"], pull["html_url"], request.branch, request.base,
                              bool(pull.get("draft")), True)

    def comment(self, repository: str, number: int, body: str, *, on: str = "pull") -> None:
        # Issues and pull requests share one number space and one comments endpoint on GitHub.
        self._check(self._request("POST", f"repos/{repository}/issues/{number}/comments", json={"body": body}),
                    "comment")


def make_publisher(config: CodingConfig) -> Publisher:
    pub = config.publisher
    if pub.kind == "github":
        return GitHubPublisher.from_env(pub.token_env, api_url=pub.github_api_url,
                                        git_url_override=pub.github_git_url)
    if pub.local_remote is None:
        raise PublishError("local publisher needs [publisher].local_remote")
    return LocalGitPublisher(pub.local_remote)
