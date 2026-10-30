"""LLM and GitHub clients against mocked HTTP transports."""
import json

import httpx
import pytest

from evo_coding.config import LLMConfig
from evo_coding.llm import PRICE_TABLE, LLMError, OpenAICompatibleClient, pricing_for
from evo_coding.publish import GitHubPublisher, PRRequest, PublishError

TOOLS = [{"type": "function", "function": {"name": "bash", "description": "d", "parameters": {"type": "object"}}}]


def llm(api_style="chat", **kw):
    return LLMConfig(base_url="https://llm.test/v1", model="m-1", api_style=api_style, input_usd_per_mtok=2.0,
                     output_usd_per_mtok=8.0, retry_backoff_seconds=0, **kw)


def chat_reply(command="ls"):
    return {"choices": [{"message": {"role": "assistant", "content": "thinking", "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "bash", "arguments": json.dumps({"command": command})}}]},
        "finish_reason": "tool_calls"}], "usage": {"prompt_tokens": 1000, "completion_tokens": 500}}


def test_chat_completions_request_and_cost():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=chat_reply())

    client = OpenAICompatibleClient(llm(), transport=httpx.MockTransport(handler), api_key="k-123")
    response = client.complete([{"role": "user", "content": "hi"}], TOOLS)
    assert seen["url"] == "https://llm.test/v1/chat/completions"
    assert seen["auth"] == "Bearer k-123"
    assert seen["body"]["model"] == "m-1" and seen["body"]["tools"] == TOOLS
    assert response.tool_calls[0].parsed_arguments() == {"command": "ls"}
    assert response.cost_usd == pytest.approx((1000 * 2 + 500 * 8) / 1e6)


def test_responses_api_translation():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"status": "completed", "output": [
            {"type": "message", "content": [{"type": "output_text", "text": "ok"}]},
            {"type": "function_call", "id": "fc1", "call_id": "call1", "name": "bash",
             "arguments": "{\"command\": \"pwd\"}"}], "usage": {"input_tokens": 10, "output_tokens": 5}})

    client = OpenAICompatibleClient(llm("responses"), transport=httpx.MockTransport(handler), api_key="k")
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "task"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call0", "type": "function", "function": {"name": "bash", "arguments": "{\"command\":\"ls\"}"}}]},
        {"role": "tool", "tool_call_id": "call0", "content": "<output>a</output>"},
    ]
    response = client.complete(messages, TOOLS)
    body = seen["body"]
    assert seen["url"].endswith("/v1/responses")
    assert body["instructions"] == "sys" and body["store"] is False
    assert body["tools"][0]["name"] == "bash" and "function" not in body["tools"][0]
    assert [i.get("type", i.get("role")) for i in body["input"]] == ["user", "function_call", "function_call_output"]
    assert response.text == "ok" and response.tool_calls[0].id == "call1"
    assert (response.input_tokens, response.output_tokens) == (10, 5)


def test_luna_defaults_pricing_and_reasoning_round_trip():
    config = LLMConfig(model="gpt-6-luna", base_url="https://llm.test/v1")
    assert config.api_style == "responses" and config.reasoning_effort == "high"
    luna = pricing_for(config)
    assert luna == PRICE_TABLE["gpt-6-luna"]
    assert luna.cost(200_000, 80_000, 100_000) == pytest.approx(0.012 + 0.0008 + 0.05)
    assert luna.cost(300_000, 0, 1000) == pytest.approx(300_000 * 0.2 / 1e6 + 1000 * 0.75 / 1e6)  # long context
    bodies = []
    reasoning = {"type": "reasoning", "id": "rs1", "encrypted_content": "opaque", "summary": []}

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"status": "completed", "output": [
            reasoning, {"type": "function_call", "id": "fc1", "call_id": "c1", "name": "bash", "arguments": "{}"}],
            "usage": {"input_tokens": 2000, "input_tokens_details": {"cached_tokens": 1000}, "output_tokens": 300,
                      "output_tokens_details": {"reasoning_tokens": 200}}})

    client = OpenAICompatibleClient(config, transport=httpx.MockTransport(handler), api_key="k")
    first = client.complete([{"role": "user", "content": "task"}], TOOLS)
    assert bodies[0]["reasoning"] == {"effort": "high"} and bodies[0]["include"] == ["reasoning.encrypted_content"]
    assert (first.cached_tokens, first.reasoning_tokens) == (1000, 200)
    assert first.cost_usd == pytest.approx((1000 * 0.10 + 1000 * 0.01 + 300 * 0.50) / 1e6)
    history = [{"role": "user", "content": "task"}, first.assistant_message(),
               {"role": "tool", "tool_call_id": "c1", "content": "out"}]
    client.complete(history, TOOLS)
    assert bodies[1]["input"][1] == reasoning  # encrypted reasoning is sent back verbatim
    assert [i.get("type", i.get("role")) for i in bodies[1]["input"]] == [
        "user", "reasoning", "function_call", "function_call_output"]


def test_transient_errors_are_retried_once():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(503, text="busy") if len(calls) == 1 else httpx.Response(200, json=chat_reply())

    client = OpenAICompatibleClient(llm(), transport=httpx.MockTransport(handler), api_key="k")
    assert client.complete([{"role": "user", "content": "x"}], TOOLS).tool_calls
    assert len(calls) == 2


def test_persistent_errors_raise_llm_error():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(500 if len(calls) < 5 else 200, text="down")

    client = OpenAICompatibleClient(llm(), transport=httpx.MockTransport(handler), api_key="k")
    with pytest.raises(LLMError, match="500"):
        client.complete([{"role": "user", "content": "x"}], TOOLS)
    assert len(calls) == 2
    auth = OpenAICompatibleClient(llm(), transport=httpx.MockTransport(lambda r: httpx.Response(401)), api_key="k")
    with pytest.raises(LLMError, match="401"):
        auth.complete([{"role": "user", "content": "x"}], TOOLS)


def test_client_requires_model_and_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(LLMError, match="model"):
        OpenAICompatibleClient(LLMConfig(model=None), api_key="k")
    with pytest.raises(LLMError, match="API key"):
        OpenAICompatibleClient(LLMConfig(model="m"))


class FakeGitHub:
    def __init__(self, draft_supported=True, existing=None):
        self.calls = []
        self.draft_supported = draft_supported
        self.existing = existing or []

    def __call__(self, request):
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, request.url.path, dict(request.url.params), body))
        path = request.url.path
        if request.method == "GET" and path.endswith("/pulls"):
            return httpx.Response(200, json=self.existing)
        if request.method == "POST" and path.endswith("/pulls"):
            if body["draft"] and not self.draft_supported:
                return httpx.Response(422, json={"message": "Draft pull requests are not supported in this repository"})
            return httpx.Response(201, json={"number": 7, "html_url": "https://github.com/o/r/pull/7",
                                             "draft": body["draft"], "base": {"ref": body["base"]}})
        if request.method == "POST" and (path.endswith("/labels") or path.endswith("/comments")):
            return httpx.Response(201, json={})
        return httpx.Response(404, json={"message": "not found"})


REQUEST = PRRequest(repository="o/r", branch="evo/issue-1-x", base="main", title="T", body="B", issue_id=1,
                    labels=["evo:coding"])


def test_github_opens_draft_pr_with_labels():
    api = FakeGitHub()
    pub = GitHubPublisher("tok", api_url="https://gh.test", transport=httpx.MockTransport(api))
    pr = pub.open_or_get_pr(REQUEST)
    assert (pr.number, pr.draft, pr.created) == (7, True, True)
    methods = [(m, p) for m, p, _, _ in api.calls]
    assert methods == [("GET", "/repos/o/r/pulls"), ("POST", "/repos/o/r/pulls"),
                       ("POST", "/repos/o/r/issues/7/labels")]
    assert api.calls[0][2] == {"head": "o:evo/issue-1-x", "state": "open"}
    assert api.calls[1][3]["draft"] is True and api.calls[1][3]["base"] == "main"
    assert not any("merge" in p for _, p, _, _ in api.calls)


def test_github_reuses_existing_pr():
    api = FakeGitHub(existing=[{"number": 4, "html_url": "u", "draft": True, "base": {"ref": "main"}}])
    pub = GitHubPublisher("tok", api_url="https://gh.test", transport=httpx.MockTransport(api))
    pr = pub.open_or_get_pr(REQUEST)
    assert (pr.number, pr.created) == (4, False)
    assert len(api.calls) == 1


def test_github_falls_back_when_drafts_unsupported():
    api = FakeGitHub(draft_supported=False)
    pub = GitHubPublisher("tok", api_url="https://gh.test", transport=httpx.MockTransport(api))
    pr = pub.open_or_get_pr(REQUEST)
    assert pr.draft is False
    assert api.calls[2][3]["title"].startswith("[Draft]")


def test_github_errors_and_token_hygiene():
    with pytest.raises(PublishError):
        GitHubPublisher("")
    pub = GitHubPublisher("tok-secret-1", api_url="https://gh.test",
                          transport=httpx.MockTransport(lambda r: httpx.Response(403, json={"message": "no"})))
    with pytest.raises(PublishError, match="403"):
        pub.comment("o/r", 1, "hi")
    env = pub.git_env()
    assert "tok-secret-1" not in json.dumps(env)  # only the base64 form, via env not argv
    assert "tok-secret-1" in pub.secrets()
