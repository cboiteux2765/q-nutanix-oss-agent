"""Project workspace endpoints shared by the dashboard and main conversation."""

import asyncio
import json
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from .common import redact_all
from .config import get_api_key



Message = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100_000)]


class WorkflowInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: Message
    source: dict | None = None


class WorkflowMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: Message


class WorkflowSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_concurrent_workflows: int = Field(default=3, ge=1, le=20, strict=True)
    max_project_tokens: int | None = Field(default=None, gt=0, strict=True)
    max_project_cost_usd: float | None = Field(default=None, gt=0, allow_inf_nan=False)


class ActivitySettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    paused: bool = False
    mode: Literal["suggest", "local_auto"] = "suggest"
    repository: str | None = None
    branches: list[str] = Field(default_factory=list, max_length=3)
    ignored_actors: list[str] = Field(default_factory=list, max_length=100)


def router(project_dep):
    api = APIRouter(prefix="/api/projects/{project_id}")

    @api.get("/dashboard")
    async def dashboard(request: Request, project: project_dep):
        cursor = request.app.state.store.project_event_cursor(project["id"])
        overview = request.app.state.supervisor.overview(project["id"])
        return {**overview, "event_cursor": cursor,
                "activity": request.app.state.proactivity.dashboard(project["id"])}

    @api.get("/events")
    async def events(request: Request, project: project_dep,
                     after: Annotated[int, Query(ge=0, le=2**63 - 1)] = 0):
        from fastapi.responses import StreamingResponse

        last = request.headers.get("last-event-id", "")
        if last.isascii() and last.isdecimal() and len(last) <= 19:
            after = max(after, min(int(last), 2**63 - 1))

        async def stream():
            cursor = after
            while not await request.is_disconnected():
                batch = request.app.state.store.project_events(project["id"], after=cursor, limit=100)
                for event in batch:
                    cursor = event["seq"]
                    yield f"id: {cursor}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                if not batch:
                    yield ": keepalive\n\n"
                if len(batch) < 100:
                    await asyncio.sleep(1)

        return StreamingResponse(stream(), media_type="text/event-stream")

    @api.get("/workflows")
    async def workflows(request: Request, project: project_dep):
        return request.app.state.supervisor.list_workflows(project["id"])

    @api.get("/artifacts/{artifact_id}")
    async def artifact(request: Request, project: project_dep, artifact_id: str):
        record = request.app.state.store.get("artifacts", artifact_id)
        if not record or record.get("project_id") != project["id"]:
            raise HTTPException(404, "Artifact not found.")
        payload = request.app.state.artifacts.read(artifact_id)
        run = request.app.state.store.get("runs", record["run_id"]) or {}
        settings = run.get("settings_snapshot") or {}
        if settings.get("base_url") and (secret := get_api_key(settings)):
            clean = redact_all(payload, secret)
            if clean != payload:
                return clean | {"redacted": True}
        return payload

    @api.post("/workflows", status_code=201)
    async def start_workflow(request: Request, project: project_dep, body: WorkflowInput):
        if body.source and len(json.dumps(body.source)) > 16_000:
            raise ValueError("Workflow source context must be 16 KB or smaller.")
        source = {name: value for name, value in (body.source or {}).items()
                  if name in {"suggestion_id", "repository", "issue_number", "pr_number", "url", "subject"}}
        if suggestion_id := source.get("suggestion_id"):
            return await request.app.state.supervisor.start_suggestion(project["id"], suggestion_id, body.message)
        return await request.app.state.scheduler.start(project["id"], body.message, source=source or None)

    @api.get("/workflows/{run_id}")
    async def workflow(request: Request, project: project_dep, run_id: str):
        return request.app.state.supervisor.status(project["id"], run_id)

    @api.post("/workflows/{run_id}/messages")
    async def message(request: Request, project: project_dep, run_id: str, body: WorkflowMessage):
        return await request.app.state.supervisor.send(project["id"], run_id, body.message)

    @api.post("/workflows/{run_id}/pause")
    async def pause(request: Request, project: project_dep, run_id: str):
        return await request.app.state.scheduler.pause(project["id"], run_id)

    @api.post("/workflows/{run_id}/resume")
    async def resume(request: Request, project: project_dep, run_id: str):
        return await request.app.state.scheduler.resume(project["id"], run_id)

    @api.post("/workflows/{run_id}/cancel")
    async def cancel(request: Request, project: project_dep, run_id: str):
        return await request.app.state.scheduler.cancel(project["id"], run_id)

    @api.get("/workflow-settings")
    async def settings(request: Request, project: project_dep):
        return request.app.state.scheduler.settings(project["id"])

    @api.patch("/workflow-settings")
    async def configure(request: Request, project: project_dep, body: WorkflowSettings):
        return request.app.state.scheduler.configure(project["id"], body.model_dump(exclude_unset=True))

    @api.get("/activity")
    async def activity(request: Request, project: project_dep,
                       limit: Annotated[int, Query(ge=1, le=200)] = 50):
        return request.app.state.proactivity.dashboard(project["id"], limit=limit)

    @api.get("/activity/settings")
    async def activity_settings(request: Request, project: project_dep):
        return request.app.state.proactivity.settings(project["id"])

    @api.patch("/activity/settings")
    async def configure_activity(request: Request, project: project_dep, body: ActivitySettings):
        return await request.app.state.proactivity.configure(project["id"], **body.model_dump(exclude_unset=True))

    @api.post("/activity/poll")
    async def poll(request: Request, project: project_dep):
        return await request.app.state.proactivity.poll(project["id"])

    return api
