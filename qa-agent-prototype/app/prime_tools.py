"""Operator-scoped QA tools. No agent creation, model calls, or workflow scheduling."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from uuid import uuid4

from pydantic import Field

from . import gitutil
from .agent import review
from .checks import CheckContext, run_check
from .checks import security, secrets, dependencies
from .models import Contract, Policy
from .risk import classify


class Target(Contract):
    repo: Path
    head_ref: str
    base_sha: str = Field(pattern=r"^[a-f0-9]{40}$")
    head_sha: str = Field(pattern=r"^[a-f0-9]{40}$")
    verification: Path | None = None
    spec_hash: str | None = None
    test_profile_hash: str | None = None


class OperatorConfig(Contract):
    schema_version: str = "1.0"
    targets: dict[str, Target]
    output_dir: Path
    allowed_tools: list[str] = Field(default_factory=lambda: ["qa_inspect", "qa_scan", "qa_review"])
    policy: Policy = Field(default_factory=Policy)


TOOL_PERMISSIONS = {
    "qa_inspect": {"permissions": ["repository.read"], "effects": ["audit.append"]},
    "qa_scan": {"permissions": ["repository.read", "scanner.execute"],
                "effects": ["temporary_files.write", "audit.append", "report.write"]},
    "qa_review": {"permissions": ["repository.read", "scanner.execute", "test_evidence.read"],
                  "effects": ["temporary_files.write", "audit.append", "report.write"]},
}


class QATools:
    """Trusted host constructs this service; callers supply IDs, never paths or policy."""

    def __init__(self, config: OperatorConfig):
        self.config = config.model_copy(deep=True)
        if config.schema_version != "1.0":
            raise ValueError("Unsupported operator configuration")
        if set(config.allowed_tools) - TOOL_PERMISSIONS.keys():
            raise ValueError("Unknown tool grant")
        paths = [config.output_dir] + [p for t in config.targets.values()
                 for p in (t.repo, t.verification) if p is not None]
        if any(not p.is_absolute() for p in paths):
            raise ValueError("Operator paths must be absolute")
        for target in config.targets.values():
            if config.output_dir.resolve().is_relative_to(target.repo.resolve()):
                raise ValueError("QA output must be outside the reviewed repository")
        self.lock = RLock()

    def _event(self, call_id, tool, target_id, status):
        self.config.output_dir.mkdir(parents=True, exist_ok=True)
        event = dict(schema_version="1.0", call_id=call_id, tool=tool, target_id=target_id,
                     status=status, timestamp=datetime.now(timezone.utc).isoformat())
        with (self.config.output_dir / "events.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event) + "\n")

    def call(self, tool: str, target_id: str) -> dict:
        with self.lock:
            call_id = uuid4().hex
            # Unknown identifiers are not echoed into logs or errors.
            if tool not in self.config.allowed_tools or target_id not in self.config.targets:
                self._event(call_id, "denied", None, "denied")
                raise PermissionError("Tool or target is not authorized by the operator")
            self._event(call_id, tool, target_id, "started")
            try:
                result = self._call(tool, self.config.targets[target_id], call_id)
            except Exception:
                self._event(call_id, tool, target_id, "error")
                raise
            self._event(call_id, tool, target_id, "completed")
            return {"schema_version": "1.0", "call_id": call_id, "target_id": target_id, **result}

    def _call(self, tool: str, target: Target, call_id: str) -> dict:
        if gitutil.resolve(target.repo, target.head_ref) != target.head_sha:
            return {"status": "STALE", "reason": "Registered head moved; operator must register fresh inputs"}
        if gitutil.resolve(target.repo, target.base_sha) != target.base_sha:
            raise ValueError("Registered base is unavailable")
        files = gitutil.changed_files(target.repo, target.base_sha, target.head_sha)
        tier, reasons = classify(files, self.config.policy)
        identity = dict(base_sha=target.base_sha, head_sha=target.head_sha,
                        risk_tier=tier.name, risk_reasons=reasons)
        if tool == "qa_inspect":
            return {"status": "INSPECTED", **identity,
                    "changed_files": [f.model_dump() for f in files]}
        output = self.config.output_dir / call_id
        if tool == "qa_review":
            if not target.verification or not target.spec_hash or not target.test_profile_hash:
                return {"status": "BLOCKED", **identity,
                        "reason": "Operator must register Test evidence and expected spec/profile hashes"}
            # Bind the evidence base to the operator target before invoking legacy review.
            try:
                data = json.loads(target.verification.read_bytes())
                if data.get("base_sha") != target.base_sha:
                    raise ValueError("Test evidence base differs from registered base")
            except (ValueError, OSError, AttributeError):
                return {"status": "BLOCKED", **identity, "reason": "Invalid or mismatched Test evidence"}
            decision = review(target.repo, target.verification, target.head_sha, output,
                              self.config.policy, expected_spec_hash=target.spec_hash,
                              expected_test_profile_hash=target.test_profile_hash)
            result = {"status": decision.verdict.value, "decision": decision.model_dump(mode="json")}
        else:
            ctx = CheckContext(target.repo, target.base_sha, target.head_sha, files,
                               gitutil.added_lines(target.repo, target.base_sha, target.head_sha),
                               self.config.policy, tier)
            checks = [run_check(module.NAME, module.check, ctx)
                      for module in (security, secrets, dependencies)]
            result = {"status": "SCANNED", **identity,
                      "checks": [check.model_dump(mode="json") for check in checks],
                      "approval": False,
                      "limitations": ["Scan results alone are not QA approval",
                                      "Dependency feed is fictional demo data"]}
        if gitutil.resolve(target.repo, target.head_ref) != target.head_sha:
            result = {"status": "STALE", **identity, "reason": "Head changed during QA; rerun on fresh inputs"}
            if tool == "qa_review":
                from .models import Verdict
                from .report import write_outputs
                decision.verdict = Verdict.STALE
                decision.reasons = [result["reason"]]
                decision.next_owner = "Test Agent"
                write_outputs(decision, output)
        output.mkdir(parents=True, exist_ok=True)
        (output / "tool_result.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        return result


def load_tools(path: Path) -> QATools:
    if not path.is_absolute():
        raise ValueError("EVO_QA_CONFIG must be an absolute operator-owned path")
    config = OperatorConfig.model_validate_json(path.read_bytes())
    if any(path.resolve().is_relative_to(t.repo.resolve()) for t in config.targets.values()):
        raise ValueError("Operator config must be outside the reviewed repository")
    return QATools(config)
