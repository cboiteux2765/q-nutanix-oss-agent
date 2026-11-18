"""Model backends shared by both product-spec harnesses, the evals and the EVO Spec Lab.

A *chat function* takes OpenAI-style messages plus an optional output format and returns
the reply text. `Meter` wraps one to count calls and tokens. On top of that:

  LocalRLM       stand-in for Prime Agent's `rlm` runtime (async spawn / collect); each
                 child is ONE completion, so the real `learning.PrimeBackend` can run locally.
  SchemaBackend  the Prime-style orchestrator's direct backend (like learning.OllamaBackend).

Backends: `ollama_chat` (local, supports JSON / JSON-schema constrained decoding),
`evo_chat` (whatever provider is configured in EVO Settings; no constrained decoding),
`openai_compatible_chat` (hosted Chat Completions, e.g. UCSB AI Commons via `aicommons_config`),
and `recorded_mini_chat` (offline demo script for the mini agent, no model).
"""
import asyncio
import hashlib
import json
from pathlib import Path
from threading import Lock
from types import SimpleNamespace
from urllib.request import Request, urlopen

from agent.learning import GUARD

ROOT = Path(__file__).resolve().parents[1]
RECORDINGS = ROOT / "examples" / "recorded"
OLLAMA = "http://127.0.0.1:11434"


def ollama_chat(model, num_ctx=32768, temperature=0.0, num_predict=6000):
    def chat(messages, fmt=None, role=None):
        body = {"model": model, "stream": False,
                "messages": [{"role": m["role"], "content": m["content"]} for m in messages],
                "options": {"temperature": temperature, "num_ctx": num_ctx, "num_predict": num_predict}}
        if fmt is not None:
            body["format"] = fmt
        request = Request(f"{OLLAMA}/api/chat", json.dumps(body).encode(), {"Content-Type": "application/json"})
        with urlopen(request, timeout=300) as response:
            data = json.load(response)
        if not data.get("done") or data.get("done_reason") != "stop":
            raise ValueError("Incomplete model output (Ollama stopped before the end of the reply).")
        return data["message"]["content"], {"input_tokens": data.get("prompt_eval_count") or 0,
                                            "output_tokens": data.get("eval_count") or 0}
    chat.label = f"ollama:{model}"
    return chat


class QuotaError(RuntimeError):
    """The hosted provider refused the call for quota reasons (HTTP 429)."""


def aicommons_config():
    """UCSB AI Commons settings from the environment (or a gitignored .env), or None.

    AICOMMONS_API_KEY  key from AI Commons → Settings → API Keys (needs the `openai` group)
    AICOMMONS_BASE_URL base URL shown on that page, ending in /v1
    """
    import os
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    except ImportError:
        pass
    key, base = os.environ.get("AICOMMONS_API_KEY", "").strip(), os.environ.get("AICOMMONS_BASE_URL", "").strip()
    return {"api_key": key, "base_url": base.rstrip("/")} if key and base else None


def _openai_request(config, method, path, body=None, timeout=300):
    from urllib.error import HTTPError
    request = Request(config["base_url"] + path, json.dumps(body).encode() if body is not None else None,
                      {"Authorization": f"Bearer {config['api_key']}", "Content-Type": "application/json"},
                      method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except HTTPError as exc:
        try:
            message = json.load(exc).get("error", {}).get("message", "")
        except (ValueError, AttributeError):
            message = ""
        message = message.replace(config["api_key"], "[redacted]")
        if exc.code == 429:
            raise QuotaError(f"AI Commons quota exceeded (429): {message}") from None
        hint = {401: " Check AICOMMONS_API_KEY and that your account is in the `openai` group.",
                404: " Check AICOMMONS_BASE_URL (it should end in /v1) and the model name."}.get(exc.code, "")
        raise ValueError(f"AI Commons HTTP {exc.code}: {message}{hint}") from None


def json_instruction(fmt):
    """Prompt text that stands in for constrained decoding on providers that lack it.

    `fmt` is "json" or a JSON schema. Ollama enforces a schema while decoding; hosted
    Chat Completions here cannot, so the schema has to be shown to the model instead.
    """
    if isinstance(fmt, dict):
        return ("\n\nReturn only one JSON value, with no Markdown fences, that matches this JSON Schema "
                "exactly (every required field, no extra fields):\n" + json.dumps(fmt, separators=(",", ":")))
    return "\n\nReturn only one JSON value, with no Markdown fences."


def openai_compatible_chat(config, model, temperature=0.0, max_tokens=4000):
    """Chat Completions against an OpenAI-compatible endpoint such as UCSB AI Commons.

    AI Commons does not document `response_format`, so JSON is requested in the prompt and
    Markdown fences are stripped, as for EVO's provider. temperature is ignored by some models.
    """
    def chat(messages, fmt=None, role=None):
        messages = [{"role": m["role"], "content": m["content"]} for m in messages]
        if fmt is not None:
            messages[-1] = {**messages[-1], "content": messages[-1]["content"] + json_instruction(fmt)}
        data = _openai_request(config, "POST", "/chat/completions",
                               {"model": model, "messages": messages, "temperature": temperature,
                                "max_tokens": max_tokens})
        choice = data["choices"][0]
        if choice.get("finish_reason") == "length":
            raise ValueError("Incomplete model output (stopped at max_tokens).")
        text = (choice["message"].get("content") or "").strip()
        if fmt is not None and text.startswith("```"):
            text = text.strip("`").removeprefix("json").strip()
        usage = data.get("usage") or {}
        return text, {"input_tokens": usage.get("prompt_tokens") or 0,
                      "output_tokens": usage.get("completion_tokens") or 0}
    chat.label = f"aicommons:{model}"
    return chat


class BudgetExceeded(RuntimeError):
    """A token budget was used up; no further calls are made through this chat function."""


def budgeted(chat, limit, name="budget"):
    """Wrap a chat function with a hard token budget, checked before every call.

    Usage is counted from each reply (input + output tokens). The call that crosses the limit
    completes, so the overshoot is at most one call. Budgets nest: wrap a per-run budget
    around a per-command one.
    """
    state = {"used": 0, "limit": limit}

    def call(messages, fmt=None, role=None):
        if state["used"] >= state["limit"]:
            raise BudgetExceeded(f"{name} of {limit:,} tokens used up ({state['used']:,} spent).")
        text, usage = chat(messages, fmt, role=role)
        state["used"] += (usage.get("input_tokens") or 0) + (usage.get("output_tokens") or 0)
        return text, usage
    call.label, call.state = getattr(chat, "label", "model"), state
    return call


def aicommons_models(config):
    return sorted(m["id"] for m in _openai_request(config, "GET", "/models", timeout=15).get("data", []))


def aicommons_usage(config):
    """Monthly quota: {limit, used, remaining, reset_at}. Not metered."""
    return _openai_request(config, "GET", "/token-usage", timeout=15)


def ollama_models():
    """Installed Ollama models, or None when Ollama is not running."""
    try:
        with urlopen(f"{OLLAMA}/api/tags", timeout=2) as response:
            return [m["name"] for m in json.load(response).get("models", [])]
    except (OSError, ValueError):
        return None


def evo_chat(settings, api_key, provider_class=None):
    """Use EVO's configured provider. Must be called from a worker thread (it runs its own loop)."""
    if provider_class is None:
        from evo.providers import ModelProvider as provider_class

    async def complete(messages):
        provider = provider_class(settings, api_key)
        try:
            return await provider.complete(messages)
        finally:
            await provider.close()

    def chat(messages, fmt=None, role=None):
        if fmt is not None:  # no portable constrained decoding: ask for JSON in the prompt instead
            messages = [*messages[:-1], {**messages[-1], "content": messages[-1]["content"] + json_instruction(fmt)}]
        response = asyncio.run(complete([{"role": m["role"], "content": m["content"]} for m in messages]))
        usage = response.usage or {}
        text = response.text.strip()
        if text.startswith("```") and fmt is not None:
            text = text.strip("`").removeprefix("json").strip()
        return text, {"input_tokens": usage.get("input_tokens") or usage.get("prompt_tokens") or 0,
                      "output_tokens": usage.get("output_tokens") or usage.get("completion_tokens") or 0}
    chat.label = f"evo:{settings.get('model', '?')}"
    return chat


def recorded_mini_chat():
    """Offline script for the mini agent on the bundled fixture: search, check feedback, submit."""
    draft = json.loads((ROOT / "examples/product-replay.json").read_text(encoding="utf-8"))[-1]
    draft = {**draft, "assumptions": []}
    steps = ['THOUGHT: Read every pinned source first.\n\n```mswea_bash_command\nevidence_search ""\n```',
             "THOUGHT: Check reviewer suggestions (unverified).\n\n```mswea_bash_command\nfeedback_memory\n```",
             "THOUGHT: Submit the recorded draft; the validators still run.\n\n```mswea_bash_command\n"
             "submit <<'EOF'\n" + json.dumps(draft, indent=1) + "\nEOF\n```"]

    def chat(messages, fmt=None, role=None):
        turn = sum(m["role"] == "assistant" for m in messages)
        return steps[min(turn, len(steps) - 1)], {"input_tokens": 0, "output_tokens": 0}
    chat.label = "scripted fixture"
    return chat


class Meter:
    """Thread-safe call and token counter around a chat function."""

    def __init__(self, chat):
        self.chat_fn, self.lock = chat, Lock()
        self.calls = self.input_tokens = self.output_tokens = 0
        self.label = getattr(chat, "label", "model")

    def chat(self, messages, fmt=None, role=None):
        text, usage = self.chat_fn(messages, fmt, role=role)
        with self.lock:
            self.calls += 1
            self.input_tokens += usage.get("input_tokens") or 0
            self.output_tokens += usage.get("output_tokens") or 0
        return text

    def stats(self):
        return {"model_calls": self.calls, "input_tokens": self.input_tokens, "output_tokens": self.output_tokens}


class LocalRLM:
    """Stand-in for Prime's `rlm` module: async spawn(prompt, name) / collect(handle, timeout_ms).

    A real Prime child is a sandboxed REPL agent; here each child is ONE JSON-mode completion.
    """

    def __init__(self, meter):
        self.meter, self.children = meter, {}

    async def spawn(self, prompt, *, name):
        role = name.split("-")[1] if name.count("-") >= 2 else None  # PrimeBackend names: spec-<role>-<id>
        self.children[name] = asyncio.create_task(
            asyncio.to_thread(self.meter.chat, [{"role": "user", "content": prompt}], "json", role))
        return name

    async def collect(self, handle, *, timeout_ms):
        try:
            text = await asyncio.wait_for(self.children.pop(handle), timeout_ms / 1000)
            return [SimpleNamespace(settled=True, status="done", answer_text=text)]
        except asyncio.TimeoutError:
            return [SimpleNamespace(settled=False, status="running", answer_text="")]
        except NotRecorded:
            raise
        except Exception as exc:
            return [SimpleNamespace(settled=True, status="error", answer_text=str(exc))]


class SchemaBackend:
    """Prime-style orchestrator backend with schema-constrained decoding (where supported)."""
    mode = "live-local"

    def __init__(self, meter):
        self.meter = meter

    async def query(self, role, instruction, payload, schema):
        text = await asyncio.to_thread(self.meter.chat, [{"role": "system", "content": GUARD + instruction},
                                                         {"role": "user", "content": json.dumps(payload)}], schema, role)
        return json.loads(text)


# ------------------------------------------------------------------ record / replay
class NotRecorded(ValueError):
    """A recorded-mode call that the recording does not contain."""


def stream_key(case_id, harness, guidance, role):
    """Recordings are per (case, harness, supplemental prompt, specialist role); replies replay in order."""
    return f"{case_id}|{harness}|{hashlib.sha256((guidance or '').strip().encode()).hexdigest()[:12]}|{role or 'mini'}"


class Recording:
    """Model replies captured from a live run, replayed in order per stream.

    Replaying runs the real harness loop, tools and validators; only the model is replaced.
    The order of calls *within* one specialist is deterministic, so per-role streams replay
    correctly even though specialists run concurrently.
    """

    def __init__(self, path):
        self.path = Path(path)
        self.data = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {
            "model": None, "recorded_at": None, "streams": {}}
        self.lock = Lock()

    @classmethod
    def default(cls):
        files = sorted(RECORDINGS.glob("*.json"), key=lambda f: f.stat().st_mtime)
        return cls(files[-1]) if files else None  # the newest recording

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=1, ensure_ascii=False), encoding="utf-8")

    def has(self, case_id, harness, guidance):
        prefix = stream_key(case_id, harness, guidance, "x").rsplit("|", 1)[0] + "|"
        return any(k.startswith(prefix) for k in self.data["streams"])

    def recorder(self, chat, case_id, harness, guidance):
        """Wrap a live chat function; every reply is appended to its stream."""
        def record(messages, fmt=None, role=None):
            text, usage = chat(messages, fmt, role=role)
            with self.lock:
                self.data["streams"].setdefault(stream_key(case_id, harness, guidance, role), []).append(
                    {"text": text, "usage": usage})
            return text, usage
        record.label = getattr(chat, "label", "model")
        return record

    def replay(self, case_id, harness, guidance):
        positions = {}

        def chat(messages, fmt=None, role=None):
            key = stream_key(case_id, harness, guidance, role)
            with self.lock:
                stream = self.data["streams"].get(key)
                index = positions.get(key, 0)
                positions[key] = index + 1
            if not stream or index >= len(stream):
                raise NotRecorded(f"No recorded reply for {harness} on {case_id} ({role or 'mini'} call {index + 1}) "
                                  "with this prompt version. Use a live model for unrecorded combinations.")
            return stream[index]["text"], stream[index]["usage"]
        chat.label = f"recorded:{self.data.get('model')}"
        return chat
