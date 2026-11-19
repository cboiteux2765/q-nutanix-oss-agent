"""Read-only, project-scoped access to agent transcripts and execution events."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from .common import redact_all
from .config import get_api_key
from .store import Store

router = APIRouter(prefix="/api/projects/{project_id}/runs/{run_id}/agents/{agent_id}/trace")

Cursor = Annotated[int, Query(ge=0, le=2**63 - 1)]
PageSize = Annotated[int, Query(ge=1, le=200)]


def _trace_run(request: Request, project_id: str, run_id: str, agent_id: str) -> dict:
    store: Store = request.app.state.store
    if not store.get("projects", project_id):
        raise HTTPException(404, "Project not found.")
    run = store.get("runs", run_id)
    if not run or run.get("project_id") != project_id:
        raise HTTPException(404, "Run not found.")
    agent = store.get("agents", agent_id)
    if not agent or agent.get("run_id") != run_id:
        raise HTTPException(404, "Agent not found.")
    return run


def _page(rows: list[dict], limit: int) -> dict:
    return {"entries": rows[:limit], "next_cursor": rows[limit - 1]["seq"] if len(rows) > limit else None}


@router.get("/transcript", dependencies=[Depends(_trace_run)])
def transcript(request: Request, agent_id: str, after: Cursor = 0, limit: PageSize = 100) -> dict:
    store: Store = request.app.state.store
    page = _page(store.transcript(agent_id, after, limit + 1), limit)
    return page | {"completeness": store.transcript_completeness(agent_id)}


@router.get("/events")
def execution_events(request: Request, agent_id: str, run: Annotated[dict, Depends(_trace_run)],
                     after: Cursor = 0, limit: PageSize = 100) -> dict:
    store: Store = request.app.state.store
    page = _page(store.events(run["id"], agent_id, after, limit + 1), limit)
    settings = run.get("settings_snapshot") or {}
    if settings.get("base_url") and (secret := get_api_key(settings)):
        for entry in page["entries"]:
            entry["data"] = redact_all(entry["data"], secret)
    return page
