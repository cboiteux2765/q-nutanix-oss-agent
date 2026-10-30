"""A scripted OpenAI-compatible server (Chat Completions + Responses) for end-to-end tests."""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

COMPLETE = "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"


class FakeOpenAIServer:
    """Each model request pops the next scripted bash command and returns it as a tool call.

    Script items are a bash command string, {"tool": name, "args": {...}} for any other tool call,
    {"text": "..."} for a reply without a tool call, or {"status": 500} for a transient server error.
    """

    def __init__(self, script: list, input_tokens: int = 1000, output_tokens: int = 100):
        self.script = list(script)
        self.requests: list[dict] = []
        self.paths: list[str] = []
        self.auth_headers: list[str] = []
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self._lock = threading.Lock()
        self._counter = 0
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # silence
                pass

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
                status, payload = server._respond(self.path, body, self.headers.get("Authorization", ""))
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._httpd.server_address[1]}/v1"

    def __enter__(self) -> FakeOpenAIServer:
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()

    def _next(self):
        with self._lock:
            self._counter += 1
            item = self.script.pop(0) if self.script else COMPLETE
            return self._counter, item

    def _respond(self, path: str, body: dict, auth: str) -> tuple[int, dict]:
        n, item = self._next()
        self.requests.append(body)
        self.paths.append(path)
        self.auth_headers.append(auth)
        if isinstance(item, dict) and "status" in item:
            return item["status"], {"error": {"message": "simulated failure", "type": "server_error"}}
        text = item.get("text") if isinstance(item, dict) else None
        if isinstance(item, str):
            call = ("bash", {"command": item})
        elif "tool" in item:
            call = (item["tool"], item["args"])
        else:
            call = None
        if path.rstrip("/").endswith("/responses"):
            return 200, self._responses_payload(n, body, call, text)
        return 200, self._chat_payload(n, body, call, text)

    def _usage_chat(self):
        return {"prompt_tokens": self.input_tokens, "completion_tokens": self.output_tokens,
                "total_tokens": self.input_tokens + self.output_tokens}

    def _chat_payload(self, n, body, call, text):
        message = {"role": "assistant", "content": text or f"THOUGHT: step {n}"}
        finish = "stop"
        if call is not None:
            message["tool_calls"] = [{"id": f"call_{n}", "type": "function",
                                      "function": {"name": call[0], "arguments": json.dumps(call[1])}}]
            finish = "tool_calls"
        return {"id": f"chatcmpl-{n}", "object": "chat.completion", "created": int(time.time()),
                "model": body.get("model", "fake"), "choices": [{"index": 0, "message": message,
                                                                 "finish_reason": finish}],
                "usage": self._usage_chat()}

    def _responses_payload(self, n, body, call, text):
        output = []
        if text or call is None:
            output.append({"type": "message", "id": f"msg_{n}", "status": "completed", "role": "assistant",
                           "content": [{"type": "output_text", "text": text or "", "annotations": []}]})
        if call is not None:
            output.append({"type": "function_call", "id": f"fc_{n}", "call_id": f"call_{n}", "name": call[0],
                           "arguments": json.dumps(call[1]), "status": "completed"})
        return {"id": f"resp_{n}", "object": "response", "created_at": int(time.time()), "status": "completed",
                "model": body.get("model", "fake"), "output": output, "parallel_tool_calls": True,
                "tool_choice": "auto", "tools": body.get("tools", []), "error": None, "incomplete_details": None,
                "instructions": None, "metadata": {}, "temperature": 1.0, "top_p": 1.0,
                "usage": {"input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
                          "total_tokens": self.input_tokens + self.output_tokens,
                          "input_tokens_details": {"cached_tokens": 0},
                          "output_tokens_details": {"reasoning_tokens": 0}}}


FIXED_STATS = '''"""Small statistics helpers."""
from collections import Counter


def mean(values):
    if not values:
        raise ValueError("mean() requires at least one value")
    return sum(values) / len(values)


def median(values):
    if not values:
        raise ValueError("median() requires at least one value")
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def mode(values):
    if not values:
        raise ValueError("mode() requires at least one value")
    return Counter(values).most_common(1)[0][0]
'''

MEDIAN_TESTS = '''import pytest

from pilot_stats import median


def test_median_even():
    assert median([1, 2, 3, 4]) == 2.5


def test_median_empty():
    with pytest.raises(ValueError):
        median([])
'''

NOTES = json.dumps({"summary": "Fixed median() for even-length and empty input; added tests.",
                    "criteria": {"AC-1": {"status": "IMPLEMENTED", "notes": "average middle pair"},
                                 "AC-2": {"status": "IMPLEMENTED", "notes": "ValueError"},
                                 "AC-3": {"status": "IMPLEMENTED", "notes": "unchanged"}}})

MEDIAN_FIX_SCRIPT = [
    "cat src/pilot_stats/stats.py",
    f"cat > src/pilot_stats/stats.py <<'EOF'\n{FIXED_STATS}EOF",
    f"cat > tests/test_median.py <<'EOF'\n{MEDIAN_TESTS}EOF",
    "python -m pytest -q",
    f"mkdir -p .evo-artifacts && cat > .evo-artifacts/implementation_notes.json <<'EOF'\n{NOTES}\nEOF",
    COMPLETE,
]
