"""Pilot fixtures: a stand-in for the Spec Agent's output and the offline end-to-end demo.

`build_handoff` produces exactly what the Spec Agent + human approval would: a product_spec.md and a
handoff.json bound to the spec's content hash and a pinned source commit. Until the real Spec Agent
exists, this is how the Coding Agent gets realistic, valid input.
"""
from __future__ import annotations

import json
import os
import shutil
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import gitops
from .agent import CodingAgent
from .config import CodingConfig, PolicyConfig
from .handoff import spec_hash
from .implementers.scripted import ScriptedImplementer
from .models import CodingResult, Handoff, Owner, RevisionRequest, RunState, utc_now
from .publish import LocalGitPublisher


def pilot_dir() -> Path:
    if os.environ.get("EVO_PILOT_DIR"):
        return Path(os.environ["EVO_PILOT_DIR"])
    return Path(__file__).resolve().parents[2] / "pilot"


def scenario_files() -> list[Path]:
    """Each pilot scenario is one TOML file: handoff params, the spec, the expected outcome, a recorded
    implementation for the scripted implementer and (optionally) revision feedback."""
    return sorted((pilot_dir() / "scenarios").glob("*.toml"))


def load_scenario(path: Path) -> dict[str, Any]:
    return tomllib.loads(Path(path).read_text(encoding="utf-8"))


def create_pilot_remote(dest: Path, template: Path | None = None) -> tuple[Path, str]:
    """Create a bare 'origin' from the pilot template. Returns (remote path, main commit sha)."""
    template = template or pilot_dir() / "repo"
    work = dest / "upstream-work"
    shutil.copytree(template, work, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    gitops.git(work, "init", "--quiet", "-b", "main")
    gitops.git(work, "add", "--all")
    env = {"GIT_AUTHOR_NAME": "Pilot Maintainer", "GIT_AUTHOR_EMAIL": "maintainer@example.invalid",
           "GIT_COMMITTER_NAME": "Pilot Maintainer", "GIT_COMMITTER_EMAIL": "maintainer@example.invalid",
           "GIT_AUTHOR_DATE": "2026-10-01T12:00:00Z", "GIT_COMMITTER_DATE": "2026-10-01T12:00:00Z"}
    gitops.git(work, "commit", "--quiet", "-m", "Initial pilot repository", env=env)
    sha = gitops.head_sha(work)
    remote = dest / "origin.git"
    gitops.git(None, "clone", "--quiet", "--bare", str(work), str(remote))
    return remote, sha


def build_handoff(scenario: Path, source_commit: str, out_dir: Path, run_id: str,
                  repository: str = "evo-pilot/pilot-stats", approved_by: str | None = "Pilot Spec Reviewer",
                  **overrides: Any) -> Path:
    """Write product_spec.md + handoff.json for a scenario file into out_dir and return the handoff path."""
    params = load_scenario(scenario)
    out_dir.mkdir(parents=True, exist_ok=True)
    spec_bytes = params["spec"].encode("utf-8")
    (out_dir / "product_spec.md").write_bytes(spec_bytes)
    digest = spec_hash(spec_bytes)
    now = utc_now()
    data: dict[str, Any] = {
        "schema_version": "0.1",
        "run_id": run_id,
        "repository": repository,
        "issue_id": params["issue_id"],
        "issue_title": params["issue_title"],
        "source_commit": source_commit,
        "base_branch": "main",
        "spec_version": params.get("spec_version", 1),
        "spec_hash": digest,
        "spec_uri": "product_spec.md",
        "requirement_ids": [r["id"] for r in params["requirements"]],
        "requirements": params["requirements"],
        "acceptance_criteria": params["acceptance_criteria"],
        "status": "SPEC_APPROVED",
        "approval": ({"approved_by": approved_by, "approved_at": now.isoformat(), "spec_hash": digest}
                     if approved_by else None),
        "risk_tier": params.get("risk_tier", "LOW"),
        "unresolved_items": params.get("unresolved_items", []),
        "next_owner": "coding_agent",
        "timestamp": now.isoformat(),
        "scope": params["scope"],
        "environment": params.get("environment", {}),
        "budget_limits": params.get("budget_limits", {}),
    }
    data.update(overrides)
    Handoff.model_validate(data)  # fail fast on a bad fixture
    path = out_dir / "handoff.json"
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return path


def feedback_for(scenario: Path, run_id: str, tested_head_sha: str) -> RevisionRequest:
    data = load_scenario(scenario)["feedback"]
    return RevisionRequest.model_validate({**data, "run_id": run_id, "tested_head_sha": tested_head_sha})


@dataclass
class ScenarioOutcome:
    name: str
    passed: bool
    results: list[CodingResult] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def check_expected(expected: dict[str, Any], result: CodingResult, status_key: str = "status") -> list[str]:
    problems = []
    if result.status != RunState(expected[status_key]):
        problems.append(f"status {result.status}, expected {expected[status_key]}")
    if "blocker" in expected and expected["blocker"] not in [b.code for b in result.blockers]:
        problems.append(f"blockers {[b.code for b in result.blockers]} missing {expected['blocker']}")
    if "next_owner" in expected and result.next_owner != Owner(expected["next_owner"]):
        problems.append(f"next_owner {result.next_owner}, expected {expected['next_owner']}")
    for code in expected.get("violations", []):
        found = [v["code"] for b in result.blockers for v in b.details.get("violations", [])]
        if code not in found:
            problems.append(f"violation {code} not reported (got {found})")
    return problems


def run_scenario(scenario: Path, out: Path, remote: Path, source_commit: str,
                 config: CodingConfig | None = None) -> ScenarioOutcome:
    data = load_scenario(scenario)
    expected = data["expected"]
    run_id = f"pilot-{scenario.stem}"
    handoff = build_handoff(scenario, source_commit, out / "handoffs" / scenario.stem, run_id)
    config = config or CodingConfig(work_dir=out / "work", policy=PolicyConfig())
    agent = CodingAgent(config, implementer=ScriptedImplementer(data["implementation"]),
                        publisher=LocalGitPublisher(remote))
    outcome = ScenarioOutcome(scenario.stem, True)
    result = agent.run(handoff)
    outcome.results.append(result)
    outcome.problems += check_expected(expected, result)
    if "revision_status" in expected and result.status == RunState.TESTING:
        revised = agent.revise(run_id, feedback_for(scenario, run_id, result.pr_head_sha))
        outcome.results.append(revised)
        outcome.problems += [f"revision: {p}" for p in check_expected(expected, revised, "revision_status")]
    outcome.passed = not outcome.problems
    return outcome


def run_demo(out: Path) -> list[ScenarioOutcome]:
    if out.exists() and any(out.iterdir()):
        raise FileExistsError(f"{out} is not empty; demo output is never overwritten")
    out.mkdir(parents=True, exist_ok=True)
    remote, sha = create_pilot_remote(out)
    outcomes = [run_scenario(s, out, remote, sha) for s in scenario_files()]
    summary = [{"scenario": o.name, "passed": o.passed, "problems": o.problems,
                "results": [{"revision": r.revision, "status": r.status, "next_owner": r.next_owner,
                             "pr_number": r.pr_number, "pr_head_sha": r.pr_head_sha,
                             "blockers": [b.code for b in r.blockers]} for r in o.results]} for o in outcomes]
    (out / "demo_summary.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    return outcomes
