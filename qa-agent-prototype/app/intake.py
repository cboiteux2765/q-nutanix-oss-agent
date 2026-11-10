"""Intake: only a fresh READY_FOR_QA handoff starts a review (PRD QA-01)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ValidationError

from .models import Handoff, Verdict

SUPPORTED_VERIFICATION_VERSIONS = {"1.0"}


@dataclass
class IntakeResult:
    handoff: Handoff | None
    verification_sha256: str | None
    stop_verdict: Verdict | None = None  # set when QA must not start
    reasons: list[str] = field(default_factory=list)
    next_owner: str = ""

    @property
    def ok(self) -> bool:
        return self.stop_verdict is None


def load_handoff(
    verification_path: Path,
    current_head_sha: str,
    expected_spec_hash: str | None = None,
    expected_test_profile_hash: str | None = None,
) -> IntakeResult:
    try:
        raw = verification_path.read_bytes()
    except OSError as exc:
        return IntakeResult(None, None, Verdict.BLOCKED, [f"Cannot read verification.json: {exc}"], "Test Agent")

    digest = hashlib.sha256(raw).hexdigest()
    try:
        data = json.loads(raw)
        handoff = Handoff.model_validate(data)
    except (json.JSONDecodeError, ValidationError) as exc:
        reason = f"verification.json is not a valid Test Agent handoff: {_short(exc)}"
        return IntakeResult(None, digest, Verdict.BLOCKED, [reason], "Test Agent")

    if handoff.schema_version not in SUPPORTED_VERIFICATION_VERSIONS:
        reason = f"Unsupported verification schema_version {handoff.schema_version!r}"
        return IntakeResult(handoff, digest, Verdict.BLOCKED, [reason], "Test Agent")

    stale = []
    if handoff.reviewed_head_sha != current_head_sha:
        stale.append(
            f"PR head moved: Test verified {handoff.reviewed_head_sha[:12]}, current head is {current_head_sha[:12]}"
        )
    if expected_spec_hash and handoff.spec_hash != expected_spec_hash:
        stale.append("Approved spec changed after the test run")
    if expected_test_profile_hash and handoff.test_profile_hash != expected_test_profile_hash:
        stale.append("Test profile changed after the test run")
    if handoff.readiness == "STALE":
        stale.append("Test Agent already marked this evidence STALE")
    if stale:
        return IntakeResult(handoff, digest, Verdict.STALE, stale, "Test Agent (rerun on current head)")

    if handoff.readiness != "READY_FOR_QA":
        reason = f"Test readiness is {handoff.readiness or 'missing'}, not READY_FOR_QA; QA does not start"
        return IntakeResult(handoff, digest, Verdict.BLOCKED, [reason], "Test Agent / implementer")

    try:
        validate_evidence(verification_path, handoff)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return IntakeResult(handoff, digest, Verdict.BLOCKED,
                            [f"Test evidence integrity check failed: {_short(exc)}"], "Test Agent")
    return IntakeResult(handoff, digest)


def validate_evidence(verification_path: Path, handoff: Handoff) -> None:
    # The Test prototype places its manifest beside verification.json. Its URI
    # is an API route, not an instruction to fetch an arbitrary local path.
    root = verification_path.parent.resolve()
    manifest_path = root / "evidence_manifest.json"
    if manifest_path.is_symlink():
        raise ValueError("Linked evidence manifest is not allowed")
    raw = manifest_path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != handoff.artifact_manifest_hash:
        raise ValueError("Artifact manifest checksum mismatch")
    manifest = json.loads(raw)
    if manifest["run_id"] != handoff.run_id or manifest["identity_hash"] != handoff.identity_hash:
        raise ValueError("Manifest belongs to another Test run or identity")
    identity = manifest["identity"]
    pairs = {"repo": handoff.repo, "pr_number": handoff.pr_number, "base_sha": handoff.base_sha,
             "pr_head_sha": handoff.reviewed_head_sha, "spec_hash": handoff.spec_hash,
             "test_profile_hash": handoff.test_profile_hash,
             "environment_fingerprint": handoff.environment_fingerprint, "policy_version": handoff.policy_version}
    if any(identity.get(k) != v for k, v in pairs.items()):
        raise ValueError("Manifest identity differs from verification")
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    if hashlib.sha256(canonical).hexdigest() != handoff.identity_hash:
        raise ValueError("Test identity checksum mismatch")
    if not manifest["evidence"]:
        raise ValueError("Manifest contains no evidence")
    for entry in manifest["evidence"]:
        relative = Path(entry["path"])
        path = root / relative
        if relative.is_absolute() or ".." in relative.parts or any(
            (root.joinpath(*relative.parts[:i])).is_symlink() for i in range(1, len(relative.parts)+1)
        ) or not path.resolve().is_relative_to(root):
            raise ValueError("Evidence path escapes its artifact directory or uses a link")
        if hashlib.sha256(path.read_bytes()).hexdigest() != entry["sha256"]:
            raise ValueError(f"Evidence checksum mismatch: {entry['path']}")


def _short(exc: Exception) -> str:
    text = str(exc).replace("\n", " ")
    return text if len(text) <= 300 else text[:297] + "..."
