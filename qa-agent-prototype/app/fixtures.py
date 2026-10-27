"""Build small local Git repositories and Test Agent handoffs for the demo and tests.

Everything here is fake on purpose. Secret-shaped strings are assembled at
runtime so this source file itself never contains one.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

BASE_FILES = {
    "README.md": "# calc\n\nA tiny calculator library used as a QA fixture.\n",
    "CHANGELOG.md": "# Changelog\n\n## 0.1.0\n- First release\n",
    "requirements.txt": "requests==2.32.3\n",
    "src/calc/__init__.py": "",
    "src/calc/util.py": "def add(a, b):\n    return a + b\n",
    "tests/test_util.py": "from calc.util import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n",
    ".github/workflows/ci.yml": "name: ci\non: [pull_request]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - run: pytest -q\n",
}


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True)
    return proc.stdout.strip()


def write_files(repo: Path, files: dict[str, str | None]) -> None:
    for path, text in files.items():
        target = repo / path
        if text is None:
            target.unlink(missing_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")


def commit(repo: Path, message: str) -> str:
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=EVO Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def make_repo(root: Path, extra_base: dict[str, str | None] | None = None) -> tuple[Path, str]:
    """Create a repo on branch main with the base files. Returns (path, base_sha)."""
    repo = root / "repo"
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    write_files(repo, {**BASE_FILES, **(extra_base or {})})
    return repo, commit(repo, "base")


def open_pr(repo: Path, changes: dict[str, str | None], message: str = "PR change", branch: str = "pr") -> str:
    """Create the PR branch off main with the given file changes. Returns head SHA."""
    git(repo, "checkout", "-q", "-b", branch, "main")
    write_files(repo, changes)
    sha = commit(repo, message)
    git(repo, "checkout", "-q", "main")
    return sha


def push_to_pr(repo: Path, changes: dict[str, str | None], branch: str = "pr") -> str:
    git(repo, "checkout", "-q", branch)
    write_files(repo, changes)
    sha = commit(repo, "follow-up commit")
    git(repo, "checkout", "-q", "main")
    return sha


def _h(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def write_verification(
    path: Path, base_sha: str, head_sha: str, readiness: str = "READY_FOR_QA", run_id: str = "run-demo-1"
) -> Path:
    """A handoff shaped like the Test Agent's verification.json (schema v1.0)."""
    data = {
        "schema_version": "1.0",
        "run_id": run_id,
        "repo": "evo-fixtures/calc",
        "pr_number": 7,
        "base_sha": base_sha,
        "reviewed_head_sha": head_sha,
        "spec_id": "SPEC-CALC-1",
        "spec_hash": _h("spec"),
        "test_profile_hash": _h("profile"),
        "environment_fingerprint": _h("env"),
        "policy_version": "test-policy-1",
        "identity_hash": _h(base_sha + head_sha),
        "execution_status": "COMPLETED",
        "readiness": readiness,
        "reasons": [],
        "tests": {"total": 2, "passed": 2, "failed": 0, "errors": 0, "skipped": 0},
        "requirements": [],
        "unresolved_findings": [],
        "artifact_manifest_uri": f"artifacts/{run_id}/evidence_manifest.json",
        "human_approval_required": True,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return path


def fake_aws_key() -> str:
    return "AK" + "IA" + "Z3QXDEMO4KEY77AB"


# File changes for each demo scenario.
CLEAN_CHANGE = {
    "src/calc/util.py": "def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n",
    "tests/test_util.py": BASE_FILES["tests/test_util.py"]
    + "\n\ndef test_sub():\n    from calc.util import sub\n\n    assert sub(5, 3) == 2\n",
    "CHANGELOG.md": BASE_FILES["CHANGELOG.md"] + "\n## Unreleased\n- Add sub()\n",
}

SHELL_INJECTION_CHANGE = {
    "src/calc/runner.py": (
        "import subprocess\n\n\n"
        "def evaluate(expression):\n"
        "    # BAD: user input goes straight into a shell\n"
        "    return subprocess.run(f\"python -c 'print({expression})'\", shell=True, capture_output=True)\n"
    ),
}


def secret_change() -> dict[str, str]:
    return {"src/calc/config.py": f'AWS_ACCESS_KEY_ID = "{fake_aws_key()}"\nREGION = "us-west-2"\n'}


VULNERABLE_DEP_CHANGE = {"requirements.txt": "requests==2.32.3\nevo-demo-yamlish==1.0.1\n"}

HIGH_RISK_CHANGE = {
    ".github/workflows/ci.yml": BASE_FILES[".github/workflows/ci.yml"] + "      - run: echo deploy\n",
}
