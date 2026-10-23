"""Authenticated asynchronous REST service and explicit-context legacy adapter."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import inspect
import json
import os
import re
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .models import CreateRunRequest, Principal, ReviewRequest, FeedbackRequest


class ComputeBudget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_runtime_seconds: int = Field(default=1200, ge=1, le=1200, strict=True)
    max_llm_cost_usd: float = Field(default=2.0, ge=0, le=2.0)
    max_test_executions: int = Field(default=4, ge=1, le=4, strict=True)


class LegacyVerifyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    context_id: str = Field(min_length=16, max_length=128)
    pytest_suite: str = Field(default="pytest-default-v1", min_length=1, max_length=100)
    max_debug_iterations: int = Field(default=2, ge=0, le=2, strict=True)
    compute_budget: ComputeBudget | float = Field(default_factory=ComputeBudget)


async def _value(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


def _dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if hasattr(value, "to_dict"):
        return value.to_dict()
    raise TypeError("Expected a structured run record")


def _repo_name(repo_url: str) -> str:
    if not isinstance(repo_url, str):
        raise HTTPException(422, "Invalid repository identifier")
    value = repo_url.strip().rstrip("/").removesuffix(".git")
    if "://" in value:
        parsed = urlparse(value)
        if parsed.scheme != "https" or parsed.hostname != "github.com":
            raise HTTPException(422, "Only registered GitHub repositories are supported")
        value = parsed.path.strip("/")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value):
        raise HTTPException(422, "Invalid repository identifier")
    return value


def create_app(
    manager: Any | None = None,
    *,
    tokens: Mapping[str, Principal] | None = None,
    webhook_secret: str | bytes | None = None,
    webhook_senders: set[str] | frozenset[str] | None = None,
) -> FastAPI:
    """Construct an app with injectable registry, credentials, and webhook secret.

    With no injected token mapping, credentials come only from environment
    configuration; an unconfigured service grants no authenticated access.
    """
    if manager is None:
        from .run_manager import RunManager
        manager = RunManager(Path(os.getenv("EVO_DATA_DIR", ".evo-data")))
    token_map = dict(tokens or {})
    if tokens is None:
        repos = frozenset(item.strip() for item in os.getenv("EVO_ALLOWED_REPOS", "org/service").split(",") if item.strip())
        for role, variable in (("engineer", "EVO_ENGINEER_TOKEN"), ("qa", "EVO_QA_TOKEN")):
            credential = os.getenv(variable)
            if credential:
                if credential in token_map:
                    raise ValueError("Engineer and QA bearer credentials must be distinct")
                name = os.getenv(f"EVO_{role.upper()}_NAME", f"configured-{role}")
                token_map[credential] = Principal(name=name, roles=frozenset({role}), repos=repos)
    secret = webhook_secret if webhook_secret is not None else os.getenv("GITHUB_WEBHOOK_SECRET")
    if isinstance(secret, str):
        secret = secret.encode()
    allowed_senders = frozenset(webhook_senders) if webhook_senders is not None else frozenset(item.strip() for item in os.getenv("EVO_WEBHOOK_SENDERS", "").split(",") if item.strip())
    @asynccontextmanager
    async def lifespan(application: FastAPI):
        yield
        pending = [(run_id, task) for run_id, task in application.state.tasks.items() if not task.done()]
        for run_id, _task in pending:
            try:
                record = _dict(manager.get_run(run_id))
                service = Principal(name="service-shutdown", roles=frozenset({"engineer"}), repos=frozenset({record["repo"]}))
                manager.cancel_run(run_id, service)
            except (KeyError, ValueError, PermissionError):
                pass
        tasks = [task for _run_id, task in pending]
        if tasks:
            try:
                await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=5)
            except asyncio.TimeoutError:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    app = FastAPI(title="EVO Test Agent", version="1.0.0", lifespan=lifespan)
    app.state.manager = manager
    app.state.tasks = {}
    app.state.legacy_contexts = {}
    app.state.webhook_lock = asyncio.Lock()

    @app.exception_handler(KeyError)
    async def missing(_request: Request, _error: KeyError):
        return JSONResponse(status_code=404, content={"detail": "Run or registered input not found"})

    @app.exception_handler(PermissionError)
    async def forbidden(_request: Request, error: PermissionError):
        return JSONResponse(status_code=403, content={"detail": str(error)})

    @app.exception_handler(ValueError)
    async def conflict(_request: Request, error: ValueError):
        return JSONResponse(status_code=409, content={"detail": str(error)})

    async def principal(authorization: str | None = Header(default=None)) -> Principal:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(401, "Bearer authentication required", headers={"WWW-Authenticate": "Bearer"})
        identity = token_map.get(authorization[7:])
        if identity is None:
            raise HTTPException(401, "Invalid bearer token", headers={"WWW-Authenticate": "Bearer"})
        return identity

    def authorize(identity: Principal, repo: str, role: str | None = None) -> None:
        if repo not in identity.repos and "*" not in identity.repos:
            raise HTTPException(403, "Repository access denied")
        if role and role not in identity.roles:
            raise HTTPException(403, f"{role} role required")

    async def get_run(run_id: str, identity: Principal) -> dict[str, Any]:
        record = _dict(await _value(manager.get_run(run_id)))
        authorize(identity, record["repo"])
        return record

    def queue(run_id: str) -> asyncio.Task:
        existing = app.state.tasks.get(run_id)
        if existing and not existing.done():
            return existing
        task = asyncio.create_task(manager.execute(run_id), name=f"test-agent:{run_id}")
        app.state.tasks[run_id] = task
        # Retrieve worker exceptions while retaining the task for status/legacy waits.
        def consume(done: asyncio.Task) -> None:
            if not done.cancelled():
                done.exception()
        task.add_done_callback(consume)
        return task

    async def create(request: CreateRunRequest, identity: Principal, key: str | None, trigger: str) -> dict[str, Any]:
        authorize(identity, request.repo, "engineer")
        record, reused = await _value(manager.create_run(request, identity, idempotency_key=key, trigger_identity=trigger))
        record = _dict(record)
        if record["execution_status"] == "QUEUED":
            queue(record["run_id"])
        return {"run_id": record["run_id"], "status_uri": f"/v1/test-runs/{record['run_id']}", "reused": reused}

    @app.post("/v1/test-runs", status_code=202)
    async def create_run(request: CreateRunRequest, identity: Principal = Depends(principal), idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
        if idempotency_key is not None and (not idempotency_key.strip() or len(idempotency_key) > 200):
            raise HTTPException(422, "Invalid Idempotency-Key")
        return await create(request, identity, idempotency_key, f"user:{identity.name}")

    @app.get("/v1/test-runs/{run_id}")
    async def status(run_id: str, identity: Principal = Depends(principal)):
        return await get_run(run_id, identity)

    @app.get("/v1/test-runs/{run_id}/artifacts")
    async def artifacts(run_id: str, identity: Principal = Depends(principal)):
        await get_run(run_id, identity)
        return await _value(manager.artifacts(run_id))

    @app.get("/v1/test-runs/{run_id}/artifacts/{name:path}")
    async def artifact_file(run_id: str, name: str, identity: Principal = Depends(principal)):
        await get_run(run_id, identity)
        parts = name.split("/")
        if any(not re.fullmatch(r"[A-Za-z0-9_.-]+", part) or part in {".", ".."} for part in parts):
            raise HTTPException(404, "Artifact not found")
        path = Path(await _value(manager.artifact_path(run_id, name)))
        if not path.is_file():
            raise HTTPException(404, "Artifact not found")
        return FileResponse(path, filename=path.name, headers={"Cache-Control": "no-store"})

    @app.post("/v1/test-runs/{run_id}/cancel")
    async def cancel(run_id: str, identity: Principal = Depends(principal)):
        record = await get_run(run_id, identity)
        authorize(identity, record["repo"], "engineer")
        return await _value(manager.cancel_run(run_id, identity))

    @app.post("/v1/test-runs/{run_id}/review")
    async def review(run_id: str, request: ReviewRequest, identity: Principal = Depends(principal)):
        record = await get_run(run_id, identity)
        authorize(identity, record["repo"], "qa")
        return await _value(manager.review_run(run_id, request, identity))

    @app.post("/v1/test-runs/{run_id}/feedback")
    async def feedback(run_id: str, request: FeedbackRequest, identity: Principal = Depends(principal)):
        record = await get_run(run_id, identity)
        authorize(identity, record["repo"], "engineer")
        return await _value(manager.feedback_run(run_id, request, identity))

    @app.post("/github/webhook", status_code=202)
    async def github_webhook(request: Request):
        if not secret:
            raise HTTPException(503, "GitHub webhook secret is not configured")
        raw = await request.body()
        if len(raw) > 2 * 1024 * 1024:
            raise HTTPException(413, "Webhook body exceeds configured limit")
        supplied = request.headers.get("X-Hub-Signature-256", "")
        expected = "sha256=" + hmac.new(secret, raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(supplied, expected):
            raise HTTPException(401, "Invalid GitHub webhook signature")
        delivery = request.headers.get("X-GitHub-Delivery", "")
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", delivery):
            raise HTTPException(422, "A valid X-GitHub-Delivery is required")
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise HTTPException(400, "Invalid webhook JSON")
        if not isinstance(payload, dict):
            raise HTTPException(400, "Webhook JSON must be an object")
        event = request.headers.get("X-GitHub-Event", "")
        def object_field(name: str) -> dict[str, Any]:
            value = payload.get(name, {})
            if not isinstance(value, dict):
                raise HTTPException(422, f"Invalid webhook {name} object")
            return value
        repo = _repo_name(object_field("repository").get("full_name", ""))
        sender = object_field("sender").get("login", "")
        if not isinstance(sender, str) or sender not in allowed_senders:
            raise HTTPException(403, "GitHub sender is not authorized to trigger verification")
        pr = object_field("pull_request")
        eligible = event == "pull_request" and payload.get("action") == "labeled" and object_field("label").get("name") == "test:verify"
        if event == "issue_comment":
            issue = object_field("issue")
            comment_body = object_field("comment").get("body", "")
            eligible = payload.get("action") == "created" and bool(issue.get("pull_request")) and isinstance(comment_body, str) and comment_body.strip() == "/test-agent run"
            pr_number = issue.get("number")
        else:
            pr_number = pr.get("number", payload.get("number"))
        if event == "pull_request" and payload.get("action") == "synchronize":
            if isinstance(pr_number, bool) or not isinstance(pr_number, int) or pr_number < 1:
                raise HTTPException(422, "Invalid PR number")
            current_request = manager.registry.request_for_pr(repo, pr_number)
            current = manager.registry.resolve(current_request)
            head = pr.get("head", {})
            if not isinstance(head, dict) or head.get("sha") != current.head_sha:
                raise HTTPException(409, "Registered source mirror must match the new PR head before synchronization")
            invalidated = manager.invalidate_stale(repo, pr_number)
            return {"accepted": True, "queued": False, "invalidated_run_ids": invalidated}
        if not eligible:
            return {"accepted": False, "reason": "Event is not an eligible Test Agent trigger"}
        if isinstance(pr_number, bool) or not isinstance(pr_number, int) or pr_number < 1:
            raise HTTPException(422, "Invalid PR number")
        webhook_identity = Principal(name=f"github:{sender}", roles=frozenset({"engineer"}), repos=frozenset({repo}))
        # The registry must resolve an allowlisted repository/PR; payload URLs,
        # commands, spec contents and identities never become trusted inputs.
        async with app.state.webhook_lock:
            previous = manager.delivery_runs.get(delivery)
            if previous:
                original = _dict(manager.get_run(previous))
                if original["repo"] != repo or original["pr_number"] != pr_number:
                    raise HTTPException(409, "Delivery identity conflicts with its original repository or PR")
                return {"run_id": previous, "status_uri": f"/v1/test-runs/{previous}", "reused": True}
            resolved = await _value(manager.registry.request_for_pr(repo, pr_number))
            result = await create(resolved, webhook_identity, f"github:{delivery}", f"github:{delivery}")
            manager.delivery_runs[delivery] = result["run_id"]
            manager.persist()
            return result

    def memory_events() -> list[dict[str, Any]]:
        memory = getattr(manager, "memory", None)
        return memory.read_events() if memory is not None else []

    @app.get("/listenToSpecAndCodeAgents")
    async def listen(repo_url: str, pr_number: int, prd_reference: str, memory_source: str = "local", timeout: float = 10, identity: Principal = Depends(principal)):
        repo = _repo_name(repo_url)
        authorize(identity, repo, "engineer")
        if not 0 < timeout <= 30 or pr_number < 1:
            raise HTTPException(422, "Invalid timeout or PR number")
        resolved = await _value(manager.registry.request_for_pr(repo, pr_number))
        if prd_reference not in {resolved.spec_id, resolved.spec_uri}:
            raise HTTPException(409, "PRD reference does not match registered approved artifact")
        if memory_source != "local":
            raise HTTPException(422, "Only local redacted evolution memory is supported")
        context = manager.registry.resolve(resolved)
        history = []
        for event in memory_events():
            if event.get("source_identity", {}).get("repo") == repo:
                history.append({key: event.get(key) for key in ("event_id", "kind", "run_id", "recorded_at", "source_identity", "payload")})
        detail = {
            "prd_intent": context.spec.intent,
            "code_diff": manager.registry.diff(context),
            "prerequisite_status": [item.model_dump(mode="json") for item in context.environment.prerequisites],
            "historical_learnings": history[-10:],
            "memory_source": "local",
            "history_note": "Redacted learning records remain in the local evolution memory; no remote source was queried.",
        }
        context_id = secrets.token_urlsafe(24)
        app.state.legacy_contexts[context_id] = {"subject": identity.name, "request": resolved, "identity_hash": context.identity_hash, "timeout": timeout}
        return {**detail, "context_id": context_id}

    @app.post("/verifyAndJudgeReadiness")
    async def legacy_verify(request: LegacyVerifyBody, identity: Principal = Depends(principal)):
        context = app.state.legacy_contexts.get(request.context_id)
        if context is None or context["subject"] != identity.name:
            raise HTTPException(404, "Context not found for authenticated user")
        resolved = context["request"]
        authorize(identity, resolved.repo, "engineer")
        if manager.registry.resolve(resolved).identity_hash != context["identity_hash"]:
            raise HTTPException(409, "Context is stale; fetch an explicit context for the updated inputs")
        if request.pytest_suite != resolved.test_profile_id:
            raise HTTPException(422, "pytest_suite must match the registered test profile")
        budget = request.compute_budget
        if isinstance(budget, (int, float)):
            if not 0 <= budget <= 2:
                raise HTTPException(422, "Compute budget must be between zero and US$2")
            changes = {"max_llm_cost_usd": budget}
        else:
            changes = budget.model_dump()
        resolved = CreateRunRequest.model_validate({**resolved.model_dump(), **changes, "max_debug_iterations": request.max_debug_iterations})
        result = await create(resolved, identity, f"legacy:{request.context_id}", f"legacy:{identity.name}:{request.context_id}")
        task = app.state.tasks.get(result["run_id"])
        if task and not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=context["timeout"])
            except asyncio.TimeoutError:
                pass
        record = await get_run(result["run_id"], identity)
        ready = record["readiness"] == "READY_FOR_QA"
        owner = "QA" if ready else (record["unresolved_findings"][0]["owner_role"] if record.get("unresolved_findings") else "engineer")
        if record["readiness"] == "CHANGES_REQUIRED":
            owner = "Code Agent"
        return {
            **result,
            "deployment_readiness": ready,
            "human_approval_required": True,
            "readiness": record["readiness"],
            "regression_fix_commit": record.get("candidate_patch", {}).get("commit_sha") if record.get("candidate_patch") else None,
            "rca_diagnostics": record.get("unresolved_findings", []),
            "memory_update": {"recorded": any(event.get("run_id") == record["run_id"] for event in memory_events()), "run_id": record["run_id"]},
            "next_phase_target": owner,
        }

    return app


app = create_app()
