"""Compatibility utilities; no agent needs to be initialized.

Keep evidence and specification rendering in their existing maintained modules.
"""
from spec_agent.models import Requirement, Requirements, Evidence, RepositoryFile, RepositoryFindings
from spec_agent.services.artifact_renderer import render
from spec_agent.services.validation import validate_findings
from .legacy import from_task_plan

__all__ = ["Requirement", "Requirements", "Evidence", "RepositoryFile", "RepositoryFindings",
           "render", "validate_findings", "from_task_plan"]
