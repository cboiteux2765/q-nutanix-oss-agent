"""SQLite persistence for agent state and the durable run event stream."""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any, Iterator
from uuid import uuid4

from .common import Conflict, NotFound

SCHEMA_VERSION = 2
# Record fields that list() filters on often enough to deserve an index.
_INDEXED_FIELDS = ("run_id", "project_id", "parent_id")
_FIELD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def require(store: Store, collection: str, record_id: str) -> dict:
    record = store.get(collection, record_id)
    if record is None:
        raise NotFound(f"Unknown {collection} record {record_id!r}")
    return record


class Store:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._db = sqlite3.connect(self.data_dir / "evo.db", timeout=15, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode = WAL")
        # Each covers list()'s filter and its creation order.
        indexes = "".join(
            f"CREATE INDEX IF NOT EXISTS records_{field} ON records"
            f"(collection, json_extract(data, '$.{field}'), created_at, id);\n"
            for field in _INDEXED_FIELDS
        )
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS records (
                collection TEXT NOT NULL,
                id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                data TEXT NOT NULL,
                PRIMARY KEY (collection, id)
            );
            CREATE INDEX IF NOT EXISTS records_created ON records(collection, created_at, id);
            CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                id TEXT NOT NULL UNIQUE,
                run_id TEXT NOT NULL,
                agent_id TEXT,
                kind TEXT NOT NULL,
                data TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS events_run ON events(run_id, seq);
            CREATE INDEX IF NOT EXISTS events_agent ON events(run_id, agent_id, seq);
            CREATE TABLE IF NOT EXISTS shared_values (
                scope TEXT NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                version INTEGER NOT NULL,
                PRIMARY KEY (scope, key)
            );
            -- Each agent's model-visible history, one row per message. Appends are cheap;
            -- compaction replaces an agent's rows in one transaction.
            CREATE TABLE IF NOT EXISTS agent_messages (
                agent_id TEXT NOT NULL,
                position INTEGER NOT NULL,
                event_id TEXT,
                data TEXT NOT NULL,
                PRIMARY KEY (agent_id, position)
            );
            """ + indexes
        )
        self._migrate()

    def _migrate(self) -> None:
        version = self._db.execute("PRAGMA user_version").fetchone()[0]
        if version < 2:
            # Version 1 kept each agent's messages inside its record and rewrote the whole history
            # on every append. Move them to agent_messages.
            with self._transaction() as db:
                rows = db.execute(
                    "SELECT id, data FROM records WHERE collection = 'agents' "
                    "AND json_type(data, '$.messages') IS NOT NULL"
                ).fetchall()
                for row in rows:
                    agent = json.loads(row["data"])
                    messages = agent.pop("messages")
                    ids = list(agent.pop("message_event_ids", None) or [])
                    ids += [None] * (len(messages) - len(ids))
                    db.executemany(
                        "INSERT OR REPLACE INTO agent_messages (agent_id, position, event_id, data) "
                        "VALUES (?, ?, ?, ?)",
                        [(row["id"], index, event_id, _encode(message))
                         for index, (message, event_id) in enumerate(zip(messages, ids))],
                    )
                    db.execute("UPDATE records SET data = ? WHERE collection = 'agents' AND id = ?",
                               (_encode(agent), row["id"]))
        if version < SCHEMA_VERSION:
            with self._lock:
                self._db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock, self._db:
            self._db.execute("BEGIN IMMEDIATE")
            yield self._db

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def get(self, collection: str, id: str) -> dict | None:
        with self._lock:
            row = self._db.execute(
                "SELECT data FROM records WHERE collection = ? AND id = ?", (collection, id)
            ).fetchone()
        return json.loads(row["data"]) if row else None

    def list(self, collection: str, **field_filters: Any) -> list[dict]:
        """Records in creation order whose top-level fields equal the given values."""
        clauses, values = ["collection = ?"], [collection]
        for key, value in field_filters.items():
            if not _FIELD.fullmatch(key):
                raise ValueError(f"Invalid record field {key!r}.")
            if value is None:
                clauses.append(f"json_type(data, '$.{key}') = 'null'")
            else:
                clauses.append(f"json_extract(data, '$.{key}') = ?")
                values.append(value)
        with self._lock:
            rows = self._db.execute(
                f"SELECT data FROM records WHERE {' AND '.join(clauses)} ORDER BY created_at, id",
                values,
            ).fetchall()
        return [json.loads(row["data"]) for row in rows]

    def put(self, collection: str, id: str, data: dict) -> dict:
        with self._transaction() as db:
            previous = self.get(collection, id) or {}
            now = utc_now()
            created_at = previous.get("created_at", now)
            record = previous | data | {"id": id, "created_at": created_at, "updated_at": now}
            db.execute(
                """
                INSERT INTO records (collection, id, created_at, data) VALUES (?, ?, ?, ?)
                ON CONFLICT (collection, id) DO UPDATE SET data = excluded.data
                """,
                (collection, id, created_at, _encode(record)),
            )
        return record

    def delete(self, collection: str, id: str) -> None:
        with self._transaction() as db:
            db.execute("DELETE FROM records WHERE collection = ? AND id = ?", (collection, id))

    def messages(self, agent_id: str, with_event_ids: bool = False) -> list:
        """An agent's model-visible history, or (message, event_id) pairs."""
        with self._lock:
            rows = self._db.execute(
                "SELECT event_id, data FROM agent_messages WHERE agent_id = ? ORDER BY position",
                (agent_id,),
            ).fetchall()
        if with_event_ids:
            return [(json.loads(row["data"]), row["event_id"]) for row in rows]
        return [json.loads(row["data"]) for row in rows]

    def message_count(self, agent_id: str) -> int:
        with self._lock:
            return self._db.execute(
                "SELECT count(*) FROM agent_messages WHERE agent_id = ?", (agent_id,)
            ).fetchone()[0]

    def append_messages(self, agent_id: str, items: list[tuple[dict, str | None]]) -> None:
        with self._transaction() as db:
            start = db.execute(
                "SELECT coalesce(max(position) + 1, 0) FROM agent_messages WHERE agent_id = ?",
                (agent_id,),
            ).fetchone()[0]
            db.executemany(
                "INSERT INTO agent_messages (agent_id, position, event_id, data) VALUES (?, ?, ?, ?)",
                [(agent_id, start + index, event_id, _encode(message))
                 for index, (message, event_id) in enumerate(items)],
            )

    def replace_messages(self, agent_id: str, items: list[tuple[dict, str | None]]) -> None:
        with self._transaction() as db:
            db.execute("DELETE FROM agent_messages WHERE agent_id = ?", (agent_id,))
            db.executemany(
                "INSERT INTO agent_messages (agent_id, position, event_id, data) VALUES (?, ?, ?, ?)",
                [(agent_id, index, event_id, _encode(message))
                 for index, (message, event_id) in enumerate(items)],
            )

    def event(self, run_id: str, agent_id: str | None, kind: str, data: dict) -> dict:
        id = uuid4().hex
        created_at = utc_now()
        with self._transaction() as db:
            cursor = db.execute(
                """
                INSERT INTO events (id, run_id, agent_id, kind, data, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (id, run_id, agent_id, kind, _encode(data), created_at),
            )
        return {"id": id, "seq": cursor.lastrowid, "run_id": run_id, "agent_id": agent_id,
                "kind": kind, "data": data, "created_at": created_at}

    @staticmethod
    def _decode_event(row: sqlite3.Row) -> dict:
        event = dict(row)
        event["data"] = json.loads(event["data"])
        return event

    def events(self, run_id: str, agent_id: str | None = None, after: int = 0,
               limit: int = 500) -> list[dict]:
        with self._lock:
            if agent_id is None:
                rows = self._db.execute(
                    "SELECT * FROM events WHERE run_id = ? AND seq > ? ORDER BY seq LIMIT ?",
                    (run_id, after, limit),
                ).fetchall()
            else:
                rows = self._db.execute(
                    """
                    SELECT * FROM events WHERE run_id = ? AND agent_id = ? AND seq > ?
                    ORDER BY seq LIMIT ?
                    """,
                    (run_id, agent_id, after, limit),
                ).fetchall()
        return [self._decode_event(row) for row in rows]

    def events_by_id(self, event_ids: list[str], project_id: str,
                     run_id: str | None = None) -> dict[str, dict]:
        """Events with the given IDs that belong to the project (and run, if given), keyed by ID."""
        if not event_ids:
            return {}
        marks = ", ".join("?" * len(event_ids))
        query = (
            "SELECT events.* FROM events JOIN records AS runs "
            "ON runs.collection = 'runs' AND runs.id = events.run_id "
            f"WHERE events.id IN ({marks}) AND json_extract(runs.data, '$.project_id') = ?"
        )
        values: list = [*event_ids, project_id]
        if run_id is not None:
            query += " AND events.run_id = ?"
            values.append(run_id)
        with self._lock:
            rows = self._db.execute(query, values).fetchall()
        return {row["id"]: self._decode_event(row) for row in rows}

    def recent_events(self, run_id: str, agent_ids: list[str], limit: int) -> list[dict]:
        """The latest events from any of the given agents, oldest first."""
        if not agent_ids:
            return []
        marks = ", ".join("?" * len(agent_ids))
        with self._lock:
            rows = self._db.execute(
                f"SELECT * FROM events WHERE run_id = ? AND agent_id IN ({marks}) "
                "ORDER BY seq DESC LIMIT ?",
                (run_id, *agent_ids, limit),
            ).fetchall()
        return [self._decode_event(row) for row in reversed(rows)]

    def search_history(self, query: str, run_id: str | None = None, agent_id: str | None = None,
                       project_id: str | None = None, limit: int = 20) -> list[dict]:
        query = query.strip()
        if not query or limit < 1:
            return []
        filters = [
            "EXISTS (SELECT 1 FROM json_tree(events.data) "
            "WHERE type = 'text' AND instr(lower(atom), lower(?)) > 0)"
        ]
        values: list = [query]
        for clause, value in (("events.run_id = ?", run_id), ("events.agent_id = ?", agent_id),
                              ("json_extract(runs.data, '$.project_id') = ?", project_id)):
            if value is not None:
                filters.append(clause)
                values.append(value)
        join = ""
        if project_id is not None:
            join = "JOIN records AS runs ON runs.collection = 'runs' AND runs.id = events.run_id"
        with self._lock:
            rows = self._db.execute(
                f"SELECT events.* FROM events {join} WHERE {' AND '.join(filters)} "
                "ORDER BY events.seq DESC LIMIT ?",
                (*values, limit),
            ).fetchall()
        return [self._decode_event(row) for row in rows]

    def project_messages(self, project_id: str, limit: int = 500, before: int | None = None) -> list[dict]:
        """Read the central chat without scanning or truncating the tool transcript."""
        with self._lock:
            rows = self._db.execute(
                "SELECT events.* FROM events JOIN records AS runs "
                "ON runs.collection = 'runs' AND runs.id = events.run_id "
                "WHERE json_extract(runs.data, '$.project_id') = ? AND events.kind = 'message' "
                "AND json_extract(events.data, '$.visible') = 1 "
                "AND (events.agent_id IS NULL OR events.agent_id = json_extract(runs.data, '$.root_agent_id')) "
                "AND (? IS NULL OR events.seq < ?) ORDER BY events.seq DESC LIMIT ?",
                (project_id, before, before, limit),
            ).fetchall()
        return [self._decode_event(row) for row in reversed(rows)]

    def shared_get(self, scope: str, key: str) -> dict | None:
        with self._lock:
            row = self._db.execute(
                "SELECT value, version FROM shared_values WHERE scope = ? AND key = ?", (scope, key)
            ).fetchone()
        if row is None:
            return None
        return {"key": key, "value": json.loads(row["value"]), "version": row["version"]}

    def shared_set(self, scope: str, key: str, value: Any, expected_version: int | None = None) -> dict:
        with self._transaction() as db:
            current = self.shared_get(scope, key)
            version = current["version"] if current else 0
            if expected_version is not None and expected_version != version:
                raise Conflict(f"Version conflict for {key!r}. Current version is {version}.")
            version += 1
            db.execute(
                """
                INSERT INTO shared_values (scope, key, value, version) VALUES (?, ?, ?, ?)
                ON CONFLICT (scope, key) DO UPDATE SET value = excluded.value, version = excluded.version
                """,
                (scope, key, _encode(value), version),
            )
        return {"key": key, "value": value, "version": version}
