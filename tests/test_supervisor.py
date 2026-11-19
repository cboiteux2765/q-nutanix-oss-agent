"""Scripted project conversations and workflow routing."""

import asyncio
import json

import pytest

from evo.config import update_settings
from evo.providers import ModelResponse, ToolCall
from evo.runtime import Runtime
from evo.supervisor import SupervisorService

from .conftest import reply, settle
from .test_conversation import scripted, until  # noqa: F401


def tools(*calls):
    return ModelResponse(
        "", [ToolCall(str(index), name, json.dumps(arguments))
             for index, (name, arguments) in enumerate(calls)],
        usage={"input": 10, "output": 10, "cached": 0, "cost_usd": 0}, finish_reason="tool_calls",
    )


def test_single_supervisor_reads_two_workflows_and_forwards_attribution(store, project, fake_provider):
    project_id = project["id"]

    async def scenario():
        runtime = Runtime(store)
        supervisor = SupervisorService(runtime)
        other_project = store.put("projects", "other", {"name": "Other", "path": project["path"]})
        try:
            # Explicit selectors produce two independent workflow roots.
            one = await supervisor.message(project_id, "Implement outline", new_assignment=True)
            await settle(runtime)
            two = await supervisor.message(project_id, "Build import queue", new_assignment=True)
            await settle(runtime)
            assert one["id"] == two["id"]
            assert one["phase"] is None
            first_id = one["routing"]["workflow_id"]
            second_id = two["routing"]["workflow_id"]
            first, second = [store.get("runs", run_id) for run_id in (first_id, second_id)]
            assert first["branch"] != second["branch"]
            assert first["workdir"] != second["workdir"]
            assert first["root_agent_id"] != second["root_agent_id"]
            assert len(supervisor.list_workflows(project_id)) == 2
            assert len(supervisor.overview(project_id)["workflows"]) == 2
            original = runtime.artifacts.write(second_id, second["root_agent_id"], "Evidence", "A complete result.")

            def ask(messages):
                overview = messages[1]["content"]
                assert first_id in overview and second_id in overview
                assert "workflow_count" in overview
                return tools(
                    ("workflows_agents", {"run_id": first_id}),
                    ("workflows_transcript", {"run_id": second_id, "agent_id": second["root_agent_id"]}),
                    ("workflows_artifacts", {"run_id": second_id}),
                    ("artifacts_read", {"artifact_id": original["id"]}),
                )

            def answer(messages):
                assert "Workflow Coordinator" in messages[-4]["content"]
                assert "entries" in messages[-3]["content"]
                assert original["id"] in messages[-2]["content"]
                assert "A complete result." in messages[-1]["content"]
                return reply("I checked both workflows and read the original artifact.")

            fake_provider.script = [ask, answer]
            chat = await supervisor.message(project_id, "Where are the two workflows?")
            await settle(runtime)
            assert chat["id"] == one["id"]
            assert one["root_agent_id"] not in runtime.repls
            with pytest.raises(KeyError):
                supervisor.status(other_project["id"], first_id)
            with pytest.raises(KeyError):
                supervisor.transcript(project_id, first_id, second["root_agent_id"])
            visible = store.project_messages(project_id)
            assert [e["data"]["text"] for e in visible if e["data"]["role"] == "user"] == [
                "Implement outline", "Build import queue", "Where are the two workflows?",
            ]
            assert {e["data"].get("source_run_id") for e in visible} >= {first_id, second_id}
            assert visible[-1]["data"]["text"] == "I checked both workflows and read the original artifact."
        finally:
            await runtime.close()

    asyncio.run(scenario())


def test_selected_workflow_receipt_tracks_delivery_and_application(store, project, fake_provider):
    project_id = project["id"]

    async def scenario():
        runtime = Runtime(store)
        supervisor = SupervisorService(runtime)
        try:
            response = await supervisor.message(project_id, "First task", new_assignment=True)
            await settle(runtime)
            run_id = response["routing"]["workflow_id"]
            run = store.get("runs", run_id)
            await runtime.pause(run_id)
            directed = await supervisor.message(project_id, "Use the revised acceptance criteria", run_id=run_id)
            receipt = directed["routing"]
            event_id = receipt["message_event_id"]
            assert receipt["status"] == "received"
            assert receipt["source_event_id"] == directed["message_event_id"]
            assert store.get("runs", run_id)["status"] == "paused"
            assert not runtime.tasks
            with pytest.raises(ValueError, match="has not read"):
                supervisor.applied(project_id, run_id, run["root_agent_id"], event_id, "Changed")

            def coordinator(messages):
                assert event_id in messages[-1]["content"]
                assert supervisor.delivery(project_id, run_id, event_id)["status"] == "delivered"
                return reply("I applied the revised criteria.")

            fake_provider.script = [coordinator]
            await runtime.resume(run_id)
            await settle(runtime)
            applied = supervisor.applied(
                project_id, run_id, run["root_agent_id"], event_id, "Updated the criteria",
            )
            assert applied["status"] == "applied"
            assert supervisor.delivery(project_id, run_id, event_id) == applied
            visible = store.project_messages(project_id)
            message = next(e for e in visible if e["data"]["text"] == "I applied the revised criteria.")
            assert message["data"]["source_event_id"] == directed["message_event_id"]
            assert len([e for e in visible if e["data"]["text"] == "Use the revised acceptance criteria"]) == 1
        finally:
            await runtime.close()

    asyncio.run(scenario())


def test_passive_suggestion_has_no_model_or_workflow_and_refreshes_provider(store, project, fake_provider):
    project_id = project["id"]

    async def scenario():
        runtime = Runtime(store)
        supervisor = SupervisorService(runtime)
        try:
            suggestion = {"id": "hint-1", "title": "Triage open report", "summary": "A new issue needs triage.",
                          "source_urls": ["https://example.test/report"], "event_ids": ["activity-1"]}
            posted = await supervisor.suggest(project_id, suggestion)
            duplicate = await supervisor.suggest(project_id, suggestion)
            assert posted == duplicate
            assert not fake_provider.calls
            assert not supervisor.list_workflows(project_id)
            assert not runtime.tasks
            messages = store.project_messages(project_id)
            assert len(messages) == 1
            assert messages[0]["data"]["sources"] == suggestion["source_urls"]
            assert messages[0]["data"]["activity_event_ids"] == suggestion["event_ids"]

            update_settings(store, {"model": "new-project-model"})

            def respond(messages):
                assert "A new issue needs triage." in messages[1]["content"]
                assert store.get("runs", posted["run_id"])["settings_snapshot"]["model"] == "new-project-model"
                return reply("There is one suggested issue to triage.")

            fake_provider.script = [respond]
            response = await supervisor.message(project_id, "What was suggested?")
            await settle(runtime)
            assert response["id"] == posted["run_id"]
            assert len(fake_provider.calls) == 1
        finally:
            await runtime.close()

    asyncio.run(scenario())


def test_supervisor_tools_use_only_active_human_turn_and_refuse_python(store, project, fake_provider):
    project_id = project["id"]

    async def scenario():
        runtime = Runtime(store)
        supervisor = SupervisorService(runtime)
        try:
            def finished(messages):
                assert "unavailable to the supervisor" in messages[-1]["content"]
                return reply("The read-only request completed.")

            fake_provider.script = [tools(("python", {"code": "import os"})), finished]
            run = await supervisor.message(project_id, "Read the project status")
            await settle(runtime)
            assert not runtime.repls
            assert not supervisor.list_workflows(project_id)
            with pytest.raises(ValueError, match="require an instruction"):
                await runtime.broker.dispatch(run["root_agent_id"], "workflows.start", ["Unauthorized"], {})
            with pytest.raises(PermissionError):
                await runtime.broker.dispatch(run["root_agent_id"], "workflow.advance", ["code"], {})
        finally:
            await runtime.close()

    asyncio.run(scenario())


def test_busy_supervisor_quick_response_reads_project_workflows(store, project, scripted):
    project_id = project["id"]

    async def scenario():
        runtime = Runtime(store)
        supervisor = SupervisorService(runtime)
        gate = asyncio.Event()
        entered = asyncio.Event()
        holder = {}

        async def route(messages, model_tools):
            system = messages[0]["content"]
            if "working process is busy" in system:
                assert holder["workflow_id"] in system
                return reply("The selected workflow is waiting.")
            if "persistent project supervisor" not in system:
                return reply("The workflow is waiting.")
            if "Hold the first reply" in messages[-1]["content"]:
                entered.set()
                await gate.wait()
                return reply("The first reply ended.")
            return reply("Continuing.")

        scripted.route = route
        try:
            created = await supervisor.message(project_id, "Workflow to inspect", new_assignment=True)
            holder["workflow_id"] = created["routing"]["workflow_id"]
            await settle(runtime)
            await supervisor.message(project_id, "Hold the first reply")
            await asyncio.wait_for(entered.wait(), 5)
            await supervisor.message(project_id, "How is the selected workflow?")
            await until(lambda: any(e["data"].get("responder") for e in store.project_messages(project_id)))
            quick = next(e for e in store.project_messages(project_id) if e["data"].get("responder"))
            assert quick["data"]["text"] == "The selected workflow is waiting."
        finally:
            gate.set()
            await runtime.close()

    asyncio.run(scenario())
