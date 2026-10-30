"""Component 1: read a GitHub issue into a commit-pinned local run."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen
from uuid import uuid4
from agent.events import EventLog

LOG = EventLog(Path("runs/actions.jsonl"))


def github_get(path):
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "product-spec-prototype"}
    if token := os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {token}"
    request = Request(f"https://api.github.com{path}", headers=headers)
    LOG("github.request", method="GET", endpoint=request.full_url)
    started = time.monotonic()
    try:
        with urlopen(request, timeout=30) as response:
            result = json.load(response)
            LOG("github.response", endpoint=request.full_url, status=response.status, elapsed_seconds=round(time.monotonic() - started, 3))
            return result
    except HTTPError as exc:
        LOG("github.failed", endpoint=request.full_url, status=exc.code)
        hints = {401: "Check GITHUB_TOKEN.", 403: "Check token permissions or API rate limit.",
                 404: "Check the repository, issue, ref, and token access.",
                 429: "GitHub rate limit reached; try again later."}
        raise ValueError(f"GitHub HTTP {exc.code}. {hints.get(exc.code, 'Try again later.')}") from None
    except (URLError, TimeoutError):
        LOG("github.failed", endpoint=request.full_url, error="Connection failed or timed out")
        raise ValueError("Cannot reach GitHub; check your connection and try again.") from None


def collect(issue_url, allowed_repo, ref=None, get=github_get):
    match = re.fullmatch(r"https://github\.com/([\w.-]+/[\w.-]+)/issues/([1-9][0-9]*)/?", issue_url)
    if not match:
        raise ValueError("Expected https://github.com/OWNER/REPO/issues/NUMBER.")
    repo, number = match[1], int(match[2])
    if repo.casefold() != allowed_repo.casefold():
        raise ValueError(f"Repository is not allowed. Configured repository: {allowed_repo}")
    root = f"/repos/{repo}"
    metadata = get(root)
    commit = get(f"{root}/commits/{quote(ref or metadata['default_branch'], safe='')}")
    sha = commit["sha"]
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("GitHub did not return a full commit SHA.")
    LOG("intake.commit.pinned", repo=repo, commit=sha)
    issue = get(f"{root}/issues/{number}")
    if "pull_request" in issue:
        raise ValueError("This number identifies a pull request; provide an issue instead.")
    comments = []
    page = 1
    while True:
        batch = get(f"{root}/issues/{number}/comments?per_page=100&page={page}")
        comments.extend({"id": c["id"], "actor": (c.get("user") or {}).get("login"),
                         "body": c.get("body") or "", "url": c["html_url"],
                         "updated_at": c["updated_at"]} for c in batch)
        if len(batch) < 100:
            break
        page += 1
    LOG("intake.context.ready", repo=repo, issue_number=number, comment_count=len(comments))
    return {
        "repo": repo, "issue_number": number, "source_commit": sha,
        "source_url": f"https://github.com/{repo}/tree/{sha}",
        "title": issue["title"], "body": issue.get("body") or "",
        "actor": (issue.get("user") or {}).get("login"),
        "issue_url": issue["html_url"], "issue_updated_at": issue["updated_at"],
        "labels": [label["name"] for label in issue["labels"]], "comments": comments,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("issue_url")
    parser.add_argument("--allowed-repo", required=True, help="Single allowed OWNER/REPO")
    parser.add_argument("--ref", help="Branch, tag, or SHA; default: repository default branch")
    parser.add_argument("--output", type=Path, default=Path("runs"))
    args = parser.parse_args()
    started = time.monotonic()
    try:
        LOG("intake.started", issue_url=args.issue_url)
        context = collect(args.issue_url, args.allowed_repo, args.ref)
        run_id = str(uuid4())
        run = {"schema_version": "0.1", "run_id": run_id, "correlation_id": run_id,
               "status": "CONTEXT_COLLECTED", "trigger": "manual",
               "collected_at": datetime.now(timezone.utc).isoformat(),
               "elapsed_seconds": round(time.monotonic() - started, 3),
               "llm_cost_usd": 0, "input": context}
        args.output.mkdir(parents=True, exist_ok=True)
        destination = args.output / f"{run_id}.json"
        with destination.open("x", encoding="utf-8") as stream:
            json.dump(run, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
        LOG("intake.saved", path=str(destination.resolve()), run_id=run_id)
        print(destination.resolve())
    except (ValueError, OSError, KeyError, TypeError) as exc:
        LOG("intake.failed", error=str(exc))
        print(f"Intake failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
