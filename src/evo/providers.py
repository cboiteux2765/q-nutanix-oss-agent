"""Stateless OpenAI-compatible model requests."""

import asyncio
import json
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx

from .common import redact

RAW_ITEMS = "_responses_items"


class ProviderError(Exception):
    pass


def require_provider_key(settings: dict, api_key: str) -> None:
    if not api_key and urlsplit(settings["base_url"]).hostname == "api.openai.com":
        raise ProviderError(f"Set {settings['api_key_env']} or enter an API key in Settings.")


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str

    def parsed_arguments(self) -> dict:
        value = json.loads(self.arguments)
        if not isinstance(value, dict):
            raise ValueError("Tool arguments must be a JSON object")
        return value


@dataclass
class ModelResponse:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    output_items: list[dict] | None = None
    finish_reason: str | None = None
    chat_reasoning: str | None = None

    def assistant_message(self) -> dict:
        message = {"role": "assistant", "content": self.text or (None if self.tool_calls else "")}
        if self.tool_calls:
            message["tool_calls"] = [
                {"id": call.id, "type": "function",
                 "function": {"name": call.name, "arguments": call.arguments}}
                for call in self.tool_calls
            ]
        if self.output_items is not None:
            message[RAW_ITEMS] = self.output_items
        if self.chat_reasoning:
            message["reasoning_content"] = self.chat_reasoning
        return message


def responses_input(messages: list[dict]) -> tuple[str, list[dict]]:
    instructions = []
    items = []
    for message in messages:
        role = message["role"]
        if role == "system":
            instructions.append(message["content"])
        elif role == "tool":
            items.append({"type": "function_call_output", "call_id": message["tool_call_id"],
                          "output": message["content"]})
        elif role == "assistant" and message.get(RAW_ITEMS):
            items.extend(message[RAW_ITEMS])
        else:
            if message.get("content"):
                items.append({"role": role, "content": message["content"]})
            for call in message.get("tool_calls", []):
                items.append({"type": "function_call", "call_id": call["id"], **call["function"]})
    return "\n\n".join(instructions), items


class ModelProvider:
    def __init__(self, settings: dict, api_key: str, transport=None):
        require_provider_key(settings, api_key)
        self.settings = settings
        self.api_key = api_key
        self.chat_limit_field = "max_completion_tokens"
        self.chat_stream_usage = True
        self.client = httpx.AsyncClient(
            base_url=settings["base_url"].rstrip("/") + "/",
            headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
            timeout=httpx.Timeout(180, connect=20),
            transport=transport,
        )

    async def close(self):
        await self.client.aclose()

    async def count_input_tokens(self, messages: list[dict], tools=None) -> int | None:
        """Measure the actual Responses payload near the working-context limit.

        Other providers keep the local estimate; an unavailable counting endpoint
        must not break otherwise usable Chat Completions/Responses integrations.
        """
        if (self.settings["api_style"] != "responses"
                or urlsplit(self.settings["base_url"]).hostname != "api.openai.com"):
            return None
        instructions, items = responses_input(messages)
        payload = {"model": self.settings["model"], "input": items}
        if instructions:
            payload["instructions"] = instructions
        if tools:
            payload["tools"] = [{"type": "function", **tool["function"]} for tool in tools]
            payload["parallel_tool_calls"] = False
        if self.settings.get("reasoning_effort"):
            payload["reasoning"] = {"effort": self.settings["reasoning_effort"]}
        try:
            response = await self.client.post("responses/input_tokens", json=payload, timeout=20)
            response.raise_for_status()
            count = response.json().get("input_tokens")
            return count if isinstance(count, int) and not isinstance(count, bool) and count >= 0 else None
        except (httpx.HTTPError, ValueError, AttributeError):
            return None

    def _redact(self, text: str) -> str:
        return redact(text, self.api_key)

    async def complete(self, messages: list[dict], tools=None, on_delta=None) -> ModelResponse:
        settings = self.settings
        responses = settings["api_style"] == "responses"
        body = {"model": settings["model"]}
        if responses:
            instructions, items = responses_input(messages)
            body.update(input=items, store=False, include=["reasoning.encrypted_content"])
            if instructions:
                body["instructions"] = instructions
            if tools:
                body["tools"] = [{"type": "function", **tool["function"]} for tool in tools]
                body["parallel_tool_calls"] = False
            if settings.get("reasoning_effort"):
                body["reasoning"] = {"effort": settings["reasoning_effort"]}
        else:
            body["messages"] = [{k: v for k, v in m.items() if k != RAW_ITEMS} for m in messages]
            if tools:
                body["tools"] = tools
            if settings.get("reasoning_effort"):
                body["reasoning_effort"] = settings["reasoning_effort"]
        if settings.get("max_output_tokens"):
            key = "max_output_tokens" if responses else self.chat_limit_field
            body[key] = settings["max_output_tokens"]
        if settings.get("temperature") is not None:
            body["temperature"] = settings["temperature"]
        route = "responses" if responses else "chat/completions"
        if on_delta:
            body["stream"] = True
            if not responses and self.chat_stream_usage:
                body["stream_options"] = {"include_usage": True}
        data = await self._request(route, body, on_delta)
        try:
            response = self._decode(data, responses)
        except (KeyError, TypeError, ValueError, IndexError, AttributeError) as exc:
            raise ProviderError("Provider returned a malformed response.") from exc
        return response

    async def _request(self, route: str, body: dict, on_delta) -> dict:
        failures = 0
        emitted = False

        async def delta(text):
            nonlocal emitted
            if text:
                emitted = True
                await on_delta(text)

        while True:
            try:
                if body.get("stream"):
                    return await self._stream(route, body, delta)
                response = await self.client.post(route, json=body)
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                detail = self._redact(exc.response.text)
                if route == "chat/completions" and status in {400, 422}:
                    if "max_completion_tokens" in body and "max_completion_tokens" in detail:
                        self.chat_limit_field = "max_tokens"
                        body["max_tokens"] = body.pop("max_completion_tokens")
                        continue
                    if "stream_options" in body and "stream_options" in detail:
                        self.chat_stream_usage = False
                        body.pop("stream_options")
                        continue
                if failures == 2 or emitted or (status not in {408, 429} and status < 500):
                    raise ProviderError(f"Provider returned HTTP {status}: {detail[:600]}") from exc
            except httpx.TransportError as exc:
                if failures == 2 or emitted:
                    raise ProviderError(f"Provider connection failed: {type(exc).__name__}") from exc
            except ValueError as exc:
                raise ProviderError("Provider did not return JSON.") from exc
            await asyncio.sleep(0.5 * 2**failures)
            failures += 1

    async def _stream(self, route, body, on_delta):
        async with self.client.stream("POST", route, json=body) as response:
            if not response.is_success:
                await response.aread()
                response.raise_for_status()
            if "text/event-stream" not in response.headers.get("content-type", ""):
                await response.aread()
                return response.json()
            if route == "responses":
                async for event in self._sse(response):
                    kind = event.get("type")
                    if kind in {"response.output_text.delta", "response.refusal.delta"}:
                        await on_delta(event.get("delta", ""))
                    elif kind in {"response.completed", "response.incomplete", "response.failed"}:
                        return event["response"]
                    elif kind == "error":
                        raise ProviderError(self._redact(str(event.get("message") or "Provider stream failed.")))
                raise ProviderError("Provider closed the Responses stream before the final response.")
            return await self._chat_stream(response, on_delta)

    @staticmethod
    async def _sse(response):
        lines = []
        async for line in response.aiter_lines():
            if line.startswith("data:"):
                lines.append(line[5:].lstrip(" "))
            elif not line and lines:
                payload = "\n".join(lines)
                lines.clear()
                if payload == "[DONE]":
                    return
                yield json.loads(payload)
        if lines and lines != ["[DONE]"]:
            yield json.loads("\n".join(lines))

    async def _chat_stream(self, response, on_delta):
        text, refusal, reasoning = [], [], []
        calls = {}
        usage = {}
        finish = None
        async for event in self._sse(response):
            if error := event.get("error"):
                raise ProviderError(self._redact(str(error.get("message") or "Provider stream failed.")))
            usage = event.get("usage") or usage
            choices = event.get("choices") or []
            if not choices:
                continue
            choice = choices[0]
            finish = choice.get("finish_reason") or finish
            delta = choice.get("delta") or {}
            if content := delta.get("content"):
                text.append(content)
                await on_delta(content)
            if content := delta.get("refusal"):
                refusal.append(content)
                await on_delta(content)
            if content := delta.get("reasoning_content"):
                reasoning.append(content)
            for item in delta.get("tool_calls") or []:
                call = calls.setdefault(item["index"], {
                    "id": "", "function": {"name": "", "arguments": ""},
                })
                call["id"] = item.get("id") or call["id"]
                function = item.get("function") or {}
                call["function"]["name"] += function.get("name") or ""
                call["function"]["arguments"] += function.get("arguments") or ""
        if not finish:
            raise ProviderError("Provider closed the Chat Completions stream before the final response.")
        message = {"content": "".join(text), "refusal": "".join(refusal),
                   "reasoning_content": "".join(reasoning),
                   "tool_calls": [calls[index] for index in sorted(calls)]}
        return {"choices": [{"message": message, "finish_reason": finish}], "usage": usage}

    def _decode(self, data: dict, responses: bool) -> ModelResponse:
        usage = data.get("usage") or {}
        input_tokens = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
        output_tokens = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
        details = usage.get("input_tokens_details") or usage.get("prompt_tokens_details") or {}
        cached = min(input_tokens, int(details.get("cached_tokens", usage.get("prompt_cache_hit_tokens")) or 0))
        settings = self.settings
        cost = ((input_tokens - cached) * settings.get("input_price", 0)
                + cached * settings.get("cached_input_price", 0)
                + output_tokens * settings.get("output_price", 0)) / 1_000_000
        totals = {"input": input_tokens, "output": output_tokens, "cached": cached, "cost_usd": cost}
        if not responses:
            choice = data["choices"][0]
            message = choice["message"]
            calls = [ToolCall(call["id"], call["function"]["name"], call["function"].get("arguments", ""))
                     for call in message.get("tool_calls") or []]
            return ModelResponse(message.get("content") or message.get("refusal") or "", calls,
                                 totals, finish_reason=choice.get("finish_reason"),
                                 chat_reasoning=message.get("reasoning_content"))

        if data.get("status") in {"failed", "cancelled"} or data.get("error"):
            error = data.get("error") or {}
            detail = self._redact(str(error.get("message") or data.get("status") or "Provider error"))
            raise ProviderError(detail)
        items = data.get("output") or []
        parts = []
        calls = []
        for item in items:
            if item["type"] == "message":
                for part in item.get("content", []):
                    if part["type"] == "output_text":
                        parts.append(part["text"])
                    elif part["type"] == "refusal":
                        parts.append(part["refusal"])
            elif item["type"] == "function_call":
                calls.append(ToolCall(item["call_id"], item["name"], item.get("arguments", "")))
        finish = data.get("status")
        if finish == "incomplete":
            reason = (data.get("incomplete_details") or {}).get("reason", "unknown")
            finish = f"incomplete:{reason}"
        return ModelResponse("\n".join(parts), calls, totals, items, finish)
