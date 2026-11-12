"""Real seeded pilot replay and independent-review gate failure cases."""
import asyncio
from copy import deepcopy
import json

import pytest
from pydantic import ValidationError

from benchmark_pilot import (IndependentReview, audit_case, percentile, pilot_cases,
                             render_report, rescore, run_benchmark, score_results)
from app.providers import SourceRegistry
from app.run_manager import RunManager


@pytest.fixture(scope="module")
def replay(tmp_path_factory):
    directory = tmp_path_factory.mktemp("benchmark") / "pilot"
    return directory, asyncio.run(run_benchmark(directory))


def reviewed(row, **updates):
    return IndependentReview(**{**dict(case_id=row["case_id"], run_id=row["run_id"],
        identity_hash=row["identity_hash"], artifact_manifest_hash=row["artifact_manifest_hash"],
        reviewer="Independent engineer", independent=True,
        classification=row["classification"], rationale="Inspected source snapshots and JUnit evidence."), **updates})


def test_fifteen_real_snapshots_cover_six_categories_and_all_seeded_goals(replay):
    directory, summary = replay
    assert len(pilot_cases()) == summary["case_count"] == 15
    assert len(summary["categories"]) == 6
    assert min(summary["categories"].values()) >= 2
    assert summary["TGC"] == summary["seed_rca_accuracy"] == 1
    assert summary["observed_integrity_violations"] == 0
    assert summary["gates"]["requirement_mapping"]["correct"] == 30
    assert summary["gates"]["rca_independent_review"]["status"] == "PENDING_REVIEW"
    assert not summary["all_pilot_gates_passed"]
    for row in summary["cases"]:
        assert len(row["base_sha"]) == len(row["reviewed_head_sha"]) == 40
        assert row["base_sha"] != row["reviewed_head_sha"]
        assert (directory / row["evidence_manifest"]).is_file()
        assert row["evidence_count"] >= 1


def test_blockers_never_test_or_patch_and_budget_stops_after_two_attempts(replay):
    for row in replay[1]["cases"]:
        if row["blocker"]:
            assert row["blocker_ok"] and not row["patch_proposed"]
            assert row["metrics"]["test_executions"] == row["metrics"]["debug_iterations"] == 0
        if row["category"] == "budget_exhaustion":
            assert row["budget_ok"] and row["metrics"]["debug_iterations"] == 2
            assert row["metrics"]["test_executions"] == 4
        if row["category"] == "code_regression":
            assert row["patch_proposed"] and row["patch_valid"]
            assert row["readiness"] == "CHANGES_REQUIRED"
            assert row["original_tests"]["failed"] == 1
        if row["category"] == "flaky_test":
            assert row["classification"] == "TEST_FLAKY" and not row["patch_proposed"]


def test_reports_define_denominators_and_exclude_candidate_test_inflation(replay):
    directory, summary = replay
    assert summary["APT"] == sum(r["original_tests"]["passed"] for r in summary["cases"]) / 15
    assert summary["performance"]["model_spend_usd"]["total"] == 0
    assert summary["performance"]["runtime_seconds"]["p95"] >= summary["performance"]["runtime_seconds"]["p50"]
    report = (directory / "benchmark_report.md").read_text(encoding="utf-8")
    assert "PENDING_REVIEW" in report and "synthetic" in report and "Candidate/base passes excluded" in report
    assert json.loads((directory / "benchmark_results.json").read_text())["case_count"] == 15
    template = json.loads((directory / "independent_review_template.json").read_text())
    assert all(r["reviewer"] == "" and r["classification"] is None for r in template)


def test_independent_review_threshold_requires_complete_coverage(replay):
    rows = replay[1]["cases"]
    assert score_results(rows, [reviewed(r) for r in rows])["all_pilot_gates_passed"]
    incomplete = score_results(rows, [reviewed(rows[0])])
    assert incomplete["gates"]["rca_independent_review"]["status"] == "PENDING_REVIEW"
    failures = [reviewed(r, classification="UNKNOWN") if i < 4 else reviewed(r) for i, r in enumerate(rows)]
    assert score_results(rows, failures)["gates"]["rca_independent_review"]["status"] == "FAIL"
    boundary = [reviewed(r, classification="UNKNOWN") if i < 3 else reviewed(r) for i, r in enumerate(rows)]
    assert score_results(rows, boundary)["gates"]["rca_independent_review"]["status"] == "PASS"


@pytest.mark.parametrize("updates", [{"identity_hash": "a" * 64}, {"artifact_manifest_hash": "b" * 64},
    {"run_id": "wrong"}, {"case_id": "unknown"}, {"reviewer": "benchmark-seeder"}, {"reviewer": " "}])
def test_unbound_or_nonindependent_reviews_are_rejected(replay, updates):
    row = replay[1]["cases"][0]
    with pytest.raises(ValueError):
        score_results([row], [reviewed(row, **updates)])


def test_duplicate_and_missing_review_fields_are_rejected(replay):
    row = replay[1]["cases"][0]
    with pytest.raises(ValueError, match="duplicate"):
        score_results([row], [reviewed(row), reviewed(row)])
    with pytest.raises(ValidationError):
        reviewed(row, independent=False)
    with pytest.raises(ValueError):
        score_results([])
    with pytest.raises(ValueError):
        score_results([row, row])


def test_metric_thresholds_do_not_round_failures_into_passes(replay):
    rows = deepcopy(replay[1]["cases"])
    rows[0]["mapping_correct"] = rows[1]["mapping_correct"] = 0
    rows[0]["integrity_ok"] = False
    result = score_results(rows)
    assert result["gates"]["requirement_mapping"]["status"] == "FAIL"
    assert result["gates"]["version_evidence_integrity"]["status"] == "FAIL"
    assert "FAIL" in render_report(result)


def test_percentiles_small_samples_and_invalid_values():
    assert percentile([], .95) is None
    assert percentile([3], .95) == 3
    assert percentile([4, 1, 3, 2], .5) == 2.5
    assert percentile([0, 10], .95) == 9.5
    for values, quantile in [([1], 2), ([float("nan")], .5), ([-1], .5)]:
        with pytest.raises(ValueError):
            percentile(values, quantile)


def test_rescore_reaudits_evidence(tmp_path):
    directory = tmp_path / "replay"
    summary = asyncio.run(run_benchmark(directory, [pilot_cases()[0]]))
    row = summary["cases"][0]
    reviews_path = tmp_path / "reviews.json"
    reviews_path.write_text(json.dumps([reviewed(row).model_dump()]), encoding="utf-8")
    assert not rescore(directory, reviews_path)["all_pilot_gates_passed"]
    manager = RunManager(directory / row["case_id"] / "state", SourceRegistry(directory / row["case_id"] / "sources"))
    evidence = manager.artifact_path(row["run_id"], "initial/pytest.log")
    evidence.write_text("forged", encoding="utf-8")
    damaged = rescore(directory, reviews_path)
    assert damaged["gates"]["version_evidence_integrity"]["status"] == "FAIL"
    assert damaged["cases"][0]["integrity_errors"]


def test_malformed_evidence_is_not_a_pass(tmp_path):
    directory = tmp_path / "replay"
    summary = asyncio.run(run_benchmark(directory, [pilot_cases()[0]]))
    row = summary["cases"][0]
    manager = RunManager(directory / row["case_id"] / "state", SourceRegistry(directory / row["case_id"] / "sources"))
    run = manager.get_run(row["run_id"])
    manager.artifact_path(run.run_id, "initial/junit.xml").write_text("broken XML", encoding="utf-8")
    assert not audit_case(pilot_cases()[0], manager, run, 0)["integrity_ok"]
    with pytest.raises(FileExistsError):
        asyncio.run(run_benchmark(directory))


def test_rescore_preserves_unicode_reviews_under_non_utf8_locale(tmp_path, monkeypatch):
    from pathlib import Path
    directory = tmp_path / "unicode-replay"
    summary = asyncio.run(run_benchmark(directory, [pilot_cases()[0]]))
    review = reviewed(summary["cases"][0], reviewer="独立审核员",
                      rationale="已独立检查源代码快照与测试执行证据。")
    reviews = tmp_path / "reviews.json"
    reviews.write_text(json.dumps([review.model_dump()], ensure_ascii=False), encoding="utf-8")
    rescore(directory, reviews)
    original_read = Path.read_text

    def non_utf8_default(path, encoding=None, errors=None):
        return original_read(path, encoding=encoding or "cp1252", errors=errors)

    monkeypatch.setattr(Path, "read_text", non_utf8_default)
    rescored = rescore(directory, reviews)
    assert rescored["independent_reviews"][0]["reviewer"] == review.reviewer
