"""Operator configuration (TOML + environment), run budgets and cooperative cancellation.

Operator caps are hard ceilings: a handoff may lower budgets, never raise them.
"""
from __future__ import annotations

import json
import os
import threading
import time
import tomllib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .models import BudgetLimits, BudgetUsed


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WorkspaceConfig(_Section):
    backend: Literal["local", "docker"] = "local"
    command_timeout_seconds: int = Field(default=300, ge=1)
    docker_image: str = "evo-coding-sandbox:py312"
    docker_network_during_setup: bool = False
    docker_user: str | None = None
    docker_memory: str = "2g"
    docker_cpus: str = "2"
    docker_pids_limit: int = 256


class LLMConfig(_Section):
    base_url: str = Field(default_factory=lambda: os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"))
    api_key_env: str = "OPENAI_API_KEY"
    model: str | None = Field(default_factory=lambda: os.environ.get("EVO_LLM_MODEL", "gpt-6-luna"))
    # gpt-6-luna only allows tool calls on Chat Completions with reasoning off, so the Responses API is the default.
    api_style: Literal["chat", "responses"] = "responses"
    reasoning_effort: Literal["none", "low", "medium", "high", "xhigh", "max"] | None = "high"
    temperature: float | None = None
    max_output_tokens: int | None = 4096
    request_timeout_seconds: float = 120.0
    # USD per 1M tokens. Leave unset to use the built-in price table (llm.PRICE_TABLE); unknown models cost 0.
    input_usd_per_mtok: float | None = None
    cached_input_usd_per_mtok: float | None = None
    output_usd_per_mtok: float | None = None
    retry_backoff_seconds: float = 2.0

    def api_key(self) -> str | None:
        return os.environ.get(self.api_key_env)


class ImplementerConfig(_Section):
    kind: Literal["mini_swe", "native", "pi", "rlm", "scripted"] = "mini_swe"
    script_path: Path | None = None
    step_limit: int = Field(default=40, ge=1)
    command_timeout_seconds: int = Field(default=120, ge=1)
    max_observation_chars: int = Field(default=10_000, ge=500)
    pi_command: list[str] = Field(default_factory=lambda: ["pi"],
                                  description="pi executable, or e.g. [\"node\", \"/path/to/pi/dist/bundle/cli.js\"]")
    pi_extra_args: list[str] = Field(default_factory=list)
    rlm_sub_model: str | None = Field(default=None, description="Model for llm_query sub-calls (default: [llm].model)")
    rlm_max_subcalls: int = Field(default=50, ge=0)


class PublisherConfig(_Section):
    kind: Literal["local", "github"] = "local"
    local_remote: Path | None = Field(default=None, description="Bare/regular git repo acting as 'origin' (local mode)")
    github_api_url: str = "https://api.github.com"
    github_git_url: str | None = Field(default=None, description="Override clone/push URL (tests, GHE)")
    token_env: str = "EVO_GITHUB_TOKEN"
    labels: list[str] = Field(default_factory=lambda: ["evo:coding"])
    comment_on_issue: bool = True


class PolicyConfig(_Section):
    branch_prefix: str = "evo/"
    keep_workspaces: bool = True
    extra_forbidden_paths: list[str] = Field(default_factory=list)
    hard_max_usd: float = Field(default=2.0, ge=0)
    hard_max_seconds: int = Field(default=1800, ge=1)
    hard_max_steps: int = Field(default=40, ge=1)
    hard_max_revisions: int = Field(default=2, ge=0)
    # Cap on total spend across all runs in work_dir, from spend_ledger.jsonl. None = no lifetime cap.
    lifetime_max_usd: float | None = Field(default=None, ge=0)
    commit_author_name: str = "EVO Coding Agent"
    commit_author_email: str = "evo-coding-agent@users.noreply.github.com"


class CodingConfig(_Section):
    work_dir: Path = Path(".evo-coding")
    repo_url: str | None = Field(default=None, description="Clone source override; defaults to the publisher's")
    spec_root: Path | None = None
    workspace: WorkspaceConfig = Field(default_factory=WorkspaceConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    implementer: ImplementerConfig = Field(default_factory=ImplementerConfig)
    publisher: PublisherConfig = Field(default_factory=PublisherConfig)
    policy: PolicyConfig = Field(default_factory=PolicyConfig)

    @classmethod
    def load(cls, path: Path | None = None) -> CodingConfig:
        path = path or (Path(os.environ["EVO_CODING_CONFIG"]) if os.environ.get("EVO_CODING_CONFIG") else None)
        if path is None:
            return cls()
        data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
        config = cls.model_validate(data)
        base = Path(path).resolve().parent
        # Relative paths in a config file are relative to that file.
        if not config.work_dir.is_absolute():
            config.work_dir = base / config.work_dir
        if config.spec_root and not config.spec_root.is_absolute():
            config.spec_root = base / config.spec_root
        if config.publisher.local_remote and not config.publisher.local_remote.is_absolute():
            config.publisher.local_remote = base / config.publisher.local_remote
        if config.implementer.script_path and not config.implementer.script_path.is_absolute():
            config.implementer.script_path = base / config.implementer.script_path
        return config


class BudgetExceeded(Exception):
    def __init__(self, kind: str, message: str):
        self.kind = kind
        super().__init__(message)


class Cancelled(Exception):
    pass


@dataclass(frozen=True)
class EffectiveBudget:
    max_usd: float
    max_seconds: int
    max_steps: int
    max_revisions: int

    @classmethod
    def resolve(cls, policy: PolicyConfig, requested: BudgetLimits) -> EffectiveBudget:
        def lower(cap, value):
            return cap if value is None else min(cap, value)

        return cls(
            max_usd=lower(policy.hard_max_usd, requested.max_usd),
            max_seconds=lower(policy.hard_max_seconds, requested.max_seconds),
            max_steps=lower(policy.hard_max_steps, requested.max_steps),
            max_revisions=lower(policy.hard_max_revisions, requested.max_revisions),
        )


class SpendLedger:
    """What every attempt in a work dir spent, one JSON line each, so a lifetime cap can be enforced."""

    def __init__(self, work_dir: Path):
        self.path = Path(work_dir) / "spend_ledger.jsonl"

    def entries(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def total_usd(self) -> float:
        return sum(float(entry.get("usd", 0.0)) for entry in self.entries())

    def record(self, **entry) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, default=str) + "\n")

    def clamp(self, limits: EffectiveBudget, lifetime_max_usd: float | None) -> EffectiveBudget:
        if lifetime_max_usd is None:
            return limits
        remaining = max(0.0, lifetime_max_usd - self.total_usd())
        return replace(limits, max_usd=min(limits.max_usd, remaining))


class BudgetTracker:
    def __init__(self, limits: EffectiveBudget, clock=time.monotonic):
        self.limits = limits
        self._clock = clock
        self._start = clock()
        self._lock = threading.Lock()
        self.usd = 0.0
        self.llm_calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.cached_input_tokens = 0
        self.reasoning_tokens = 0
        self.steps = 0

    @property
    def elapsed(self) -> float:
        return self._clock() - self._start

    @property
    def remaining_seconds(self) -> float:
        return max(0.0, self.limits.max_seconds - self.elapsed)

    @property
    def remaining_usd(self) -> float:
        return max(0.0, self.limits.max_usd - self.usd)

    @property
    def remaining_steps(self) -> int:
        return max(0, self.limits.max_steps - self.steps)

    def charge_llm(self, usd: float = 0.0, input_tokens: int = 0, output_tokens: int = 0,
                   cached_tokens: int = 0, reasoning_tokens: int = 0) -> None:
        with self._lock:
            self.usd += max(0.0, usd)
            self.llm_calls += 1
            self.input_tokens += input_tokens
            self.output_tokens += output_tokens
            self.cached_input_tokens += cached_tokens
            self.reasoning_tokens += reasoning_tokens

    def charge_step(self) -> None:
        with self._lock:
            self.steps += 1

    def check(self) -> None:
        if self.elapsed > self.limits.max_seconds:
            raise BudgetExceeded("time", f"run exceeded {self.limits.max_seconds}s time budget")
        if self.usd > self.limits.max_usd:
            raise BudgetExceeded("usd", f"run exceeded US${self.limits.max_usd:.2f} model budget")
        if self.steps > self.limits.max_steps:
            raise BudgetExceeded("steps", f"run exceeded {self.limits.max_steps} implementer steps")

    def snapshot(self) -> BudgetUsed:
        return BudgetUsed(
            usd=round(self.usd, 6),
            seconds=round(self.elapsed, 3),
            llm_calls=self.llm_calls,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            cached_input_tokens=self.cached_input_tokens,
            reasoning_tokens=self.reasoning_tokens,
            steps=self.steps,
        )


class CancelToken:
    """Cancelled when set in-process or when the run's CANCEL file exists (cross-process)."""

    def __init__(self, cancel_file: Path | None = None):
        self.cancel_file = cancel_file
        self._event = threading.Event()

    def cancel(self, reason: str = "cancelled") -> None:
        self._event.set()
        if self.cancel_file:
            self.cancel_file.parent.mkdir(parents=True, exist_ok=True)
            self.cancel_file.write_text(reason, encoding="utf-8")

    @property
    def cancelled(self) -> bool:
        return self._event.is_set() or bool(self.cancel_file and self.cancel_file.exists())

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise Cancelled("run was cancelled")
