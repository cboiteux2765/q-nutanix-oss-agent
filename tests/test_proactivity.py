import asyncio
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from evo.github_activity import GitHubError, GitHubPage, GitHubReader
from evo.proactivity import CHECKPOINTS, ProactivityService
from evo.store import Store

REPO = "octocat/welcome"
ROOT = f"/repos/{REPO}"
A, B = "a" * 40, "b" * 40
BASE = datetime(2026, 11, 19, 10, 0, tzinfo=UTC)


def stamp(offset: int) -> str:
    return (BASE + timedelta(seconds=offset)).isoformat()


def issue(number, seconds, *, title="Broken build", state="open", created=None, **fields):
    return {"id": number, "number": number, "title": title, "state": state, "body": "Steps to reproduce",
            "updated_at": stamp(seconds), "created_at": stamp(seconds if created is None else created),
            "user": {"login": "guest", "type": "User"},
            "html_url": f"https://github.com/{REPO}/issues/{number}", **fields}


def comment(number, seconds, *, created=None, body="Confirmed", actor="guest", object_id=499):
    return {"id": object_id, "issue_url": f"https://api.github.com{ROOT}/issues/{number}",
            "html_url": f"https://github.com/{REPO}/issues/{number}#issuecomment-{object_id}",
            "user": {"login": actor, "type": "User"}, "body": body,
            "created_at": stamp(seconds if created is None else created), "updated_at": stamp(seconds)}


def commit(sha):
    return {"sha": sha, "html_url": f"https://github.com/{REPO}/commit/{sha}",
            "commit": {"message": "Update test harness\n\nDetailed message", "committer": {"date": stamp(-15)}}}


def pull(number, sha, seconds):
    return {"number": number, "title": "Update the app", "head": {"ref": "feature", "sha": sha},
            "created_at": stamp(-600), "updated_at": stamp(seconds)}


def check(id, conclusion, seconds):
    return {"id": id, "name": "CI", "started_at": stamp(seconds - 5), "completed_at": stamp(seconds),
            "status": "completed", "conclusion": conclusion}


class FakeGitHub:
    def __init__(self):
        self.responses = {
            ROOT: {"id": 9001, "default_branch": "main"},
            ROOT + "/issues": [],
            ROOT + "/issues/comments": [],
            ROOT + "/pulls/comments": [],
            ROOT + "/pulls": [],
            ROOT + "/commits": [],
        }
        self.calls = []

    async def get(self, target, *, etag=None):
        url = urlsplit(target)
        params = parse_qs(url.query)
        self.calls.append((url.path, params, etag))
        assert url.path in self.responses, url.path
        response = self.responses[url.path]
        if callable(response):
            response = response(params, etag)
        return response if isinstance(response, GitHubPage) else GitHubPage(200, response)


class Clock:
    now = BASE

    def __call__(self):
        return self.now

    def move(self, seconds):
        self.now = BASE + timedelta(seconds=seconds)


def test_baseline_deduplicates_changes_filters_noise_and_coalesces(store, project):
    async def scenario():
        upstream, clock, delivered = FakeGitHub(), Clock(), []

        async def notify(project_id, suggestion):
            delivered.append((project_id, suggestion))

        service = ProactivityService(store, github=upstream, clock=clock, on_suggestion=notify)
        project_id = project["id"]
        assert (await service.poll(project_id))["settings"]["enabled"] is False
        assert not upstream.calls
        await service.configure(project_id, repository=REPO.upper(), enabled=True, ignored_actors=["OurService"])
        upstream.responses[ROOT + "/issues"] = [issue(1, -20)]
        upstream.responses[ROOT + "/issues/comments"] = [comment(1, -8)]
        assert (await service.poll(project_id))["events"] == []
        assert not delivered
        assert upstream.calls[0][0] == ROOT

        clock.move(120)
        # A PR/issue updated_at move by itself has no semantic content.
        upstream.responses[ROOT + "/issues"] = [issue(1, 20, created=-20), issue(2, 24), issue(2, 24)]
        upstream.responses[ROOT + "/issues/comments"] = [
            comment(1, 15, created=-8, body="Confirmed on Windows"),
            comment(2, 25, actor="automation[bot]", object_id=500),
            comment(2, 26, actor="ourservice", object_id=501),
            comment(2, 30, object_id=502),
        ]
        activity = await service.poll(project_id)
        assert {(event["kind"], event["action"], event["number"]) for event in activity["events"]} == {
            ("issue", "opened", 2), ("comment", "edited", 1), ("comment", "created", 2),
        }
        assert len(delivered) == 2
        second = next(s for s in activity["suggestions"] if s["subject"] == "thread:2")
        assert len(second["event_ids"]) == 2
        assert len(second["source_urls"]) == 2

        clock.move(200)
        upstream.responses[ROOT + "/issues/comments"] += [
            comment(1, 18, created=-8, body="Actually on Windows 11"),
            comment(1, -8),
        ]
        activity = await service.poll(project_id)
        assert len(activity["events"]) == 4
        assert len(activity["suggestions"]) == 2
        assert len(delivered) == 2
        assert not store.list("runs")

        run = store.put("runs", "explicit-request", {"project_id": project_id})
        linked = service.associate_workflow(project_id, second["id"], run["id"])
        assert linked["status"] == "started"
        assert linked["run_id"] == run["id"]

    asyncio.run(scenario())


def test_partial_scan_recovers_after_reopen_and_keeps_time_overlap(tmp_path):
    async def scenario():
        upstream, clock = FakeGitHub(), Clock()
        # The second link switches to GitHub's numeric repository path.
        numeric = "/repositories/9001/issues"
        next_link = f"<https://api.github.com{numeric}?page=2>; rel=\"next\""
        data_dir = tmp_path / "activity"
        store = Store(data_dir)
        project = store.put("projects", "activity-project", {"path": str(tmp_path)})
        project_id = project["id"]
        service = ProactivityService(store, github=upstream, clock=clock)
        await service.configure(project_id, enabled=True, repository=REPO)
        await service.poll(project_id)
        endpoint = next(record for record in store.list(CHECKPOINTS)
                        if record.get("route", "").startswith(ROOT + "/issues?"))
        verified = endpoint["cursor"]

        clock.move(40)
        upstream.responses[ROOT + "/issues"] = GitHubPage(200, [issue(3, 20)], {"Link": next_link, "ETag": "first"})
        upstream.responses[numeric] = GitHubPage(503)
        partial = await service.poll(project_id)
        assert partial["settings"]["last_error"].endswith("(HTTP 503).")
        assert [item["number"] for item in partial["events"]] == [3]
        checkpoint = store.get(CHECKPOINTS, endpoint["id"])
        assert checkpoint["cursor"] == verified
        assert checkpoint["next"] == numeric + "?page=2"
        assert checkpoint["etag"] is None
        assert checkpoint["needs_replay"]
        store.close()

        store = Store(data_dir)
        service = ProactivityService(store, github=upstream, clock=clock)
        clock.move(70)
        # The first unseen row shifted back into the already-read page during downtime.
        upstream.responses[ROOT + "/issues"] = GitHubPage(200, [issue(5, 22)], {"Link": next_link})
        upstream.responses[numeric] = [issue(4, 25)]
        resumed = await service.poll(project_id)
        assert resumed["settings"]["last_error"] is None
        assert [item["number"] for item in resumed["events"]] == [4, 5, 3]
        assert store.get(CHECKPOINTS, endpoint["id"])["next"] is None
        assert store.get(CHECKPOINTS, endpoint["id"])["cursor"] != verified

        clock.move(120)
        # A late arrival from inside the overlap and duplicate rows from the completed sweep.
        upstream.responses[ROOT + "/issues"] = [issue(6, 24), issue(3, 20), issue(4, 25), issue(5, 22)]
        overlapping = await service.poll(project_id)
        assert {item["number"] for item in overlapping["events"]} == {3, 4, 5, 6}
        latest_call = next(call for call in reversed(upstream.calls) if call[0] == ROOT + "/issues")
        assert latest_call[1]["since"][0] < stamp(24)
        store.close()

    asyncio.run(scenario())


def test_rate_limit_blocks_even_manual_poll_until_retry_and_respects_pause(store, project):
    async def scenario():
        upstream, clock = FakeGitHub(), Clock()
        service = ProactivityService(store, github=upstream, clock=clock, interval_seconds=30)
        project_id = project["id"]
        await service.configure(project_id, repository=REPO, enabled=True)
        upstream.responses[ROOT] = GitHubPage(429, headers={"Retry-After": "180"})
        throttled = await service.poll(project_id)
        assert throttled["settings"]["next_poll_at"] == (BASE + timedelta(seconds=180)).isoformat(timespec="microseconds")
        calls = len(upstream.calls)
        assert calls == 1
        assert (await service.poll(project_id))["settings"]["last_error"] == "GitHub rate limit reached."
        assert len(upstream.calls) == calls

        clock.move(180)
        upstream.responses[ROOT] = {"id": 9001, "default_branch": "main"}
        await service.configure(project_id, paused=True)
        await service.poll(project_id)
        assert len(upstream.calls) == calls
        await service.configure(project_id, paused=False)
        assert (await service.poll(project_id))["settings"]["last_error"] is None
        assert len(upstream.calls) > calls

    asyncio.run(scenario())


def test_monitor_reads_open_pull_reviews_checks_and_tracked_commits(store, project):
    async def scenario():
        upstream, clock, delivered = FakeGitHub(), Clock(), []

        async def notify(_, suggestion):
            delivered.append(suggestion)

        service = ProactivityService(store, github=upstream, on_suggestion=notify, clock=clock)
        project_id = project["id"]
        await service.configure(project_id, repository=REPO, enabled=True, mode="local_auto", branches=["integration"])
        upstream.responses[ROOT + "/pulls"] = GitHubPage(200, [pull(10, A, -10)], {"etag": "pulls"})
        upstream.responses[ROOT + "/pulls/10/reviews"] = [
            {"id": 1, "state": "APPROVED", "submitted_at": stamp(-10), "user": {"login": "reviewer"}},
        ]
        upstream.responses[ROOT + "/commits"] = [commit(A)]
        upstream.responses[ROOT + f"/commits/{A}/check-runs"] = {"check_runs": [check(1, "failure", -10)]}
        assert not (await service.poll(project_id))["events"]

        clock.move(90)
        upstream.responses[ROOT + "/pulls"] = [pull(10, B, 10)]
        upstream.responses[ROOT + "/pulls/10/reviews"] = [
            {"id": 1, "state": "DISMISSED", "submitted_at": stamp(-10), "user": {"login": "reviewer"}},
            {"id": 2, "state": "CHANGES_REQUESTED", "submitted_at": stamp(20), "user": {"login": "reviewer"}},
        ]
        upstream.responses[ROOT + "/commits"] = lambda params, _: [commit(B if params["sha"] == ["main"] else A)]
        upstream.responses[ROOT + f"/compare/{A}...{B}"] = {"commits": [commit(B)]}
        upstream.responses[ROOT + f"/commits/{B}/check-runs"] = {"check_runs": [
            check(2, "failure", 30), check(3, "success", 30),
        ]}
        activity = await service.poll(project_id)
        assert sorted(item["kind"] for item in activity["events"]) == [
            "branch_head", "check", "check", "commit", "pull_head", "review", "review",
        ]
        assert any(event["facts"].get("before_sha") == A for event in activity["events"])
        assert (ROOT + "/pulls", {"state": ["open"], "sort": ["updated"], "direction": ["desc"],
                                  "per_page": ["8"]}, "pulls") in upstream.calls
        checks = [event for event in activity["events"] if event["kind"] == "check"]
        assert {item["facts"]["conclusion"]: item["actionable"] for item in checks} == {"failure": True, "success": False}
        assert all(item["mode"] == "local_auto" and item["status"] == "pending" for item in delivered)
        assert not store.list("runs")

    asyncio.run(scenario())


def test_reader_limits_requests_to_github_and_redacts_credential(monkeypatch):
    async def scenario():
        secret = "fake-credential-activity-test"
        monkeypatch.setenv("GH_TOKEN", secret)
        requests = []

        def handle(request):
            requests.append(request)
            return httpx.Response(200, json={"description": f"token: {secret}"},
                                  headers={"ETag": "stable", "X-Other": secret})

        reader = GitHubReader(transport=httpx.MockTransport(handle))
        try:
            page = await reader.get(ROOT, etag="existing")
            assert page.data["description"] == "token: [redacted]"
            assert page.headers == {"etag": "stable"}
            assert requests[0].method == "GET"
            assert requests[0].url.host == "api.github.com"
            assert requests[0].headers["If-None-Match"] == "existing"
            assert requests[0].headers["Authorization"] == f"Bearer {secret}"
            with pytest.raises(GitHubError):
                await reader.get("https://other.example/")
            assert len(requests) == 1
        finally:
            await reader.close()

    asyncio.run(scenario())


def test_pagination_cannot_follow_a_different_repository(store, project):
    async def scenario():
        upstream, clock = FakeGitHub(), Clock()
        service = ProactivityService(store, github=upstream, clock=clock)
        await service.configure(project["id"], repository=REPO, enabled=True)
        upstream.responses[ROOT + "/issues"] = GitHubPage(
            200, [], {"Link": '<https://api.github.com/repositories/42/issues?page=2>; rel="next"'},
        )
        dashboard = await service.poll(project["id"])
        assert dashboard["settings"]["last_error"] == "GitHub returned an invalid pagination link."
        assert not any(path.startswith("/repositories/42") for path, _, _ in upstream.calls)

    asyncio.run(scenario())




def test_monitor_keeps_check_capacity_after_large_reviews_and_permission_errors(store, project):
    async def scenario():
        upstream, clock = FakeGitHub(), Clock()
        project_id = project["id"]
        service = ProactivityService(store, github=upstream, clock=clock)
        await service.configure(project_id, repository=REPO, enabled=True)
        upstream.responses[ROOT + "/pulls"] = [pull(i, f"{i:040x}", 0) for i in range(1, 9)]
        upstream.responses[ROOT + "/pulls/1/reviews"] = GitHubPage(403)
        for i in range(1, 9):
            if i > 1:
                reviews = ROOT + f"/pulls/{i}/reviews"
                upstream.responses[reviews] = GitHubPage(
                    200, [], {"Link": f'<https://api.github.com{reviews}?page=2>; rel="next"'},
                )
            upstream.responses[ROOT + f"/commits/{i:040x}/check-runs"] = {"check_runs": []}

        activity = await service.poll(project_id)
        assert activity["settings"]["last_error"] == "GitHub activity request failed (HTTP 403)."
        assert activity["settings"]["catching_up"]
        assert sum(path.endswith("/check-runs") for path, _, _ in upstream.calls) == 8
        assert not any(params.get("page") for _, params, _ in upstream.calls)

    asyncio.run(scenario())


def test_pause_during_request_prevents_further_requests(store, project):
    async def scenario():
        started, released = asyncio.Event(), asyncio.Event()

        class SlowMetadata(FakeGitHub):
            async def get(self, target, *, etag=None):
                if target == ROOT:
                    started.set()
                    await released.wait()
                return await super().get(target, etag=etag)

        upstream = SlowMetadata()
        service = ProactivityService(store, github=upstream, clock=Clock())
        project_id = project["id"]
        await service.configure(project_id, repository=REPO, enabled=True)
        polling = asyncio.create_task(service.poll(project_id))
        await started.wait()
        await service.configure(project_id, paused=True)
        released.set()
        activity = await polling
        assert len(upstream.calls) == 1
        assert activity["settings"]["paused"]
        assert not activity["suggestions"]

    asyncio.run(scenario())
