"""Minimal OpenAI-compatible client supporting Chat Completions and the Responses API, with cost tracking.

Messages are kept in Chat Completions shape internally and converted for the Responses API. With the
Responses API, the model's raw output items (including encrypted reasoning) ride along on the assistant
message and are sent back verbatim next turn, so a reasoning model keeps its chain of thought across tool
calls without server-side storage (store=false).

Transient failures (timeouts, connection errors, 429/5xx) are retried exactly once.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from .config import LLMConfig

RAW_ITEMS_KEY = "_responses_items"  # private key on assistant messages; never sent to Chat Completions


@dataclass(frozen=True)
class Pricing:
    """USD per 1M tokens. Above `long_context_threshold` input tokens the whole request is billed at the
    long-context multipliers."""

    input: float
    cached_input: float
    output: float
    long_context_threshold: int | None = None
    long_input_multiplier: float = 1.0
    long_output_multiplier: float = 1.0

    def cost(self, input_tokens: int, cached_tokens: int, output_tokens: int) -> float:
        input_rate, cached_rate, output_rate = self.input, self.cached_input, self.output
        if self.long_context_threshold and input_tokens > self.long_context_threshold:
            input_rate *= self.long_input_multiplier
            cached_rate *= self.long_input_multiplier
            output_rate *= self.long_output_multiplier
        uncached = max(0, input_tokens - cached_tokens)
        return (uncached * input_rate + cached_tokens * cached_rate + output_tokens * output_rate) / 1e6


# Official standard-tier prices (https://developers.openai.com/api/docs/models/gpt-6-luna, checked 2026-10-29).
PRICE_TABLE: dict[str, Pricing] = {
    "gpt-6-luna": Pricing(input=0.10, cached_input=0.01, output=0.50, long_context_threshold=272_000,
                          long_input_multiplier=2.0, long_output_multiplier=1.5),
}


def pricing_for(config: LLMConfig) -> Pricing:
    """Prices set in [llm] win; otherwise the price table; unknown models cost 0."""
    if config.input_usd_per_mtok is None and config.output_usd_per_mtok is None:
        return PRICE_TABLE.get(config.model or "", Pricing(0.0, 0.0, 0.0))
    input_rate = config.input_usd_per_mtok or 0.0
    cached_rate = config.cached_input_usd_per_mtok
    return Pricing(input_rate, input_rate if cached_rate is None else cached_rate, config.output_usd_per_mtok or 0.0)


def usage_tokens(usage: dict[str, Any]) -> tuple[int, int, int, int]:
    """(input, output, cached input, reasoning) from a Chat Completions or Responses usage object."""
    inp = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
    out = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
    in_details = usage.get("input_tokens_details") or usage.get("prompt_tokens_details") or {}
    out_details = usage.get("output_tokens_details") or usage.get("completion_tokens_details") or {}
    return inp, out, int(in_details.get("cached_tokens") or 0), int(out_details.get("reasoning_tokens") or 0)


class LLMError(Exception):
    pass


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str

    def parsed_arguments(self) -> dict[str, Any]:
        value = json.loads(self.arguments or "{}")
        if not isinstance(value, dict):
            raise ValueError("tool arguments must be a JSON object")
        return value


@dataclass
class LLMResponse:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    finish_reason: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)
    cached_tokens: int = 0
    reasoning_tokens: int = 0
    output_items: list[dict[str, Any]] | None = None  # Responses API only

    def charge(self, budget) -> None:
        budget.charge_llm(self.cost_usd, self.input_tokens, self.output_tokens, self.cached_tokens,
                          self.reasoning_tokens)

    def assistant_message(self) -> dict[str, Any]:
        """The assistant turn in Chat Completions shape, carrying raw Responses items when present."""
        message: dict[str, Any] = {"role": "assistant", "content": self.text or (None if self.tool_calls else "")}
        if self.tool_calls:
            message["tool_calls"] = [{"id": c.id, "type": "function",
                                      "function": {"name": c.name, "arguments": c.arguments}}
                                     for c in self.tool_calls]
        if self.output_items is not None:
            message[RAW_ITEMS_KEY] = self.output_items
        return message


def _is_transient(exc: Exception) -> bool:
    if isinstance(exc, (httpx.TimeoutException, httpx.TransportError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 429 or exc.response.status_code >= 500
    return False


class OpenAICompatibleClient:
    def __init__(self, config: LLMConfig, transport: httpx.BaseTransport | None = None,
                 api_key: str | None = None, model: str | None = None):
        if model:
            config = config.model_copy(update={"model": model})
        if not config.model:
            raise LLMError("no model configured: set [llm].model or EVO_LLM_MODEL")
        key = api_key or config.api_key()
        if not key:
            raise LLMError(f"no API key: set {config.api_key_env}")
        self.config = config
        self.pricing = pricing_for(config)
        self._client = httpx.Client(
            base_url=config.base_url.rstrip("/") + "/",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            timeout=config.request_timeout_seconds,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> LLMResponse:
        for attempt in (1, 2):
            try:
                if self.config.api_style == "responses":
                    return self._responses(messages, tools)
                return self._chat(messages, tools)
            except Exception as exc:  # noqa: BLE001 - classify below
                if attempt == 1 and _is_transient(exc):
                    time.sleep(self.config.retry_backoff_seconds)
                    continue
                if isinstance(exc, httpx.HTTPStatusError):
                    raise LLMError(f"LLM API error {exc.response.status_code}: {exc.response.text[:300]}") from exc
                if isinstance(exc, (httpx.HTTPError, KeyError, ValueError)):
                    raise LLMError(f"LLM request failed: {exc}") from exc
                raise
        raise AssertionError("unreachable")

    # ------------------------------------------------------------ chat completions

    def _chat(self, messages, tools) -> LLMResponse:
        clean = [{k: v for k, v in m.items() if k != RAW_ITEMS_KEY} for m in messages]
        body: dict[str, Any] = {"model": self.config.model, "messages": clean}
        if tools:
            body["tools"] = tools
        if self.config.reasoning_effort:
            body["reasoning_effort"] = self.config.reasoning_effort
        if self.config.temperature is not None:
            body["temperature"] = self.config.temperature
        if self.config.max_output_tokens:
            body["max_completion_tokens"] = self.config.max_output_tokens
        response = self._client.post("chat/completions", json=body)
        response.raise_for_status()
        data = response.json()
        choice = data["choices"][0]
        message = choice["message"]
        calls = [ToolCall(c["id"], c["function"]["name"], c["function"].get("arguments", ""))
                 for c in message.get("tool_calls") or []]
        usage = data.get("usage") or {}
        inp, out = int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)
        cached = int((usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
        reasoning = int((usage.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0)
        return LLMResponse(message.get("content") or "", calls, inp, out, self.pricing.cost(inp, cached, out),
                           choice.get("finish_reason"), data, cached, reasoning)

    # ------------------------------------------------------------ responses API

    @staticmethod
    def to_responses_input(messages: list[dict[str, Any]]) -> tuple[str | None, list[dict[str, Any]]]:
        instructions = None
        items: list[dict[str, Any]] = []
        for message in messages:
            role = message["role"]
            if role == "system":
                instructions = (instructions + "\n\n" if instructions else "") + message["content"]
            elif role == "tool":
                items.append({"type": "function_call_output", "call_id": message["tool_call_id"],
                              "output": message["content"]})
            elif role == "assistant" and message.get(RAW_ITEMS_KEY) is not None:
                items.extend(message[RAW_ITEMS_KEY])  # verbatim, including encrypted reasoning
            elif role == "assistant":
                if message.get("content"):
                    items.append({"role": "assistant", "content": message["content"]})
                for call in message.get("tool_calls") or []:
                    items.append({"type": "function_call", "call_id": call["id"],
                                  "name": call["function"]["name"], "arguments": call["function"]["arguments"]})
            else:
                items.append({"role": role, "content": message["content"]})
        return instructions, items

    def _responses(self, messages, tools) -> LLMResponse:
        instructions, items = self.to_responses_input(messages)
        body: dict[str, Any] = {"model": self.config.model, "input": items, "store": False}
        if tools:
            body["tools"] = [{"type": "function", **t["function"]} for t in tools]
        if instructions:
            body["instructions"] = instructions
        if self.config.reasoning_effort:
            body["reasoning"] = {"effort": self.config.reasoning_effort}
            body["include"] = ["reasoning.encrypted_content"]
        if self.config.temperature is not None:
            body["temperature"] = self.config.temperature
        if self.config.max_output_tokens:
            body["max_output_tokens"] = self.config.max_output_tokens
        response = self._client.post("responses", json=body)
        response.raise_for_status()
        data = response.json()
        texts, calls = [], []
        output = data.get("output") or []
        for item in output:
            if item.get("type") == "message":
                texts += [c.get("text", "") for c in item.get("content", []) if c.get("type") == "output_text"]
            elif item.get("type") == "function_call":
                calls.append(ToolCall(item.get("call_id") or item["id"], item["name"], item.get("arguments", "")))
        usage = data.get("usage") or {}
        inp, out = int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0)
        cached = int((usage.get("input_tokens_details") or {}).get("cached_tokens") or 0)
        reasoning = int((usage.get("output_tokens_details") or {}).get("reasoning_tokens") or 0)
        finish = data.get("status")
        if finish == "incomplete":
            finish = f"incomplete:{(data.get('incomplete_details') or {}).get('reason', 'unknown')}"
        return LLMResponse("".join(texts), calls, inp, out, self.pricing.cost(inp, cached, out), finish, data,
                           cached, reasoning, output)
