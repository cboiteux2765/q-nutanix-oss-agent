"""Durable, opt-in GitHub polling and suggestions for local projects."""

from __future__ import annotations

import asyncio
import logging
import re
import subprocess
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

from .common import Conflict
from .github_activity import (
    FAILURES, SHA, Activity, GitHubError, GitHubReader, branch_activity, check_activity,
    comment_activity, commit_activity, continuation, digest, issue_activity, origin_repository,
    pull_head_activity, repository_name, review_activity, route, timestamp,
)
from .store import require

LOGGER = logging.getLogger(__name__)
SETTINGS = "github_activity_settings"
CHECKPOINTS = "github_activity_checkpoints"
SNAPSHOTS = "github_activity_snapshots"
EVENTS = "github_activity_events"
SUGGESTIONS = "github_activity_suggestions"
HOSTS = "github_activity_hosts"
DEFAULTS = {
    "enabled": False, "paused": False, "mode": "suggest", "repository": None, "branches": [],
    "ignored_actors": [], "last_poll_at": None, "next_poll_at": None, "last_error": None,
    "catching_up": False,
}

PULL_LIMIT = 8
PAGE_SIZE = 100
PAGES_PER_ENDPOINT = 4
REQUESTS_PER_POLL = 48
BASELINE_LOOKBACK = timedelta(days=1)
CURSOR_OVERLAP = timedelta(minutes=10)
COALESCE_WINDOW = timedelta(minutes=15)
SUGGESTIONS_PER_POLL = 3
BRANCH = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./-]{0,199}")
ACTOR = re.compile(r"[a-zA-Z0-9-]{1,80}(?:\[bot\])?")


def _time(value: datetime) -> str:
    return value.isoformat(timespec="microseconds")


def _integer(value, fallback=0) -> int:
    try:
        return int(value)
    except (ValueError, TypeError, OverflowError):
        return fallback


def _action(activity: Activity, previous: dict, baseline: datetime, occurred: datetime) -> str | None:
    if activity.kind in {"issue", "pull_request"}:
        state = activity.facts["state"]
        before = (previous.get("facts") or {}).get("state")
        if state != before and state in {"closed", "merged"}:
            return state
        if before == "closed" and state == "open":
            return "reopened"
        if previous:
            return "edited"
        created = timestamp(activity.opened_at)
        return "opened" if created and created > baseline else "changed"
    if activity.kind in {"branch_head", "pull_head"}:
        return "updated" if previous else None
    if activity.kind in {"comment", "review_comment"}:
        return "edited" if previous or (timestamp(activity.opened_at) or occurred) <= baseline else "created"
    if activity.kind == "review":
        if activity.facts["state"] == "dismissed":
            return "dismissed"
        return "edited" if previous else "submitted"
    if activity.kind == "check":
        return "completed"
    if activity.kind == "commit":
        return "added"
    return None


def _is_actionable(activity: Activity, action: str) -> bool:
    if activity.kind == "check":
        return activity.facts["conclusion"] in FAILURES
    return activity.kind != "commit" and action not in {"closed", "merged", "dismissed"}


class ProactivityService:
    def __init__(self, store, *, github=None, on_suggestion=None, interval_seconds=300,
                 clock=None):
        self.store = store
        self.github = github if github is not None else GitHubReader()
        self.on_suggestion = on_suggestion
        self.interval = max(1, interval_seconds)
        self.clock = clock or (lambda: datetime.now(UTC))
        self._lock = asyncio.Lock()
        self._settings_lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None

    def settings(self, project_id: str) -> dict:
        require(self.store, "projects", project_id)
        record = self.store.get(SETTINGS, project_id) or {}
        return {key: record.get(key, default) for key, default in DEFAULTS.items()}

    async def configure(self, project_id: str, **patch) -> dict:
        project = require(self.store, "projects", project_id)
        if set(patch) - {"enabled", "paused", "mode", "repository", "branches", "ignored_actors"}:
            raise ValueError("Unknown GitHub activity setting.")
        for field in ("enabled", "paused"):
            if field in patch and not isinstance(patch[field], bool):
                raise ValueError(f"{field.capitalize()} must be true or false.")
        if "mode" in patch and patch["mode"] not in ("suggest", "local_auto"):
            raise ValueError("Choose the suggest or local_auto proactivity mode.")
        if "repository" in patch and patch["repository"] is not None:
            if not isinstance(patch["repository"], str):
                raise ValueError("Use a GitHub repository in owner/name format.")
            patch["repository"] = repository_name(patch["repository"].strip())
        if "branches" in patch:
            branches = patch["branches"]
            if (not isinstance(branches, list) or len(branches) > 3
                    or any(not isinstance(ref, str) or not BRANCH.fullmatch(ref) or ".." in ref or "//" in ref
                           or any(segment in {".", ".."} or segment.endswith(".lock") for segment in ref.split("/"))
                           or ref.endswith(("/", ".")) for ref in branches)):
                raise ValueError("Select up to three additional branch names.")
            patch["branches"] = list(dict.fromkeys(branches))
        if "ignored_actors" in patch:
            actors = patch["ignored_actors"]
            if (not isinstance(actors, list) or len(actors) > 25
                    or any(not isinstance(actor, str) or not ACTOR.fullmatch(actor) for actor in actors)):
                raise ValueError("Enter up to 25 GitHub account names to ignore.")
            patch["ignored_actors"] = list(dict.fromkeys(actor.casefold() for actor in actors))

        async with self._settings_lock:
            previous = self.settings(project_id)
            settings = previous | patch
            if settings["enabled"] and not settings["repository"]:
                try:
                    settings["repository"] = await asyncio.to_thread(origin_repository, project["path"])
                except (subprocess.SubprocessError, ValueError) as exc:
                    raise ValueError("Connect a GitHub origin or select a repository in owner/name format.") from exc
            if settings["repository"] != previous["repository"]:
                settings |= {key: DEFAULTS[key] for key in
                             ("last_poll_at", "next_poll_at", "last_error", "catching_up")}
            if any(settings[key] != previous[key] for key in ("enabled", "paused", "branches", "ignored_actors", "mode")):
                settings["next_poll_at"] = None
            if settings["enabled"]:
                repository = settings["repository"]
                key = digest(project_id, repository, "baseline")
                if not self.store.get(CHECKPOINTS, key):
                    self.store.put(CHECKPOINTS, key, {"baseline_at": _time(self.clock())})
            self.store.put(SETTINGS, project_id, settings)
            self._wake.set()
            return self.settings(project_id)

    def dashboard(self, project_id: str, limit: int = 50) -> dict:
        settings = self.settings(project_id)
        limit = max(1, min(100, limit))
        filters = {"project_id": project_id, "repository": settings["repository"]}
        events = self.store.list(EVENTS, **filters)
        suggestions = self.store.list(SUGGESTIONS, **filters)
        events.sort(key=lambda item: (item["occurred_at"], item["created_at"], item["id"]), reverse=True)
        suggestions.sort(key=lambda item: (item["updated_at"], item["id"]), reverse=True)
        return {"settings": settings, "events": events[:limit], "suggestions": suggestions[:limit],
                "counts": {"events": len(events), "suggestions": len(suggestions),
                           "pending": sum(item["status"] == "pending" for item in suggestions)}}

    def associate_workflow(self, project_id: str, suggestion_id: str, run_id: str) -> dict:
        suggestion = require(self.store, SUGGESTIONS, suggestion_id)
        run = require(self.store, "runs", run_id)
        if suggestion["project_id"] != project_id or run["project_id"] != project_id:
            raise ValueError("Choose a suggestion and workflow from this project.")

        def associate(tx):
            suggestion = tx.get(SUGGESTIONS, suggestion_id)
            if suggestion.get("run_id") not in (None, run_id):
                raise Conflict("This suggestion already has a workflow.")
            return tx.put(SUGGESTIONS, suggestion_id, {"run_id": run_id, "status": "started"})

        return self.store.transact(associate)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._monitor(), name="evo-github-activity")

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        close = getattr(self.github, "close", None)
        if close is not None:
            await close()

    async def _monitor(self) -> None:
        while True:
            self._wake.clear()
            for record in self.store.list(SETTINGS, enabled=True, paused=False):
                try:
                    if self.store.get("projects", record["id"]):
                        await self._poll(record["id"], scheduled=True)
                except Exception:
                    LOGGER.warning("Could not process a project's GitHub activity.")
            try:
                await asyncio.wait_for(self._wake.wait(), min(30, self.interval))
            except TimeoutError:
                pass

    async def poll(self, project_id: str) -> dict:
        await self._poll(project_id, scheduled=False)
        return self.dashboard(project_id)

    async def _poll(self, project_id: str, *, scheduled: bool) -> None:
        async with self._lock:
            settings = self.settings(project_id)
            if not settings["enabled"] or settings["paused"]:
                return
            now = self.clock()
            due = timestamp(settings["next_poll_at"])
            if scheduled and due and due > now:
                return

            rate_limit = self.store.get(HOSTS, "github.com") or {}
            blocked_until = timestamp(rate_limit.get("blocked_until"))
            if blocked_until and blocked_until > now:
                self.store.put(SETTINGS, project_id, {"last_error": "GitHub rate limit reached.",
                                                       "next_poll_at": _time(blocked_until)})
                return

            current = _Poll(self, project_id, settings, now)
            try:
                async with asyncio.timeout(55):
                    await current.scan()
            except _PollChanged:
                return
            except _RequestBudget:
                current.catching_up = True
            except GitHubError as exc:
                current.errors.append(str(exc))
            except TimeoutError:
                current.errors.append("GitHub activity polling timed out.")
            except (KeyError, TypeError, ValueError):
                current.errors.append("GitHub returned unexpected activity data.")

            if not current.still_selected():
                return
            next_call = self.clock() + timedelta(seconds=current.interval)
            host = self.store.get(HOSTS, "github.com") or {}
            blocked_until = timestamp(host.get("blocked_until"))
            if blocked_until and blocked_until > next_call:
                next_call = blocked_until
            self.store.put(SETTINGS, project_id, {
                "last_poll_at": _time(now), "next_poll_at": _time(next_call),
                "last_error": current.errors[0] if current.errors else None,
                "catching_up": current.catching_up,
            })
            self._suggest(project_id, settings, self.clock())
            await self._notify(project_id, settings["repository"])

    def _suggest(self, project_id: str, settings: dict, now: datetime) -> None:
        repository = settings["repository"]
        filters = {"project_id": project_id, "repository": repository}
        events = self.store.list(EVENTS, **filters, suggestion_id=None)
        groups = defaultdict(list)
        for event in events:
            if event["actionable"]:
                groups[event["subject"]].append(event)
        if not groups:
            return
        recent = {}
        for suggestion in self.store.list(SUGGESTIONS, **filters, status="pending"):
            if timestamp(suggestion["last_activity_at"]) >= now - COALESCE_WINDOW:
                recent[suggestion["subject"]] = suggestion

        for subject, batch in list(groups.items())[:SUGGESTIONS_PER_POLL]:
            prior = recent.get(subject) or {}
            batch.sort(key=lambda item: (item["occurred_at"], item["created_at"], item["id"]))
            evidence = sorted([*prior.get("events", []), *batch],
                              key=lambda item: (item["occurred_at"], item["created_at"], item["id"]))
            ids = [*prior.get("event_ids", []), *(item["id"] for item in batch)]
            source_urls = list(dict.fromkeys(item["url"] for item in reversed(evidence)))[:12]
            newest = evidence[-1]
            subject_label = f"#{newest['number']}" if newest["number"] else subject.removeprefix("branch:")
            title = f"Review GitHub activity on {subject_label}"
            summary = f"Latest activity on {subject_label}: {newest['title']} ({newest['action']})."
            if len(ids) > 1:
                summary += f" Includes {len(ids)} related events."
            key = prior.get("id") or digest(project_id, repository, subject, ids[0])

            def save(tx):
                existing = tx.get(SUGGESTIONS, key) or {}
                tx.put(SUGGESTIONS, key, {
                    "project_id": project_id, "repository": repository, "subject": subject,
                    "title": title, "summary": summary, "source_urls": source_urls, "event_ids": ids,
                    "events": evidence[-12:], "status": existing.get("status", "pending"),
                    "last_activity_at": _time(now), "mode": settings["mode"],
                    "run_id": existing.get("run_id"), "notified_at": existing.get("notified_at"),
                })
                for event in batch:
                    tx.put(EVENTS, event["id"], {"suggestion_id": key})

            self.store.transact(save)

    async def _notify(self, project_id: str, repository: str) -> None:
        if self.on_suggestion is None:
            return
        pending = self.store.list(SUGGESTIONS, project_id=project_id, repository=repository,
                                  status="pending", notified_at=None)
        for suggestion in pending[:SUGGESTIONS_PER_POLL]:
            settings = self.settings(project_id)
            if not settings["enabled"] or settings["paused"] or settings["repository"] != repository:
                return
            try:
                async with asyncio.timeout(45):
                    await self.on_suggestion(project_id, suggestion)
            except Exception:
                LOGGER.warning("Could not deliver a GitHub activity suggestion.")
                continue
            self.store.put(SUGGESTIONS, suggestion["id"], {"notified_at": _time(self.clock())})


class _RequestBudget(Exception):
    pass


class _PollChanged(Exception):
    pass


class _Poll:
    def __init__(self, service: ProactivityService, project_id: str, settings: dict, now: datetime):
        self.service, self.store = service, service.store
        self.project_id, self.repository, self.settings, self.now = project_id, settings["repository"], settings, now
        baseline_key = self.key("baseline")
        record = self.store.get(CHECKPOINTS, baseline_key) or {}
        self.baseline = timestamp(record.get("baseline_at")) or now
        self.ignored = set(settings["ignored_actors"])
        self.interval = service.interval
        self.catching_up = False
        self.errors: list[str] = []
        self.requests = 0
        self.repository_id: int | None = None

    def key(self, name: str) -> str:
        return digest(self.project_id, self.repository, name)

    def checkpoint(self, name: str) -> dict:
        return self.store.get(CHECKPOINTS, self.key(name)) or {}

    def _set_rate_limit(self, headers: dict, status: int) -> datetime:
        now = self.service.clock()
        host = self.store.get(HOSTS, "github.com") or {}
        failures = min(_integer(host.get("failures")) + 1, 6)
        retry = _integer(headers.get("retry-after"))
        reset = _integer(headers.get("x-ratelimit-reset"))
        delay = max(retry, reset - int(now.timestamp()) + 1 if headers.get("x-ratelimit-remaining") == "0" else 0)
        if delay <= 0:
            delay = 60 * 2 ** (failures - 1)
        until = now + timedelta(seconds=delay)
        self.store.put(HOSTS, "github.com", {"blocked_until": _time(until), "failures": failures, "status": status})
        return until

    def still_selected(self) -> bool:
        current = self.service.settings(self.project_id)
        return (current["enabled"] and not current["paused"] and current["repository"] == self.repository
                and all(current[key] == self.settings[key] for key in ("branches", "ignored_actors", "mode")))

    async def request(self, target: str, etag: str | None = None):
        if not self.still_selected():
            raise _PollChanged
        if self.requests >= REQUESTS_PER_POLL:
            raise _RequestBudget
        host = self.store.get(HOSTS, "github.com") or {}
        now = self.service.clock()
        blocked_until = timestamp(host.get("blocked_until"))
        if blocked_until and blocked_until > now:
            raise GitHubError("GitHub rate limit reached.", retry_at=blocked_until)
        self.requests += 1
        page = await self.service.github.get(target, etag=etag)
        headers = page.headers
        self.interval = max(self.interval, _integer(headers.get("x-poll-interval")))
        if (page.status == 429 or page.rate_limited
                or (page.status == 403 and (headers.get("x-ratelimit-remaining") == "0" or "retry-after" in headers))):
            until = self._set_rate_limit(headers, page.status)
            raise GitHubError(f"GitHub limited activity requests (HTTP {page.status}).",
                              status=page.status, retry_at=until)
        if headers.get("x-ratelimit-remaining") == "0":
            self._set_rate_limit(headers, page.status)
        elif page.status in {200, 304} and host.get("failures"):
            self.store.put(HOSTS, "github.com", {"blocked_until": None, "failures": 0})
        if page.status == 401:
            raise GitHubError("GitHub authentication failed. Check gh auth login.", status=401)
        if page.status not in {200, 304}:
            raise GitHubError(f"GitHub activity request failed (HTTP {page.status}).", status=page.status)
        return page

    def _event(self, tx, name: str, activity: Activity) -> None:
        observed = timestamp(activity.occurred_at)
        if observed is None:
            raise ValueError("Missing activity timestamp.")
        object_key = digest(self.key(name), activity.object_id)
        previous = tx.get(SNAPSHOTS, object_key) or {}
        version = digest(activity.revision)
        earlier = timestamp(previous.get("occurred_at"))
        if earlier and observed < earlier:
            return
        seen = previous.get("seen", []) if earlier == observed else []
        if version in seen:
            return
        changed = previous.get("version") != version
        tx.put(SNAPSHOTS, object_key, {"version": version, "occurred_at": _time(observed),
                                       "seen": [*seen, version][-16:], "facts": activity.facts,
                                       "revision": activity.revision})
        if not changed or activity.bot or (activity.actor or "").casefold() in self.ignored:
            return
        # Reviews can be dismissed or edited without an updated_at value in the REST representation.
        if activity.kind == "review" and previous and observed == earlier:
            observed = self.now
        if observed <= self.baseline:
            return
        action = _action(activity, previous, self.baseline, observed)
        if action is None:
            return
        key = digest(object_key, action, _time(observed), version)
        if tx.get(EVENTS, key):
            return
        facts = dict(activity.facts)
        if previous and activity.kind in {"branch_head", "pull_head"}:
            facts["before_sha"] = previous["facts"]["sha"]
        if previous and action == "edited":
            fields = ("title", "body", "state", "labels", "assignees", "path", "line", "sha")
            facts["changed_fields"] = [field for field in fields
                                       if previous["revision"].get(field) != activity.revision.get(field)]
        tx.put(EVENTS, key, {
            "project_id": self.project_id, "repository": self.repository, "kind": activity.kind,
            "action": action, "subject": activity.subject, "number": activity.number, "title": activity.title,
            "url": activity.url, "occurred_at": _time(observed), "actor": activity.actor, "facts": facts,
            "actionable": _is_actionable(activity, action), "suggestion_id": None,
        })

    def _save(self, name: str, data: dict, activities=(), *, activity_scope: str | None = None) -> None:
        def save(tx):
            for activity in activities:
                if activity is not None:
                    self._event(tx, activity_scope or name, activity)
            tx.put(CHECKPOINTS, self.key(name), data)

        self.store.transact(save)

    async def _single(self, name: str, target: str, normalize) -> dict:
        existing = self.checkpoint(name)
        page = await self.request(target, existing.get("etag") if target == existing.get("route") else None)
        if page.status == 304:
            return existing.get("value", {})
        value, activities = normalize(page.data)
        self._save(name, {"route": target, "etag": page.headers.get("etag"), "value": value}, activities)
        return value

    async def _pages(self, name: str, first: str, normalize, *, data_key=None,
                     activity_scope=None, max_pages=PAGES_PER_ENDPOINT) -> bool:
        existing = self.checkpoint(name)
        start = existing.get("scan_at") or _time(self.now)
        first = existing.get("scan_first") or first
        saved_next = existing.get("next") if not existing.get("needs_replay") else None
        try:
            target = continuation(first, saved_next, self.repository, self.repository_id) or first
        except GitHubError:
            # The repository's numeric ID can change if its name was reused.
            target = first
        etag = existing.get("etag") if existing.get("route") == first and target == first else None
        for _ in range(max_pages):
            if target != first:
                # Offset pages can shift after an interrupted request. Re-read the prefix after
                # an error or process crash and deduplicate it against the already-saved snapshots.
                self._save(name, {"needs_replay": True})
            page = await self.request(target, etag)
            if page.status == 304:
                self._save(name, {"cursor": start, "scan_at": None, "scan_first": None,
                                  "next": None, "needs_replay": False})
                return True
            rows = page.data[data_key] if data_key else page.data
            if not isinstance(rows, list):
                raise ValueError("Unexpected endpoint response.")
            after = continuation(target, page.next_link, self.repository, self.repository_id)
            if after == target or (after and after == first):
                raise GitHubError("GitHub returned a repeating pagination link.")
            activities = [normalize(row) for row in rows]
            # A full first page can become one of several pages without its ETag changing.
            validator = page.headers.get("etag") if target == first and not after and len(rows) < PAGE_SIZE else None
            self._save(name, {"route": first, "etag": validator, "next": after, "needs_replay": False,
                              "scan_at": start if after else None, "scan_first": first if after else None,
                              "cursor": existing.get("cursor") if after else start},
                       activities, activity_scope=activity_scope)
            if not after:
                return True
            target, etag = after, None
        self.catching_up = True
        return False

    async def _incremental(self, name, endpoint, normalize, **query) -> None:
        earliest = timestamp(self.checkpoint(name).get("cursor")) or self.baseline - BASELINE_LOOKBACK
        window = (earliest - CURSOR_OVERLAP).replace(minute=0, second=0, microsecond=0)
        first = route(self.repository, endpoint, since=window.strftime("%Y-%m-%dT%H:%M:%SZ"),
                      sort="updated", direction="asc", per_page=PAGE_SIZE, **query)
        await self._pages(name, first, normalize)

    async def _optional(self, call, *args, missing=False) -> None:
        try:
            await call(*args)
        except GitHubError as exc:
            if exc.retry_at or exc.status == 401:
                raise
            if not missing or exc.status not in {404, 409}:
                self.errors.append(str(exc))
        except (KeyError, TypeError, ValueError):
            self.errors.append("GitHub returned unexpected activity data.")

    async def _reviews(self, number: int) -> None:
        await self._pages(f"review:{number}", route(self.repository, f"/pulls/{number}/reviews", per_page=PAGE_SIZE),
                          lambda item: review_activity(self.repository, number, item), max_pages=1)

    async def _checks(self, subject: str, sha: str) -> None:
        await self._pages(f"check:{sha}",
                          route(self.repository, f"/commits/{sha}/check-runs", filter="latest", per_page=PAGE_SIZE),
                          lambda item: check_activity(self.repository, subject, sha, item), data_key="check_runs",
                          max_pages=1)

    def _metadata(self, data: dict):
        return {"id": int(data["id"]), "default_branch": str(data["default_branch"])}, []

    def _pulls(self, data: list):
        if not isinstance(data, list):
            raise ValueError("Unexpected pull request response.")
        items = [pull_head_activity(self.repository, item, _time(self.now)) for item in data[:PULL_LIMIT]]
        return {"heads": [{"number": item.number, "sha": item.facts["sha"]} for item in items]}, items

    async def _branch(self, branch: str) -> str | None:
        name = f"branch:{branch}"

        def normalize(rows: list):
            if not isinstance(rows, list):
                raise ValueError("Unexpected branch commit response.")
            if not rows:
                return {"head": None}, []
            activity = branch_activity(self.repository, branch, rows[0], _time(self.now))
            sha = activity.facts["sha"]
            if not SHA.fullmatch(sha):
                raise ValueError("Unexpected branch SHA.")
            return {"head": sha}, [activity]

        state = await self._single(name, route(self.repository, "/commits", sha=branch, per_page=1), normalize)
        head = state.get("head")
        if not head:
            return None
        confirmed = self.checkpoint(name).get("confirmed_sha")
        if not confirmed:
            self._save(name, {"confirmed_sha": head})
        elif head != confirmed or self.checkpoint(f"commits:{branch}").get("comparison_head"):
            await self._optional(self._compare_branch, name, branch, confirmed, head, missing=True)
        return head

    async def _compare_branch(self, name: str, branch: str, confirmed: str, head: str) -> None:
        key = f"commits:{branch}"
        compare = self.checkpoint(key)
        head = compare.get("comparison_head") or head
        self._save(key, {"comparison_head": head})
        target = route(self.repository, f"/compare/{quote(confirmed)}...{quote(head)}", per_page=PAGE_SIZE)
        try:
            complete = await self._pages(key, target,
                                         lambda item: commit_activity(self.repository, branch, item, _time(self.now)),
                                         data_key="commits", activity_scope=name + ":commits", max_pages=1)
        except GitHubError as exc:
            if exc.status != 404:
                raise
            # The branch-head event still records a force push between unrelated histories.
            self._save(key, {"comparison_head": None, "next": None, "scan_at": None,
                             "scan_first": None, "needs_replay": False})
            complete = True
        if complete:
            def save(tx):
                tx.put(CHECKPOINTS, self.key(name), {"confirmed_sha": head})
                tx.put(CHECKPOINTS, self.key(key), {"comparison_head": None})
            self.store.transact(save)

    async def scan(self) -> None:
        meta = await self._single("repository", route(self.repository), self._metadata)
        self.repository_id = meta["id"]
        await self._optional(lambda: self._incremental(
            "issues", "/issues", lambda item: issue_activity(self.repository, item), state="all"))
        await self._optional(lambda: self._incremental(
            "issue_comments", "/issues/comments", lambda item: comment_activity(self.repository, item)))
        await self._optional(lambda: self._incremental(
            "review_comments", "/pulls/comments", lambda item: comment_activity(self.repository, item, review=True)))

        try:
            pulls = await self._single("pull_heads", route(self.repository, "/pulls", state="open",
                                       sort="updated", direction="desc", per_page=PULL_LIMIT), self._pulls)
        except GitHubError as exc:
            if exc.retry_at or exc.status == 401:
                raise
            self.errors.append(str(exc))
            pulls = self.checkpoint("pull_heads").get("value", {"heads": []})
        head_subjects = {}
        for pull in pulls["heads"]:
            await self._optional(self._reviews, pull["number"], missing=True)
            if SHA.fullmatch(pull["sha"]):
                head_subjects.setdefault(pull["sha"], f"thread:{pull['number']}")
        for branch in dict.fromkeys([meta["default_branch"], *self.settings["branches"]]):
            try:
                head = await self._branch(branch)
            except GitHubError as exc:
                if exc.retry_at or exc.status == 401:
                    raise
                self.errors.append(f"Could not read branch {branch!r}.")
                continue
            if head:
                head_subjects.setdefault(head, f"branch:{branch}")
        for sha, subject in head_subjects.items():
            await self._optional(self._checks, subject, sha, missing=True)
