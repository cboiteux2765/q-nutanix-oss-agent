"""Bash-tool agent loops: a dependency-free native loop and mini-SWE-agent.

Both run every command through our Workspace (local or Docker), so the secret-stripped environment,
timeouts, budgets and cancellation stay ours.

- NativeImplementer: our own OpenAICompatibleClient, no LLM library.
- MiniSweImplementer: mini-SWE-agent's agent loop + litellm model (`mini-swe` extra; litellm pinned).
"""
from __future__ import annotations

import json
import os
import platform
from typing import Any

from ..config import BudgetTracker, CancelToken, ImplementerConfig, LLMConfig
from ..llm import OpenAICompatibleClient, pricing_for, usage_tokens
from ..safety import redact
from ..workspace import Workspace
from .base import (
    COMPLETION_MARKER,
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    ImplementationOutcome,
    Implementer,
    TaskBrief,
    build_task_prompt,
    read_notes,
)

BASH_TOOL = {
    "type": "function",
    "function": {
        "name": "bash",
        "description": "Execute one bash command in the repository root and return its output",
        "parameters": {
            "type": "object",
            "properties": {"command": {"type": "string", "description": "The bash command to execute"}},
            "required": ["command"],
        },
    },
}
MAX_FORMAT_ERRORS = 3


def format_observation(returncode: int, output: str, limit: int) -> str:
    if len(output) > limit:
        half = limit // 2
        output = (f"{output[:half]}\n[... {len(output) - limit} characters elided; use head/tail/grep to see "
                  f"less output ...]\n{output[-half:]}")
    return f"<returncode>{returncode}</returncode>\n<output>\n{output}\n</output>"


class NativeImplementer(Implementer):
    name = "native-bash-loop"
    version = "1"
    prompt_version = PROMPT_VERSION

    def __init__(self, llm: LLMConfig, config: ImplementerConfig, client: OpenAICompatibleClient | None = None):
        self.llm_config = llm
        self.config = config
        self.client = client or OpenAICompatibleClient(llm)
        self.model = llm.model

    def close(self) -> None:
        self.client.close()

    def implement(self, brief: TaskBrief, workspace: Workspace, budget: BudgetTracker,
                  cancel: CancelToken) -> ImplementationOutcome:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_task_prompt(brief)},
        ]
        secrets = [self.llm_config.api_key() or ""]
        transcript: list[dict[str, Any]] = []
        format_errors = 0
        steps = 0
        while steps < self.config.step_limit:
            cancel.raise_if_cancelled()
            budget.check()
            response = self.client.complete(messages, [BASH_TOOL])
            response.charge(budget)
            steps += 1
            budget.charge_step()
            transcript.append({"step": steps, "assistant": response.text, "tool_calls": [
                {"id": c.id, "name": c.name, "arguments": c.arguments} for c in response.tool_calls],
                "usage": {"input_tokens": response.input_tokens, "output_tokens": response.output_tokens,
                          "cached_tokens": response.cached_tokens, "reasoning_tokens": response.reasoning_tokens,
                          "cost_usd": response.cost_usd}})
            if not response.tool_calls:
                format_errors += 1
                if format_errors >= MAX_FORMAT_ERRORS:
                    return ImplementationOutcome("FormatError", steps=steps, transcript=transcript,
                                                 error="model repeatedly answered without a bash tool call")
                messages.append(response.assistant_message())
                messages.append({"role": "user", "content": "Format error: every response must call the bash "
                                 "tool exactly once. Try again."})
                continue
            format_errors = 0
            messages.append(response.assistant_message())
            submitted: str | None = None
            for call in response.tool_calls:
                if submitted is not None:
                    observation = "not executed: task already submitted"
                else:
                    try:
                        command = call.parsed_arguments()["command"]
                        if call.name != "bash":
                            raise ValueError(f"unknown tool {call.name!r}")
                    except (ValueError, KeyError, json.JSONDecodeError) as exc:
                        observation = f"Format error in tool call: {exc}"
                    else:
                        cancel.raise_if_cancelled()
                        result = workspace.run(command, timeout=min(self.config.command_timeout_seconds,
                                                                    max(1.0, budget.remaining_seconds)))
                        lines = result.output.lstrip().splitlines()
                        if lines and lines[0].strip() == COMPLETION_MARKER and result.returncode == 0:
                            submitted = "\n".join(lines[1:])
                        observation = format_observation(result.returncode, redact(result.output, secrets),
                                                         self.config.max_observation_chars)
                        transcript[-1].setdefault("observations", []).append(
                            {"command": command, "returncode": result.returncode,
                             "output": redact(result.output[-4000:], secrets)})
                messages.append({"role": "tool", "tool_call_id": call.id, "content": observation})
            if submitted is not None:
                summary, claims = read_notes(workspace.root, submitted)
                return ImplementationOutcome("Submitted", summary, claims, steps, transcript=transcript)
        return ImplementationOutcome("StepLimit", steps=steps, transcript=transcript,
                                     error=f"step limit {self.config.step_limit} reached before submission")


def _import_minisweagent():
    os.environ.setdefault("MSWEA_SILENT_STARTUP", "1")
    os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    # PRD: retry transient tool failures once (mini-SWE-agent's default is 10 attempts with backoff).
    os.environ.setdefault("MSWEA_MODEL_RETRY_STOP_AFTER_ATTEMPT", "2")
    try:
        from minisweagent import __version__
        from minisweagent.agents.default import DefaultAgent
        from minisweagent.exceptions import Submitted
        from minisweagent.models.litellm_model import LitellmModel
        from minisweagent.models.litellm_response_model import LitellmResponseModel
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise RuntimeError("mini-swe-agent is not installed; install with `uv sync --extra mini-swe`") from exc
    return __version__, DefaultAgent, Submitted, LitellmModel, LitellmResponseModel


class WorkspaceEnvironment:
    """mini-SWE-agent Environment protocol backed by an EVO Workspace."""

    def __init__(self, workspace: Workspace, timeout: int, submitted_exc, secrets: list[str]):
        self.workspace = workspace
        self.timeout = timeout
        self._submitted = submitted_exc
        self._secrets = secrets
        self.log: list[dict[str, Any]] = []

    def execute(self, action: dict, cwd: str = "", *, timeout: int | None = None) -> dict[str, Any]:
        command = action.get("command", "")
        result = self.workspace.run(command, timeout=timeout or self.timeout)
        output = redact(result.output, self._secrets)
        self.log.append({"command": command, "returncode": result.returncode, "output": output[-4000:]})
        payload = {"output": output, "returncode": result.returncode,
                   "exception_info": "command timed out" if result.timed_out else ""}
        lines = output.lstrip().splitlines(keepends=True)
        if lines and lines[0].strip() == COMPLETION_MARKER and result.returncode == 0:
            submission = "".join(lines[1:])
            raise self._submitted({"role": "exit", "content": submission,
                                   "extra": {"exit_status": "Submitted", "submission": submission}})
        return payload

    def get_template_vars(self, **kwargs) -> dict[str, Any]:
        return {"cwd": self.workspace.display_root, **platform.uname()._asdict(), **kwargs}

    def serialize(self) -> dict:
        return {"info": {"config": {"environment_type": f"evo:{self.workspace.backend}"}}}


class MiniSweImplementer(Implementer):
    name = "mini-swe-agent"
    prompt_version = PROMPT_VERSION

    def __init__(self, llm: LLMConfig, config: ImplementerConfig):
        if not llm.model:
            raise RuntimeError("no model configured: set [llm].model or EVO_LLM_MODEL")
        self.llm_config = llm
        self.config = config
        self.model = llm.model
        version, *_ = _import_minisweagent()
        self.version = version

    def _make_model(self, LitellmModel, LitellmResponseModel):
        kwargs: dict[str, Any] = {
            "api_base": self.llm_config.base_url,
            "api_key": self.llm_config.api_key() or "",
            "timeout": self.llm_config.request_timeout_seconds,
            "drop_params": True,
            "num_retries": 1,
        }
        if self.llm_config.temperature is not None:
            kwargs["temperature"] = self.llm_config.temperature
        effort = self.llm_config.reasoning_effort
        if self.llm_config.api_style == "responses":
            # mini-SWE replays output items statelessly; encrypted reasoning keeps the chain of thought intact.
            kwargs["store"] = False
            if effort:
                kwargs["reasoning"] = {"effort": effort}
                kwargs["include"] = ["reasoning.encrypted_content"]
        elif effort:
            kwargs["reasoning_effort"] = effort
        name = self.model if "/" in self.model else f"openai/{self.model}"
        cls = LitellmResponseModel if self.llm_config.api_style == "responses" else LitellmModel
        return cls(model_name=name, model_kwargs=kwargs, cost_tracking="ignore_errors",
                   observation_template="{{ evo_observation(output) }}")

    def implement(self, brief: TaskBrief, workspace: Workspace, budget: BudgetTracker,
                  cancel: CancelToken) -> ImplementationOutcome:
        _, DefaultAgent, Submitted, LitellmModel, LitellmResponseModel = _import_minisweagent()
        pricing = pricing_for(self.llm_config)
        max_obs = self.config.max_observation_chars

        class BudgetedAgent(DefaultAgent):
            """Charges our budget per call (token-priced if configured) and checks cancellation."""

            def query(self):
                cancel.raise_if_cancelled()
                budget.check()
                message = super().query()
                # Chat models keep the raw response under extra; Responses-API messages are the response itself.
                usage = ((message.get("extra", {}).get("response") or {}).get("usage") or message.get("usage")
                         or {})
                inp, out, cached, reasoning = usage_tokens(usage)
                priced = pricing.cost(inp, cached, out)
                budget.charge_llm(max(priced, message.get("extra", {}).get("cost", 0.0)), inp, out, cached,
                                  reasoning)
                budget.charge_step()
                return message

            def get_template_vars(self, **kwargs) -> dict:
                variables = super().get_template_vars(**kwargs)
                variables["evo_observation"] = lambda o: format_observation(
                    o.get("returncode", -1), (o.get("exception_info") or "") + o.get("output", ""), max_obs)
                return variables

        env = WorkspaceEnvironment(workspace, self.config.command_timeout_seconds, Submitted,
                                   [self.llm_config.api_key() or ""])
        agent = BudgetedAgent(
            self._make_model(LitellmModel, LitellmResponseModel),
            env,
            system_template="{{ evo_system_prompt }}",
            instance_template="{{ task }}",
            step_limit=min(self.config.step_limit, budget.remaining_steps or 1),
            cost_limit=0,
            wall_time_limit_seconds=int(budget.remaining_seconds) or 1,
        )
        result = agent.run(task=build_task_prompt(brief), evo_system_prompt=SYSTEM_PROMPT)
        exit_status = result.get("exit_status", "Error")
        transcript = [{k: v for k, v in m.items() if k != "extra"} for m in agent.messages]
        if exit_status == "Submitted":
            summary, claims = read_notes(workspace.root, result.get("submission", ""))
            return ImplementationOutcome("Submitted", summary, claims, agent.n_calls, transcript=transcript)
        mapped = {"LimitsExceeded": "StepLimit", "TimeExceeded": "StepLimit",
                  "RepeatedFormatError": "FormatError"}.get(exit_status, "Error")
        return ImplementationOutcome(mapped, steps=agent.n_calls, transcript=transcript,
                                     error=result.get("exception_str") or exit_status)
