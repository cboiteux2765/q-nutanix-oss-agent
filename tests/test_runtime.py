import asyncio


from evo.runtime import Runtime

from .conftest import FakeProvider, python_call, reply, settle, transcript


def run_async(coro):
    return asyncio.run(coro)


def test_simple_reply_finishes_waiting(store, project, fake_provider):
    fake_provider.script = [reply("Hello there.")]

    async def scenario():
        runtime = Runtime(store)
        run = await runtime.start_run(project["id"], "Say hello")
        await settle(runtime)
        await runtime.close()
        return run["id"]

    run_id = run_async(scenario())
    run = store.get("runs", run_id)
    assert run["status"] == "waiting"
    assert run["token_usage"]["input"] == 10
    root = store.get("agents", run["root_agent_id"])
    assert root["status"] == "waiting"
    assert root["result"] == "Hello there."
    messages = transcript(store, root["id"])
    assert [m["role"] for m in messages] == ["system", "user", "assistant"]
    chat = store.project_messages(project["id"])
    assert [(e["data"]["role"], e["data"]["text"]) for e in chat] == [
        ("user", "Say hello"), ("assistant", "Hello there.")]


def test_tool_call_runs_python_and_records_result(store, project, fake_provider):
    fake_provider.script = [python_call("print(len(agents.list()))"), reply("One agent.")]

    async def scenario():
        runtime = Runtime(store)
        run = await runtime.start_run(project["id"], "Count agents")
        await settle(runtime)
        await runtime.close()
        return run["id"]

    run_id = run_async(scenario())
    run = store.get("runs", run_id)
    assert run["status"] == "waiting", run.get("error")
    messages = transcript(store, run["root_agent_id"])
    assert [m["role"] for m in messages] == ["system", "user", "assistant", "tool", "assistant"]
    assert "1" in messages[3]["content"]
    kinds = [e["kind"] for e in store.events(run_id)]
    assert "tool.started" in kinds and "tool.finished" in kinds
    # The interim tool-calling assistant message is recorded but hidden from the chat.
    visible = [e["data"]["text"] for e in store.project_messages(project["id"])]
    assert visible == ["Count agents", "One agent."]


def test_budget_refuses_a_call_that_might_not_fit(store, project, fake_provider):
    """The budget is checked before each call against its worst case, so it is never overshot."""
    store.put("settings", "provider", {"max_run_tokens": 100})
    fake_provider.script = [reply("never")]

    async def scenario():
        runtime = Runtime(store)
        run = await runtime.start_run(project["id"], "Spend")
        await settle(runtime)
        await runtime.close()
        return run["id"]

    run = store.get("runs", run_async(scenario()))
    assert run["status"] == "failed"
    assert run["error"].startswith("Run reached its token budget: the next model call could use up to")
    assert FakeProvider.calls == [] and run["token_usage"]["input"] + run["token_usage"]["output"] == 0


def test_budget_reads_the_saved_run_snapshot(store, project, fake_provider):
    """Changing a run's saved budget applies to its next model call, mid-turn."""
    holder = {}

    def lower_budget(messages):
        run = store.get("runs", holder["run_id"])
        store.put("runs", run["id"], {"settings_snapshot": run["settings_snapshot"] | {"max_run_tokens": 1_000}})
        return python_call("1", tokens=30)

    fake_provider.script = [lower_budget, reply("never")]

    async def scenario():
        runtime = Runtime(store)
        run = await runtime.start_run(project["id"], "Spend")
        holder["run_id"] = run["id"]
        await settle(runtime)
        await runtime.close()
        return run["id"]

    run = store.get("runs", run_async(scenario()))
    assert run["status"] == "failed" and "token budget" in run["error"]
    assert len(FakeProvider.calls) == 1
    assert run["token_usage"]["input"] + run["token_usage"]["output"] == 60


def test_conversation_bound_uses_the_reported_prefix():
    from evo.budget import ConversationBound, input_bound
    bound = ConversationBound()
    history = [{"role": "system", "content": "x" * 5000}, {"role": "user", "content": "hi"}]
    full = input_bound(history)
    assert bound.bound("a", history) == full
    bound.record("a", history, {"input": 1300})
    grown = [*history, {"role": "assistant", "content": "ok"}, {"role": "user", "content": "more"}]
    tighter = bound.bound("a", grown)
    assert tighter == 1300 + input_bound(grown) - input_bound(history)
    # A rewritten history (compaction) falls back to the byte bound.
    rewritten = [{"role": "system", "content": "y" * 5000}, *grown[1:]]
    assert bound.bound("a", rewritten) == input_bound(rewritten)
    # No reported usage: no shortcut.
    bound.record("a", history, {})
    assert bound.bound("a", grown) == input_bound(grown)


def test_compaction_replaces_older_history(store, project, fake_provider):
    fake_provider.script = [reply("first"), reply("second"), reply("third"), reply("Summary of earlier turns.")]

    async def scenario():
        runtime = Runtime(store)
        run = await runtime.start_run(project["id"], "one")
        await settle(runtime)
        await runtime.steer(run["id"], "two")
        await settle(runtime)
        await runtime.steer(run["id"], "three")
        await settle(runtime)
        result = await runtime.compact(run["root_agent_id"])
        await runtime.close()
        return run["id"], result

    run_id, result = run_async(scenario())
    assert result["status"] == "completed"
    root_id = store.get("runs", run_id)["root_agent_id"]
    messages = transcript(store, root_id)
    assert [m["role"] for m in messages] == ["system", "user", "user", "assistant"]
    assert messages[1]["content"].startswith("Historical context checkpoint")
    assert "Summary of earlier turns." in messages[1]["content"]
    assert messages[2]["content"] == "three"
    assert any(e["kind"] == "compaction" for e in store.events(run_id, limit=1000))


def test_spawned_child_reports_to_parent(store, project, fake_provider):
    def router(messages):
        if "Your assigning agent" in messages[0]["content"]:
            return reply("Child finished the task.")
        last = messages[-1]
        if last["role"] == "user" and last["content"] == "Delegate":
            return python_call("print(agents.spawn('helper', 'look around')['role'])")
        if last["role"] == "tool":
            return reply("Parent waits.")
        return reply("Parent got the report.")

    fake_provider.script = [router] * 10

    async def scenario():
        runtime = Runtime(store)
        run = await runtime.start_run(project["id"], "Delegate")
        await settle(runtime)
        await runtime.close()
        return run["id"]

    run_id = run_async(scenario())
    run = store.get("runs", run_id)
    agents = store.list("agents", run_id=run_id)
    assert {a["role"] for a in agents} == {"orchestrator", "helper"}
    child = next(a for a in agents if a["role"] == "helper")
    assert child["status"] == "completed"
    assert child["parent_id"] == run["root_agent_id"]
    root = store.get("agents", run["root_agent_id"])
    assert root["result"] == "Parent got the report.", run.get("error")
    assert "Child finished the task." in transcript(store, root["id"])[-2]["content"]


def test_secret_is_redacted_from_stream_and_events(store, project, fake_provider):
    from evo.config import get_settings, set_api_key

    secret = "sk-test-SECRET-1234567890"
    set_api_key(get_settings(store), secret)
    fake_provider.script = [reply(f"The key is {secret}.")]

    async def scenario():
        runtime = Runtime(store)
        run = await runtime.start_run(project["id"], "Leak it")
        await settle(runtime)
        await runtime.close()
        return run["id"]

    run_id = run_async(scenario())
    try:
        dump = str(store.events(run_id, limit=1000))
        assert secret not in dump
        assert "[redacted]" in dump
    finally:
        from evo import config
        config._session_keys.clear()
