"""Select evaluation tasks from the replay mirror with fixed, documented criteria.

The criteria below were written before any agent ran on the new tasks, and nothing here looks at
agent results. Running this module rebuilds `pytest_tasks.json` and `pytest_selection.json`
deterministically from the mirror:

    python -m evo.evolve.select_tasks --data-dir <dir>      # discover, build and verify, write catalog

Selection criteria (a candidate must meet all of them):

1. The pull request was merged into `main` and opened inside the mirrored window, so its
   discussion, reviews and CI were mirrored in full.
2. Its description closes exactly one issue ("closes/fixes/resolves #N"). That issue was opened
   inside the window (so its comments were mirrored) and before the pull request.
3. The fix is small and self-contained: 1-3 Python files under `src/`, at most 80 changed source
   lines, at least one changed test file under `testing/`, and no changed files other than
   `src/`, `testing/`, `changelog/`, `doc/` and the `AUTHORS` list (no packaging or CI changes).
4. Reference verification on the as-of snapshot (see tasks.py): the fix's patch applies, at least
   one of its tests fails before the source change and passes after it, and the test run finishes
   within the check timeout. Tests that cannot run here (for example skipped on Windows) never
   become checks.
5. One task per issue. When two candidates close the same issue, or an issue was marked a
   duplicate of another candidate's issue, the earliest pull request is kept.

Split rule: held-out tasks are the newest. The boundary is placed so every reflection and
validation fix was merged before the earliest held-out snapshot, so nothing the search sees is
newer than any held-out task. Reflection and validation tasks are interleaved in time (assigned
round-robin by snapshot date). Tasks already used by an experiment keep their role: a task the
search has seen is never held out, and a held-out task is never searched (the same rule the
service enforces).
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path

from .tasks import CATALOG, TaskBundles, _git, _iso_minus_second, replay_dir

SELECTION = CATALOG.with_name("pytest_selection.json")
CLOSES = re.compile(r"(?i)\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\b[:\s]+"
                    r"(?:https://github\.com/pytest-dev/pytest/issues/|#)(\d+)")
ALLOWED_PREFIXES = ("src/", "testing/", "changelog/", "doc/")
ALLOWED_FILES = {"AUTHORS"}  # the contributor list; first-time contributors add themselves
MAX_SOURCE_FILES = 3
MAX_SOURCE_LINES = 80
# Roles fixed by experiments that already ran on these tasks (the 2026-11-12 live experiment used
# 14492 for reflection, 14483 for validation and held out 15132).
EXPOSED = {"pytest-14483": "search", "pytest-14492": "search", "pytest-15132": "heldout"}
CRITERIA_VERSION = 1


def _numstat(git_dir: Path, merge: str) -> list[tuple[int, int, str]]:
    rows = []
    for line in _git(git_dir, "diff", "--numstat", f"{merge}^1", merge).decode().splitlines():
        added, deleted, name = line.split("\t", 2)
        rows.append((int(added) if added != "-" else 0, int(deleted) if deleted != "-" else 0, name))
    return rows


def discover() -> list[dict]:
    """Every merged in-window pull request with a closing reference, with the reason it is excluded (if any)."""
    replay = replay_dir()
    if not replay:
        raise RuntimeError("Set EVO_REPLAY_DATA to the replay mirror (history.db + repo.git).")
    git_dir = replay / "repo.git"
    db = sqlite3.connect(f"file:{replay / 'history.db'}?mode=ro", uri=True)
    try:
        duplicates = {}
        for number, extra in db.execute("SELECT number, extra_json FROM issue_events WHERE event='marked_as_duplicate'"):
            duplicates[number] = extra
        candidates = []
        rows = db.execute(
            "SELECT i.number, i.title, i.body, i.created_at, p.merged_at, p.merge_commit_sha, p.base_ref "
            "FROM items i JOIN pulls p ON p.number=i.number "
            "WHERE i.is_pr=1 AND p.merged_at IS NOT NULL AND i.in_window=1 ORDER BY i.created_at").fetchall()
        for pr, title, body, created, merged, merge, base in rows:
            issues = sorted({int(n) for n in CLOSES.findall(body or "")} - {pr})
            if not issues:
                continue
            entry = {"pr": pr, "pr_title": title, "pr_created_at": created, "merged_at": merged,
                     "merge_commit": merge, "issues": issues, "excluded": None}
            candidates.append(entry)
            if base != "main":
                entry["excluded"] = f"merged into {base}, not main"
                continue
            if len(issues) != 1:
                entry["excluded"] = f"closes {len(issues)} issues"
                continue
            issue = db.execute("SELECT number, is_pr, title, body, created_at, in_window FROM items WHERE number=?",
                               (issues[0],)).fetchone()
            if not issue:
                entry["excluded"] = "closed issue is not in the mirror"
                continue
            entry.update(issue=issue[0], title=issue[2], issue_created_at=issue[4])
            if issue[1]:
                entry["excluded"] = "closing reference points to a pull request"
                continue
            if not issue[5]:
                entry["excluded"] = "issue opened before the mirrored window (comments not mirrored)"
                continue
            if issue[4] >= created:
                entry["excluded"] = "issue opened after the pull request"
                continue
            if not (issue[3] or "").strip():
                entry["excluded"] = "issue has no description"
                continue
            if not merge:
                entry["excluded"] = "no merge commit in the mirror"
                continue
            try:
                stats = _numstat(git_dir, merge)
            except RuntimeError as exc:
                entry["excluded"] = f"merge commit unavailable ({exc})"
                continue
            source = [(a, d, n) for a, d, n in stats if n.startswith("src/") and n.endswith(".py")]
            tests = [n for _, _, n in stats if n.startswith("testing/") and n.endswith(".py")]
            other = [n for _, _, n in stats if not n.startswith(ALLOWED_PREFIXES) and n not in ALLOWED_FILES]
            entry.update(source_files=[n for _, _, n in source], test_files=tests,
                         source_lines=sum(a + d for a, d, _ in source), other_files=other)
            if other:
                entry["excluded"] = f"changes files outside src/testing/changelog/doc/AUTHORS ({', '.join(other[:3])})"
            elif not source:
                entry["excluded"] = "no source change under src/"
            elif len(source) > MAX_SOURCE_FILES:
                entry["excluded"] = f"{len(source)} source files (limit {MAX_SOURCE_FILES})"
            elif entry["source_lines"] > MAX_SOURCE_LINES:
                entry["excluded"] = f"{entry['source_lines']} changed source lines (limit {MAX_SOURCE_LINES})"
            elif not tests:
                entry["excluded"] = "no test change under testing/"
            if issue[0] in duplicates:
                entry["marked_duplicate"] = duplicates[issue[0]]
        # Rule 5: one task per issue, earliest pull request wins.
        seen = {}
        for entry in candidates:
            if entry["excluded"] or "issue" not in entry:
                continue
            if entry["issue"] in seen:
                entry["excluded"] = f"issue already covered by PR #{seen[entry['issue']]}"
            else:
                seen[entry["issue"]] = entry["pr"]
        return candidates
    finally:
        db.close()


def _related(tasks: list[dict]) -> list[dict]:
    """Pairs of selected tasks that touch the same source file: reported, not hidden."""
    pairs = []
    for i, first in enumerate(tasks):
        for second in tasks[i + 1:]:
            shared = sorted(set(first["source_files"]) & set(second["source_files"]))
            if shared:
                pairs.append({"tasks": [first["id"], second["id"]], "splits": [first["split"], second["split"]],
                              "shared_files": shared})
    return pairs


def assign_splits(tasks: list[dict], heldout_target: int) -> None:
    """Newest tasks are held out; the boundary keeps every search fix older than every held-out snapshot."""
    ordered = sorted(tasks, key=lambda t: t["as_of"])
    fixed_heldout = [t for t in ordered if EXPOSED.get(t["id"]) == "heldout"]
    fixed_search = [t for t in ordered if EXPOSED.get(t["id"]) == "search"]
    best = None
    for cut in range(1, len(ordered)):
        search, heldout = ordered[:cut], ordered[cut:]
        if any(t in heldout for t in fixed_search) or any(t in search for t in fixed_heldout):
            continue
        # Chronological separation: every search fix merged before the earliest held-out snapshot.
        if max(t["merged_at"] for t in search) >= min(t["as_of"] for t in heldout):
            continue
        gap = abs(len(heldout) - heldout_target)
        if best is None or gap < best[0]:
            best = (gap, cut)
    if best is None:
        raise RuntimeError("No split boundary keeps the fixed assignments and chronological separation.")
    cut = best[1]
    for task in ordered[cut:]:
        task["split"] = "heldout"
    for index, task in enumerate(ordered[:cut]):
        task["split"] = ("train", "validation")[index % 2]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-dir", required=True, help="Where to build and verify the task bundles.")
    parser.add_argument("--heldout", type=int, default=8, help="Target number of held-out tasks.")
    parser.add_argument("--discover-only", action="store_true")
    args = parser.parse_args()

    candidates = discover()
    eligible = [c for c in candidates if not c["excluded"]]
    print(f"{len(candidates)} merged PRs close an issue; {len(eligible)} pass criteria 1-3 and 5.")
    if args.discover_only:
        for c in candidates:
            print(c["pr"], c.get("issue"), c["excluded"] or "eligible", c.get("title", c["pr_title"])[:70])
        return

    specs = [{"id": f"pytest-{c['issue']}", "issue": c["issue"], "pr": c["pr"], "split": "pending",
              "title": c["title"]} for c in eligible]
    bundles = TaskBundles(Path(args.data_dir), specs=specs)
    verified = []
    for c, spec in zip(eligible, specs):
        try:
            task = bundles.load(spec["id"])
            saved = bundles.root / spec["id"] / "verify.json"
            check = json.loads(saved.read_text(encoding="utf-8")) if saved.exists() else None
            if not check or check.get("version") != task["version"]:
                check = {**bundles.verify(spec["id"]), "version": task["version"]}
                saved.write_text(json.dumps(check, indent=1), encoding="utf-8")
        except Exception as exc:  # recorded as an exclusion reason
            c["excluded"] = f"reference verification failed: {str(exc)[:200]}"
            print(f"  excluded {spec['id']}: {c['excluded']}", flush=True)
            continue
        hidden = bundles.hidden_checks(spec["id"])
        c["verification"] = {"fail_to_pass": len(hidden["fail_to_pass"]),
                             "pass_to_pass": len(hidden["pass_to_pass"]),
                             "unchanged": check["unchanged"], "reference": check["reference"]}
        if not check["ok"]:
            c["excluded"] = "scoring check failed: the reference fix is not resolved, or the unchanged snapshot is"
            print(f"  excluded {spec['id']}: {c['excluded']} {check}", flush=True)
            continue
        statement = task["statement"]
        c["statement_chars"] = len(statement)
        c["comments_before_as_of"] = statement.count("\n## Comment by ")
        verified.append({**spec, "as_of": task["as_of"], "merged_at": c["merged_at"],
                         "source_files": task["source_files"], "base_commit": task["base_commit"],
                         "fix_commit": task["fix_commit"]})
        print(f"  verified {spec['id']}: {c['verification']}", flush=True)

    assign_splits(verified, args.heldout)
    verified.sort(key=lambda t: t["as_of"])
    catalog = {
        "description": "Real pytest issues fixed by small merged pull requests, selected by "
                       "src/evo/evolve/select_tasks.py (criteria in its docstring). Held-out tasks are the "
                       "newest, and every reflection/validation fix predates the earliest held-out snapshot. "
                       "Held-out results are computed once per experiment and never shown to the optimizer.",
        "repository": "pytest-dev/pytest",
        "criteria_version": CRITERIA_VERSION,
        "tasks": [{"id": t["id"], "issue": t["issue"], "pr": t["pr"], "split": t["split"], "title": t["title"],
                   "as_of": t["as_of"]} for t in verified],
    }
    CATALOG.write_text(json.dumps(catalog, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    report = {
        "criteria_version": CRITERIA_VERSION, "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mirror": {"window_start": "2026-04-01", "source": "capstone replay mirror (history.db + repo.git)"},
        "counts": {"candidates": len(candidates), "verified": len(verified),
                   "splits": dict(Counter(t["split"] for t in verified))},
        "boundary": {"latest_search_fix_merged": max(t["merged_at"] for t in verified if t["split"] != "heldout"),
                     "earliest_heldout_snapshot": min(t["as_of"] for t in verified if t["split"] == "heldout")},
        "related": _related(verified),
        "candidates": candidates,
    }
    SELECTION.write_text(json.dumps(report, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(report["counts"]), json.dumps(report["boundary"]))


if __name__ == "__main__":
    main()
