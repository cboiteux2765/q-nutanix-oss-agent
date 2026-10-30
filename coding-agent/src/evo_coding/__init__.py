"""EVO Coding Agent: approved spec handoff -> scoped draft pull request + work report."""
from __future__ import annotations

from pathlib import Path

from .agent import CodingAgent, RunConflict, RunInProgress, RunNotFound
from .config import CodingConfig
from .handoff import HandoffSchemaError, load_handoff
from .models import CodingResult, Handoff, RevisionRequest, RunState

__version__ = "0.1.0"

__all__ = [
    "CodingAgent", "CodingConfig", "CodingResult", "Handoff", "HandoffSchemaError", "RevisionRequest", "RunConflict",
    "RunInProgress", "RunNotFound", "RunState", "load_handoff", "revise_coding_task", "run_coding_task",
]


def run_coding_task(handoff_path: Path, config: CodingConfig | None = None, **agent_kwargs) -> CodingResult:
    """Orchestrator entry point for the coding phase."""
    return CodingAgent(config, **agent_kwargs).run(Path(handoff_path))


def revise_coding_task(run_id: str, request: RevisionRequest, config: CodingConfig | None = None,
                       **agent_kwargs) -> CodingResult:
    """Orchestrator entry point when Test/QA returns CHANGES_REQUIRED."""
    return CodingAgent(config, **agent_kwargs).revise(run_id, request)
