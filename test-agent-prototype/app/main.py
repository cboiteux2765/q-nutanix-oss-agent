"""Optional human QA/evidence API; Prime owns all execution intake and scheduling.

Run with ``uvicorn app.main:create_app --factory``. Importing this module does
not create a manager, write storage, queue work, or start an agent.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
from typing import Mapping

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from .models import FeedbackRequest, Principal, ReviewRequest
from .run_manager import RunManager


def create_app(
    manager: RunManager | None = None,
    *,
    tokens: Mapping[str, Principal] | None = None,
) -> FastAPI:
    """Expose existing results and authenticated human decisions only.

    Use one operator-owned manager. A separate server must not concurrently
    open the same single-process run store as a running MCP server.
    """
    if manager is None:
        manager = RunManager(Path(os.getenv("EVO_DATA_DIR", ".evo-data")))
    token_map = dict(tokens or {})
    if tokens is None:
        repos = frozenset(item.strip() for item in
            os.getenv("EVO_ALLOWED_REPOS", "org/service").split(",") if item.strip())
        for role, variable in (("engineer", "EVO_ENGINEER_TOKEN"), ("qa", "EVO_QA_TOKEN")):
            credential = os.getenv(variable)
            if credential:
                if credential in token_map:
                    raise ValueError("Engineer and QA bearer credentials must be distinct")
                name = os.getenv(f"EVO_{role.upper()}_NAME", f"configured-{role}")
                token_map[credential] = Principal(name=name, roles=frozenset({role}), repos=repos)
    app = FastAPI(title="EVO Test-stage human QA", version="1.0.0")
    app.state.manager = manager

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
            raise HTTPException(401, "Bearer authentication required",
                                headers={"WWW-Authenticate": "Bearer"})
        identity = token_map.get(authorization[7:])
        if identity is None:
            raise HTTPException(401, "Invalid bearer token",
                                headers={"WWW-Authenticate": "Bearer"})
        return identity

    def get_run(run_id: str, identity: Principal):
        # Authorize using trusted metadata before freshness checks or file I/O.
        manager.authorize(identity, manager.runs[run_id].context.repo)
        return manager.get_run(run_id)

    @app.get("/v1/test-runs/{run_id}")
    async def status(run_id: str, identity: Principal = Depends(principal)):
        result = get_run(run_id, identity)
        manager.artifact_path(run_id, "verification.json")
        return result

    @app.get("/v1/test-runs/{run_id}/artifacts")
    async def artifacts(run_id: str, identity: Principal = Depends(principal)):
        get_run(run_id, identity)
        return manager.artifacts(run_id)

    @app.get("/v1/test-runs/{run_id}/artifacts/{name:path}")
    async def artifact_file(run_id: str, name: str, identity: Principal = Depends(principal)):
        get_run(run_id, identity)
        if any(not re.fullmatch(r"[A-Za-z0-9_.-]+", part) or part in {".", ".."}
               for part in name.split("/")):
            raise HTTPException(404, "Artifact not found")
        path = manager.artifact_path(run_id, name)
        return FileResponse(path, filename=path.name, headers={"Cache-Control": "no-store"})

    @app.post("/v1/test-runs/{run_id}/review")
    async def review(run_id: str, request: ReviewRequest, identity: Principal = Depends(principal)):
        get_run(run_id, identity)
        return manager.review_run(run_id, request, identity)

    @app.post("/v1/test-runs/{run_id}/feedback")
    async def feedback(run_id: str, request: FeedbackRequest, identity: Principal = Depends(principal)):
        get_run(run_id, identity)
        return manager.feedback_run(run_id, request, identity)

    return app
