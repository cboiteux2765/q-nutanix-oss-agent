"""The `evo-code` CLI driven end to end, as an orchestrator or a human operator would."""
import json

import pytest
from typer.testing import CliRunner

from conftest import SCENARIOS
from evo_coding.cli import app
from evo_coding.pilot import load_scenario

pytestmark = pytest.mark.e2e
runner = CliRunner()


def invoke(*args):
    result = runner.invoke(app, [str(a) for a in args])
    return result.exit_code, result.output


def test_schema_export(tmp_path):
    code, out = invoke("schema", "--out", tmp_path)
    assert code == 0
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == ["coding-result-v0.1.schema.json", "handoff-v0.1.schema.json",
                     "revision-request-v0.1.schema.json"]
    handoff = json.loads((tmp_path / "handoff-v0.1.schema.json").read_text(encoding="utf-8"))
    assert {"run_id", "spec_hash", "source_commit", "scope"} <= set(handoff["required"])


def test_validate_exit_codes(pilot, tmp_path):
    code, _ = invoke("make-handoff", SCENARIOS / "01-median-even.toml", "--repo", pilot.remote, "--out", tmp_path / "h",
                     "--run-id", "cli-1")
    assert code == 0
    handoff = tmp_path / "h" / "handoff.json"
    assert invoke("validate", handoff)[0] == 0
    (tmp_path / "h" / "product_spec.md").write_text("tampered", encoding="utf-8")
    code, out = invoke("validate", handoff)
    assert code == 1 and "SPEC_HASH_MISMATCH" in out
    handoff.write_text("{not json", encoding="utf-8")
    code, out = invoke("validate", handoff)
    assert code == 2 and "invalid JSON" in out


def test_run_status_revise_cancel_flow(pilot, tmp_path):
    work = tmp_path / "work"
    scenario = SCENARIOS / "02-percentile.toml"
    invoke("make-handoff", scenario, "--repo", pilot.remote, "--out", tmp_path / "h", "--run-id", "cli-2")
    common = ["--work-dir", work, "--implementer", "scripted", "--script", scenario,
              "--publisher", "local", "--remote", pilot.remote]

    code, out = invoke("run", tmp_path / "h" / "handoff.json", *common)
    assert code == 0, out
    first = json.loads(out)
    assert first["status"] == "TESTING" and first["pr_number"] == 1

    code, out = invoke("test-agent-request", first["result"])
    assert code == 0 and json.loads(out)["pr_number"] == 1

    feedback = load_scenario(scenario)["feedback"]
    feedback.update(run_id="cli-2", tested_head_sha=first["pr_head_sha"])
    (tmp_path / "feedback.json").write_text(json.dumps(feedback), encoding="utf-8")
    revise_opts = [o for o in common if o not in ("--work-dir", work)]
    code, out = invoke("revise", "cli-2", tmp_path / "feedback.json", "--work-dir", work, *revise_opts)
    assert code == 0, out
    assert json.loads(out)["revision"] == 1

    code, out = invoke("status", "cli-2", "--work-dir", work)
    assert code == 0 and [r["status"] for r in json.loads(out)] == ["TESTING", "TESTING"]

    assert invoke("cancel", "cli-2", "--work-dir", work)[0] == 0
    feedback.update(tested_head_sha=json.loads(out)[1]["pr_head_sha"], findings=["again"])
    (tmp_path / "feedback.json").write_text(json.dumps(feedback), encoding="utf-8")
    code, out = invoke("revise", "cli-2", tmp_path / "feedback.json", "--work-dir", work, *revise_opts)
    assert code == 4 and json.loads(out)["status"] == "CANCELLED"


def test_revise_accepts_test_agent_verification(pilot, tmp_path):
    work = tmp_path / "work"
    scenario = SCENARIOS / "02-percentile.toml"
    invoke("make-handoff", scenario, "--repo", pilot.remote, "--out", tmp_path / "h", "--run-id", "cli-3")
    common = ["--implementer", "scripted", "--script", scenario, "--publisher", "local",
              "--remote", pilot.remote]
    first = json.loads(invoke("run", tmp_path / "h" / "handoff.json", "--work-dir", work, *common)[1])
    verification = {"run_id": "ta-1", "readiness": "CHANGES_REQUIRED", "reviewed_head_sha": first["pr_head_sha"],
                    "requirements": [{"id": "REQ-1", "test_ids": ["hidden::p100"], "result": "FAIL",
                                      "reason": "IndexError for p=100"}], "unresolved_findings": [], "reasons": []}
    (tmp_path / "verification.json").write_text(json.dumps(verification), encoding="utf-8")
    code, out = invoke("revise", "cli-3", tmp_path / "verification.json", "--work-dir", work, *common)
    assert code == 0, out


def test_blocked_run_exit_code(pilot, tmp_path):
    scenario = SCENARIOS / "05-out-of-scope.toml"
    invoke("make-handoff", scenario, "--repo", pilot.remote, "--out", tmp_path / "h", "--run-id", "cli-4")
    code, out = invoke("run", tmp_path / "h" / "handoff.json", "--work-dir", tmp_path / "w", "--implementer",
                       "scripted", "--script", scenario, "--publisher", "local", "--remote",
                       pilot.remote)
    assert code == 3 and json.loads(out)["status"] == "BLOCKED"


def test_demo_command(tmp_path):
    code, out = invoke("demo", "--out", tmp_path / "demo")
    assert code == 0, out
    lines = [line for line in out.splitlines() if line.startswith(("PASS", "FAIL"))]
    assert len(lines) == 6 and all(line.startswith("PASS") for line in lines)
    assert "r0:TESTING -> r1:TESTING" in out
    code, out = invoke("demo", "--out", tmp_path / "demo")
    assert code != 0, "existing demo output is never overwritten"
