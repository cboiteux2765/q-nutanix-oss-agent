"""Deterministic validation before a test process is allowed to start.

No model is consulted here. A failed preflight is a blocker, never a test pass.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
import re
import shutil
import stat


@dataclass(frozen=True)
class PreflightFinding:
    code: str
    message: str
    owner_role: str
    remediation: str
    requirement_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class PreflightResult:
    ok: bool
    findings: list[PreflightFinding] = field(default_factory=list)


def value(obj, name: str, default=None):
    """Read validated models, dataclasses, or test fixture dictionaries."""
    return obj.get(name, default) if isinstance(obj, dict) else getattr(obj, name, default)


def validate_test_path(node_id: str) -> str:
    """Accept only a relative pytest collection path, never command-line flags."""
    if not isinstance(node_id, str) or not node_id or len(node_id) > 500:
        raise ValueError("A test target must be a nonempty relative pytest path.")
    if any(c in node_id for c in "\x00\r\n") or node_id.startswith("-"):
        raise ValueError("Test targets cannot contain options or control characters.")
    normalized = node_id.replace("\\", "/")
    path_part = normalized.split("::", 1)[0]
    path = PurePosixPath(path_part)
    if (path.is_absolute() or re.match(r"^[A-Za-z]:", path_part)
            or any(p in ("", ".", "..") for p in path_part.split("/"))
            or any(p.startswith("-") for p in path.parts)):
        raise ValueError("Test targets must stay inside the snapshot.")
    if not re.fullmatch(r"[\w./:\[\],=+() @-]+", normalized, flags=re.ASCII):
        raise ValueError("Test target contains unsupported characters.")
    return normalized


def target_covers(target: str, node_id: str) -> bool:
    target, node_id = validate_test_path(target), validate_test_path(node_id)
    return (node_id == target or node_id.startswith(target + "::")
            or ("::" not in target and not target.endswith(".py")
                and node_id.startswith(target.rstrip("/") + "/")))


def validate_snapshot(root: Path) -> None:
    """Reject links, junctions, special files, and paths escaping the snapshot."""
    root = Path(root)
    if root.is_symlink() or (hasattr(root, "is_junction") and root.is_junction()):
        raise ValueError("Snapshot root must not be a symlink or junction.")
    resolved = root.resolve(strict=True)
    if not resolved.is_dir():
        raise ValueError("Snapshot must be a directory.")
    for path in resolved.rglob("*"):
        if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
            raise ValueError(f"Snapshot contains a link: {path.relative_to(resolved)}")
        if not path.resolve(strict=True).is_relative_to(resolved):
            raise ValueError("Snapshot path escapes its root.")
        mode = path.stat(follow_symlinks=False).st_mode
        if not stat.S_ISREG(mode) and not stat.S_ISDIR(mode):
            raise ValueError("Snapshot contains a nonregular file.")


def prepare_workspace(snapshot_root: Path, dest: Path) -> Path:
    """Copy an immutable snapshot into a new, disposable execution workspace."""
    snapshot_root, dest = Path(snapshot_root), Path(dest)
    validate_snapshot(snapshot_root)
    if dest.resolve().is_relative_to(snapshot_root.resolve()):
        raise ValueError("Execution workspace must be outside the source snapshot.")
    if dest.exists():
        raise ValueError("Execution workspace must be new; refusing to reuse it.")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(snapshot_root.resolve(), dest, symlinks=False)
    validate_snapshot(dest)
    return dest.resolve()


def preflight(context) -> PreflightResult:
    findings: list[PreflightFinding] = []

    def add(code, message, owner, remediation, ids=None):
        findings.append(PreflightFinding(code, message, owner, remediation, ids or []))

    # Revalidate models to catch accidental in-memory mutation after resolution.
    if hasattr(context, "model_dump") and hasattr(type(context), "model_validate"):
        try:
            type(context).model_validate(context.model_dump())
        except (TypeError, ValueError) as exc:
            add("INPUT_INVALID", f"Resolved contract is invalid: {exc}", "request_owner",
                "Submit a request matching the versioned input schema.")

    spec = value(context, "spec")
    if spec is None:
        add("SPEC_AMBIGUOUS", "Approved specification is missing.", "spec_owner",
            "Provide a pinned, approved specification.")
        return PreflightResult(False, findings)
    status = value(spec, "status", "")
    status = getattr(status, "value", status)
    if status != "APPROVED":
        add("SPEC_AMBIGUOUS", f"Specification status is {status!r}, not APPROVED.",
            "spec_owner", "Approve or replace the specification before verification.")
    conflicts = value(spec, "conflicts", [])
    if conflicts or value(spec, "ambiguous", False):
        add("SPEC_AMBIGUOUS", "Conflicting acceptance criteria: " + str(conflicts),
            "spec_owner", "Which acceptance behavior is authoritative? Resolve the cited conflicts.",
            [value(r, "id", "") for r in value(spec, "requirements", [])])
    if value(spec, "unresolved_questions", []):
        add("SPEC_AMBIGUOUS", "Unresolved specification questions: " + str(value(spec, "unresolved_questions")),
            "spec_owner", "Answer the blocking specification questions before testing.")

    requirements = value(spec, "requirements", [])
    tests = value(spec, "acceptance_tests", [])
    req_ids = [value(r, "id", "") for r in requirements]
    test_ids = [value(t, "id", "") for t in tests]
    if not requirements or any(not x for x in req_ids) or len(set(req_ids)) != len(req_ids):
        add("SPEC_AMBIGUOUS", "Requirements are empty or have missing/duplicate IDs.",
            "spec_owner", "Assign a unique ID to every required behavior.")
    if not tests or any(not x for x in test_ids) or len(set(test_ids)) != len(test_ids):
        add("SPEC_AMBIGUOUS", "Acceptance tests are empty or have missing/duplicate IDs.",
            "spec_owner", "Provide unique acceptance test IDs and expected behaviors.")
    known = set(test_ids)
    for requirement in requirements:
        rid = value(requirement, "id", "")
        mapped = value(requirement, "test_ids", [])
        if not value(requirement, "description", "").strip() or not mapped or set(mapped) - known:
            add("SPEC_AMBIGUOUS", f"Requirement {rid} has no complete acceptance-test mapping.",
                "spec_owner", f"Supply the expected behavior and test IDs for {rid}.", [rid])

    profile = value(context, "profile")
    targets = value(profile, "test_paths", [])
    if not targets:
        add("INPUT_INVALID", "Trusted test profile has no test targets.", "test_owner",
            "Select a server-registered test profile with explicit targets.")
    for target in targets:
        try:
            validate_test_path(target)
        except ValueError as exc:
            add("INPUT_INVALID", str(exc), "test_owner", "Correct the registered test target.")
    for test in tests:
        node = value(test, "node_id", "")
        try:
            validate_test_path(node)
            covered = any(target_covers(t, node) for t in targets)
        except (TypeError, ValueError):
            covered = False
        if not covered or not value(test, "expected_behavior", "").strip():
            add("SPEC_AMBIGUOUS", f"Acceptance test {value(test, 'id', '')} is not fully defined in the selected profile.",
                "spec_owner", "Provide its expected behavior and include its pytest node in the trusted profile.")

    backend = value(profile, "backend", "")
    backend = getattr(backend, "value", backend)
    if backend == "local_trusted":
        if not value(profile, "trusted", False):
            add("ENVIRONMENT_BLOCKED", "Local execution is restricted to trusted demo fixtures.",
                "environment_owner", "Use the pinned, isolated Docker profile for untrusted code.")
    elif backend == "docker":
        image = value(profile, "docker_image", "") or ""
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/:\-]*@sha256:[a-f0-9]{64}", image):
            add("ENVIRONMENT_BLOCKED", "Docker image must be pinned by its SHA-256 digest.",
                "environment_owner", "Register a prebuilt pytest image using image@sha256:digest.")
        if shutil.which("docker") is None:
            add("ENVIRONMENT_BLOCKED", "Docker CLI is unavailable.", "environment_owner",
                "Install and start Docker, then rerun verification.")
    else:
        add("ENVIRONMENT_BLOCKED", f"Unsupported runner backend: {backend!r}.",
            "environment_owner", "Use local_trusted for demo fixtures or a pinned Docker profile.")

    environment = value(context, "environment")
    if environment is None or not value(environment, "fingerprint", ""):
        add("ENVIRONMENT_BLOCKED", "A pinned environment manifest is required.",
            "environment_owner", "Resolve and pin an environment manifest before testing.")
    for prerequisite in value(environment, "prerequisites", []):
        if not value(prerequisite, "available", False):
            add("ENVIRONMENT_BLOCKED", f"Prerequisite unavailable: {value(prerequisite, 'name', 'unnamed')}",
                value(prerequisite, "owner_role", "environment_owner"),
                value(prerequisite, "remediation", "Restore the prerequisite and rerun verification."))
    for field_name in ("snapshot_root", "base_snapshot_root"):
        root = value(context, field_name)
        try:
            validate_snapshot(Path(root))
        except (OSError, ValueError, TypeError) as exc:
            add("INPUT_INVALID", f"Invalid {field_name}: {exc}", "code_owner",
                "Resolve an immutable, link-free commit snapshot.")
    return PreflightResult(not findings, findings)
