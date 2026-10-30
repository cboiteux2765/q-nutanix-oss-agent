"""pi coding agent (https://pi.dev, npm `@earendil-works/pi-coding-agent`) as the implementer.

pi runs as a subprocess in the repository, in JSON mode with an ephemeral session and a throwaway config
directory (PI_CODING_AGENT_DIR) that defines a single OpenAI-compatible provider. Extensions, skills, MCP,
prompt templates and repository context files (AGENTS.md/CLAUDE.md) are disabled, so nothing from the
repository or the user's ~/.pi configuration is loaded. We read pi's JSONL event stream to charge the
budget per assistant message, count turns as steps, and kill the process on cancel or budget exhaustion.

pi executes its tools on the host, so this implementer supports the local workspace backend only.
"""
from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from ..config import BudgetTracker, CancelToken, ImplementerConfig, LLMConfig
from ..llm import pricing_for
from ..safety import redact
from ..workspace import LocalWorkspace, Workspace, WorkspaceError, kill_process_tree
from .base import (
    NOTES_PATH,
    PI_SYSTEM_APPEND,
    PROMPT_VERSION,
    ImplementationOutcome,
    Implementer,
    TaskBrief,
    build_task_prompt,
    read_notes,
)

PROVIDER = "evo"
KEY_ENV = "EVO_PI_API_KEY"
LOCKDOWN_FLAGS = ["--no-session", "-ne", "--no-mcp", "-ns", "-np", "--no-themes", "-nc"]


def resolve_pi_command(command: list[str]) -> list[str]:
    """On Windows npm installs pi as a .cmd shim; run its JavaScript entry point with node directly instead."""
    if len(command) != 1:
        return command
    found = shutil.which(command[0])
    if not found:
        raise RuntimeError(f"pi not found ({command[0]!r}); install @earendil-works/pi-coding-agent or set "
                           "[implementer].pi_command")
    if os.name == "nt" and found.lower().endswith((".cmd", ".bat")):
        base = Path(found).parent / "node_modules" / "@earendil-works" / "pi-coding-agent"
        for entry in (base / "dist" / "bundle" / "cli.js", base / "dist" / "cli.js"):
            if entry.is_file():
                return [shutil.which("node") or "node", str(entry)]
    return [found]


class PiImplementer(Implementer):
    name = "pi"
    prompt_version = PROMPT_VERSION

    def __init__(self, llm: LLMConfig, config: ImplementerConfig):
        if not llm.model:
            raise RuntimeError("no model configured: set [llm].model or EVO_LLM_MODEL")
        self.llm_config = llm
        self.config = config
        self.model = llm.model
        self.command = resolve_pi_command(list(config.pi_command))
        try:
            out = subprocess.run([*self.command, "--version"], capture_output=True, text=True, timeout=60,
                                 env={**os.environ, "PI_OFFLINE": "1", "PI_SKIP_VERSION_CHECK": "1"})
            self.version = (out.stdout or out.stderr).strip().splitlines()[-1] if out.returncode == 0 else "unknown"
        except (OSError, subprocess.TimeoutExpired, IndexError):
            self.version = "unknown"

    def _write_agent_dir(self, agent_dir: Path) -> Path:
        api = "openai-responses" if self.llm_config.api_style == "responses" else "openai-completions"
        model: dict[str, Any] = {"id": self.model, "reasoning": bool(self.llm_config.reasoning_effort)}
        price = pricing_for(self.llm_config)
        if price.input or price.output:
            model["cost"] = {"input": price.input, "output": price.output, "cacheRead": price.cached_input,
                             "cacheWrite": 0}
        models = {"providers": {PROVIDER: {"baseUrl": self.llm_config.base_url, "api": api,
                                           "apiKey": f"${KEY_ENV}", "models": [model]}}}
        (agent_dir / "models.json").write_text(json.dumps(models, indent=2), encoding="utf-8")
        system = agent_dir / "evo-system.md"
        system.write_text(PI_SYSTEM_APPEND, encoding="utf-8")
        return system

    def _thinking_args(self) -> list[str]:
        effort = self.llm_config.reasoning_effort
        if not effort:
            return []
        return ["--thinking", {"none": "off", "max": "xhigh"}.get(effort, effort)]

    def argv(self, system_prompt: Path) -> list[str]:
        return [*self.command, "--mode", "json", "--model", f"{PROVIDER}/{self.model}", *LOCKDOWN_FLAGS,
                "--append-system-prompt", str(system_prompt), *self._thinking_args(), *self.config.pi_extra_args,
                "Implement the task described above."]

    def implement(self, brief: TaskBrief, workspace: Workspace, budget: BudgetTracker,
                  cancel: CancelToken) -> ImplementationOutcome:
        if not isinstance(workspace, LocalWorkspace):
            raise WorkspaceError("the pi implementer runs its tools on the host and supports only the local "
                                 "workspace backend; use mini_swe, native or rlm with Docker")
        key = self.llm_config.api_key() or ""
        agent_dir = Path(tempfile.mkdtemp(prefix="evo-pi-"))
        try:
            system_prompt = self._write_agent_dir(agent_dir)
            env = {**workspace.host_env(), "PI_CODING_AGENT_DIR": str(agent_dir), "PI_OFFLINE": "1",
                   "PI_SKIP_VERSION_CHECK": "1", "PI_TELEMETRY": "0", KEY_ENV: key}
            stderr_path = workspace.root / ".evo-artifacts" / "pi.stderr.log"
            stderr_path.parent.mkdir(parents=True, exist_ok=True)
            task = build_task_prompt(brief, finish_instruction=f"When finished, write {NOTES_PATH} and stop.")
            with stderr_path.open("ab") as stderr:
                proc = subprocess.Popen(self.argv(system_prompt), cwd=workspace.root, env=env,
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr,
                                        encoding="utf-8", errors="replace", start_new_session=os.name == "posix")
            try:
                return self._supervise(proc, task, workspace, budget, cancel, [key])
            finally:
                kill_process_tree(proc)
        finally:
            shutil.rmtree(agent_dir, ignore_errors=True)

    def _supervise(self, proc: subprocess.Popen, task: str, workspace: Workspace, budget: BudgetTracker,
                   cancel: CancelToken, secrets: list[str]) -> ImplementationOutcome:
        proc.stdin.write(task)
        proc.stdin.close()
        lines: queue.Queue[str | None] = queue.Queue()

        def pump() -> None:
            for line in proc.stdout:
                lines.put(line)
            lines.put(None)

        threading.Thread(target=pump, daemon=True).start()
        transcript: list[dict[str, Any]] = []
        steps = 0
        last_text = ""
        error: str | None = None
        ended = False
        while True:
            cancel.raise_if_cancelled()
            budget.check()
            try:
                line = lines.get(timeout=0.5)
            except queue.Empty:
                continue
            if line is None:
                break
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = event.get("type")
            if kind == "message_end" and (event.get("message") or {}).get("role") == "assistant":
                message = event["message"]
                usage = message.get("usage") or {}
                # pi reports uncached input and cache reads separately.
                cached = int(usage.get("cacheRead") or 0)
                inp, out = int(usage.get("input") or 0) + cached, int(usage.get("output") or 0)
                reported = float((usage.get("cost") or {}).get("total") or 0.0)
                priced = pricing_for(self.llm_config).cost(inp, cached, out)
                budget.charge_llm(max(reported, priced), inp, out, cached)
                text = "".join(c.get("text", "") for c in message.get("content") or [] if c.get("type") == "text")
                last_text = text or last_text
                if message.get("stopReason") in ("error", "aborted"):
                    error = message.get("errorMessage") or f"pi stopped with {message.get('stopReason')}"
                transcript.append({"event": kind, "text": redact(text[:2000], secrets),
                                   "usage": {"input": inp, "output": out}, "stopReason": message.get("stopReason")})
            elif kind == "turn_end":
                steps += 1
                budget.charge_step()
                if steps >= self.config.step_limit:
                    return ImplementationOutcome("StepLimit", steps=steps, transcript=transcript,
                                                 error=f"step limit {self.config.step_limit} reached")
            elif kind in ("tool_execution_start", "tool_execution_end"):
                transcript.append({"event": kind, "tool": event.get("toolName"),
                                   "args": redact(json.dumps(event.get("args"))[:1000], secrets)
                                   if kind == "tool_execution_start" else None,
                                   "isError": event.get("isError")})
            elif kind == "agent_end":
                ended = True
            elif kind == "auto_retry_end" and event.get("success") is False:
                error = str(event.get("finalError"))
        deadline = time.monotonic() + 30
        while proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.1)
        returncode = proc.poll()
        if returncode != 0 or not ended or error:
            return ImplementationOutcome("Error", steps=steps, transcript=transcript,
                                         error=error or f"pi exited with {returncode} (agent_end seen: {ended})")
        summary, claims = read_notes(workspace.root)
        return ImplementationOutcome("Submitted", summary or last_text[:2000], claims, steps, transcript=transcript)


__all__ = ["PiImplementer", "resolve_pi_command"]
