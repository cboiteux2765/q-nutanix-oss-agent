"""Replay 15 seeded historical PRs using real local Git snapshots and pytest.

These are synthetic pilot fixtures, not claims about customer PR history.
Seed labels and independent reviewer judgments are reported separately. Run with
--output NEW_DIRECTORY, then --rescore DIRECTORY --reviews reviews.json after
an independent reviewer has inspected the generated evidence.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from hashlib import sha256
import json
import math
from pathlib import Path
import re
import time
from typing import Literal
from uuid import uuid4
import xml.etree.ElementTree as ET

from pydantic import Field

from app.models import (AcceptanceTest, EnvironmentManifest,
    ImmutableContract, Principal, Prerequisite, Requirement, SpecDocument,
    TestProfile, Verification, canonical_hash)
from app.providers import (BAD_SOURCE, GOOD_SOURCE, PRRegistration, SourceRegistry,
                           git, tree_hash)
from app.rca_and_repair import CandidatePatchProposal, FixtureRepairAdapter
from app.run_manager import RunManager, write_json


Category = Literal["clean_pass", "code_regression", "missing_environment",
                   "ambiguous_specification", "flaky_test", "budget_exhaustion"]
ENGINEER = Principal(name="benchmark-seeder", roles=frozenset({"engineer"}),
                     repos=frozenset({"pilot/service"}))


class PilotCase(ImmutableContract):
    id: str = Field(pattern=r"^pr-\d{3}$")
    category: Category
    seed: int = Field(ge=0, strict=True)
    expected_readiness: str
    expected_classification: str
    expected_requirements: dict[str, str]


class IndependentReview(ImmutableContract):
    """Operator-imported review, not an authenticated remote identity claim."""
    case_id: str
    run_id: str
    identity_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    artifact_manifest_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    reviewer: str = Field(min_length=1, max_length=200)
    independent: Literal[True]
    classification: Literal["NONE", "CODE_REGRESSION", "ENVIRONMENT_BLOCKED",
                            "SPEC_AMBIGUOUS", "TEST_FLAKY", "UNKNOWN"]
    rationale: str = Field(min_length=10, max_length=8000)


def pilot_cases() -> list[PilotCase]:
    groups = [("clean_pass", 3, "READY_FOR_QA", "NONE"),
        ("code_regression", 3, "CHANGES_REQUIRED", "CODE_REGRESSION"),
        ("missing_environment", 3, "BLOCKED", "ENVIRONMENT_BLOCKED"),
        ("ambiguous_specification", 2, "BLOCKED", "SPEC_AMBIGUOUS"),
        ("flaky_test", 2, "INCONCLUSIVE", "TEST_FLAKY"),
        ("budget_exhaustion", 2, "CHANGES_REQUIRED", "CODE_REGRESSION")]
    cases = []
    for category, count, readiness, classification in groups:
        for _ in range(count):
            index = len(cases) + 1
            outcomes = ({"REQ-EMPTY": "PASS", "REQ-VALUES": "PASS"} if category == "clean_pass"
                else {"REQ-EMPTY": "BLOCKED", "REQ-VALUES": "BLOCKED"} if readiness == "BLOCKED"
                else {"REQ-EMPTY": "FAIL", "REQ-VALUES": "NOT_VERIFIED"})
            cases.append(PilotCase(id=f"pr-{index:03}", category=category, seed=18900 + index,
                expected_readiness=readiness, expected_classification=classification,
                expected_requirements=outcomes))
    return cases


def seed_repository(case: PilotCase, directory: Path) -> tuple[SourceRegistry, PRRegistration]:
    """Create independent approved base/head snapshots, with seed labels fixed first."""
    root = directory / "repository"
    (root / "src").mkdir(parents=True, exist_ok=False)
    (root / "tests").mkdir()
    (root / "src/__init__.py").write_text("", encoding="utf-8")
    a, b = case.seed % 13 + 1, case.seed % 7 + 2
    # Deterministic scheduled flakiness: the same pinned suite fails on initial
    # and base invocations and passes on the diagnostic rerun. No RNG, network,
    # source mutation, or hidden counter outside the recorded workspace is used.
    flaky = ("    from pathlib import Path\n"
             "    assert Path.cwd().name == 'diagnostic-rerun', 'seeded intermittent failure'\n"
             if case.category == "flaky_test" else "")
    tests = ("from src.service import summarize\n\ndef test_empty():\n" + flaky
             + "    assert summarize([]) == 0\n\ndef test_values():\n"
             + f"    assert summarize([{a}, {b}]) == {a + b}\n")
    (root / "tests/test_service.py").write_text(tests, encoding="utf-8")
    (root / "src/service.py").write_text(GOOD_SOURCE, encoding="utf-8")
    git(root, "init", "--initial-branch=main")
    git(root, "config", "user.name", "Benchmark fixture author")
    git(root, "config", "user.email", "benchmark@localhost")
    git(root, "config", "core.hooksPath", str(root / "disabled-hooks"))
    git(root, "add", ".")
    git(root, "commit", "-m", f"Approved baseline seed {case.seed}")
    base = git(root, "rev-parse", "HEAD")
    git(root, "checkout", "-b", case.id)
    regression = case.category in {"code_regression", "budget_exhaustion"}
    source = BAD_SOURCE if regression else GOOD_SOURCE
    (root / "src/service.py").write_text(source + f"\n# Historical fixture seed {case.seed}\n", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-m", f"Historical PR {case.id}")
    spec = SpecDocument(spec_id=f"SPEC-{case.id}", intent="Safely sum empty and populated inputs.",
        requirements=(Requirement(id="REQ-EMPTY", description="Empty input returns zero.", test_ids=("AT-EMPTY",)),
                      Requirement(id="REQ-VALUES", description="Nonempty input returns its sum.", test_ids=("AT-VALUES",))),
        acceptance_tests=(AcceptanceTest(id="AT-EMPTY", node_id="tests/test_service.py::test_empty", expected_behavior="summarize([]) == 0"),
                          AcceptanceTest(id="AT-VALUES", node_id="tests/test_service.py::test_values", expected_behavior=f"summarize([{a}, {b}]) == {a + b}")),
        conflicts=("REQ-EMPTY returns zero versus raises ValueError: which behavior is approved?",)
                  if case.category == "ambiguous_specification" else ())
    registration = PRRegistration(repo="pilot/service", pr_number=int(case.id[3:]), repo_root=root,
        head_ref=f"refs/heads/{case.id}", base_sha=base, spec=spec,
        profile=TestProfile(trusted=True, backend="local_trusted"),
        environment=EnvironmentManifest(id=f"environment-{case.id}", prerequisites=(
            Prerequisite(name="sandbox-db", available=case.category != "missing_environment"),)))
    registry = SourceRegistry(directory / "sources")
    registry.register(registration)
    return registry, registration


def audit_case(case: PilotCase, manager: RunManager, run: Verification,
               wall_seconds: float) -> dict:
    """Audit bound files and actual JUnit, without inventing missing observations."""
    errors = []
    observed_nodes = {}
    state = manager.runs[run.run_id]
    executions = state.executions
    initial = next((e for e in executions if e["label"] == "initial"), None)
    base = next((e for e in executions if e["label"] == "base"), None)
    candidate_runs = [e for e in executions if e["label"].startswith("candidate-")]
    manifest = {}
    try:
        manifest = manager.artifacts(run.run_id)
        if manifest["identity_hash"] != run.identity_hash or canonical_hash(manifest["identity"]) != run.identity_hash:
            raise ValueError("Manifest identity differs from run")
        context = state.context
        if (context.identity_hash != run.identity_hash or context.head_sha != run.reviewed_head_sha
                or context.base_sha != run.base_sha or canonical_hash(context.spec) != run.spec_hash
                or context.profile.fingerprint != run.test_profile_hash
                or context.environment.fingerprint != run.environment_fingerprint):
            raise ValueError("Run identity is not bound to resolved inputs")
        if not all(re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", s) for s in (run.reviewed_head_sha, run.base_sha)):
            raise ValueError("Missing Git SHA binding")
        current = manager.registry.resolve(state.request)
        if current.identity_hash != run.identity_hash:
            raise ValueError("Registered historical snapshot changed")
        for item in manifest["evidence"]:
            manager.artifact_path(run.run_id, item["path"])
        manager.artifact_path(run.run_id, "verification.json")
        if manifest["executions"] != executions:
            raise ValueError("Execution ledger differs from manifest")
        for execution in executions:
            if not execution["command"] or not execution["evidence_ids"]:
                raise ValueError("Missing executed command/evidence")
            path = manager.artifact_path(run.run_id, execution["label"] + "/junit.xml")
            xml = ET.fromstring(path.read_bytes())
            observed_nodes[execution["label"]] = {
                item.get("classname", "").replace(".", "/") + ".py::" + item.get("name", ""):
                "failed" if item.find("failure") is not None or item.find("error") is not None
                else "skipped" if item.find("skipped") is not None else "passed"
                for item in xml.iter("testcase")}
            counts = Counter("failed" if item.find("failure") is not None or item.find("error") is not None
                else "skipped" if item.find("skipped") is not None else "passed" for item in xml.iter("testcase"))
            if any(counts[name] != execution[name] for name in ("passed", "failed", "skipped")):
                raise ValueError("JUnit and recorded counts disagree")
            if execution["exit_code"] == 0 and (counts["failed"] or not counts["passed"]):
                raise ValueError("Passing execution lacks passing JUnit evidence")
        if initial and any(getattr(run.tests, n) != initial[n] for n in ("passed", "failed", "skipped")):
            raise ValueError("Original PR totals overwritten by diagnostic/candidate results")
        if not initial and (run.tests.passed or run.tests.failed or run.tests.skipped):
            raise ValueError("Tests reported without execution")
        paths = {e["path"] for e in manifest["evidence"]}
        if any(e not in paths for r in run.requirements for e in r.evidence_ids):
            raise ValueError("Requirement references nonexistent evidence")
    except (ValueError, KeyError, OSError, ET.ParseError) as error:
        errors.append(str(error))

    expected_nodes = {"AT-EMPTY": "tests/test_service.py::test_empty",
                      "AT-VALUES": "tests/test_service.py::test_values"}
    expected_ids = {"REQ-EMPTY": ["AT-EMPTY"], "REQ-VALUES": ["AT-VALUES"]}
    actual = {r.id: r for r in run.requirements}
    actual_nodes = {a.id: a.node_id for a in state.context.spec.acceptance_tests}
    mapping_correct = sum(int(rid in actual and actual[rid].test_ids == expected_ids[rid]
        and str(actual[rid].result) == expected and all(actual_nodes.get(t) == expected_nodes[t]
        for t in expected_ids[rid])) for rid, expected in case.expected_requirements.items())
    classification = str(run.unresolved_findings[0].failure_class) if run.unresolved_findings else "NONE"
    blocker = case.category in {"missing_environment", "ambiguous_specification"}
    owner = "environment_owner" if case.category == "missing_environment" else "spec_owner"
    blocker_ok = (not blocker or (not executions and run.metrics.debug_iterations == 0
        and run.candidate_patch is None and run.readiness == "BLOCKED"
        and any(f.owner_role == owner for f in run.unresolved_findings)))
    regression = case.category in {"code_regression", "budget_exhaustion"}
    regression_ok = not regression or bool(initial and base and initial["exit_code"] == 1
        and initial["failed"] > 0 and base["exit_code"] == 0 and base["passed"] > 0
        and initial["trusted"] and base["trusted"] and run.tests.failed > 0
        and observed_nodes.get("initial", {}).get(expected_nodes["AT-EMPTY"]) == "failed"
        and observed_nodes.get("base", {}).get(expected_nodes["AT-EMPTY"]) == "passed")
    patch_ok = True
    if run.candidate_patch:
        patch = run.candidate_patch
        try:
            candidate = next(e for e in candidate_runs if e["exit_code"] == 0 and e["passed"] > 0
                             and not e["failed"] and e["trusted"])
            number = candidate["label"].split("-")[-1]
            work = manager.storage / "runs" / run.run_id / "work" / f"candidate-source-{number}"
            patch_ok = (patch["validated"] is True and patch["accepted"] is False
                and patch["publication"] == "LOCAL_DRAFT_ONLY"
                and patch["commit_sha"] == patch["tested_candidate_sha"] == git(work, "rev-parse", "HEAD")
                and patch["original_head_sha"] == run.reviewed_head_sha
                and tree_hash(work) == tree_hash(manager.storage / "runs" / run.run_id / "work" / candidate["label"])
                and patch["changed_paths"] == ["src/service.py"]
                and all(observed_nodes.get(candidate["label"], {}).get(n) == "passed"
                        for n in expected_nodes.values())
                and run.readiness == "CHANGES_REQUIRED")
        except (KeyError, OSError, ValueError, StopIteration):
            patch_ok = False
    budget_ok = case.category != "budget_exhaustion" or (
        run.metrics.debug_iterations == 2 and run.metrics.test_executions == 4
        and len(candidate_runs) == 2 and all(e["failed"] > 0 for e in candidate_runs)
        and run.candidate_patch is None and any("NEEDS_ENGINEER" in s for s in run.reasons))
    goal = (not errors and str(run.readiness) == case.expected_readiness
        and classification == case.expected_classification and mapping_correct == 2
        and blocker_ok and regression_ok and patch_ok and budget_ok
        and (case.category != "code_regression" or run.candidate_patch is not None))
    return dict(case_id=case.id, category=case.category, seed=case.seed, run_id=run.run_id,
        identity_hash=run.identity_hash, artifact_manifest_hash=run.artifact_manifest_hash,
        base_sha=run.base_sha, reviewed_head_sha=run.reviewed_head_sha,
        spec_hash=run.spec_hash, evidence_count=len(manifest.get("evidence", [])),
        readiness=str(run.readiness), classification=classification,
        expected_classification=case.expected_classification,
        integrity_ok=not errors, integrity_errors=errors,
        mapping_correct=mapping_correct, mapping_total=2, blocker=blocker, blocker_ok=blocker_ok,
        regression=regression, regression_evidence_ok=regression_ok,
        patch_proposed=bool(run.candidate_patch), patch_valid=patch_ok,
        budget_ok=budget_ok, task_goal_completed=bool(goal),
        original_tests=run.tests.model_dump(), metrics=run.metrics.model_dump(),
        wall_seconds=wall_seconds,
        evidence_manifest=f"{case.id}/state/runs/{run.run_id}/artifacts/evidence_manifest.json")


def percentile(values: list[float], q: float) -> float | None:
    """Linear interpolation, with singleton/empty sample semantics explicit."""
    if not values:
        return None
    if not 0 <= q <= 1 or not all(math.isfinite(v) and v >= 0 for v in values):
        raise ValueError("Invalid percentile inputs")
    values = sorted(values)
    position = (len(values) - 1) * q
    low, high = math.floor(position), math.ceil(position)
    return values[low] + (values[high] - values[low]) * (position - low)


def score_results(rows: list[dict], reviews: list[IndependentReview] | None = None) -> dict:
    if not rows or len({r["case_id"] for r in rows}) != len(rows):
        raise ValueError("Benchmark requires nonempty, unique case observations")
    reviews = reviews or []
    by_case = {r["case_id"]: r for r in rows}
    seen, reviewed_correct = set(), 0
    for review in reviews:
        if review.case_id not in by_case or review.case_id in seen:
            raise ValueError("Unknown or duplicate independent review")
        row = by_case[review.case_id]
        if (not review.reviewer.strip() or review.reviewer.strip().lower() in {"benchmark-seeder", "fixture", "seed", "agent"}
                or not review.independent):
            raise ValueError("Independent reviewer identity required")
        if any(getattr(review, k) != row[k] for k in ("run_id", "identity_hash", "artifact_manifest_hash")):
            raise ValueError("Independent review identity/evidence mismatch")
        seen.add(review.case_id)
        reviewed_correct += int(review.classification == row["classification"])

    def gate(correct, total, target, *, pending=False):
        rate = correct / total if total else None
        return dict(correct=correct, total=total, rate=rate, target=target,
            status="PENDING_REVIEW" if pending else "NOT_APPLICABLE" if not total
                   else "PASS" if rate >= target else "FAIL")

    blockers = [r for r in rows if r["blocker"]]
    regressions = [r for r in rows if r["regression"]]
    patches = [r for r in rows if r["patch_proposed"]]
    expected = {(c.id, c.category, c.seed) for c in pilot_cases()}
    completed = {(r["case_id"], r["category"], r["seed"]) for r in rows}
    gates = dict(
        pilot_coverage=gate(len(expected & completed), len(expected | completed), 1),
        version_evidence_integrity=gate(sum(r["integrity_ok"] for r in rows), len(rows), 1),
        requirement_mapping=gate(sum(r["mapping_correct"] for r in rows), sum(r["mapping_total"] for r in rows), .9),
        rca_independent_review=gate(reviewed_correct, len(reviews), .8, pending=len(reviews) != len(rows)),
        blocker_handling=gate(sum(r["blocker_ok"] for r in blockers), len(blockers), 1),
        patch_validity=gate(sum(r["patch_valid"] for r in patches), len(patches), 1),
        regression_verification=gate(sum(r["regression_evidence_ok"] for r in regressions), len(regressions), 1))
    performance = {}
    for key in ("runtime_seconds", "queue_seconds", "model_spend_usd"):
        values = [r["metrics"][key] for r in rows]
        performance[key] = dict(p50=percentile(values, .5), p95=percentile(values, .95), total=sum(values))
    performance["wall_seconds"] = dict(p50=percentile([r["wall_seconds"] for r in rows], .5),
        p95=percentile([r["wall_seconds"] for r in rows], .95))
    confusion = {}
    for row in rows:
        key = row["expected_classification"]
        observed = row["classification"]
        confusion.setdefault(key, {})[observed] = confusion.get(key, {}).get(observed, 0) + 1
    return dict(schema_version="1.0", fixture_provenance="SYNTHETIC_SEEDED_HISTORICAL_PRS",
        case_count=len(rows), categories=dict(Counter(r["category"] for r in rows)), gates=gates,
        all_pilot_gates_passed=all(g["status"] == "PASS" for g in gates.values()),
        seed_rca_accuracy=sum(r["classification"] == r["expected_classification"] for r in rows) / len(rows),
        seed_rca_confusion_matrix=confusion,
        independent_review_coverage=len(reviews) / len(rows),
        observed_integrity_violations=sum(not r["integrity_ok"] for r in rows),
        performance=performance,
        TGC=sum(r["task_goal_completed"] for r in rows) / len(rows),
        APT=sum(r["original_tests"]["passed"] for r in rows) / len(rows),
        definitions=dict(TGC="Fraction meeting seeded task goals, including correct blockers; not release approval.",
            APT="Mean passed original-head tests over all cases; blocked cases contribute zero. Candidate/base passes excluded.",
            requirement_mapping="Correct seeded requirement/test-node mapping AND original-head outcome / required mappings (30 in the full pilot).",
            rca_review="All cases need an independent, version-bound operator-imported review; seed labels are not reviews.",
            runtime="Active RunManager time; queue time separate; wall time includes fixture/snapshot setup.",
            integrity="Checks bound identities, checksums, execution ledgers and JUnit consistency; not signed attestation."),
        limitations=["PRD source was not supplied; gates implement the thresholds requested in the task.",
            "Synthetic fixtures are not measured customer PR productivity or production readiness.",
            "Flakiness is a deterministic scheduled fixture, not a stochastic flake-rate estimate.",
            "Local trusted pytest, deterministic repair adapter, no paid model calls or GitHub writes.",
            "Independent reviewer identities are asserted in operator-imported data, not remotely authenticated."],
        independent_reviews=[r.model_dump(mode="json") for r in reviews], cases=rows)


def render_report(summary: dict) -> str:
    def percent(value):
        return "N/A" if value is None else f"{value:.1%}"
    lines = ["# Test Agent seeded PR pilot benchmark", "",
        f"Cases: {summary['case_count']}. Provenance: **synthetic seeded historical PR snapshots**.",
        f"All pilot gates passed: **{summary['all_pilot_gates_passed']}**.", "",
        "## Pilot gates", "", "| Gate | Correct / evaluated | Rate | Target | Status |",
        "| --- | --- | --- | --- | --- |"]
    for name, g in summary["gates"].items():
        lines.append(f"| {name} | {g['correct']} / {g['total']} | {percent(g['rate'])} | {percent(g['target'])} | {g['status']} |")
    lines += ["", f"Seed-label RCA accuracy: {percent(summary['seed_rca_accuracy'])}; independent review coverage: {percent(summary['independent_review_coverage'])}.",
        f"Observed integrity violations: {summary['observed_integrity_violations']}.",
        "", "## Performance and task outcomes", "", "| Metric | p50 | p95 | Total |", "| --- | --- | --- | --- |"]
    for name, m in summary["performance"].items():
        lines.append(f"| {name} | {m['p50']:.6f} | {m['p95']:.6f} | {m.get('total', 'N/A')} |")
    lines += ["", f"TGC: {percent(summary['TGC'])}. APT: {summary['APT']:.3f}.", "", "## Definitions", ""]
    lines += [f"- **{k}**: {v}" for k, v in summary["definitions"].items()]
    lines += ["", "## Per-case evidence", "", "| Case | Category | Readiness | RCA | Goal | Evidence |",
              "| --- | --- | --- | --- | --- | --- |"]
    for row in summary["cases"]:
        lines.append(f"| {row['case_id']} | {row['category']} | {row['readiness']} | {row['classification']} | {row['task_goal_completed']} | [manifest]({row['evidence_manifest']}) |")
    lines += ["", "## Review and limitations", ""] + ["- " + s for s in summary["limitations"]]
    lines += ["", "Use independent_review_template.json to collect named evidence-bound reviews. "
        "Leave no blank reviewer or classification fields before importing with --rescore and --reviews.", ""]
    return "\n".join(lines)


def write_reports(directory: Path, summary: dict) -> None:
    write_json(directory / "benchmark_results.json", summary)
    (directory / "benchmark_report.md").write_text(render_report(summary), encoding="utf-8")


async def run_benchmark(output: Path, cases: list[PilotCase] | None = None) -> dict:
    cases = pilot_cases() if cases is None else cases
    if not cases or len({c.id for c in cases}) != len(cases):
        raise ValueError("Require nonempty unique cases")
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    rows = []
    for case in cases:
        started = time.monotonic()
        directory = output / case.id
        registry, registration = seed_repository(case, directory)
        adapter = FixtureRepairAdapter(changes={"src/service.py": GOOD_SOURCE})
        if case.category == "budget_exhaustion":
            adapter = FixtureRepairAdapter(proposals=[CandidatePatchProposal(
                {"src/service.py": BAD_SOURCE + f"\n# failed attempt {i}\n"}, "Seeded failed repair") for i in (1, 2)])
        manager = RunManager(directory / "state", registry, repair_adapter=adapter)
        request = registry.request_for_pr(registration.repo, registration.pr_number)
        run, _ = manager.create_run(request, ENGINEER, trigger_identity=case.id)
        await manager.execute(run.run_id)
        rows.append(audit_case(case, manager, manager.get_run(run.run_id), time.monotonic() - started))
        # Persist progress; a host interruption never masquerades as 15 completions.
        write_json(output / "cases.json", [c.model_dump(mode="json") for c in cases])
        write_reports(output, score_results(rows))
    summary = score_results(rows)
    write_reports(output, summary)
    write_json(output / "independent_review_template.json", [dict(
        case_id=r["case_id"], run_id=r["run_id"], identity_hash=r["identity_hash"],
        artifact_manifest_hash=r["artifact_manifest_hash"], reviewer="", independent=True,
        classification=None, rationale="") for r in rows])
    return summary


def rescore(directory: Path, reviews_path: Path) -> dict:
    directory = directory.resolve()
    previous = json.loads((directory / "benchmark_results.json").read_text(encoding="utf-8"))
    cases = [PilotCase.model_validate(c) for c in json.loads((directory / "cases.json").read_text(encoding="utf-8"))]
    rows = []
    previous_rows = {r["case_id"]: r for r in previous["cases"]}
    if set(previous_rows) != {c.id for c in cases}:
        raise ValueError("Incomplete replay; cannot score all pilot gates")
    for case in cases:
        old = previous_rows[case.id]
        manager = RunManager(directory / case.id / "state", SourceRegistry(directory / case.id / "sources"))
        row = audit_case(case, manager, manager.get_run(old["run_id"]), old["wall_seconds"])
        rows.append(row)
    reviews = [IndependentReview.model_validate(r) for r in json.loads(reviews_path.read_text(encoding="utf-8"))]
    summary = score_results(rows, reviews)
    write_reports(directory, summary)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--rescore", type=Path)
    parser.add_argument("--reviews", type=Path)
    args = parser.parse_args()
    if args.rescore:
        if not args.reviews or args.output:
            parser.error("--rescore requires --reviews and cannot use --output")
        summary = rescore(args.rescore, args.reviews)
        output = args.rescore
    else:
        if args.reviews:
            parser.error("Collect version-bound reviews after replay, then use --rescore")
        output = args.output or Path("benchmark-output") / uuid4().hex[:10]
        summary = asyncio.run(run_benchmark(output))
    print(f"{summary['case_count']} cases; TGC={summary['TGC']:.1%}; "
          f"RCA review gate={summary['gates']['rca_independent_review']['status']}")
    print(f"Reports: {output.resolve()}")


if __name__ == "__main__":
    main()
