"""Run a live main-agent -> Python REPL -> Test tools -> evidence smoke test.

Usage: uv run python scripts/smoke_test_stage.py --live --output .evo/test-stage-live
Only a newly created trusted fixture is executed. No remote repository writes,
agent delegation or human QA decisions are allowed in this diagnostic run.
"""
from __future__ import annotations

import argparse
import asyncio
from hashlib import sha256
import json
import os
from pathlib import Path
from uuid import uuid4

from evo.test_stage_demo import prepare_demo


PROMPT = """Perform a direct Test tool integration diagnostic on the already published,
host-approved fixture. This diagnostic overrides delegation: call the tools yourself.
Use only test.*, workflow.status, artifacts.read and shared.set; do not spawn agents,
edit files, run shell commands, advance the workflow or approve QA.
In Python REPL variables, get t = test.target(), p = test.preflight(t),
v = test.verify(t), m = test.manifest(v['run_id']), and
a = test.artifact(v['run_id'], 'initial/junit.xml'). These tools are synchronous.
Group these assignments in one Python cell to avoid spending one model step per
tool. All results are dictionaries. Check p['ready'], v['readiness'] equals
'READY_FOR_QA', v['tests']['passed'] equals 2, v['reviewed_head_sha'] equals
t['expected_head_sha'], m['manifest']['identity_hash'] equals v['identity_hash'],
and m['artifact_manifest_hash'] equals v['artifact_manifest_hash']. There is no
top-level m['identity_hash']; do not invent fields or prompt versions. Read the imported
main artifact via artifacts.read(v['main_artifact_id']). In a later Python cell,
inspect the existing v and a variables to prove persistence. Store a small summary
with shared.set('test-integration', ...). Finish by stating what you observed and
that human QA remains pending. Do not invent values or make changes.
"""


async def run(output: Path, model: str, max_steps: int = 12):
    if not 2 <= max_steps <= 16:
        raise ValueError('Smoke model steps must be between 2 and 16.')
    case = output / ("case-" + uuid4().hex[:8])
    case.mkdir(parents=True)
    runtime, store, agents, request = prepare_demo(case, settings={
        "base_url": os.getenv("EVO_BASE_URL", "https://api.openai.com/v1"),
        "api_style": os.getenv("EVO_API_STYLE", "chat_completions"), "model": model,
        "max_steps": max_steps, "max_output_tokens": 2500, "max_run_tokens": 100000})
    run_id, root_id = "test-tool-demo", agents["orchestrator"]["id"]
    store.put("runs", run_id, {"tool_policy": {"deny": [
        "agents.*", "repo.*", "shell.*", "github.*", "web.*", "memory.*", "skills.*",
        "workflow.advance", "workflow.handoff", "workflow.finding"]}})
    report = {"live_model": True, "model": model, "case": str(case), "passed": False,
              "max_model_steps": max_steps}
    try:
        runtime._enqueue(agents["orchestrator"], PROMPT, visible=True, user=True)
        runtime._schedule(root_id)
        async with asyncio.timeout(240):
            while runtime.tasks:
                await asyncio.gather(*list(runtime.tasks.values()))
        root = store.get("agents", root_id)
        events = store.events(run_id)
        tool_events = [e for e in events if e["kind"] == "tool.finished"]
        names = [e["data"].get("name") for e in tool_events]
        python_results = [e["data"].get("result", {}) for e in tool_events if e["data"].get("name") == "python"]
        report.update({"main_agent_status": root["status"], "main_agent_answer": root.get("result"),
            "tool_names": names, "main_token_usage": store.get("runs", run_id).get("token_usage"),
            "python_cells": len(python_results),
            "failed_python_cells": sum(not r.get("ok") for r in python_results),
            "pricing_note": "USD is unpriced unless provider pricing is configured.",
            "workflow_phase": store.get("runs", run_id)["phase"]})
        assert root["status"] == "waiting", root.get("error") or root.get("result") or root["status"]
        assert {"test.target", "test.preflight", "test.verify", "test.manifest", "test.artifact", "artifacts.read"} <= set(names), names
        assert len(python_results) >= 2 and all(r.get("ok") for r in python_results), python_results
        record = store.list("test_stage_results", run_id=run_id)[0]
        bundle = json.loads(runtime.artifacts.read(record["artifact_id"])["content"])
        result, manifest = bundle["verification"], bundle["evidence"]
        assert result["reviewed_head_sha"] == request.expected_head_sha
        assert manifest["artifact_manifest_hash"] == result["artifact_manifest_hash"]
        assert manifest["manifest"]["identity_hash"] == result["identity_hash"]
        assert result["readiness"] == "READY_FOR_QA" and result["tests"]["passed"] == 2
        assert result["human_approval_required"] and result["candidate_patch"] is None
        core = runtime.test_stage.tools
        for entry in manifest["manifest"]["evidence"]:
            artifact = core.artifact(result["run_id"], entry["path"])
            assert sha256(artifact.content_utf8.encode()).hexdigest() == entry["sha256"]
        assert core.manager.get_run(result["run_id"]).qa_review.status == "PENDING"
        assert report["workflow_phase"] == "test"
        report.update({"passed": True, "reviewed_head_sha": result["reviewed_head_sha"],
            "spec_hash": result["spec_hash"], "tests": result["tests"],
            "readiness": result["readiness"], "main_artifact_id": record["artifact_id"],
            "artifact_manifest_hash": result["artifact_manifest_hash"],
            "evidence_count": len(manifest["manifest"]["evidence"]), "human_qa": "PENDING"})
    except Exception as exc:
        report["error"] = str(exc) or type(exc).__name__
    finally:
        await runtime.close()
        store.close()
        (output / "live_results.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=True))
    return report["passed"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", required=True, help="Use the configured model API (billable).")
    parser.add_argument("--model", default=os.getenv("EVO_TEST_SMOKE_MODEL", "gpt-4.1-mini"))
    parser.add_argument("--output", type=Path, default=Path(".evo/test-stage-live"))
    parser.add_argument("--max-steps", type=int, choices=range(2, 17), default=12)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    raise SystemExit(0 if asyncio.run(run(args.output.resolve(), args.model, args.max_steps)) else 1)
