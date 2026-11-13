"""The main agent stays responsive while its team works (scripted models, no API calls)."""

import asyncio
import json

import pytest

from evo import runtime as runtime_module
from evo.providers import ModelResponse, ToolCall
from evo.runtime import Runtime

from .conftest import settle, transcript

RESPONDER_MARK = "answering the user in the conversation while your own working process"


def reply(text):
    return ModelResponse(text, usage={"input": 5, "output": 5, "cached": 0, "cost_usd": 0}, finish_reason="stop")


def python(code, call_id="c1"):
    return ModelResponse("", [ToolCall(call_id, "python", json.dumps({"code": code}))],
                         usage={"input": 5, "output": 5, "cached": 0, "cost_usd": 0}, finish_reason="tool_calls")


class Scripted:
    """A provider whose answers come from an async route, so tests can hold a call open."""

    route = None
    calls: list = []

    def __init__(self, settings, api_key, transport=None):
        self.settings = settings
        self.api_key = api_key

    async def close(self):
        pass

    async def count_input_tokens(self, messages, tools=None):
        return None

    async def complete(self, messages, tools=None, on_delta=None):
        Scripted.calls.append(messages)
        response = await Scripted.route(messages, tools)
        if on_delta and response.text:
            await on_delta(response.text)
        return response


@pytest.fixture
def scripted(monkeypatch):
    Scripted.calls = []
    monkeypatch.setattr(runtime_module, "ModelProvider", Scripted)
    monkeypatch.setattr(runtime_module, "UPDATE_BATCH_SECONDS", 0.05)
    return Scripted


def is_responder(messages):
    return RESPONDER_MARK in messages[0]["content"]


def is_child(messages):
    return "Your assigning agent" in messages[0]["content"]


def visible(store, project_id):
    return [(e["data"]["role"], e["data"]["text"], e["data"].get("channel"), e["data"].get("responder"))
            for e in store.project_messages(project_id)]


async def until(condition, timeout=10.0):
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.02)


def test_question_while_waiting_on_a_subagent(store, project, scripted):
    """The user gets an answer while the coordinator waits; the helper keeps working; nothing repeats."""
    gate = asyncio.Event()

    async def route(messages, tools):
        if is_responder(messages):
            assert "Helper" in messages[0]["content"]  # it sees the team state
            return reply("Helper is still looking around; nothing to report yet.")
        if is_child(messages):
            await gate.wait()
            return reply("Found it in app.py.")
        last = messages[-1]
        if last["role"] == "user" and last["content"] == "Investigate":
            return python("h = agents.spawn('helper', 'look around')\nagents.wait([h['id']], timeout=60)")
        if last["role"] == "user" and "How is it going" in last["content"]:
            assert "A quick reply to this message is being sent" in last["content"] or \
                "you already replied" in last["content"]
            return python("agents.wait([a['id'] for a in agents.list() if a['role'] == 'helper'], timeout=60)",
                          call_id="c2")
        if last["role"] == "tool":
            return reply("Waiting for the helper.")
        return reply("The helper found it in app.py.")

    scripted.route = route

    async def scenario():
        runtime = Runtime(store)
        run = await runtime.start_run(project["id"], "Investigate")
        await until(lambda: any(a["role"] == "helper" for a in store.list("agents", run_id=run["id"]))
                    and run["root_agent_id"] in runtime.waiting)
        await runtime.steer(run["id"], "How is it going?")
        await until(lambda: any(m[3] for m in visible(store, project["id"])))
        helper = next(a for a in store.list("agents", run_id=run["id"]) if a["role"] == "helper")
        assert helper["status"] == "running"  # answering did not stop the work
        gate.set()
        await settle(runtime)
        await runtime.close()
        return run["id"]

    run_id = asyncio.run(scenario())
    run = store.get("runs", run_id)
    assert run["status"] == "waiting", run.get("error")
    chat = visible(store, project["id"])
    assert chat == [
        ("user", "Investigate", None, None),
        ("user", "How is it going?", None, None),
        ("assistant", "Helper is still looking around; nothing to report yet.", "reply", True),
        ("assistant", "The helper found it in app.py.", "reply", None),
    ]
    helpers = [a for a in store.list("agents", run_id=run_id) if a["role"] == "helper"]
    assert len(helpers) == 1 and helpers[0]["status"] == "completed"
    events = store.events(run_id, limit=2000)
    delivered = [e for e in events if e["kind"] == "message.delivered"]
    question = next(e for e in events if e["kind"] == "message" and e["data"].get("text") == "How is it going?")
    assert any(e["data"]["message_event_id"] == question["id"] for e in delivered)
    # agents.wait returned early for the message instead of running to its 60-second timeout.
    waits = [e for e in events if e["kind"] == "tool.finished" and e["data"].get("name") == "agents.wait"]
    assert "received a new message" in json.dumps(waits[0]["data"])
    answer = next(e for e in events if e["kind"] == "message" and e["data"].get("responder"))
    assert answer["data"]["reply_to"] == question["id"]
    assert answer["data"]["first_text_ms"] is not None and answer["data"]["scheduled_ms"] is not None
    # Streaming deltas are tagged so the UI can tell a reply from the coordinator's private draft.
    assert {e["data"].get("stream") for e in events if e["kind"] == "agent.delta"} <= {"reply", "draft"}


def test_question_during_a_model_call_and_a_tool_cell(store, project, scripted):
    gate = asyncio.Event()
    state = {"step": 0}

    async def route(messages, tools):
        if is_responder(messages):
            return reply(f"Answer {sum(1 for m in Scripted.calls if is_responder(m))}.")
        state["step"] += 1
        if state["step"] == 1:
            await gate.wait()  # a slow model call
            return python("import time\ntime.sleep(1.0)\n'slept'")
        if messages[-1]["role"] == "tool":
            return reply("Done sleeping.")
        return reply("Noted.")

    scripted.route = route

    async def scenario():
        runtime = Runtime(store)
        run = await runtime.start_run(project["id"], "Sleep")
        await until(lambda: state["step"] == 1)
        await runtime.steer(run["id"], "Question during the model call?")
        await until(lambda: sum(1 for m in visible(store, project["id"]) if m[3]) == 1)
        gate.set()
        await until(lambda: any(e["kind"] == "tool.started" and e["data"].get("name") == "python"
                                for e in store.events(run["id"], limit=2000)))
        await runtime.steer(run["id"], "Question during the tool cell?")
        await until(lambda: sum(1 for m in visible(store, project["id"]) if m[3]) == 2)
        cell_running = not any(e["kind"] == "tool.finished" and e["data"].get("name") == "python"
                               for e in store.events(run["id"], limit=2000))
        await settle(runtime)
        await runtime.close()
        return run["id"], cell_running

    run_id, cell_running = asyncio.run(scenario())
    assert cell_running, "the reply should arrive before the tool cell finishes"
    assert store.get("runs", run_id)["status"] == "waiting"
    answers = [m[1] for m in visible(store, project["id"]) if m[3]]
    assert answers == ["Answer 1.", "Answer 2."]


def test_steering_reaches_the_coordinator_and_is_not_marked_applied(store, project, scripted):
    gate = asyncio.Event()

    async def route(messages, tools):
        if is_responder(messages):
            assert "Never say that it has been applied" in messages[0]["content"]
            return reply("Received; I've passed that to the coordinator.")
        if is_child(messages):
            await gate.wait()
            return reply("Code finished.")
        last = messages[-1]
        if last["content"] == "Build it":
            return python("c = agents.spawn('code', 'implement')\nagents.wait([c['id']], timeout=60)")
        if "Use approach B" in str(last["content"]):
            return python("agents.send([a['id'] for a in agents.list() if a['role']=='code'][0], 'Use approach B')\n"
                          "chat.reply('Applied: Code now uses approach B.')", call_id="c2")
        if last["role"] == "tool":
            return reply("Waiting.")
        return reply("Code is done with approach B.")

    scripted.route = route

    async def scenario():
        runtime = Runtime(store)
        run = await runtime.start_run(project["id"], "Build it")
        await until(lambda: run["root_agent_id"] in runtime.waiting)
        await runtime.steer(run["id"], "Use approach B")
        try:
            await until(lambda: any("Applied" in m[1] for m in visible(store, project["id"])))
        except TimeoutError:
            for e in store.events(run["id"], limit=2000):
                if e["kind"] not in ("agent.delta", "usage"):
                    print(e["kind"], json.dumps(e["data"])[:300])
            raise
        gate.set()
        await settle(runtime)
        await runtime.close()
        return run["id"]

    run_id = asyncio.run(scenario())
    chat = visible(store, project["id"])
    texts = [m[1] for m in chat]
    assert texts.index("Received; I've passed that to the coordinator.") < texts.index(
        "Applied: Code now uses approach B.")
    code = next(a for a in store.list("agents", run_id=run_id) if a["role"] == "code")
    assert any("Use approach B" in m.get("content", "") for m in transcript(store, code["id"]))


def test_only_the_main_agent_talks_to_the_user(store, project, scripted):
    async def route(messages, tools):
        if is_child(messages):
            if messages[-1]["role"] == "tool":
                return reply("ok")
            return python("chat.reply('hi from a worker')")
        if messages[-1]["role"] == "tool":
            return reply("waiting")
        if messages[-1]["content"] == "Go":
            return python("h = agents.spawn('helper', 'say hi')\nagents.wait([h['id']], timeout=30)")
        return reply("done")

    scripted.route = route

    async def scenario():
        runtime = Runtime(store)
        run = await runtime.start_run(project["id"], "Go")
        await settle(runtime)
        await runtime.close()
        return run["id"]

    run_id = asyncio.run(scenario())
    assert "hi from a worker" not in [m[1] for m in visible(store, project["id"])]
    helper = next(a for a in store.list("agents", run_id=run_id) if a["role"] == "helper")
    assert "Only the main agent talks to the user" in json.dumps(transcript(store, helper["id"]))


def test_milestone_updates_are_grounded_batched_and_not_repeated(store, project, scripted):
    async def route(messages, tools):
        last = messages[-1]
        if last["content"] == "Plan it":
            return python("workflow.advance('spec')\nworkflow.advance('spec')\n"
                          "chat.notify('Which API should it use?', kind='question')\n"
                          "chat.notify('Which API should it use?', kind='question')")
        if last["role"] == "tool":
            return reply("Started planning.")
        return reply("ok")

    scripted.route = route

    async def scenario():
        runtime = Runtime(store)
        run = await runtime.start_run(project["id"], "Plan it")
        await settle(runtime)
        await asyncio.sleep(0.3)  # let the batched update flush
        await runtime.close()
        return run["id"]

    run_id = asyncio.run(scenario())
    updates = [e for e in store.project_messages(project["id"]) if e["data"].get("channel") == "update"]
    assert [u["data"]["text"] for u in updates] == ["Which API should it use?", "Spec is working out the plan."]
    milestone = updates[1]["data"]
    phase_events = [e["id"] for e in store.events(run_id, limit=2000) if e["kind"] == "workflow.phase"]
    assert milestone["sources"] == phase_events[:1]


def test_repository_switch_keeps_pending_replies_separate(store, project, repo, scripted, tmp_path):
    import shutil
    other_repo = tmp_path / "other"
    shutil.copytree(repo, other_repo)
    other = store.put("projects", "p2", {"name": "other", "path": str(other_repo), "description": ""})
    gate = asyncio.Event()

    async def route(messages, tools):
        if is_responder(messages):
            return reply("Reply for the first repository.")
        if "Project other" in messages[0]["content"]:
            return reply("Second repository answer.")
        if messages[-1]["content"] == "Slow one":
            await gate.wait()
            return reply("First repository finished.")
        return reply("Nothing further on the status.")

    scripted.route = route

    async def scenario():
        runtime = Runtime(store)
        first = await runtime.message_project(project["id"], "Slow one")
        await until(lambda: len(Scripted.calls) == 1)
        await runtime.message_project(project["id"], "Status?")
        second = await runtime.message_project(other["id"], "Hello other")
        await until(lambda: any(m[3] for m in visible(store, project["id"])))
        gate.set()
        await settle(runtime)
        await runtime.close()
        return first["id"], second["id"]

    first_id, second_id = asyncio.run(scenario())
    assert first_id != second_id
    assert [m[1] for m in visible(store, other["id"])] == ["Hello other", "Second repository answer."]
    assert [m[1] for m in visible(store, project["id"])] == [
        "Slow one", "Status?", "Reply for the first repository.", "First repository finished.",
        "Nothing further on the status."]


def test_wait_wakes_promptly_when_a_child_finishes(store, project, scripted):
    async def route(messages, tools):
        if is_child(messages):
            await asyncio.sleep(0.3)
            return reply("child done")
        if messages[-1]["content"] == "Go":
            return python("h = agents.spawn('helper', 'quick')\nagents.wait([h['id']], timeout=60)")
        return reply("done")

    scripted.route = route

    async def scenario():
        runtime = Runtime(store)
        run = await runtime.start_run(project["id"], "Go")
        await settle(runtime)
        await runtime.close()
        return run["id"]

    from datetime import datetime
    run_id = asyncio.run(scenario())
    events = store.events(run_id, limit=2000)
    helper_done = next(e for e in events if e["kind"] == "agent.status" and e["data"]["status"] == "completed")
    wait_done = next(e for e in events if e["kind"] == "tool.finished" and e["data"].get("name") == "agents.wait")
    delay = (datetime.fromisoformat(wait_done["created_at"]) - datetime.fromisoformat(helper_done["created_at"]))
    assert delay.total_seconds() < 0.5


def test_reports_from_awaited_agents_do_not_cut_a_wait_short(store, project, scripted):
    async def route(messages, tools):
        if is_child(messages):
            await asyncio.sleep(0.1 if "fast" in messages[-1]["content"] else 0.6)
            return reply("child done")
        if messages[-1]["content"] == "Go":
            return python("a = agents.spawn('helper', 'fast job')\nb = agents.spawn('spec', 'slow job')\n"
                          "[r['status'] for r in agents.wait([a['id'], b['id']], timeout=60)]")
        return reply("done")

    scripted.route = route

    async def scenario():
        runtime = Runtime(store)
        run = await runtime.start_run(project["id"], "Go")
        await settle(runtime)
        await runtime.close()
        return run["id"]

    run_id = asyncio.run(scenario())
    waits = [e for e in store.events(run_id, limit=2000)
             if e["kind"] == "tool.finished" and e["data"].get("name") == "agents.wait"]
    assert len(waits) == 1
    assert [r["status"] for r in waits[0]["data"]["result"]] == ["completed", "completed"]


def test_identical_answers_to_different_questions_are_both_saved(store, project, scripted):
    gate = asyncio.Event()

    async def route(messages, tools):
        if is_responder(messages):
            return reply("Code is still working.")
        if messages[-1]["content"] == "Build":
            await gate.wait()
            return reply("Built.")
        return reply("Nothing new.")

    scripted.route = route

    async def scenario():
        runtime = Runtime(store)
        run = await runtime.start_run(project["id"], "Build")
        await until(lambda: len(Scripted.calls) == 1)
        await runtime.steer(run["id"], "Status?")
        await until(lambda: sum(1 for m in visible(store, project["id"]) if m[3]) == 1)
        await runtime.steer(run["id"], "And now?")
        await until(lambda: sum(1 for m in visible(store, project["id"]) if m[3]) == 2)
        gate.set()
        await settle(runtime)
        await runtime.close()

    asyncio.run(scenario())
    chat = [(m[0], m[1]) for m in visible(store, project["id"])]
    assert chat[:5] == [("user", "Build"), ("user", "Status?"), ("assistant", "Code is still working."),
                        ("user", "And now?"), ("assistant", "Code is still working.")]
