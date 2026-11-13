"""Reproducible evaluation tasks rebuilt from a historical replay of a real repository.

A task is a real issue that was later fixed by a merged pull request. The bundle contains:

* `snapshot/`: the repository tree at the last main-branch commit before the fix was proposed,
  committed as a single history-free commit (no future objects), plus the issue text and comments
  that existed then. This is all the agent sees.
* `hidden/`: the test files from the fix and the test IDs that must go from failing to passing
  (FAIL_TO_PASS) or keep passing (PASS_TO_PASS). Only the evaluator reads these.

Task data comes from the replay mirror (`history.db` + `repo.git`) produced by the capstone project.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
import xml.etree.ElementTree as ElementTree
from datetime import datetime, timedelta
from pathlib import Path
from threading import Lock

CATALOG = Path(__file__).with_name("pytest_tasks.json")
VERSION_FILE = 'version = __version__ = "9.9.9.dev0"\nversion_tuple = __version_tuple__ = (9, 9, 9, "dev0")\n'
CHECK_TIMEOUT = 600
_BUILD_LOCK = Lock()


def replay_dir() -> Path | None:
    for candidate in (os.getenv("EVO_REPLAY_DATA"), "D:/capstone/data/pytest"):
        if candidate and (Path(candidate) / "history.db").exists() and (Path(candidate) / "repo.git").exists():
            return Path(candidate)
    return None


def task_python() -> str | None:
    """An interpreter with pytest's runtime dependencies (pytest itself is imported from the snapshot)."""
    candidates = [os.getenv("EVO_REPLAY_PYTHON"), "D:/evo-demos/pytest-env/Scripts/python.exe"]
    if replay := replay_dir():
        candidates.append(str(replay / "venv" / "Scripts" / "python.exe"))
        candidates.append(str(replay / "venv" / "bin" / "python"))
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return str(Path(candidate))
    return None


def catalog() -> list[dict]:
    return json.loads(CATALOG.read_text(encoding="utf-8"))["tasks"]


def _git(git_dir: Path | None, *args: str, cwd: Path | None = None, input_bytes: bytes | None = None,
         check: bool = True) -> bytes:
    command = ["git", *(["--git-dir", str(git_dir)] if git_dir else []), *args]
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    result = subprocess.run(command, cwd=cwd, input=input_bytes, capture_output=True, env=env, timeout=300)
    if check and result.returncode:
        raise RuntimeError(result.stderr.decode("utf-8", "replace").strip() or "git failed")
    return result.stdout


def _iso_minus_second(stamp: str) -> str:
    moment = datetime.fromisoformat(stamp.replace("Z", "+00:00")) - timedelta(seconds=1)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def issue_statement(db: sqlite3.Connection, issue: int, as_of: str) -> str:
    """The issue as it could be read at `as_of`: title, body and comments created by then."""
    row = db.execute("SELECT title, body, created_at FROM items WHERE number=? AND created_at<=?",
                     (issue, as_of)).fetchone()
    if not row:
        raise ValueError(f"Issue {issue} did not exist at {as_of}.")
    parts = [f"# Issue #{issue}: {row[0]}", f"Opened {row[2]}", "", (row[1] or "").strip()]
    comments = db.execute("SELECT author, body, created_at FROM comments WHERE number=? AND created_at<=? "
                          "ORDER BY created_at", (issue, as_of)).fetchall()
    for author, body, created in comments:
        parts += ["", f"## Comment by {author} ({created})", "", (body or "").strip()]
    return "\n".join(parts).strip() + "\n"


AGENTS_MD = """# Working in this snapshot

This is a snapshot of the pytest repository. The issue to fix is in `ISSUE.md`.

* Source is in `src/`, tests are in `testing/`.
* Run tests with `python run_checks.py testing/<file>.py` (it uses an interpreter with pytest's
  dependencies and imports pytest from `src/`). Pass any pytest arguments, for example `-k name -q`.
* Make the smallest change that fixes the issue, and add or update a regression test.
* Commit your change with the repository tools when you are done.
"""

RUN_CHECKS = """\"\"\"Run pytest's own tests against this checkout: python run_checks.py testing/test_x.py -q\"\"\"
import os
import subprocess
import sys
from pathlib import Path

PYTHON = {python!r}
root = Path(__file__).resolve().parent
env = dict(os.environ, PYTHONPATH=str(root / "src"), PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
command = [PYTHON, "-m", "pytest", "-p", "no:cacheprovider", *(sys.argv[1:] or ["testing", "-q", "-x"])]
sys.exit(subprocess.run(command, cwd=root, env=env).returncode)
"""


def _extract_tree(git_dir: Path, commit: str, destination: Path) -> None:
    archive = _git(git_dir, "archive", "--format=tar", commit)
    with tempfile.TemporaryFile() as handle:
        handle.write(archive)
        handle.seek(0)
        with tarfile.open(fileobj=handle) as tar:
            tar.extractall(destination, filter="data")


def run_pytest(python: str, workdir: Path, test_files: list[str], timeout: int = CHECK_TIMEOUT) -> dict:
    """Run test files and return {test_id: outcome}, outcome in passed/failed/error/skipped."""
    report = workdir / ".evo-junit.xml"
    report.unlink(missing_ok=True)
    env = {key: value for key, value in os.environ.items() if not key.startswith("PYTEST")}
    env.update(PYTHONPATH=str(workdir / "src"), PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", PYTHONDONTWRITEBYTECODE="1")
    command = [python, "-m", "pytest", "-p", "no:cacheprovider", "-q", "-o", "addopts=", "-p", "pytester",
               f"--junitxml={report}", *test_files]
    try:
        completed = subprocess.run(command, cwd=workdir, env=env, capture_output=True, timeout=timeout)
        tail = (completed.stdout + completed.stderr).decode("utf-8", "replace")[-3000:]
    except subprocess.TimeoutExpired:
        return {"timed_out": True, "outcomes": {}, "output": f"Timed out after {timeout}s."}
    outcomes = {}
    if report.exists():
        for case in ElementTree.parse(report).getroot().iter("testcase"):
            test_id = f"{case.get('classname')}::{case.get('name')}"
            children = {child.tag for child in case}
            outcomes[test_id] = ("failed" if "failure" in children else "error" if "error" in children
                                 else "skipped" if "skipped" in children else "passed")
        report.unlink(missing_ok=True)
    return {"timed_out": False, "outcomes": outcomes, "output": tail, "exit_code": completed.returncode}


def score_checks(checks: dict, outcomes: dict) -> dict:
    f2p, p2p = checks["fail_to_pass"], checks["pass_to_pass"]
    f2p_passed = sum(outcomes.get(test) == "passed" for test in f2p)
    p2p_passed = sum(outcomes.get(test) == "passed" for test in p2p)
    f2p_rate = f2p_passed / len(f2p) if f2p else 0.0
    p2p_rate = p2p_passed / len(p2p) if p2p else 1.0
    return {
        "fail_to_pass": [f2p_passed, len(f2p)], "pass_to_pass": [p2p_passed, len(p2p)],
        "score": round(f2p_rate * p2p_rate, 4),
        "resolved": bool(f2p) and f2p_passed == len(f2p) and p2p_passed == len(p2p),
        "failing": [test for test in f2p if outcomes.get(test) != "passed"][:20],
        "broken": [test for test in p2p if outcomes.get(test) != "passed"][:20],
    }


class TaskBundles:
    """Builds task bundles once into the EVO data directory and reuses them."""

    def __init__(self, data_dir: Path, specs: list[dict] | None = None):
        self.root = Path(data_dir) / "evolution" / "tasks"
        self.specs = specs  # defaults to the shipped catalog

    def _specs(self) -> list[dict]:
        return self.specs if self.specs is not None else catalog()

    def list(self) -> list[dict]:
        tasks = []
        for spec in self._specs():
            meta = self.root / spec["id"] / "task.json"
            built = json.loads(meta.read_text(encoding="utf-8")) if meta.exists() else None
            tasks.append({**spec, "built": bool(built and built.get("valid")),
                          "error": built.get("error") if built else None,
                          "checks": {k: len(v) for k, v in built["checks"].items()} if built and built.get("valid")
                          else None})
        return tasks

    def spec(self, task_id: str) -> dict:
        spec = next((task for task in self._specs() if task["id"] == task_id), None)
        if not spec:
            raise KeyError(f"No evaluation task {task_id!r}.")
        return spec

    def load(self, task_id: str) -> dict:
        """Return the built bundle, building it first if needed (slow: runs the tests twice)."""
        with _BUILD_LOCK:
            meta = self.root / task_id / "task.json"
            if not meta.exists():
                self._build(self.spec(task_id))
            task = json.loads(meta.read_text(encoding="utf-8"))
        if not task.get("valid"):
            raise RuntimeError(f"Task {task_id} is unusable: {task.get('error')}")
        # Identifies this exact snapshot and set of hidden checks, so cached rollouts expire if either changes.
        hidden = (self.root / task_id / "hidden" / "checks.json").read_bytes()
        identity = json.dumps([task["base_commit"], task["fix_commit"], task["statement"], task["checks"]],
                              sort_keys=True).encode() + hidden
        version = hashlib.sha256(identity).hexdigest()[:16]
        return task | {"dir": str(self.root / task_id), "version": version}

    def hidden_checks(self, task_id: str) -> dict:
        return json.loads((self.root / task_id / "hidden" / "checks.json").read_text(encoding="utf-8"))

    def verify(self, task_id: str) -> dict:
        """Score the untouched snapshot and the reference fix through the same path rollouts use
        (fresh clone, hidden tests overlaid, junit outcomes, score_checks). A usable task scores 0
        unchanged and is resolved by the reference fix."""
        task, hidden = self.load(task_id), self.hidden_checks(task_id)
        replay, python = replay_dir(), task_python()
        patch = _git(replay / "repo.git", "diff", "--binary", f"{task['fix_commit']}^1", task["fix_commit"])
        results = {}
        with tempfile.TemporaryDirectory(prefix=f"verify-{task_id}-") as scratch:
            for arm in ("unchanged", "reference"):
                work = Path(scratch) / arm
                _git(None, "clone", "-q", str(Path(task["dir"]) / "snapshot"), str(work))
                if arm == "reference":
                    (Path(scratch) / "fix.patch").write_bytes(patch)
                    _git(None, "apply", "--include=src/*", str(Path(scratch) / "fix.patch"), cwd=work)
                for name in hidden["test_files"]:
                    target = work / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(Path(task["dir"]) / "hidden" / name, target)
                checks = run_pytest(python, work, hidden["test_files"])
                results[arm] = ({"timed_out": True} if checks.get("timed_out") else
                                {k: v for k, v in score_checks(hidden, checks["outcomes"]).items()
                                 if k in {"score", "resolved", "fail_to_pass", "pass_to_pass"}})
            # Read-only git objects in the clones would otherwise stop the cleanup on Windows.
            for arm in ("unchanged", "reference"):
                shutil.rmtree(Path(scratch) / arm, onexc=_force_remove)
        results["ok"] = (results["reference"].get("resolved") is True
                         and results["unchanged"].get("resolved") is False
                         and results["unchanged"].get("score", 1) < 1)
        return results

    def _build(self, spec: dict) -> None:
        replay, python = replay_dir(), task_python()
        if not replay or not python:
            raise RuntimeError("Evaluation tasks need the replay mirror (EVO_REPLAY_DATA) and a Python with "
                               "pytest's dependencies (EVO_REPLAY_PYTHON).")
        git_dir = replay / "repo.git"
        db = sqlite3.connect(f"file:{replay / 'history.db'}?mode=ro", uri=True)
        target = self.root / spec["id"]
        self.root.mkdir(parents=True, exist_ok=True)
        # Stage next to the destination so the final move is a rename, not a copy.
        staging = Path(tempfile.mkdtemp(prefix=f".staging-{spec['id']}-", dir=self.root))
        try:
            pr = db.execute("SELECT i.created_at, p.merge_commit_sha FROM items i JOIN pulls p ON p.number=i.number "
                            "WHERE i.number=?", (spec["pr"],)).fetchone()
            if not pr or not pr[1]:
                raise ValueError(f"Pull request {spec['pr']} has no merge commit in the mirror.")
            as_of = _iso_minus_second(pr[0])
            merge = pr[1]
            base = _git(git_dir, "rev-list", "-n1", "--first-parent", f"--before={as_of}", "main").decode().strip()
            patch = _git(git_dir, "diff", "--binary", f"{merge}^1", merge)
            files = [line.split("\t")[-1] for line in
                     _git(git_dir, "diff", "--name-only", f"{merge}^1", merge).decode().splitlines()]
            files = [name.strip() for name in files if name.strip()]
            test_files = [f for f in files if f.startswith("testing/") and f.endswith(".py")]
            source_files = [f for f in files if f.startswith("src/")]
            statement = issue_statement(db, spec["issue"], as_of)

            snapshot = staging / "snapshot"
            snapshot.mkdir()
            _extract_tree(git_dir, base, snapshot)
            (snapshot / "src" / "_pytest" / "_version.py").write_text(VERSION_FILE, encoding="utf-8")
            (snapshot / "ISSUE.md").write_text(statement, encoding="utf-8")
            (snapshot / "AGENTS.md").write_text(AGENTS_MD, encoding="utf-8")
            (snapshot / "run_checks.py").write_text(RUN_CHECKS.format(python=python), encoding="utf-8")

            # Reference trees: base + the fix's tests (should fail), base + the whole fix (should pass).
            work = staging / "work"
            shutil.copytree(snapshot, work)
            _git(None, "init", "-q", cwd=work)
            patch_file = staging / "fix.patch"
            patch_file.write_bytes(patch)
            _git(None, "apply", "--include=testing/*", str(patch_file), cwd=work)
            before = run_pytest(python, work, test_files)
            hidden = staging / "hidden"
            for name in test_files:
                (hidden / name).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(work / name, hidden / name)
            _git(None, "apply", "--include=src/*", str(patch_file), cwd=work)
            after = run_pytest(python, work, test_files)
            if before.get("timed_out") or after.get("timed_out"):
                raise RuntimeError("Reference tests timed out.")
            f2p = sorted(t for t, o in after["outcomes"].items() if o == "passed"
                         and before["outcomes"].get(t) != "passed")
            p2p = sorted(t for t, o in after["outcomes"].items() if o == "passed"
                         and before["outcomes"].get(t) == "passed")
            checks = {"fail_to_pass": f2p, "pass_to_pass": p2p, "test_files": test_files}
            (hidden / "checks.json").write_text(json.dumps(checks, indent=1), encoding="utf-8")

            # The agent's snapshot: one commit, no history, no future objects.
            _git(None, "init", "-q", "-b", "main", cwd=snapshot)
            _git(None, "add", "--all", cwd=snapshot)
            # Normally generated at install time and ignored by .gitignore; the snapshot needs it.
            _git(None, "add", "--force", "src/_pytest/_version.py", cwd=snapshot)
            _git(None, "-c", "user.name=EVO evaluation", "-c", "user.email=evaluation@localhost", "commit", "-q",
                 "-m", f"Snapshot before the fix for issue #{spec['issue']}", cwd=snapshot)
            valid = bool(f2p)
            meta = {
                **spec, "as_of": as_of, "base_commit": base, "fix_commit": merge,
                "statement": statement, "source_files": source_files, "test_files": test_files,
                "checks": {"fail_to_pass": f2p, "pass_to_pass": p2p}, "valid": valid,
                "error": None if valid else "The fix's tests do not fail before the fix.",
                "built_at": datetime.now().isoformat(timespec="seconds"),
            }
            if target.exists():
                shutil.rmtree(target, onexc=_force_remove)
            target.mkdir(parents=True)
            shutil.move(str(snapshot), target / "snapshot")
            shutil.move(str(hidden), target / "hidden")
            (target / "task.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
        finally:
            db.close()
            shutil.rmtree(staging, onexc=_force_remove)


def _force_remove(function, path, _error):
    os.chmod(path, 0o700)
    function(path)
