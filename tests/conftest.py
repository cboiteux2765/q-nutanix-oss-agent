import asyncio
import subprocess
from pathlib import Path

import pytest

from evo import runtime as runtime_module
from evo.providers import ModelResponse, ToolCall
from evo.store import Store

TEST_SETTINGS = {
    "api_style": "chat_completions",
    "base_url": "http://127.0.0.1:9/v1",
    "model": "test-model",
    "runtime_preset": "custom",
    "max_run_tokens": 1_000_000,
}


@pytest.fixture
def store(tmp_path: Path) -> Store:
    store = Store(tmp_path / "data")
    store.put("settings", "provider", dict(TEST_SETTINGS))
    yield store
    store.close()


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "-b", "main")
    (path / "app.py").write_text("print('hello')\n", encoding="utf-8")
    _git(path, "add", "--all")
    _git(path, "-c", "user.name=Test", "-c", "user.email=test@localhost", "commit", "-m", "init")
    return path


@pytest.fixture
def project(store: Store, repo: Path) -> dict:
    return store.put("projects", "p1", {"name": "demo", "path": str(repo), "description": ""})


class FakeProvider:
    """Stands in for ModelProvider. `script` is a list of callables or ModelResponses."""

    script: list = []
    calls: list = []

    def __init__(self, settings, api_key, transport=None):
        self.settings = settings
        self.api_key = api_key

    async def close(self):
        pass

    async def count_input_tokens(self, messages, tools=None):
        return None

    async def complete(self, messages, tools=None, on_delta=None):
        FakeProvider.calls.append([dict(m) for m in messages])
        step = FakeProvider.script.pop(0) if FakeProvider.script else reply("Done.")
        response = step(messages) if callable(step) else step
        if on_delta and response.text:
            await on_delta(response.text)
        return response


def reply(text: str, tokens: int = 10) -> ModelResponse:
    return ModelResponse(text, usage={"input": tokens, "output": tokens, "cached": 0, "cost_usd": 0},
                         finish_reason="stop")


def python_call(code: str, call_id: str = "call-1", tokens: int = 10) -> ModelResponse:
    import json
    return ModelResponse("", [ToolCall(call_id, "python", json.dumps({"code": code}))],
                         usage={"input": tokens, "output": tokens, "cached": 0, "cost_usd": 0},
                         finish_reason="tool_calls")


@pytest.fixture
def fake_provider(monkeypatch):
    FakeProvider.script = []
    FakeProvider.calls = []
    monkeypatch.setattr(runtime_module, "ModelProvider", FakeProvider)
    return FakeProvider


async def settle(runtime, timeout: float = 30) -> None:
    """Wait until every scheduled agent task has finished."""
    async with asyncio.timeout(timeout):
        while runtime.tasks:
            await asyncio.gather(*list(runtime.tasks.values()), return_exceptions=True)
            await asyncio.sleep(0)


def transcript(store: Store, agent_id: str) -> list[dict]:
    """An agent's model-visible message history, wherever the store keeps it."""
    if hasattr(store, "messages"):
        return store.messages(agent_id)
    return store.get("agents", agent_id)["messages"]
