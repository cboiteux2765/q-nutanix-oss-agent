"""Trusted local Git mirror/PR registry. No network or repository discovery.

Operators register immutable approved specifications and trusted test profiles.
Webhook payloads cannot add repositories, commands, snapshots, or credentials.
"""
from __future__ import annotations

import io
import json
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import tarfile
from typing import Any
from .models import (Contract, SpecDocument, TestProfile, EnvironmentManifest,
                     CreateRunRequest, ResolvedContext, canonical_hash)


def git(root: Path, *args: str, timeout_seconds: float = 30) -> str:
    return subprocess.run(["git", "-c", "core.longpaths=true", "-c", "commit.gpgsign=false", "-C", str(root), *args], check=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          timeout=max(.01, timeout_seconds), encoding="utf-8").stdout.strip()


def tree_hash(root: Path) -> str:
    records = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if any(p in {".git", "__pycache__", ".pytest_cache"} for p in relative.parts):
            continue
        if path.is_symlink():
            raise ValueError("Symlink in immutable source snapshot")
        if path.is_file():
            from hashlib import sha256
            records.append((relative.as_posix(), sha256(path.read_bytes()).hexdigest()))
    return canonical_hash(records)


class PRRegistration(Contract):
    repo: str
    pr_number: int
    repo_root: Path
    head_ref: str
    base_sha: str
    spec: SpecDocument
    environment: EnvironmentManifest
    profile: TestProfile
    allowed_patch_paths: tuple[str, ...] = ("src/service.py",)
    policy_version: str = "pilot-v1"
    prompt_version: str = "frozen-v1"


class SourceRegistry:
    def __init__(self, storage: Path):
        self.storage = Path(storage).resolve()
        self.storage.mkdir(parents=True, exist_ok=True)
        self.registrations: dict[tuple[str, int], PRRegistration] = {}
        path = self.storage / "registry.json"
        if path.exists():
            for row in json.loads(path.read_text(encoding="utf-8")):
                entry = PRRegistration.model_validate(row)
                self.registrations[(entry.repo, entry.pr_number)] = entry

    def register(self, entry: PRRegistration) -> None:
        # This method is intentionally operator-only; there is no HTTP endpoint.
        if not entry.head_ref.startswith("refs/heads/"):
            raise ValueError("A registered local branch ref is required")
        git(entry.repo_root, "rev-parse", "--verify", entry.head_ref + "^{commit}")
        git(entry.repo_root, "rev-parse", "--verify", entry.base_sha + "^{commit}")
        self.registrations[(entry.repo, entry.pr_number)] = entry
        self.persist()

    def persist(self) -> None:
        path = self.storage / "registry.json"
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps([v.model_dump(mode="json") for v in self.registrations.values()], indent=2), encoding="utf-8")
        temp.replace(path)

    def request_for_pr(self, repo: str, pr_number: int) -> CreateRunRequest:
        item = self.registrations[(repo, pr_number)]
        return CreateRunRequest(repo=repo, pr_number=pr_number, spec_id=item.spec.spec_id,
            spec_uri=f"registry://{repo}/specs/{item.spec.spec_id}", spec_hash=canonical_hash(item.spec),
            test_profile_id=item.profile.id, environment_manifest_id=item.environment.id,
            correlation_id=f"pr-{pr_number}")

    def _snapshot(self, entry: PRRegistration, sha: str) -> Path:
        root = self.storage / "snapshots" / entry.repo.replace("/", "__") / sha
        marker = root.with_suffix(".sha256")
        if root.exists():
            if not marker.exists() or tree_hash(root) != marker.read_text().strip():
                raise ValueError("Immutable source snapshot was modified")
            return root
        raw = subprocess.run(["git", "-C", str(entry.repo_root), "archive", "--format=tar", sha],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30).stdout
        if len(raw) > 20 * 1024 * 1024:
            raise ValueError("Prototype snapshot exceeds 20 MiB limit")
        root.mkdir(parents=True)
        with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
            for member in archive:
                p = PurePosixPath(member.name)
                if (p.is_absolute() or ".." in p.parts or "\\" in member.name or
                        ":" in member.name or not (member.isfile() or member.isdir())):
                    raise ValueError("Unsupported archive path or link")
                target = root.joinpath(*p.parts)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(archive.extractfile(member).read())
        marker.write_text(tree_hash(root), encoding="ascii")
        return root

    def resolve(self, request: CreateRunRequest) -> ResolvedContext:
        entry = self.registrations[(request.repo, request.pr_number)]
        current = self.request_for_pr(request.repo, request.pr_number)
        for name in ("spec_id", "spec_uri", "spec_hash", "test_profile_id", "environment_manifest_id"):
            if getattr(current, name) != getattr(request, name):
                raise ValueError(f"Registered approved input mismatch: {name}")
        head = git(entry.repo_root, "rev-parse", "--verify", entry.head_ref + "^{commit}")
        base = git(entry.repo_root, "rev-parse", "--verify", entry.base_sha + "^{commit}")
        return ResolvedContext(repo=entry.repo, pr_number=entry.pr_number, base_sha=base,
            head_sha=head, snapshot_root=self._snapshot(entry, head),
            base_snapshot_root=self._snapshot(entry, base), spec=entry.spec,
            spec_uri=current.spec_uri, spec_hash=current.spec_hash, profile=entry.profile,
            environment=entry.environment, allowed_patch_paths=entry.allowed_patch_paths,
            policy_version=entry.policy_version, prompt_version=entry.prompt_version)

    def diff(self, context: ResolvedContext) -> str:
        entry = self.registrations[(context.repo, context.pr_number)]
        return git(entry.repo_root, "diff", context.base_sha, context.head_sha, "--")[:100_000]


def make_demo_repository(root: Path, *, regression: bool = False, missing_env: bool = False,
                         ambiguous: bool = False) -> tuple[Path, PRRegistration]:
    """Create real local commits for a tiny, explicitly trusted demonstration."""
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=False)
    (root / "src").mkdir(); (root / "tests").mkdir()
    (root / "src" / "__init__.py").write_text("", encoding="utf-8")
    (root / "src" / "service.py").write_text(GOOD_SOURCE, encoding="utf-8")
    (root / "tests" / "test_service.py").write_text(TEST_SOURCE, encoding="utf-8")
    git(root, "init", "--initial-branch=main")
    git(root, "config", "user.name", "EVO Offline Demo")
    git(root, "config", "user.email", "demo@localhost")
    git(root, "config", "core.hooksPath", str(root / "disabled-hooks"))
    git(root, "add", "."); git(root, "commit", "-m", "Approved baseline")
    base = git(root, "rev-parse", "HEAD")
    git(root, "checkout", "-b", "pr-42")
    (root / "src" / "service.py").write_text(BAD_SOURCE if regression else GOOD_SOURCE + "\n# PR implementation\n", encoding="utf-8")
    git(root, "add", "."); git(root, "commit", "-m", "Implementation PR")
    from .models import Requirement, AcceptanceTest, Prerequisite
    spec = SpecDocument(spec_id="SPEC-42", intent="Summarize user values safely, including empty input.",
        requirements=(Requirement(id="REQ-1", description="Empty input returns zero; otherwise sum the input.",
                                  test_ids=("TEST-EMPTY", "TEST-VALUES")),),
        acceptance_tests=(AcceptanceTest(id="TEST-EMPTY", node_id="tests/test_service.py::test_empty",
                                         expected_behavior="summarize([]) == 0"),
                          AcceptanceTest(id="TEST-VALUES", node_id="tests/test_service.py::test_values",
                                         expected_behavior="summarize([2, 3]) == 5")),
        conflicts=("REQ-1 says empty input returns 0; proposed REQ-2 says it raises ValueError. Which is approved?",) if ambiguous else ())
    entry = PRRegistration(repo="org/service", pr_number=42, repo_root=root,
        head_ref="refs/heads/pr-42", base_sha=base, spec=spec,
        profile=TestProfile(trusted=True, backend="local_trusted"),
        environment=EnvironmentManifest(id="sandbox-v1", prerequisites=(
            Prerequisite(name="demo-sandbox-service", available=not missing_env),)))
    return root, entry


GOOD_SOURCE = "def summarize(values):\n    return sum(values)\n"
BAD_SOURCE = "def summarize(values):\n    first = values[0]\n    return first + sum(values[1:])\n"
TEST_SOURCE = "from src.service import summarize\n\ndef test_empty():\n    assert summarize([]) == 0\n\ndef test_values():\n    assert summarize([2, 3]) == 5\n"
