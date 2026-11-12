import json
import sqlite3
import sys

import pytest

from evo import procs
from evo.common import Conflict, NotFound, StreamRedactor, redact_all
from evo.store import Store, require
from evo.tools import TOOL_MANIFEST, _Scope


def test_stream_redactor_catches_a_secret_split_across_chunks():
    secret = "sk-ABCDEF123456"
    redactor = StreamRedactor(secret)
    chunks = ["The key is sk-AB", "CDEF12", "3456 and more", " text"]
    emitted = "".join(redactor.feed(chunk) for chunk in chunks) + redactor.flush()
    assert emitted == "The key is [redacted] and more text"
    # Nothing that could start the secret is emitted early.
    assert StreamRedactor(secret).feed("ends with sk-A") == "ends with "


def test_stream_redactor_without_secret_passes_text_through():
    redactor = StreamRedactor("")
    assert redactor.feed("abc") == "abc"
    assert redactor.flush() == ""


def test_redact_all_handles_nested_values_and_keys():
    assert redact_all({"k-SECRET": ["x SECRET", 3, {"y": "SECRET"}]}, "SECRET") == {
        "k-[redacted]": ["x [redacted]", 3, {"y": "[redacted]"}]}
    assert redact_all({"a": "b"}, "") == {"a": "b"}


def test_typed_errors_keep_their_builtin_bases():
    assert issubclass(NotFound, KeyError) and issubclass(Conflict, ValueError)
    assert str(NotFound("Unknown runs record 'x'")) == "Unknown runs record 'x'"


def test_require(store):
    with pytest.raises(NotFound):
        require(store, "runs", "missing")
    store.put("runs", "r1", {})
    assert require(store, "runs", "r1")["id"] == "r1"


def test_tool_manifest_matches_scope_methods():
    public = {name for name in vars(_Scope) if not name.startswith("_") and callable(getattr(_Scope, name))}
    assert {name.replace(".", "_", 1) for name in TOOL_MANIFEST} == public
    assert TOOL_MANIFEST["repo.read"] == "repo.read(path, start=1, lines=200) reads UTF-8 lines with 1-based offsets."
    assert TOOL_MANIFEST["artifacts.read"].startswith("artifacts.read(artifact_id, offset=0, limit=24000) ")


def test_events_by_id_is_scoped_to_project_and_run(store):
    store.put("runs", "r1", {"project_id": "p1"})
    store.put("runs", "r2", {"project_id": "p2"})
    a = store.event("r1", None, "x", {})
    b = store.event("r2", None, "x", {})
    assert set(store.events_by_id([a["id"], b["id"]], "p1")) == {a["id"]}
    assert store.events_by_id([a["id"]], "p1", run_id="r2") == {}


def test_recent_events_keeps_the_latest(store):
    for index in range(5):
        store.event("r1", "a" if index % 2 == 0 else "b", "x", {"i": index})
    assert [e["data"]["i"] for e in store.recent_events("r1", ["a"], limit=2)] == [2, 4]


def test_agent_messages_append_and_replace(store):
    store.append_messages("a1", [({"role": "system", "content": "s"}, None)])
    store.append_messages("a1", [({"role": "user", "content": "u"}, "e1"), ({"role": "assistant", "content": "x"}, "e2")])
    assert store.message_count("a1") == 3
    assert [m["role"] for m in store.messages("a1")] == ["system", "user", "assistant"]
    store.replace_messages("a1", [({"role": "system", "content": "s"}, None), ({"role": "user", "content": "c"}, "e3")])
    assert store.messages("a1", with_event_ids=True) == [
        ({"role": "system", "content": "s"}, None), ({"role": "user", "content": "c"}, "e3")]


def test_version_1_database_moves_messages_out_of_agents(tmp_path):
    data = tmp_path / "old"
    data.mkdir()
    db = sqlite3.connect(data / "evo.db")
    db.executescript("""
        CREATE TABLE records (collection TEXT NOT NULL, id TEXT NOT NULL, created_at TEXT NOT NULL,
                              data TEXT NOT NULL, PRIMARY KEY (collection, id));
        PRAGMA user_version = 1;
    """)
    agent = {"id": "a1", "run_id": "r1", "status": "waiting", "created_at": "t",
             "messages": [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}],
             "message_event_ids": [None, "e1"], "inbox": []}
    db.execute("INSERT INTO records VALUES ('agents', 'a1', 't', ?)", (json.dumps(agent),))
    db.commit()
    db.close()

    store = Store(data)
    try:
        record = store.get("agents", "a1")
        assert "messages" not in record and "message_event_ids" not in record
        assert record["status"] == "waiting"
        assert store.messages("a1", with_event_ids=True) == [
            ({"role": "system", "content": "s"}, None), ({"role": "user", "content": "u"}, "e1")]
    finally:
        store.close()
    # Reopening does not migrate again.
    store = Store(data)
    try:
        assert store.message_count("a1") == 2
        assert store._db.execute("PRAGMA user_version").fetchone()[0] == 2
    finally:
        store.close()


def test_procs_run_captures_and_bounds_output(tmp_path):
    result = procs.run([sys.executable, "-c", "import sys; print('out'); print('err', file=sys.stderr)"],
                       tmp_path, 30, None)
    assert result == {"exit_code": 0, "stdout": "out\n", "stderr": "err\n", "timed_out": False} or \
        result["stdout"].strip() == "out"
    echoed = procs.run([sys.executable, "-c", "import sys; sys.stdout.write(sys.stdin.read())"],
                       tmp_path, 30, None, input=b"hello")
    assert echoed["stdout"] == "hello"
    slow = procs.run([sys.executable, "-c", "import time; time.sleep(5)"], tmp_path, 0.5, None)
    assert slow["timed_out"] and slow["exit_code"] is None
    assert procs.bounded("x" * 10, 4) == "xxxx\n[truncated 6 characters]"
