"""Attach selected, commit-pinned repository excerpts to an intake run."""

import argparse
import base64
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
import time
from urllib.parse import quote
from uuid import uuid4

from agent.spec_agent import github_get, LOG


def collect_evidence(run, selections, allowed_repo, get=github_get):
    context = run["input"]
    repo, commit = context["repo"], context["source_commit"]
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo) or repo.casefold() != allowed_repo.casefold():
        raise ValueError("Run repository does not match the allowed repository.")
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("Run must contain a full source commit SHA.")
    if not 1 <= len(selections) <= 5:
        raise ValueError("Select between 1 and 5 files.")
    parsed = []
    for selection in selections:
        match = re.fullmatch(r"([^:]+)(?::([1-9][0-9]*)-([1-9][0-9]*))?", selection)
        if not match:
            raise ValueError("Use --file PATH or --file PATH:START-END.")
        path = match[1]
        if "\\" in path or any(part in ("", ".", "..") for part in path.split("/")):
            raise ValueError("Use a repository-relative path with forward slashes.")
        start, end = (int(match[2]), int(match[3])) if match[2] else (1, 200)
        if end < start or end - start + 1 > 200:
            raise ValueError("Each excerpt must contain 1 to 200 lines.")
        parsed.append((path, start, end, bool(match[2])))

    evidence = []
    for path, start, end, explicit in parsed:
        LOG("evidence.file.started", path=path, start_line=start, end_line=end)
        encoded = quote(path, safe="/")
        data = get(f"/repos/{repo}/contents/{encoded}?ref={commit}")
        if not isinstance(data, dict) or data.get("type") != "file":
            raise ValueError(f"{path}: select a regular file, not a directory or submodule.")
        if data.get("encoding") != "base64" or data.get("size", 0) > 1_000_000:
            raise ValueError(f"{path}: only files up to 1 MB with base64 content are supported.")
        raw = base64.b64decode("".join(data["content"].split()), validate=True)
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise ValueError(f"{path}: expected UTF-8 text.") from None
        if b"\x00" in raw:
            raise ValueError(f"{path}: binary content is not supported.")
        lines = text.splitlines()
        if start > len(lines) or (explicit and end > len(lines)):
            raise ValueError(f"{path}: requested range is outside the file ({len(lines)} lines).")
        end = min(end, len(lines))
        excerpt = "\n".join(lines[start - 1:end])
        if len(excerpt.encode("utf-8")) > 32_000:
            raise ValueError(f"{path}: excerpt exceeds 32 KB; select fewer lines.")
        evidence.append({"id": f"SRC-{len(evidence) + 1}", "path": path,
                         "commit": commit, "blob_sha": data["sha"],
                         "start_line": start, "end_line": end, "total_lines": len(lines),
                         "is_excerpt": start != 1 or end != len(lines),
                         "url": f"https://github.com/{repo}/blob/{commit}/{encoded}#L{start}-L{end}",
                         "text": excerpt})
        LOG("evidence.file.ready", path=path, start_line=start, end_line=end, total_lines=len(lines))
    return evidence


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    parser.add_argument("--allowed-repo", required=True)
    parser.add_argument("--file", action="append", required=True, dest="selections",
                        help="Repository PATH or PATH:START-END; repeat up to 5 times")
    parser.add_argument("--output", type=Path, default=Path("runs"))
    args = parser.parse_args()
    started = time.monotonic()
    try:
        LOG("evidence.started", run=str(args.run), selections=args.selections)
        run = json.loads(args.run.read_text(encoding="utf-8"))
        evidence = collect_evidence(run, args.selections, args.allowed_repo)
        result = {**run, "parent_run_id": run["run_id"], "run_id": str(uuid4()),
                  "schema_version": "0.2", "status": "EVIDENCE_COLLECTED", "evidence": evidence,
                  "evidence_collected_at": datetime.now(timezone.utc).isoformat(),
                  "evidence_elapsed_seconds": round(time.monotonic() - started, 3)}
        args.output.mkdir(parents=True, exist_ok=True)
        destination = args.output / f"{result['run_id']}.json"
        with destination.open("x", encoding="utf-8") as stream:
            json.dump(result, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
        LOG("evidence.saved", path=str(destination.resolve()), source_ids=[s['id'] for s in evidence])
        print(destination.resolve())
    except (ValueError, OSError, KeyError, TypeError) as exc:
        LOG("evidence.failed", error=str(exc))
        print(f"Evidence collection failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
