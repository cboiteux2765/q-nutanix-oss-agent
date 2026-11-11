import argparse
from pathlib import Path

from spec_agent import SpecAgentRequest, WorkflowConfig, run_spec_agent
from spec_agent.services.llm_client import DemoLLM


def main():
    parser = argparse.ArgumentParser(description="Standalone specification agent demo")
    parser.add_argument("request", type=Path, help="Request JSON file")
    parser.add_argument("--workflow", type=Path, help="Override with workflow JSON or YAML")
    parser.add_argument("--output", type=Path, default=Path("output"))
    parser.add_argument("--demo", action="store_true", help="Use fake LLM; output is BLOCKED")
    args = parser.parse_args()
    request = SpecAgentRequest.model_validate_json(args.request.read_text(encoding="utf-8"))
    if args.workflow:
        request.workflow = WorkflowConfig.from_text(args.workflow.read_text(encoding="utf-8"))
    result = run_spec_agent(request, llm=DemoLLM() if args.demo else None)
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "product_spec.md").write_text(result.product_spec, encoding="utf-8")
    (args.output / "task_plan.json").write_text(result.task_plan_json, encoding="utf-8")
    (args.output / "result.json").write_text(result.model_dump_json(indent=2), encoding="utf-8")
    print(result.status)
    return 1 if result.status == "FAILED" else 0


if __name__ == "__main__":
    raise SystemExit(main())
