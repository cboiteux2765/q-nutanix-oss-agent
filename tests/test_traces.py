import asyncio
import json
import sqlite3
from contextlib import closing

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from evo.runtime import Runtime
from evo.store import Store
from evo.traces import router
from evo.workflow import ArtifactStore

from .conftest import python_call, reply, settle


def _client(store):
    app = FastAPI()
    app.state.store = store
    app.include_router(router)
    return TestClient(app)


def _trace_path(project_id, run_id, agent_id):
    return f"/api/projects/{project_id}/runs/{run_id}/agents/{agent_id}/trace"


def test_trace_survives_compaction_and_restart(store, project, fake_provider, monkeypatch):
    secret = "sk-trace-secret-12345"
    monkeypatch.setenv("EVO_TRACE_TEST_KEY", secret)
    store.put("settings", "provider", {"api_key_env": "EVO_TRACE_TEST_KEY"})
    cell = python_call(
        f"print(repo.read('app.py'))\nprint(repo.read('app.py', lines=1))\n"
        f"print({secret!r})\nprint('a' * 25000)"
    )
    cell.chat_reasoning = f"Provider reasoning includes {secret}."
    second = reply("Second answer")
    second.output_items = [
        {"type": "reasoning", "summary": [{"type": "summary_text", "text": f"Provider summary {secret}."}],
         "content": [{"type": "reasoning_text", "text": "Provider reasoning text."}],
         "encrypted_content": "opaque-replay-token"},
    ]
    fake_provider.script = [cell, reply("First answer"), second, reply("Third answer"), reply("Checkpoint")]

    async def scenario():
        runtime = Runtime(store)
        try:
            run = await runtime.start_run(project["id"], f"First question {secret}")
            await settle(runtime)
            await runtime.steer(run["id"], "Second question")
            await settle(runtime)
            await runtime.steer(run["id"], "Third question")
            await settle(runtime)
            result = await runtime.compact(run["root_agent_id"])
            assert result["status"] == "completed"
            return run
        finally:
            await runtime.close()

    run = asyncio.run(scenario())
    monkeypatch.delenv("EVO_TRACE_TEST_KEY")
    agent_id = run["root_agent_id"]
    assert not any("First answer" == m.get("content") for m in store.messages(agent_id))
    with closing(Store(store.data_dir)) as reopened, _client(reopened) as client:
        path = _trace_path(project["id"], run["id"], agent_id)
        page = client.get(f"{path}/transcript").json()
        assert page["completeness"] == "complete"
        assert page["next_cursor"] is None
        entries = page["entries"]
        assert [entry["message"]["role"] for entry in entries] == [
            "system", "user", "assistant", "tool", "assistant", "user", "assistant", "user", "assistant",
        ]
        assert entries[1]["message"]["content"] == "First question [redacted]"
        assert entries[2]["reasoning"] == [{"kind": "content", "text": "Provider reasoning includes [redacted]."}]
        assert entries[6]["reasoning"] == [
            {"kind": "summary", "text": "Provider summary [redacted]."},
            {"kind": "content", "text": "Provider reasoning text."},
        ]
        assert entries[8]["reasoning"] == []
        serialized = json.dumps(page)
        assert secret not in serialized
        assert "opaque-replay-token" not in serialized
        assert entries[4]["message"]["content"] == "First answer"

        events = client.get(f"{path}/events").json()["entries"]
        starts = [event["data"] for event in events if event["kind"] == "tool.started"]
        finishes = {event["data"]["span_id"]: event for event in events if event["kind"] == "tool.finished"}
        python, read_a, read_b = starts
        assert python["name"] == "python" and python["parent_span_id"] is None
        assert read_a["name"] == read_b["name"] == "repo.read"
        assert read_a["parent_span_id"] == read_b["parent_span_id"] == python["span_id"]
        assert read_a["span_id"] != read_b["span_id"]
        assert read_b["arguments"] == {"args": ["app.py"], "kwargs": {"lines": 1}}
        assert "print('hello')" in json.dumps(finishes[read_b["span_id"]]["data"]["result"])
        completed_cell = finishes[python["span_id"]]
        assert entries[3]["event_id"] == completed_cell["id"]
        result = completed_cell["data"]["result"]
        assert json.loads(entries[3]["message"]["content"]) == result
        output = next(event for event in events if event["id"] == result["event_id"])
        assert output["kind"] == "tool.output"
        assert output["data"]["span_id"] == python["span_id"]
        assert output["data"]["artifact_ids"] == result["artifact_ids"]
        artifact = ArtifactStore(reopened).read(result["artifact_ids"][0])
        assert "[redacted]" in artifact["content"] and secret not in artifact["content"]
        assert secret not in json.dumps(events)


def test_trace_pages_use_stable_cursors_and_scope_to_project_run_and_agent(store):
    for project_id in ("p1", "p2"):
        store.put("projects", project_id, {})
    for run_id, project_id in (("r1", "p1"), ("r2", "p1"), ("r3", "p2")):
        store.put("runs", run_id, {"project_id": project_id})
    for agent_id, run_id in (("a1", "r1"), ("sibling", "r1"), ("a2", "r2"), ("a3", "r3")):
        store.put("agents", agent_id, {"run_id": run_id})

    def record(run_id, agent_id, text):
        event = store.event(run_id, agent_id, "message", {"role": "user", "text": text})
        store.append_messages(agent_id, [({"role": "user", "content": text}, event["id"])])

    record("r1", "a1", "one")
    record("r1", "sibling", "other agent")
    record("r2", "a2", "other run")
    record("r1", "a1", "two")
    record("r3", "a3", "other project")
    record("r1", "a1", "three")

    with _client(store) as client:
        path = _trace_path("p1", "r1", "a1")
        for endpoint, get_text in (
            ("transcript", lambda entry: entry["message"]["content"]),
            ("events", lambda entry: entry["data"]["text"]),
        ):
            response = client.get(f"{path}/{endpoint}", params={"limit": 2})
            assert response.status_code == 200
            page = response.json()
            assert [get_text(entry) for entry in page["entries"]] == ["one", "two"]
            cursor = page["next_cursor"]
            assert cursor == page["entries"][-1]["seq"]
            response = client.get(f"{path}/{endpoint}", params={"after": cursor, "limit": 2})
            assert [get_text(entry) for entry in response.json()["entries"]] == ["three"]
            assert response.json()["next_cursor"] is None
            assert client.get(f"{path}/{endpoint}", params={"after": -1}).status_code == 422
            for project_id, run_id, agent_id in (
                ("p2", "r1", "a1"), ("p1", "r2", "a1"), ("p1", "r1", "a2"), ("missing", "r1", "a1"),
            ):
                wrong_path = _trace_path(project_id, run_id, agent_id)
                assert client.get(f"{wrong_path}/{endpoint}").status_code == 404
        end = client.get(f"{path}/transcript").json()["entries"][-1]["seq"]
        record("r1", "a1", "four")
        new_page = client.get(f"{path}/transcript", params={"after": end}).json()
        assert [entry["message"]["content"] for entry in new_page["entries"]] == ["four"]


def test_tool_completion_rolls_back_when_transcript_cannot_be_committed(store):
    with closing(sqlite3.connect(store.data_dir / "evo.db")) as db:
        db.execute(
            "CREATE TRIGGER fail_transcript BEFORE INSERT ON agent_transcript "
            "BEGIN SELECT RAISE(ABORT, 'transcript write failed'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="transcript write failed"):
        store.event("r1", "a1", "tool.finished", {"result": "done"},
                    message={"role": "tool", "tool_call_id": "call-1", "content": "done"})
    assert store.events("r1", "a1") == store.messages("a1") == store.transcript("a1") == []


def test_legacy_backfill_recovers_checkpoint_links_and_reports_partial_history(tmp_path):
    with closing(Store(tmp_path)) as old:
        old.put("projects", "p1", {})
        old.put("runs", "r1", {"project_id": "p1"})
        old.put("agents", "a1", {"run_id": "r1"})
        first = old.event("r1", "a1", "message", {"role": "user", "text": "First question"})
        older_checkpoint = old.event("r1", "a1", "compaction", {"event_ids": [first["id"]]})
        other_agent = old.event("r1", "a2", "message", {"role": "user", "text": "Other agent"})
        tool = old.event("r1", "a1", "tool.finished", {
            "name": "python", "call_id": "call-1", "result": {"artifact_ids": ["saved-output"]},
        })
        checkpoint = old.event("r1", "a1", "compaction", {
            "event_ids": [older_checkpoint["id"], other_agent["id"], tool["id"]],
        })
        old.replace_messages("a1", [
            ({"role": "system", "content": "Instructions"}, None),
            ({"role": "user", "content": "Current checkpoint"}, checkpoint["id"]),
            ({"role": "assistant", "content": "Retained answer", "reasoning_content": "Retained reason"}, None),
        ])
    with closing(sqlite3.connect(tmp_path / "evo.db")) as db:
        db.executescript("DROP TABLE agent_transcript; DROP TABLE legacy_transcripts; PRAGMA user_version = 2;")

    path = _trace_path("p1", "r1", "a1")
    with closing(Store(tmp_path)) as migrated, _client(migrated) as client:
        page = client.get(f"{path}/transcript").json()
        assert page["completeness"] == "legacy_partial"
        entries = page["entries"]
        assert [entry["source"] for entry in entries] == [
            "legacy_context", "legacy_event", "legacy_event", "legacy_context",
        ]
        assert entries[1]["message"]["content"] == "First question"
        assert entries[2]["message"]["tool_call_id"] == "call-1"
        assert json.loads(entries[2]["message"]["content"]) == {"artifact_ids": ["saved-output"]}
        assert entries[3]["reasoning"] == [{"kind": "content", "text": "Retained reason"}]
        assert migrated.messages("a1")[1]["content"] == "Current checkpoint"
        migrated.append_messages("a1", [({"role": "user", "content": "New message"}, None)])

    with closing(Store(tmp_path)) as reopened, _client(reopened) as client:
        page = client.get(f"{path}/transcript").json()
        assert page["completeness"] == "legacy_partial"
        assert page["entries"][:-1] == entries
        assert page["entries"][-1]["source"] == "recorded"
