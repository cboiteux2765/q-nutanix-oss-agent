"""Mini-SWE-agent spec loop with local Ollama inference and no execution tools."""

import argparse
import json
import os
from pathlib import Path
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

os.environ["MSWEA_SILENT_STARTUP"] = "1"
try:
    from minisweagent.agents.default import DefaultAgent
except ImportError:
    raise SystemExit("Install requirements-mini.txt and run with .venv/Scripts/python.exe.")

from agent.generate import INSTRUCTIONS, SCHEMA, check_draft, prepare, publish_specification, write_artifacts


from agent.events import EventLog


class NoTools:
    def execute(self, action, **kwargs):
        raise ValueError("Spec agent has no execution tools.")

    def get_template_vars(self):
        return {}

    def serialize(self):
        return {"environment": "no-tools"}


class LocalModel:
    def __init__(self, model="qwen2.5:7b", replay=None, log=None):
        self.model = model
        self.replay = replay
        self.calls = 0
        self.schema = SCHEMA
        self.log = log or (lambda *args, **kwargs: None)

    def format_message(self, **kwargs):
        return kwargs

    def get_template_vars(self):
        return {}

    def serialize(self):
        return {"model": self.model, "backend": "replay" if self.replay is not None else "ollama-local"}

    def query(self, messages):
        self.calls += 1
        if self.replay is not None:
            self.log("replay.request", attempt=self.calls, message_count=len(messages))
            if self.calls > len(self.replay):
                raise ValueError("Replay exhausted before the agent completed.")
            response = {"role": "assistant", "content": json.dumps(self.replay[self.calls - 1])}
            self.log("replay.response", attempt=self.calls, source="recorded", response_chars=len(response["content"]))
            return response
        # Fixed loopback address: no cloud endpoint or API credentials are used.
        body = {"model": self.model, "stream": False, "format": self.schema,
                "messages": [{"role": m["role"], "content": m["content"]} for m in messages],
                "options": {"temperature": 0, "num_ctx": 16384, "num_predict": 6000}}
        # Conservative input bound leaves room for output and avoids silent context loss.
        if len(json.dumps(body["messages"]).encode()) > 24000:
            raise ValueError("Local context exceeds 24 KB. Select smaller evidence excerpts.")
        request = Request("http://127.0.0.1:11434/api/chat", data=json.dumps(body).encode(),
                          headers={"Content-Type": "application/json"})
        self.log("api.request", attempt=self.calls, endpoint=request.full_url, method="POST", model=self.model)
        started = time.monotonic()
        try:
            with urlopen(request, timeout=300) as response:
                status = getattr(response, "status", 200)
                result = json.load(response)
        except HTTPError as exc:
            self.log("api.error", attempt=self.calls, status=exc.code, endpoint=request.full_url)
            raise ValueError(f"Ollama HTTP {exc.code}. Check `ollama list` and pull the selected model.") from None
        except (URLError, TimeoutError):
            self.log("api.error", attempt=self.calls, endpoint=request.full_url, status=None, error="Connection failed or timed out")
            raise ValueError("Local Ollama unavailable or timed out. Start Ollama and check `ollama list`.") from None
        self.log("api.response", attempt=self.calls, elapsed_seconds=round(time.monotonic() - started, 3),
                 endpoint=request.full_url, status=status, input_tokens=result.get("prompt_eval_count"),
                 output_tokens=result.get("eval_count"), done_reason=result.get("done_reason"))
        if not result.get("done") or result.get("done_reason") != "stop":
            raise ValueError("Local model response was incomplete; no draft will be published.")
        return {"role": "assistant", "content": result["message"]["content"],
                "extra": {"cost": 0, "usage": {"input_tokens": result.get("prompt_eval_count"),
                                                "output_tokens": result.get("eval_count")}}}


class SpecAgent(DefaultAgent):
    """Reuse mini's bounded query/observation loop; replace shell actions with validation."""
    def __init__(self, model, sources):
        super().__init__(model, NoTools(), system_template=INSTRUCTIONS,
                         instance_template="{{task}}", step_limit=3, cost_limit=0,
                         wall_time_limit_seconds=900)
        self.sources = sources
        self.checks = []

    def execute_actions(self, message):
        self.model.log("validation.started", attempt=self.n_calls)
        try:
            draft = json.loads(message["content"])
            check_draft(draft, self.sources)
        except (ValueError, KeyError, TypeError) as exc:
            self.checks.append({"attempt": self.n_calls, "passed": False, "error": str(exc)})
            self.model.log("validation.failed", attempt=self.n_calls, error=str(exc))
            self.model.log("correction.requested" if self.n_calls < 3 else "correction.limit_reached",
                           attempt=self.n_calls, remaining=max(0, 3 - self.n_calls))
            print(f"Attempt {self.n_calls}/3: structural check failed: {exc}", file=sys.stderr)
            return self.add_messages({"role": "user", "content":
                f"Validation failed: {exc}. Correct the affected fields only; preserve other content. "
                "Return the complete corrected JSON draft. Do not invent missing facts."})
        self.checks.append({"attempt": self.n_calls, "passed": True})
        self.model.log("validation.passed", attempt=self.n_calls, scope="Structure and reference IDs only")
        return self.add_messages({"role": "exit", "content": "Draft structurally valid; human review required.",
                                  "extra": {"exit_status": "DRAFT_READY", "submission": json.dumps(draft)}})


def run_spec(run, allowed_repo, output, model, product=False, specifications=None):
    model.log("run.started", repo=allowed_repo, mode="replay" if model.replay is not None else "local",
              max_calls=4 if product else 3, tools=[])
    model.log("context.validation.started")
    payload = prepare(run, allowed_repo)
    model.log("context.ready", source_count=len(payload["sources"]), commit=payload["commit"])
    agent = SpecAgent(model, payload["sources"])
    started = time.monotonic()
    try:
        if product:
            from agent.product import analyze
            payload['product_analysis'] = analyze(payload, model)
            agent.config.system_template += ("\nUse the product_analysis as a provisional brief, not verified evidence. "
                "Preserve its unresolved product decisions. Requirements must explain user outcomes; acceptance tests "
                "must specify observable pass/fail behavior and relevant edge cases. Distinguish proposals from facts.")
        model.log("drafting.started", model=model.model)
        result = agent.run(json.dumps(payload))
        if result.get("exit_status") != "DRAFT_READY":
            raise ValueError("No valid draft after at most two corrections; inspect the failure trace.")
    except Exception as exc:
        model.log("run.failed", error=str(exc))
        output.mkdir(parents=True, exist_ok=True)
        failure = output / f"failed-mini-{uuid4()}.json"
        failure.write_text(json.dumps(agent.serialize(), indent=2), encoding="utf-8")
        print(f"Failure trace: {failure.resolve()}", file=sys.stderr)
        model.log("failure_trace.saved", path=str(failure.resolve()))
        raise
    draft = json.loads(result["submission"])
    if product:
        for item in payload['product_analysis']['open_decisions']:
            question = f"{item['question']} Owner: {item['owner']}. Next action: {item['next_action']}"
            if question not in draft['unresolved_questions']:
                draft['unresolved_questions'].append(question)
        model.log('product.readiness.checked', status='BLOCKED' if draft['unresolved_questions'] else 'SPEC_DRAFT',
                  open_questions=len(draft['unresolved_questions']), next_owner='product reviewer')
    metadata = {"mode": "offline-replay" if model.replay is not None else "live-local",
                "model": model.model, "agent": "mini-swe-agent", "agent_version": "2.4.6",
                "workflow_version": "product-discovery-1" if product else "draft-1", "model_calls": model.calls,
                "correction_attempts": max(0, agent.n_calls - 1), "checks": agent.checks,
                "elapsed_seconds": round(time.monotonic() - started, 3), "llm_cost_usd": 0,
                "cost_note": "No hosted API charges; local compute cost not measured.",
                "usage": [m.get("extra", {}).get("usage") for m in agent.messages if m["role"] == "assistant"]}
    model.log("artifacts.writing", requirements=len(draft["requirements"]),
              acceptance_tests=len(draft["acceptance_tests"]), open_questions=len(draft["unresolved_questions"]))
    directory = write_artifacts(run, payload, draft, metadata, output)
    published = publish_specification(directory, run["input"], specifications) if specifications else None
    if published:
        model.log("specification.published", path=str(published), issue_number=run["input"]["issue_number"],
                  repo=run["input"]["repo"])
    clearance = None
    if product:
        from agent.review import auto_clear
        clearance = auto_clear(directory)
        model.log('review.automatic' if clearance['status'] == 'AUTO_CLEARED' else 'review.required',
                  status=clearance['status'], reasons=clearance['reasons'])
        trace_path = directory / 'trace.json'
        trace = json.loads(trace_path.read_text(encoding='utf-8'))
        trace['review'] = clearance
        trace_path.write_text(json.dumps(trace, indent=2) + '\n', encoding='utf-8')
    (directory / "agent_trace.json").write_text(json.dumps(agent.serialize(), indent=2), encoding="utf-8")
    model.log("artifacts.saved", directory=str(directory), files=sorted(p.name for p in directory.iterdir()))
    model.log("run.completed", status=('BLOCKED' if draft['unresolved_questions'] else 'SPEC_DRAFT') if product else 'DRAFT_GENERATED', correction_attempts=max(0, agent.n_calls - 1),
              review=clearance['status'] if clearance else 'Manual review available')
    return directory


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    parser.add_argument("--allowed-repo", required=True)
    parser.add_argument("--model", default="qwen2.5:7b")
    parser.add_argument("--product", action="store_true", help="Analyze users, outcomes, sources and decisions before drafting")
    parser.add_argument("--source", type=Path, action="append", default=[], help="Additional UTF-8 product document (up to 3)")
    parser.add_argument("--replay", type=Path, help="Use recorded synthetic responses; no inference")
    parser.add_argument("--output", type=Path, default=Path("runs"))
    parser.add_argument("--specifications", type=Path, default=Path("specifications"),
                        help="Folder containing a readable Markdown copy of every generated spec")
    parser.add_argument("--log", type=Path, help="Append events to this JSONL file; default: unique file in runs")
    parser.add_argument("--verbose", action="store_true", help="Show structured event metadata on stderr (no payloads)")
    args = parser.parse_args()
    log = EventLog(args.log or args.output / f"events-{uuid4()}.jsonl", args.verbose)
    try:
        log("monitor.started", log_path=str(log.path.resolve()))
        replay = json.loads(args.replay.read_text(encoding="utf-8")) if args.replay else None
        if replay is not None and (not isinstance(replay, list) or not replay):
            raise ValueError("Replay must be a nonempty JSON array of draft objects.")
        run = json.loads(args.run.read_text(encoding="utf-8"))
        if args.source:
            from agent.product import add_documents
            run = add_documents(run, args.source)
            log('product.sources.loaded', documents=len(args.source))
        print(run_spec(run, args.allowed_repo, args.output, LocalModel(args.model, replay, log),
                       args.product, args.specifications))
    except (ValueError, OSError, KeyError, TypeError) as exc:
        log("command.failed", error=str(exc))
        print(f"Mini spec generation failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
