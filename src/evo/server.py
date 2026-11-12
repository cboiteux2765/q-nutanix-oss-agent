"""HTTP API and frontend hosting for the local EVO process."""

import asyncio
import json
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, SecretStr, StringConstraints

from .common import ACTIVE, Conflict
from .config import get_api_key, get_settings, set_api_key, update_settings
from .model_defaults import runtime_recommendations
from .evolution import EvolutionService
from .providers import ModelProvider, ProviderError
from .runtime import Runtime
from .projects import project_view
from .store import Store

_DEV_ORIGINS = ("http://localhost:5173", "http://127.0.0.1:5173")
_AGENT_FIELDS = {"id", "run_id", "parent_id", "role", "name", "status", "task", "result",
                 "error", "depth", "created_at", "updated_at", "token_usage"}

NonEmpty = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Positive = Annotated[int, Field(gt=0)]
Price = Annotated[float, Field(ge=0, allow_inf_nan=False)]


def _require_text(text: str) -> str:
    if not text.strip():
        raise ValueError("Enter text.")
    return text


Text = Annotated[str, AfterValidator(_require_text)]


class SettingsPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_style: Literal["responses", "chat_completions", "chat"] | None = None
    base_url: NonEmpty | None = None
    model: NonEmpty | None = None
    api_key_env: Annotated[str, StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")] | None = None
    api_key: SecretStr | None = None
    reasoning_effort: str | None = None
    runtime_preset: Literal["auto", "custom"] | None = None
    temperature: Annotated[float, Field(ge=0, le=2)] | None = None
    max_output_tokens: Positive | None = None
    context_tokens: Positive | None = None
    compact_at: Annotated[float, Field(gt=0, le=1)] | None = None
    max_steps: Positive | None = None
    max_agents: Positive | None = None
    max_depth: Annotated[int, Field(ge=0)] | None = None
    max_run_tokens: Positive | None = None
    command_timeout: Positive | None = None
    repl_timeout: Positive | None = None
    sandbox: Literal["local"] | None = None
    input_price: Price | None = None
    cached_input_price: Price | None = None
    output_price: Price | None = None


class ProjectInput(BaseModel):
    name: NonEmpty
    path: NonEmpty
    description: str = ""


class MessageInput(BaseModel):
    message: Text


class RunInput(MessageInput):
    project_id: NonEmpty


class ProjectMessageInput(MessageInput):
    new_assignment: bool = False


class MainAgentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Annotated[NonEmpty, Field(max_length=60)]
    instructions: Annotated[str, Field(max_length=20000)] = ""
    model: Annotated[str, Field(max_length=200)] = ""


class MemoryInput(BaseModel):
    content: str
    citations: list | None = None
    expected_revision: str | None = None


class SkillInput(BaseModel):
    name: NonEmpty
    content: Text
    description: str = ""


class EvolutionInput(BaseModel):
    role: NonEmpty
    run_ids: list[str] | None = None
    feedback: str = ""


class EvaluationCase(BaseModel):
    input: NonEmpty
    expected: NonEmpty
    split: Literal["train", "heldout"]


class EvaluationInput(BaseModel):
    cases: Annotated[list[EvaluationCase], Field(min_length=1, max_length=12)]


def _provider_base_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise HTTPException(422, "Invalid provider URL.") from exc
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None
            or parsed.query or parsed.fragment or port == 0):
        raise HTTPException(422, "Provider URL must be an HTTP or HTTPS base URL without credentials.")
    return value.rstrip("/")


def _settings_view(store: Store) -> dict:
    settings = get_settings(store)
    return settings | {"api_key_configured": bool(get_api_key(settings))}


def _recover_interrupted(store: Store) -> None:
    for status in ACTIVE:
        for agent in store.list("agents", status=status):
            store.put("agents", agent["id"], {"status": "interrupted"})
            store.event(agent["run_id"], agent["id"], "agent.status", {"status": "interrupted"})
        for run in store.list("runs", status=status):
            store.put("runs", run["id"], {"status": "interrupted"})
            store.event(run["id"], run.get("root_agent_id"), "run.status", {"status": "interrupted"})


def _record(request: Request, collection: str, record_id: str) -> dict:
    record = request.app.state.store.get(collection, record_id)
    if record is None:
        raise HTTPException(404, f"{collection.removesuffix('s').capitalize()} not found.")
    return record


def _project(request: Request, project_id: str) -> dict:
    return _record(request, "projects", project_id)


def _run(request: Request, run_id: str) -> dict:
    return _record(request, "runs", run_id)


def _agent(request: Request, agent_id: str) -> dict:
    return _record(request, "agents", agent_id)


# Route parameters that resolve a path ID to its record, or answer 404.
ProjectRecord = Annotated[dict, Depends(_project)]
RunRecord = Annotated[dict, Depends(_run)]
AgentRecord = Annotated[dict, Depends(_agent)]


def create_app(data_dir: Path | None = None, frontend_dir: Path | None = None) -> FastAPI:
    data_dir = data_dir or Path(os.getenv("EVO_DATA_DIR") or ".evo")
    frontend_dir = (frontend_dir or Path(__file__).resolve().parents[2] / "frontend" / "dist").resolve()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        store = Store(data_dir)
        try:
            _recover_interrupted(store)
            runtime = Runtime(store)
            app.state.store = store
            app.state.runtime = runtime
            app.state.knowledge = runtime.knowledge
            app.state.skills = runtime.skills
            app.state.artifacts = runtime.artifacts
            app.state.evolution = EvolutionService(store, runtime)
            try:
                yield
            finally:
                await app.state.runtime.close()
        finally:
            store.close()

    app = FastAPI(title="EVO", lifespan=lifespan)

    @app.middleware("http")
    async def check_origin(request: Request, call_next):
        origin = request.headers.get("origin")
        if request.method not in {"GET", "HEAD", "OPTIONS"} and origin:
            if origin != str(request.base_url).rstrip("/") and origin not in _DEV_ORIGINS:
                return JSONResponse({"detail": "Unexpected request origin."}, status_code=403)
        return await call_next(request)

    app.add_middleware(CORSMiddleware, allow_origins=list(_DEV_ORIGINS),
                       allow_methods=["GET", "POST", "PUT"], allow_headers=["Content-Type", "Last-Event-ID"])

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        errors = [{"loc": error["loc"], "msg": error["msg"], "type": error["type"]}
                  for error in exc.errors()]
        return JSONResponse({"detail": errors}, status_code=422)

    @app.exception_handler(KeyError)
    async def missing_resource(request: Request, exc: KeyError):
        return JSONResponse({"detail": str(exc.args[0]) if exc.args else "Resource not found."}, status_code=404)

    @app.exception_handler(Conflict)
    async def conflict(request: Request, exc: Conflict):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(ValueError)
    async def invalid_value(request: Request, exc: ValueError):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(PermissionError)
    async def forbidden(request: Request, exc: PermissionError):
        return JSONResponse({"detail": str(exc)}, status_code=403)

    @app.exception_handler(RuntimeError)
    async def operation_failed(request: Request, exc: RuntimeError):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(ProviderError)
    async def provider_failed(request: Request, exc: ProviderError):
        return JSONResponse({"detail": str(exc)}, status_code=502)

    @app.get("/api/health")
    async def health():
        return {"status": "ok"}

    @app.get("/api/settings")
    async def read_settings():
        return _settings_view(app.state.store)

    @app.put("/api/settings")
    async def write_settings(body: SettingsPatch):
        changes = body.model_dump(exclude_unset=True, exclude={"api_key"})
        if any(value is None and name != "temperature" for name, value in changes.items()):
            raise HTTPException(422, "Only temperature may be set to null.")
        if "base_url" in changes:
            changes["base_url"] = _provider_base_url(changes["base_url"])
        try:
            settings = update_settings(app.state.store, changes)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        if body.api_key is not None:
            set_api_key(settings | changes, body.api_key.get_secret_value())
        return _settings_view(app.state.store)

    @app.get("/api/settings/recommendations")
    async def settings_recommendations(model: str, base_url: str):
        return runtime_recommendations(model.strip(), base_url.strip())

    @app.post("/api/settings/check")
    async def check_settings(body: SettingsPatch | None = None):
        settings = get_settings(app.state.store)
        if body is not None:
            changes = body.model_dump(exclude_unset=True, exclude={"api_key"})
            settings.update({name: value for name, value in changes.items()
                             if value is not None or name == "temperature"})
        settings["base_url"] = _provider_base_url(settings["base_url"])
        if settings["api_style"] not in {"responses", "chat_completions", "chat"}:
            raise HTTPException(422, "Unknown provider API style.")
        key = get_api_key(settings)
        if body is not None and body.api_key is not None:
            key = body.api_key.get_secret_value().strip() or key
        if urlsplit(settings["base_url"]).hostname == "api.openai.com" and not key:
            raise HTTPException(400, "An API key is required for api.openai.com.")
        provider = ModelProvider(settings | {"max_output_tokens": min(settings["max_output_tokens"], 32)}, key)
        try:
            async with asyncio.timeout(20):
                await provider.complete([{"role": "user", "content": "Reply OK."}])
        except TimeoutError as exc:
            raise HTTPException(504, "Provider connection check timed out.") from exc
        finally:
            await provider.close()
        return {"ok": True, "model": settings["model"], "api_style": settings["api_style"]}

    @app.get("/api/projects")
    async def list_projects():
        return [project_view(p) for p in app.state.store.list("projects")]

    @app.post("/api/projects", status_code=201)
    async def create_project(body: ProjectInput):
        try:
            path = Path(body.path).expanduser().resolve(strict=True)
        except (OSError, RuntimeError, ValueError) as exc:
            raise HTTPException(422, "Project path must be an existing directory.") from exc
        if not path.is_dir():
            raise HTTPException(422, "Project path must be an existing directory.")
        for project in app.state.store.list("projects"):
            if Path(project["path"]).resolve() == path:
                return project_view(project)
        return project_view(app.state.store.put("projects", uuid4().hex, body.model_dump() | {"path": str(path)}))

    @app.get("/api/projects/{project_id}")
    async def read_project(project: ProjectRecord):
        return project_view(project)

    @app.put("/api/projects/{project_id}/main-agent")
    async def update_main_agent(project: ProjectRecord, body: MainAgentInput):
        profile = project_view(project)["main_agent"] | body.model_dump()
        profile["model"] = profile["model"].strip()
        return project_view(app.state.store.put("projects", project["id"], {"main_agent": profile}))

    @app.get("/api/projects/{project_id}/conversation")
    async def read_conversation(project: ProjectRecord, before: Annotated[int | None, Query(gt=0)] = None):
        runs = app.state.store.list("runs", project_id=project["id"])
        active = [run for run in runs if run["status"] in ACTIVE]
        latest = (active or runs)[-1] if runs else None
        messages = app.state.store.project_messages(project["id"], limit=101, before=before)
        return {"main_agent": project_view(project)["main_agent"],
                "run_id": latest["id"] if latest else None,
                "messages": messages[-100:], "has_more": len(messages) > 100}

    @app.post("/api/projects/{project_id}/messages")
    async def message_project(project: ProjectRecord, body: ProjectMessageInput):
        return await app.state.runtime.message_project(project["id"], body.message, body.new_assignment)

    @app.get("/api/projects/{project_id}/runs")
    async def list_runs(project: ProjectRecord):
        return app.state.store.list("runs", project_id=project["id"])

    @app.get("/api/projects/{project_id}/files")
    def list_files(project: ProjectRecord, path: str = ""):
        root = Path(project["path"]).resolve()
        try:
            target = (root / path).resolve()
            relative = target.relative_to(root)
        except (OSError, RuntimeError, ValueError) as exc:
            raise HTTPException(422, "Path must be inside the project.") from exc
        if ".git" in relative.parts or not target.is_dir():
            raise HTTPException(404, "Directory not found.")
        entries = []
        for child in target.iterdir():
            if child.name == ".git" or child.is_symlink() or child.is_junction():
                continue
            entries.append({"name": child.name, "path": child.relative_to(root).as_posix(),
                            "type": "directory" if child.is_dir() else "file"})
        entries.sort(key=lambda entry: (entry["type"], entry["name"].casefold()))
        return {"path": "" if relative == Path(".") else relative.as_posix(), "entries": entries}

    @app.post("/api/runs", status_code=201)
    async def start_run(request: Request, body: RunInput):
        _project(request, body.project_id)
        return await app.state.runtime.start_run(body.project_id, body.message)

    @app.get("/api/runs/{run_id}")
    async def read_run(run: RunRecord, events: bool = True):
        """The run and its agents. Pass events=false for a lightweight refresh while the event
        stream delivers new events."""
        agents = [{key: value for key, value in agent.items() if key in _AGENT_FIELDS}
                  for agent in app.state.store.list("agents", run_id=run["id"])]
        detail = {"run": run, "agents": agents}
        if events:
            detail["events"] = app.state.store.events(run["id"])
        return detail

    @app.post("/api/runs/{run_id}/messages")
    async def steer_run(run: RunRecord, body: MessageInput):
        return await app.state.runtime.steer(run["id"], body.message)

    @app.post("/api/runs/{run_id}/cancel")
    async def cancel_run(run: RunRecord):
        return await app.state.runtime.cancel(run["id"])

    @app.post("/api/runs/{run_id}/resume")
    async def resume_run(run: RunRecord):
        return await app.state.runtime.resume(run["id"])

    @app.post("/api/agents/{agent_id}/compact")
    async def compact_agent(agent: AgentRecord):
        return await app.state.runtime.compact(agent["id"])

    @app.get("/api/runs/{run_id}/events")
    async def stream_events(request: Request, run: RunRecord,
                            after: Annotated[int, Query(ge=0, le=2**63 - 1)] = 0):
        run_id = run["id"]
        last_event = request.headers.get("last-event-id", "")
        if last_event.isascii() and last_event.isdecimal() and len(last_event) <= 19:
            after = max(after, min(int(last_event), 2**63 - 1))

        async def stream():
            cursor = after
            heartbeat = time.monotonic()
            while not await request.is_disconnected():
                batch = app.state.store.events(run_id, after=cursor, limit=100)
                for event in batch:
                    cursor = event["seq"]
                    yield f"id: {cursor}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                if batch or time.monotonic() - heartbeat >= 15:
                    if not batch:
                        yield ": keepalive\n\n"
                    heartbeat = time.monotonic()
                if len(batch) < 100:
                    await asyncio.sleep(0.4)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/api/projects/{project_id}/memory")
    def list_memory(project: ProjectRecord):
        return {"pages": app.state.knowledge.list_pages(project["id"])}

    @app.get("/api/projects/{project_id}/memory/{path:path}")
    def read_memory(project: ProjectRecord, path: str):
        return app.state.knowledge.read_page(project["id"], path)

    @app.put("/api/projects/{project_id}/memory/{path:path}")
    def write_memory(project: ProjectRecord, path: str, body: MemoryInput):
        return app.state.knowledge.write_page(project["id"], path, body.content, citations=body.citations,
                                               expected_revision=body.expected_revision)

    @app.get("/api/projects/{project_id}/skills")
    async def list_skills(project: ProjectRecord):
        return {"skills": app.state.skills.list(project["id"])}

    @app.post("/api/projects/{project_id}/skills", status_code=201)
    async def propose_skill(project: ProjectRecord, body: SkillInput):
        return app.state.skills.propose(project["id"], body.name, body.content, description=body.description)

    @app.post("/api/projects/{project_id}/skills/{skill_id}/promote")
    async def promote_skill(project: ProjectRecord, skill_id: str):
        return app.state.skills.promote(project["id"], skill_id)

    @app.get("/api/projects/{project_id}/prompts")
    async def list_prompts(project: ProjectRecord):
        return {"prompts": app.state.evolution.list_prompts(project["id"])}

    @app.post("/api/projects/{project_id}/evolve", status_code=201)
    async def evolve_prompt(project: ProjectRecord, body: EvolutionInput):
        return await app.state.evolution.propose(project["id"], body.role, feedback=body.feedback,
                                                 run_ids=body.run_ids)

    @app.post("/api/projects/{project_id}/prompts/{prompt_id}/promote")
    async def promote_prompt(project: ProjectRecord, prompt_id: str):
        return app.state.evolution.promote_prompt(project["id"], prompt_id)

    @app.get("/api/projects/{project_id}/prompts/{prompt_id}/evaluations")
    async def list_evaluations(request: Request, project_id: str, prompt_id: str):
        prompt = _record(request, "prompts", prompt_id)
        if prompt["project_id"] != project_id:
            raise HTTPException(404, "Prompt not found.")
        return {"evaluations": app.state.evolution.list_evaluations(project_id, prompt_id)}

    @app.post("/api/projects/{project_id}/prompts/{prompt_id}/evaluations", status_code=201)
    async def evaluate_prompt(project: ProjectRecord, prompt_id: str, body: EvaluationInput):
        return await app.state.evolution.evaluate(project["id"], prompt_id,
                                                  [case.model_dump() for case in body.cases])

    @app.get("/api/runs/{run_id}/artifacts")
    async def list_artifacts(run: RunRecord):
        return app.state.artifacts.list(run["id"])

    @app.get("/api/artifacts/{artifact_id}")
    async def read_artifact(artifact_id: str):
        return app.state.artifacts.read(artifact_id)

    @app.get("/{path:path}", include_in_schema=False)
    async def frontend(path: str):
        if path == "api" or path.startswith("api/"):
            raise HTTPException(404, "API route not found.")
        target = (frontend_dir / path).resolve()
        if not target.is_relative_to(frontend_dir):
            raise HTTPException(404, "File not found.")
        if target.is_file():
            return FileResponse(target)
        if Path(path).suffix:
            raise HTTPException(404, "File not found.")
        index = frontend_dir / "index.html"
        if not index.is_file():
            raise HTTPException(503, "Frontend build unavailable. Build it from the frontend directory.")
        return FileResponse(index)

    return app


app = create_app()
