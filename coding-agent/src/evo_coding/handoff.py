"""Loading and deterministic validation of the Spec Agent handoff, plus content hashing.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from pydantic_core import to_jsonable_python

from .models import Handoff, Owner, RunState


class HandoffSchemaError(Exception):
    """The handoff file is not valid JSON or does not match the schema."""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


@dataclass(frozen=True)
class HandoffIssue:
    code: str
    message: str
    owner: Owner = Owner.SPEC_REVIEWER
    ref: str | None = None


@dataclass
class LoadedHandoff:
    handoff: Handoff
    path: Path | None
    spec_path: Path | None
    spec_text: str | None
    issues: list[HandoffIssue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.issues


def parse_handoff(raw: str | bytes | dict) -> Handoff:
    try:
        data = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
        return Handoff.model_validate(data)
    except json.JSONDecodeError as exc:
        raise HandoffSchemaError([f"invalid JSON: {exc}"]) from exc
    except ValidationError as exc:
        raise HandoffSchemaError(
            [f"{'.'.join(str(p) for p in err['loc']) or '<root>'}: {err['msg']}" for err in exc.errors()]
        ) from exc


def resolve_spec_path(handoff: Handoff, handoff_path: Path | None, spec_root: Path | None = None) -> Path:
    uri = handoff.spec_uri
    if uri.startswith("file://"):
        uri = uri[len("file://"):]
    candidate = Path(uri)
    if candidate.is_absolute():
        return candidate
    base = spec_root or (handoff_path.parent if handoff_path else Path.cwd())
    return (base / candidate).resolve()


def load_handoff(path: Path, spec_path: Path | None = None, spec_root: Path | None = None) -> LoadedHandoff:
    path = Path(path)
    handoff = parse_handoff(path.read_bytes())
    resolved = Path(spec_path) if spec_path else resolve_spec_path(handoff, path, spec_root)
    spec_text = resolved.read_text(encoding="utf-8") if resolved.is_file() else None
    loaded = LoadedHandoff(handoff, path, resolved, spec_text)
    loaded.issues = validate_handoff(handoff, None if spec_text is None else resolved.read_bytes())
    return loaded


def validate_handoff(handoff: Handoff, spec_bytes: bytes | None) -> list[HandoffIssue]:
    """Rules the Coding Agent enforces before doing any work. Order is stable for reporting."""
    issues: list[HandoffIssue] = []
    add = issues.append

    if handoff.status != RunState.SPEC_APPROVED:
        add(HandoffIssue("WRONG_STATUS", f"handoff status is {handoff.status}, expected SPEC_APPROVED"))
    if handoff.next_owner != Owner.CODING_AGENT:
        add(HandoffIssue("WRONG_NEXT_OWNER", f"handoff next_owner is {handoff.next_owner}, expected coding_agent",
                         Owner.ORCHESTRATOR))

    if handoff.approval is None:
        add(HandoffIssue("MISSING_APPROVAL", "the specification has no recorded human approval"))
    elif handoff.approval.spec_hash != handoff.spec_hash:
        add(HandoffIssue("STALE_APPROVAL",
                         "the approval is bound to a different spec version than the handoff's spec_hash"))

    if spec_bytes is None:
        add(HandoffIssue("SPEC_NOT_FOUND", f"specification not found at {handoff.spec_uri}"))
    elif spec_hash(spec_bytes) != handoff.spec_hash:
        add(HandoffIssue("SPEC_HASH_MISMATCH",
                         "product_spec.md content does not match the approved spec_hash; re-approval required"))

    for item in handoff.unresolved_items:
        add(HandoffIssue("UNRESOLVED_ITEM", f"unresolved decision: {item}", ref=item))

    req_ids = [r.id for r in handoff.requirements]
    crit_ids = [c.id for c in handoff.acceptance_criteria]
    for label, ids in (("requirement", req_ids), ("acceptance criterion", crit_ids)):
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        if duplicates:
            add(HandoffIssue("DUPLICATE_ID", f"duplicate {label} ids: {duplicates}"))
    if sorted(set(handoff.requirement_ids)) != sorted(set(req_ids)):
        add(HandoffIssue("REQUIREMENT_IDS_MISMATCH",
                         f"requirement_ids {handoff.requirement_ids} do not match requirements {req_ids}"))
    known = set(req_ids)
    covered: set[str] = set()
    for criterion in handoff.acceptance_criteria:
        unknown = [r for r in criterion.requirement_ids if r not in known]
        if unknown:
            add(HandoffIssue("UNKNOWN_REQUIREMENT", f"{criterion.id} references unknown requirements {unknown}",
                             ref=criterion.id))
        covered.update(criterion.requirement_ids)
    for req in req_ids:
        if req not in covered:
            add(HandoffIssue("REQUIREMENT_WITHOUT_CRITERION", f"{req} has no acceptance criterion", ref=req))
    return issues


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json(value: Any) -> str:
    return json.dumps(to_jsonable_python(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def canonical_hash(value: Any) -> str:
    return sha256_bytes(canonical_json(value).encode("utf-8"))


def spec_hash(spec_bytes: bytes) -> str:
    """The spec content hash is SHA-256 of the exact product_spec.md bytes, with CRLF normalized to LF.

    Normalizing line endings keeps the hash stable across Windows checkouts with autocrlf.
    """
    return sha256_bytes(spec_bytes.replace(b"\r\n", b"\n"))
