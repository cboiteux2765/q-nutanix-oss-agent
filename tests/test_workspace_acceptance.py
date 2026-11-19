"""Project workspace acceptance coverage through the HTTP API and agent tool broker."""

import asyncio
import json
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from textwrap import dedent
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from evo import runtime as runtime_module
from evo.github_activity import GitHubPage, GitHubReader
from evo.providers import ModelResponse, ToolCall
from evo.server import create_app
from evo.workspaces import git

from .conftest import TEST_SETTINGS, python_call, reply


def _ok(response):
    assert response.is_success, f"{response.request.method} {response.url}: {response.text}"
    return response.json()


def _until(read, predicate, timeout=20):
    deadline = time.monotonic() + timeout
    while True:
        value = read()
        if predicate(value):
            return value
        if time.monotonic() >= deadline:
            pytest.fail(f"Timed out waiting for workspace state. Last response: {value!r}")
        time.sleep(0.03)


def _cell(code, call_id):
    return python_call(dedent(code).strip(), call_id=call_id)


def _tools(*calls):
    return ModelResponse("", [ToolCall(call_id, name, json.dumps(arguments)) for call_id, name, arguments in calls],
                         usage={"input": 10, "output": 10, "cached": 0, "cost_usd": 0}, finish_reason="tool_calls")


def _tool_result(messages, call_id):
    message = next(message for message in reversed(messages) if message.get("tool_call_id") == call_id)
    result = json.loads(message["content"])
    assert not isinstance(result, dict) or not result.get("error"), result
    return result


def _cell_result(messages, call_id=None):
    result = _tool_result(messages, call_id) if call_id else json.loads(messages[-1]["content"])
    assert result.get("ok"), result
    return result


def _user(messages):
    return next(message["content"] for message in reversed(messages) if message["role"] == "user")


def _is_supervisor(messages):
    return "You are the persistent project supervisor" in messages[0]["content"]


def _chat(client, path):
    return _ok(client.get(f"{path}/conversation"))


def _chat_text(chat):
    return [message["data"]["text"] for message in chat["messages"]]


def _trace(project, run, agent_id=None):
    return f"{project}/runs/{run['id']}/agents/{agent_id or run['root_agent_id']}/trace"


def _run(client, run_id):
    return _ok(client.get(f"/api/runs/{run_id}", params={"events": "false"}))["run"]


def _wait_status(client, run, status):
    result = _until(lambda: _run(client, run["id"]), lambda row: row["status"] in {status, "failed"})
    assert result["status"] == status, result.get("error")
    return result


@pytest.fixture
def workspace(repo, tmp_path, monkeypatch):
    handler = {}

    class ScriptedProvider:
        def __init__(self, settings, api_key):
            self.settings = settings
            self.api_key = api_key

        async def close(self):
            pass

        async def count_input_tokens(self, messages, tools=None):
            return None

        async def complete(self, messages, tools=None, on_delta=None):
            handler.setdefault("calls", []).append((messages[0]["content"], _user(messages)))
            return await handler["complete"](messages)

    monkeypatch.setattr(runtime_module, "ModelProvider", ScriptedProvider)
    app = create_app(data_dir=tmp_path / "data", frontend_dir=tmp_path / "dist")
    other_repo = tmp_path / "other"
    git(tmp_path, "clone", "--local", str(repo), str(other_repo))
    with TestClient(app) as client:
        _ok(client.put("/api/settings", json=TEST_SETTINGS))
        projects = [
            _ok(client.post("/api/projects", json={"name": folder.name, "path": str(folder)}))
            for folder in (repo, other_repo)
        ]
        yield client, handler, *(f"/api/projects/{project['id']}" for project in projects)


def test_supervisor_launches_reads_and_steers_independent_workflows(workspace, repo):
    client, handler, project, _ = workspace
    first_task = "Implement the alpha widget"
    second_task = "Implement the beta widget"
    start = "Please start both widget assignments."
    steer = "Record a 15-second timeout for alpha."
    inspect = "Inspect beta and send the timeout adjustment to alpha."
    selected = "Record alpha as the primary widget."
    artifact_body = "Beta interface accepts a list of widgets."
    running = set()
    receipt = {}
    answer = "Beta interface evidence found. Timeout adjustment queued for alpha."

    async def complete(messages):
        user = _user(messages)
        tool = messages[-1].get("tool_call_id")
        if _is_supervisor(messages):
            if start in user:
                if tool:
                    return reply("I started both widget assignments.")
                return _tools(("start-alpha", "workflows_start", {"message": first_task}),
                              ("start-beta", "workflows_start", {"message": second_task}))
            if inspect in user:
                if tool:
                    assert _tool_result(messages, "alpha-status")["status"] == "paused"
                    beta_artifacts = _tool_result(messages, "beta-artifacts")
                    assert any(item["name"] == "Beta interface" for item in beta_artifacts), beta_artifacts
                    beta_trace = json.dumps(_tool_result(messages, "beta-transcript"))
                    assert second_task in beta_trace and first_task not in beta_trace
                    receipt.update(_tool_result(messages, "send-alpha"))
                    assert receipt["workflow_id"] == _tool_result(messages, "start-alpha")["id"]
                    assert receipt["status"] == "received"
                    return reply(answer)
                alpha = _tool_result(messages, "start-alpha")
                beta = _tool_result(messages, "start-beta")
                return _tools(("alpha-status", "workflows_status", {"run_id": alpha["id"]}),
                              ("beta-artifacts", "workflows_artifacts", {"run_id": beta["id"]}),
                              ("beta-transcript", "workflows_transcript",
                               {"run_id": beta["id"], "agent_id": beta["root_agent_id"]}),
                              ("send-alpha", "workflows_send", {"run_id": alpha["id"], "message": steer}))
        elif second_task in user and not tool:
            return _cell(f"artifacts.write('Beta interface', {artifact_body!r})", "beta-evidence")
        elif steer in user:
            if tool:
                _cell_result(messages)
                return reply("Recorded the updated timeout for alpha.")
            event_id = re.search(r"message ID is ([0-9a-f]{32})", user)[1]
            return _cell(f"""
                artifacts.write('Alpha timeout', 'Timeout: 15 seconds')
                chat.applied({event_id!r}, 'Recorded the 15-second timeout requirement.')
                """, "apply-alpha-timeout")
        elif selected in user:
            return reply("Alpha is the primary widget.")
        elif first_task in user or second_task in user:
            if tool:
                _cell_result(messages)
            running.add(first_task if first_task in user else second_task)
            await asyncio.Future()
        raise AssertionError(f"Unexpected request: {user!r}")

    handler["complete"] = complete
    _ok(client.patch(f"{project}/workflow-settings", json={"max_concurrent_workflows": 2}))
    supervisor = _ok(client.post(f"{project}/messages", json={"message": start}))
    workflows = _until(lambda: _ok(client.get(f"{project}/workflows")), lambda rows: len(rows) == 2)
    by_title = {row["title"]: row for row in workflows}
    alpha, beta = (_run(client, by_title[task]["id"]) for task in (first_task, second_task))
    _wait_status(client, supervisor, "waiting")
    _until(lambda: running.copy(), lambda values: values == {first_task, second_task})
    assert len({alpha["id"], beta["id"], supervisor["id"]}) == 3
    assert Path(alpha["workdir"]) != Path(beta["workdir"])
    for workflow in (alpha, beta):
        assert git(Path(workflow["workdir"]), "branch", "--show-current") == workflow["branch"]
        assert git(Path(workflow["workdir"]), "rev-parse", "HEAD") == git(repo, "rev-parse", "HEAD")
    assert alpha["branch"] != beta["branch"]

    paused = _ok(client.post(f"{project}/workflows/{alpha['id']}/pause"))
    assert paused["status"] == "paused"
    assert _run(client, beta["id"])["status"] == "running"

    routed = _ok(client.post(f"{project}/messages", json={"message": inspect}))
    assert routed["id"] == supervisor["id"]
    _wait_status(client, supervisor, "waiting")
    chat = _chat(client, project)
    assert answer in _chat_text(chat)
    assert chat["run_id"] == supervisor["id"]
    assert chat["messages"][-1]["agent_id"] == supervisor["root_agent_id"]
    assert receipt["source_event_id"] == next(message["id"] for message in chat["messages"]
                                               if message["data"]["text"] == inspect)
    assert _run(client, alpha["id"])["status"] == "paused"
    assert not any(steer in user for system, user in handler["calls"] if "persistent project supervisor" not in system)

    _ok(client.post(f"{project}/workflows/{alpha['id']}/resume"))
    _wait_status(client, alpha, "waiting")
    assert _run(client, beta["id"])["status"] == "running"
    alpha_trace = _ok(client.get(f"{_trace(project, alpha)}/transcript"))
    beta_trace = _ok(client.get(f"{_trace(project, beta)}/transcript"))
    assert steer in json.dumps(alpha_trace)
    assert steer not in json.dumps(beta_trace)
    assert second_task not in json.dumps(alpha_trace)
    assert first_task not in json.dumps(beta_trace)
    alpha_events = _ok(client.get(f"{_trace(project, alpha)}/events"))["entries"]
    assert any(event["kind"] == "message.applied"
               and event["data"]["message_event_id"] == receipt["message_event_id"] for event in alpha_events)

    direct = _ok(client.post(f"{project}/messages", json={"message": selected, "run_id": alpha["id"]}))
    assert direct["id"] == supervisor["id"]
    assert direct["routing"]["workflow_id"] == alpha["id"]
    alpha_reply = "Alpha is the primary widget."
    chat = _until(lambda: _chat(client, project), lambda value: alpha_reply in _chat_text(value))
    answer_event = next(event for event in chat["messages"] if event["data"]["text"] == alpha_reply)
    assert answer_event["data"]["source_run_id"] == alpha["id"]
    assert answer_event["data"]["source_event_id"] == direct["message_event_id"]
    assert _run(client, beta["id"])["status"] == "running"


def test_workflow_trace_survives_compaction_with_nested_tool_results(workspace):
    client, handler, project, _ = workspace
    first, second, third = "Read the greeting.", "Explain its format.", "Keep the existing greeting."
    followup = "What did we decide?"
    checkpoint_text = "The existing script prints hello."
    replies = {
        first: "The greeting is hello.", second: "The format is a print statement.",
        third: "I will keep it.", followup: "We will keep the greeting.",
    }

    async def complete(messages):
        if messages[0]["content"].startswith("Summarize this historical agent transcript"):
            return reply(checkpoint_text)
        user = _user(messages)
        if user == first and messages[-1]["role"] != "tool":
            response = _cell("print(repo.read('app.py', lines=1))", "read-greeting")
            response.chat_reasoning = "Check the tracked greeting before answering."
            return response
        if user == first:
            assert "print('hello')" in _cell_result(messages)["stdout"]
        if user == followup:
            assert any(checkpoint_text in message.get("content", "") for message in messages)
            assert all(message.get("content") != replies[first] for message in messages)
        return reply(replies[user])

    handler["complete"] = complete
    run = _ok(client.post(f"{project}/workflows", json={"message": first}))
    _wait_status(client, run, "waiting")
    route = f"{project}/workflows/{run['id']}/messages"
    for message in (second, third):
        _ok(client.post(route, json={"message": message}))
        _until(lambda: _ok(client.get(f"{_trace(project, run)}/transcript")),
               lambda trace: trace["entries"][-1]["message"].get("content") == replies[message])
        _wait_status(client, run, "waiting")

    trace_path = _trace(project, run)
    before = _ok(client.get(f"{trace_path}/transcript"))
    compact = _ok(client.post(f"/api/agents/{run['root_agent_id']}/compact"))
    assert compact["status"] == "completed"
    after = _ok(client.get(f"{trace_path}/transcript"))
    assert after["completeness"] == "complete"
    assert after["entries"] == before["entries"]
    assert any(entry["message"].get("content") == replies[first] for entry in after["entries"])
    assert any(entry["reasoning"] == [{"kind": "content", "text": "Check the tracked greeting before answering."}]
               for entry in after["entries"])

    events = _ok(client.get(f"{trace_path}/events"))["entries"]
    starts = {event["data"]["name"]: event["data"] for event in events if event["kind"] == "tool.started"}
    read = starts["repo.read"]
    assert read["parent_span_id"] == starts["python"]["span_id"]
    completed = {event["data"]["span_id"]: event for event in events if event["kind"] == "tool.finished"}
    assert "print('hello')" in json.dumps(completed[read["span_id"]]["data"]["result"])
    tool = next(entry for entry in after["entries"] if entry["message"].get("tool_call_id") == "read-greeting")
    assert tool["event_id"] == completed[starts["python"]["span_id"]]["id"]
    assert any(event["kind"] == "compaction" for event in events)

    _ok(client.post(route, json={"message": followup}))
    continued = _until(lambda: _ok(client.get(f"{trace_path}/transcript")),
                       lambda trace: trace["entries"][-1]["message"].get("content") == replies[followup])
    assert continued["entries"][:len(after["entries"])] == after["entries"]


def test_local_specification_handoff_reaches_code_and_rejects_foreign_or_changed_evidence(workspace, repo, tmp_path):
    client, handler, project, other_project = workspace
    change = "Update app.py to print hello workspace."
    another = "Update the greeting in the other repository."
    implementation = "print('hello workspace')\n"
    draft = {
        "problem": "The greeting needs the workspace wording.",
        "scope": ["Update the greeting in app.py."],
        "non_goals": ["Keep the script invocation stable."],
        "assumptions": [],
        "current_behavior": [{"claim": "The script prints hello.", "source_ids": ["SRC-1"]}],
        "proposed_change": "Print hello workspace.",
        "api_changes": "Changes the single line on stdout.",
        "alternatives": [],
        "risks": ["Callers may match the current output."],
        "requirements": [{"id": "REQ-1", "description": "Print hello workspace.",
                          "source_ids": ["REQUEST"], "test_ids": ["TEST-1"]}],
        "acceptance_tests": [{"id": "TEST-1", "expected_behavior": "Running app.py prints hello workspace."}],
        "work_items": [{"id": "TASK-1", "objective": "Update the string.", "depends_on": []}],
        "timeline": [{"id": "MILESTONE-1", "phase": "Implementation", "deliverable": "Updated script",
                      "exit_criteria": "TEST-1 passes", "depends_on": []}],
        "constraints": [],
        "rollout_rollback": "Revert the greeting commit to restore the previous output.",
        "unresolved_questions": [],
    }
    publications = {}
    foreign = {}

    async def complete(messages):
        system, latest = messages[0]["content"], messages[-1]
        user = _user(messages).splitlines()[-1]
        tool = latest.get("tool_call_id")
        if _is_supervisor(messages):
            if tool:
                _tool_result(messages, "start-greeting")
                return reply("I started the greeting assignment.")
            assert user in {change, another}, user
            return _tools(("start-greeting", "workflows_start", {"message": user}))
        if "Read the request and repository" in system:
            tamper = "other repository" in user
            if not tool and tamper:
                return _cell(f"spec.context([{{'event_id': {foreign['event']!r}}}])", "foreign-source")
            if tool in {"foreign-source", "foreign-artifact", "advance-greeting"} and tamper:
                error = json.loads(latest["content"])
                assert error["ok"] is False, error
                if tool == "foreign-source":
                    return _cell(f"workflow.handoff('code', 'Use prior plan', [{foreign['artifact']!r}])",
                                 "foreign-artifact")
                if tool == "advance-greeting":
                    return reply("The specification changed after publication.")
            if not tool or tool == "foreign-artifact":
                return _cell(f"""
                    import json
                    sources = [{{'path': 'app.py', 'lines': 1}}]
                    assert spec.context(sources)['sources'][1]['text'] == "print('hello')"
                    validation = spec.validate({draft!r}, sources)
                    assert validation['valid'] and validation['ready_for_code'], validation
                    published = spec.publish({draft!r}, sources)
                    print(json.dumps(published))
                    """, "publish-greeting")
            if tool == "publish-greeting":
                publications[tamper] = json.loads(_cell_result(messages)["stdout"])
                return _cell("""
                    handoff = workflow.handoff('code', 'Ready for implementation',
                                               [published['artifact_id'], published['markdown_artifact_id']])
                    print(handoff['id'])
                    """, "handoff-greeting")
            if tool == "handoff-greeting":
                _cell_result(messages)
                if tamper:
                    artifact_path = tmp_path / "data" / "artifacts" / publications[tamper]["artifact_id"]
                    artifact_path.write_text("{}", encoding="utf-8")
                return _cell("workflow.advance('code', handoff['id'])", "advance-greeting")
            _cell_result(messages)
            return reply("The structured specification is ready for Code.")
        if "Implement the accepted spec" in system:
            if tool:
                _cell_result(messages)
                return reply("The greeting commit is published.")
            return _cell(f"""
                import json
                handoff = next(item for item in workflow.status()['handoffs'] if item['target'] == 'code')
                accepted = json.loads(artifacts.read(handoff['artifacts'][0])['content'])
                assert accepted['draft']['requirements'][0]['test_ids'] == ['TEST-1']
                repo.write('app.py', {implementation!r})
                print(repo.commit('Update workspace greeting'))
                """, "implement-greeting")
        tool_history = {message.get("tool_call_id") for message in messages}
        if "coordinate-greeting" not in tool_history:
            assert user in {change, another}, user
            return _cell(f"""
                workflow.advance('spec')
                spec_agent = agents.spawn('spec', {user!r})
                result = agents.wait([spec_agent['id']], timeout=12)[0]
                assert result['status'] == 'completed', result
                """, "coordinate-greeting")
        _cell_result(messages, "coordinate-greeting")
        if "coordinate-code" not in tool_history:
            return _cell("""
                if workflow.status()['phase'] == 'code':
                    code_agent = agents.spawn('code', 'Implement the accepted greeting spec.')
                    result = agents.wait([code_agent['id']], timeout=12)[0]
                    assert result['status'] == 'completed', result
                """, "coordinate-code")
        _cell_result(messages, "coordinate-code")
        return reply("The workflow has finished its current turn.")

    handler["complete"] = complete

    def start(path, assignment):
        _ok(client.post(f"{path}/messages", json={"message": assignment}))
        rows = _until(lambda: _ok(client.get(f"{path}/workflows")), bool)
        assert len(rows) == 1 and rows[0]["title"] == assignment
        return _wait_status(client, rows[0], "waiting")

    run = start(project, change)
    assert run["phase"] == "code"
    assert run.get("implementation_commit")
    assert git(Path(run["workdir"]), "show", f"{run['implementation_commit']}:app.py", trim=False) == implementation
    assert (repo / "app.py").read_text(encoding="utf-8") == "print('hello')\n"

    published = publications[False]
    foreign["artifact"] = published["artifact_id"]
    envelope = json.loads(_ok(client.get(f"{project}/artifacts/{foreign['artifact']}"))["content"])
    assert client.get(f"{other_project}/artifacts/{foreign['artifact']}").status_code == 404
    assert envelope["run_id"] == run["id"]
    assert envelope["base_commit"] == run["base_commit"]
    assert {source["id"]: source["text"] for source in envelope["sources"]} == {
        "REQUEST": change, "SRC-1": "print('hello')",
    }
    clarification = _ok(client.post(f"{project}/workflows/{run['id']}/messages",
                                    json={"message": "Keep one space between the words."}))
    foreign["event"] = clarification["message_event_id"]

    blocked = start(other_project, another)
    assert blocked["phase"] == "spec"
    assert blocked.get("implementation_commit") is None
    detail = _ok(client.get(f"/api/runs/{blocked['id']}"))
    assert {agent["role"] for agent in detail["agents"]} == {"orchestrator", "spec"}
    spec_agent = next(agent for agent in detail["agents"] if agent["role"] == "spec")
    blocked_trace = _ok(client.get(f"{_trace(other_project, blocked, spec_agent['id'])}/transcript"))
    tool_results = {entry["message"]["tool_call_id"]: json.loads(entry["message"]["content"])
                    for entry in blocked_trace["entries"] if entry["message"]["role"] == "tool"}
    rejected = {call: result["stderr"] for call, result in tool_results.items() if not result.get("ok")}
    assert "from this run" in rejected["foreign-source"]
    assert "from this run" in rejected["foreign-artifact"]
    assert "changed after publication" in rejected["advance-greeting"]


def test_first_proactive_suggestion_creates_central_message_without_workflow(workspace, monkeypatch):
    client, handler, project, other_project = workspace
    repo_url = "https://github.com/example/workspace/issues/17"
    issues = []
    requests = []

    async def fake_get(self, target, *, etag=None):
        path = urlsplit(target).path
        requests.append(path)
        prefix = "/repos/example/workspace"
        assert path.startswith(prefix), path
        if path == prefix:
            return GitHubPage(200, {"id": 1700, "default_branch": "main"})
        if path == f"{prefix}/issues":
            return GitHubPage(200, issues.copy())
        assert path.removeprefix(prefix) in {"/issues/comments", "/pulls/comments", "/pulls", "/commits"}, path
        return GitHubPage(200, [])

    monkeypatch.setattr(GitHubReader, "get", fake_get)
    assert _chat(client, project)["messages"] == []
    assert _ok(client.get(f"{project}/workflows")) == []
    assert _ok(client.post(f"{project}/activity/poll"))["suggestions"] == []
    assert requests == []
    _ok(client.patch(f"{project}/activity/settings", json={"enabled": True, "repository": "example/workspace"}))
    now = datetime.now(UTC).isoformat()
    issues.append({
        "id": 17, "number": 17, "title": "Document the workspace greeting", "body": "Include an example.",
        "state": "open", "created_at": now, "updated_at": now,
        "user": {"login": "contributor", "type": "User"}, "html_url": repo_url,
    })

    for _ in range(2):
        overview = _ok(client.post(f"{project}/activity/poll"))
        assert overview["counts"]["suggestions"] == 1
        suggestion, = overview["suggestions"]
        assert suggestion["status"] == "pending" and suggestion["run_id"] is None
        assert suggestion["source_urls"] == [repo_url]

    chat = _chat(client, project)
    message, = chat["messages"]
    assert chat["run_id"] == message["run_id"]
    assert message["data"]["role"] == "assistant"
    assert "Document the workspace greeting" in message["data"]["text"]
    assert repo_url in json.dumps(message["data"])
    dashboard = _ok(client.get(f"{project}/dashboard"))
    assert dashboard["supervisor"]["id"] == chat["run_id"]
    assert dashboard["workflows"] == _ok(client.get(f"{project}/workflows")) == []
    assert _chat(client, other_project)["messages"] == []
    assert _ok(client.get(f"{other_project}/activity"))["suggestions"] == []
    assert not handler.get("calls")


def test_dashboard_and_workflow_settings_are_scoped_to_each_project(workspace):
    client, handler, project, other_project = workspace
    note = "What is the first project's status?"
    assignment = "Review the first project's app.py."

    async def complete(messages):
        return reply(f"Received {_user(messages)}")

    handler["complete"] = complete
    original_settings = _ok(client.get(f"{other_project}/workflow-settings"))
    _ok(client.put("/api/settings", json={"input_price": 1.0, "output_price": 2.0}))
    updates = {"max_concurrent_workflows": 3, "max_project_tokens": 400_000, "max_project_cost_usd": 4.5}
    _ok(client.patch(f"{project}/workflow-settings", json=updates))
    assert client.patch(f"{project}/workflow-settings", json={"max_concurrent_workflows": 0}).status_code in {400, 422}
    supervisor = _ok(client.post(f"{project}/messages", json={"message": note}))
    workflow = _ok(client.post(f"{project}/workflows", json={"message": assignment}))
    for run in (supervisor, workflow):
        _wait_status(client, run, "waiting")

    dashboard = _ok(client.get(f"{project}/dashboard"))
    other = _ok(client.get(f"{other_project}/dashboard"))
    assert dashboard["supervisor"]["id"] == supervisor["id"]
    assert {row["id"] for row in dashboard["workflows"]} == {workflow["id"]}
    assert {key: dashboard["settings"][key] for key in updates} == updates
    assert other["supervisor"] is None and other["workflows"] == []
    assert _ok(client.get(f"{other_project}/workflow-settings")) == original_settings
    assert dashboard["usage"]["tokens"] == 40 and other["usage"]["tokens"] == 0
    assert _chat(client, other_project)["messages"] == []
    chat = _chat(client, project)
    assert chat["run_id"] == supervisor["id"] and note in _chat_text(chat)

    assert client.get(f"{_trace(other_project, workflow)}/transcript").status_code == 404
    assert client.post(f"{other_project}/workflows/{workflow['id']}/pause").status_code == 404
    assert client.post(f"{other_project}/messages", json={"message": "Transfer", "run_id": workflow["id"]}).status_code == 404
    assert _run(client, workflow["id"])["status"] == "waiting"
    assert _chat(client, other_project)["messages"] == []
