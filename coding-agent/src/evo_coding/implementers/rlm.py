"""Recursive Language Model (RLM) implementer.

Follows the RLM design (Zhang & Khattab, https://github.com/alexzhang13/rlm): the root model does not get the
full task in its prompt. It works through a persistent Python REPL where the task, spec and file list live in
a `context` variable, and it can delegate reading and analysis to sub-model calls via `llm_query()`.

This is a native implementation, not the `rlms` package: the REPL runs inside our Workspace (host or Docker
sandbox), and sub-calls are proxied back to the host, so the sandbox never holds API keys and every
sub-call is charged to the run budget. Recursion depth is 1 (sub-calls are plain model calls).
"""
from __future__ import annotations

import json
import queue
import threading
from importlib import resources
from typing import Any

from ..config import BudgetTracker, CancelToken, ImplementerConfig, LLMConfig
from ..llm import LLMError, OpenAICompatibleClient
from ..safety import redact
from ..workspace import Workspace, kill_process_tree
from .base import (
    PROMPT_VERSION,
    RLM_SYSTEM_PROMPT,
    ImplementationOutcome,
    Implementer,
    TaskBrief,
    build_rlm_root_prompt,
    build_task_prompt,
    read_notes,
)
from .bash_loop import format_observation

PYTHON_TOOL = {
    "type": "function",
    "function": {
        "name": "python",
        "description": "Execute Python code in the persistent REPL (cwd = repository root) and return its output",
        "parameters": {
            "type": "object",
            "properties": {"code": {"type": "string", "description": "Python code to execute"}},
            "required": ["code"],
        },
    },
}
REPL_SCRIPT = ".evo-artifacts/rlm_repl.py"
CONTEXT_FILE = ".evo-artifacts/rlm_context.json"
MAX_FORMAT_ERRORS = 3


class ReplTimeout(Exception):
    pass


class ReplSession:
    """One REPL process. Sub-model requests from the REPL are answered by `on_llm_query`."""

    def __init__(self, workspace: Workspace, stderr_path, on_llm_query, timeout: float):
        self.proc = workspace.spawn_python(REPL_SCRIPT, stderr_path)
        self.on_llm_query = on_llm_query
        self.timeout = timeout
        self.lines: queue.Queue[str | None] = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()
        ready = self._next()
        if ready.get("type") != "ready":
            raise RuntimeError(f"REPL failed to start: {ready}")

    def _pump(self) -> None:
        for line in self.proc.stdout:
            self.lines.put(line)
        self.lines.put(None)

    def _next(self) -> dict[str, Any]:
        try:
            line = self.lines.get(timeout=self.timeout)
        except queue.Empty as exc:
            raise ReplTimeout(f"REPL produced no output for {self.timeout:.0f}s") from exc
        if line is None:
            raise RuntimeError(f"REPL exited with code {self.proc.wait()}")
        return json.loads(line)

    def _send(self, message: dict[str, Any]) -> None:
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()

    def execute(self, code: str) -> dict[str, Any]:
        self._send({"type": "exec", "code": code})
        while True:
            message = self._next()
            if message.get("type") == "llm_query":
                try:
                    reply = {"type": "llm_result", "texts": self.on_llm_query(message.get("prompts", []))}
                except (LLMError, SubcallLimit) as exc:
                    reply = {"type": "llm_result", "error": str(exc)}
                self._send(reply)
            elif message.get("type") == "result":
                return message

    def close(self) -> None:
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        kill_process_tree(self.proc)


class SubcallLimit(Exception):
    pass


class RLMImplementer(Implementer):
    name = "rlm"
    version = "1"
    prompt_version = PROMPT_VERSION

    def __init__(self, llm: LLMConfig, config: ImplementerConfig, client: OpenAICompatibleClient | None = None,
                 sub_client: OpenAICompatibleClient | None = None):
        self.llm_config = llm
        self.config = config
        self.client = client or OpenAICompatibleClient(llm)
        self.sub_client = sub_client or (OpenAICompatibleClient(llm, model=config.rlm_sub_model)
                                         if config.rlm_sub_model else self.client)
        self.model = llm.model
        self.sub_model = config.rlm_sub_model or llm.model

    def close(self) -> None:
        self.client.close()
        if self.sub_client is not self.client:
            self.sub_client.close()

    def _write_repl_files(self, brief: TaskBrief, workspace: Workspace) -> None:
        artifacts = workspace.root / ".evo-artifacts"
        artifacts.mkdir(parents=True, exist_ok=True)
        source = resources.files("evo_coding.implementers").joinpath("rlm_repl.py").read_text(encoding="utf-8")
        (workspace.root / REPL_SCRIPT).write_text(source, encoding="utf-8", newline="\n")
        context = {
            "task": build_task_prompt(brief, finish_instruction="When finished, call submit(summary, criteria)."),
            "spec": brief.spec_text,
            "criteria": [c.model_dump() for c in brief.criteria],
            "scope": brief.scope.model_dump(),
            "test_command": brief.test_command,
        }
        (workspace.root / CONTEXT_FILE).write_text(json.dumps(context, indent=2), encoding="utf-8")

    def implement(self, brief: TaskBrief, workspace: Workspace, budget: BudgetTracker,
                  cancel: CancelToken) -> ImplementationOutcome:
        self._write_repl_files(brief, workspace)
        secrets = [self.llm_config.api_key() or ""]
        transcript: list[dict[str, Any]] = []
        subcalls = {"count": 0}

        def on_llm_query(prompts: list[str]) -> list[str]:
            texts = []
            for prompt in prompts:
                cancel.raise_if_cancelled()
                budget.check()
                if subcalls["count"] >= self.config.rlm_max_subcalls:
                    raise SubcallLimit(f"sub-call limit {self.config.rlm_max_subcalls} reached")
                subcalls["count"] += 1
                response = self.sub_client.complete([{"role": "user", "content": prompt}], [])
                response.charge(budget)
                transcript.append({"subcall": subcalls["count"], "model": self.sub_model,
                                   "prompt": redact(prompt[:2000], secrets), "response": response.text[:2000]})
                texts.append(response.text)
            return texts

        stderr_path = workspace.root / ".evo-artifacts" / "rlm_repl.stderr.log"
        timeout = float(self.config.command_timeout_seconds)
        session = ReplSession(workspace, stderr_path, on_llm_query, timeout)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": RLM_SYSTEM_PROMPT},
            {"role": "user", "content": build_rlm_root_prompt(brief)},
        ]
        steps = format_errors = 0
        try:
            while steps < self.config.step_limit:
                cancel.raise_if_cancelled()
                budget.check()
                response = self.client.complete(messages, [PYTHON_TOOL])
                response.charge(budget)
                steps += 1
                budget.charge_step()
                entry: dict[str, Any] = {"step": steps, "assistant": response.text, "tool_calls": [
                    {"id": c.id, "name": c.name, "arguments": c.arguments} for c in response.tool_calls]}
                transcript.append(entry)
                if not response.tool_calls:
                    format_errors += 1
                    if format_errors >= MAX_FORMAT_ERRORS:
                        return ImplementationOutcome("FormatError", steps=steps, transcript=transcript,
                                                     error="model repeatedly answered without a python tool call")
                    messages.append(response.assistant_message())
                    messages.append({"role": "user", "content": "Format error: call the python tool with code. "
                                     "Call submit(summary, criteria) in the REPL when you are done."})
                    continue
                format_errors = 0
                messages.append(response.assistant_message())
                submitted = False
                for call in response.tool_calls:
                    if submitted:
                        observation = "not executed: task already submitted"
                    else:
                        try:
                            code = call.parsed_arguments()["code"]
                            if call.name != "python":
                                raise ValueError(f"unknown tool {call.name!r}")
                        except (ValueError, KeyError) as exc:
                            observation = f"Format error in tool call: {exc}"
                        else:
                            try:
                                result = session.execute(code)
                                output, submitted = result.get("output", ""), bool(result.get("submitted"))
                            except ReplTimeout as exc:
                                session.close()
                                session = ReplSession(workspace, stderr_path, on_llm_query, timeout)
                                output = f"{exc}; the REPL was restarted and all variables were lost."
                            observation = format_observation(0, redact(output, secrets),
                                                             self.config.max_observation_chars)
                            entry.setdefault("observations", []).append(redact(output[-4000:], secrets))
                    messages.append({"role": "tool", "tool_call_id": call.id, "content": observation})
                if submitted:
                    summary, claims = read_notes(workspace.root)
                    return ImplementationOutcome("Submitted", summary, claims, steps, transcript=transcript)
            return ImplementationOutcome("StepLimit", steps=steps, transcript=transcript,
                                         error=f"step limit {self.config.step_limit} reached before submit()")
        finally:
            session.close()

