"""Redacted, versioned experience for offline evaluation, never live learning.

No model weights, prompts, SKILL.md files, production policies or repository
sources are mutated here. Promotion manifests are review artifacts, not loaders.
"""
from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
from pathlib import Path
import re
import threading
from typing import Any, Mapping, Sequence
from uuid import uuid4


_SECRET_KEYS = re.compile(r"(?:password|passwd|secret|authorization|credential|private.?key|api.?key|access.?token|refresh.?token|client.?secret)", re.I)
_TEXT_PATTERNS = (
    (re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----.*?-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", re.S), "[REDACTED PRIVATE KEY]"),
    (re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]+", re.I), "Bearer [REDACTED]"),
    (re.compile(r"\b(?:sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9]{8,}|github_pat_[A-Za-z0-9_]{8,}|AKIA[A-Z0-9]{16})\b"), "[REDACTED TOKEN]"),
    (re.compile(r"(?i)\b(password|passwd|api[_-]?key|access[_-]?token|secret|token)\s*([=:])\s*[\"']?[^\s\"',;]+"), r"\1\2[REDACTED]"),
    (re.compile(r"(https?://)[^\s/:@]+:[^\s/@]+@", re.I), r"\1[REDACTED]@"),
)


def redact(value: Any) -> Any:
    """Recursively redact common secret forms before serialization.

    This is defense in depth, not a universal detector. Callers must avoid
    supplying raw user data, entire environments or unbounded traces.
    """
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    elif is_dataclass(value) and not isinstance(value, type):
        value = asdict(value)
    if isinstance(value, Mapping):
        return {str(key): "[REDACTED]" if _SECRET_KEYS.search(str(key)) else redact(item)
                for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [redact(item) for item in value]
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (Path, datetime)):
        return str(value)
    if isinstance(value, str):
        for pattern, replacement in _TEXT_PATTERNS:
            value = pattern.sub(replacement, value)
        return value
    if value is None or isinstance(value, (int, float, bool)):
        return value
    raise TypeError(f"Unsupported memory value: {type(value).__name__}")


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


class EvolutionMemory:
    """Append-only process-safe-for-threads JSONL recorder for a local pilot.

    Source identity and configuration versions travel with every event. This
    enables replay datasets and prevents stale experiences being silently
    interpreted as evidence about a newer head/spec/policy.
    """

    _locks_guard = threading.Lock()
    _locks: dict[str, threading.Lock] = {}

    def __init__(self, path: str | Path, *, source_identity: Mapping[str, Any] | None = None,
                 configuration: Mapping[str, Any] | None = None) -> None:
        destination = Path(path)
        self.path = destination if destination.suffix == ".jsonl" else destination / "events.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.source_identity = dict(source_identity or {})
        self.configuration = {"model_policy": "frozen", "memory_schema": "1.0", **dict(configuration or {})}
        with self._locks_guard:
            self._lock = self._locks.setdefault(str(self.path.resolve()), threading.Lock())

    def append(self, kind: str, run_id: str, payload: Any, *,
               source_identity: Mapping[str, Any] | None = None,
               configuration: Mapping[str, Any] | None = None) -> dict[str, Any]:
        if kind not in {"run", "review", "feedback", "promotion_proposal"}:
            raise ValueError("Unsupported memory event kind")
        if not run_id:
            raise ValueError("run_id is required")
        if source_identity is None:
            source_identity = dict(self.source_identity)
            if isinstance(payload, Mapping):
                for key in ("identity_hash", "artifact_manifest_hash", "reviewed_head_sha", "spec_hash", "policy_version"):
                    if key in payload:
                        source_identity[key] = payload[key]
        body = redact({"schema_version": "1.0", "event_id": str(uuid4()), "kind": kind,
                       "recorded_at": datetime.now(timezone.utc).isoformat(), "run_id": run_id,
                       "source_identity": dict(self.source_identity if source_identity is None else source_identity),
                       "configuration": dict(self.configuration if configuration is None else configuration),
                       "payload": payload})
        body["event_hash"] = hashlib.sha256(_canonical(body)).hexdigest()
        encoded = _canonical(body) + b"\n"
        if len(encoded) > 1_000_000:
            raise ValueError("A memory event must not exceed 1 MB")
        with self._lock:
            with self.path.open("ab") as stream:
                stream.write(encoded)
                stream.flush()
        return body

    def append_run(self, run: Any, *, source_identity: Mapping[str, Any] | None = None,
                   configuration: Mapping[str, Any] | None = None) -> dict[str, Any]:
        payload = run.model_dump(mode="json") if hasattr(run, "model_dump") else run
        if not isinstance(payload, Mapping):
            raise TypeError("Run memory requires a structured run mapping")
        if source_identity is None:
            keys = ("repo", "pr_number", "base_sha", "reviewed_head_sha", "spec_id", "spec_hash",
                    "test_profile_hash", "environment_fingerprint", "policy_version", "identity_hash")
            source_identity = {key: payload[key] for key in keys if key in payload}
        if configuration is None:
            configuration = {**self.configuration, "policy_version": payload.get("policy_version", "unknown")}
        return self.append("run", str(payload.get("run_id", "")), payload,
                           source_identity=source_identity, configuration=configuration)

    def append_review(self, run_id: str | Mapping[str, Any], review: Any = None, **kwargs: Any) -> dict[str, Any]:
        if isinstance(run_id, Mapping):
            review = dict(run_id)
            run_id = str(review.get("run_id", ""))
        return self.append("review", run_id, review, **kwargs)

    def append_feedback(self, run_id: str | Mapping[str, Any], feedback: Any = None, **kwargs: Any) -> dict[str, Any]:
        if isinstance(run_id, Mapping):
            feedback = dict(run_id)
            run_id = str(feedback.get("run_id", ""))
        return self.append("feedback", run_id, feedback, **kwargs)

    def read_events(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        with self._lock:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        events = []
        for line in lines:
            if not line.strip():
                continue
            event = json.loads(line)
            expected = event.get("event_hash")
            body = {key: value for key, value in event.items() if key != "event_hash"}
            if not expected or hashlib.sha256(_canonical(body)).hexdigest() != expected:
                raise ValueError("Memory event integrity check failed")
            events.append(event)
        return events

    def export_offline_dataset(self, destination: str | Path, *, reviewed_only: bool = True) -> dict[str, Any]:
        """Export grouped examples for an external GEPA evaluator; run no training.

        Human feedback is an annotation, never a retroactive replacement for
        execution evidence. Unreviewed examples are excluded by default.
        """
        target = Path(destination)
        if target.resolve() == self.path.resolve():
            raise ValueError("Dataset export must not overwrite the memory log")
        events = self.read_events()
        groups: dict[str, list[dict[str, Any]]] = {}
        for event in events:
            groups.setdefault(event["run_id"], []).append(event)
        examples = []
        for run_id, records in groups.items():
            traces = [record for record in records if record["kind"] == "run"]
            annotations = [record for record in records if record["kind"] in {"review", "feedback"}]
            if not traces or (reviewed_only and not annotations):
                continue
            examples.append({"schema_version": "1.0", "run_id": run_id,
                             "source_identity": traces[-1]["source_identity"],
                             "configuration": traces[-1]["configuration"],
                             "traces": traces, "human_annotations": annotations,
                             "split": "UNASSIGNED", "eligible_for_live_activation": False})
        target.parent.mkdir(parents=True, exist_ok=True)
        # A dataset is a snapshot and may be explicitly re-exported. The source
        # JSONL log remains append-only.
        target.write_bytes(b"".join(_canonical(redact(row)) + b"\n" for row in examples))
        return {"schema_version": "1.0", "path": str(target), "examples": len(examples),
                "sha256": hashlib.sha256(target.read_bytes()).hexdigest(), "purpose": "OFFLINE_EVALUATION_ONLY"}

    def create_promotion_manifest(self, *, candidate_version: str, candidate_sha256: str,
                                  approved_by: str, evaluation_evidence: Sequence[str],
                                  canary_percentage: int, rollback_version: str,
                                  source_identity: Mapping[str, Any],
                                  expected_source_identity: Mapping[str, Any] | None = None,
                                  destination: str | Path | None = None) -> dict[str, Any]:
        """Record a reviewed future promotion plan without activating anything.

        Authorization of the named approver belongs to the caller's identity
        system. This function requires an explicit name and validation evidence,
        and refuses a stale source identity rather than reflecting it live.
        """
        if not candidate_version.strip() or not approved_by.strip() or not rollback_version.strip():
            raise ValueError("Candidate version, explicit approver and rollback version are required")
        if not re.fullmatch(r"[0-9a-f]{64}", candidate_sha256):
            raise ValueError("Candidate SHA-256 must be a lowercase 64-character digest")
        if not evaluation_evidence or any(not item.strip() for item in evaluation_evidence):
            raise ValueError("Held-out evaluation evidence is required")
        if not 1 <= canary_percentage <= 10:
            raise ValueError("Pilot canary must be between 1 and 10 percent")
        if not source_identity:
            raise ValueError("Promotion requires the evaluated source identity")
        if expected_source_identity is not None and dict(source_identity) != dict(expected_source_identity):
            raise ValueError("STALE: evaluation source identity no longer matches")
        manifest = redact({"schema_version": "1.0", "status": "REVIEWED_PLAN_ONLY", "activated": False,
                           "candidate_version": candidate_version, "candidate_sha256": candidate_sha256,
                           "approved_by": approved_by, "evaluation_evidence": list(evaluation_evidence),
                           "source_identity": dict(source_identity), "canary_percentage": canary_percentage,
                           "rollback_version": rollback_version,
                           "rollback_trigger": "Any required-check regression, provenance mismatch, or canary error increase",
                           "requires_operator_activation": True, "model_weights_updated": False,
                           "skill_files_updated": False})
        manifest["manifest_hash"] = hashlib.sha256(_canonical(manifest)).hexdigest()
        if destination is not None:
            path = Path(destination)
            if path.suffix.lower() != ".json" or path.resolve() == self.path.resolve():
                raise ValueError("Promotion output must be a separate JSON artifact")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
        return manifest
