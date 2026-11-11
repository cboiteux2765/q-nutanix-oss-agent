"""Versioned contracts for Core, workflow steps, and model responses."""
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RepositoryFile(Model):
    path: str = Field(min_length=1)
    content: str


class MemoryEntry(Model):
    content: str
    source: str | None = None


class StepConfig(Model):
    id: str = Field(min_length=1)
    type: str
    depends_on: list[str] = Field(default_factory=list)
    parameters: dict[str, Any] = Field(default_factory=dict)


class WorkflowConfig(Model):
    name: str = "spec-agent"
    version: str = "1.0"
    steps: list[StepConfig]

    @classmethod
    def from_text(cls, text: str) -> "WorkflowConfig":
        """Load JSON or YAML without Python object constructors."""
        import yaml
        return cls.model_validate(yaml.safe_load(text))


class SpecAgentRequest(Model):
    idea: str = Field(min_length=1)
    repository_files: list[RepositoryFile] = Field(default_factory=list)
    memory: list[MemoryEntry] = Field(default_factory=list)
    workflow: WorkflowConfig | None = None

    @field_validator("idea")
    @classmethod
    def meaningful_idea(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("idea cannot be blank")
        return value


class Evidence(Model):
    path: str
    quote: str = Field(min_length=1)


class IdeaAnalysis(Model):
    primary_goal: str
    explicit_requirements: list[str]
    constraints: list[str] = Field(default_factory=list)
    scope: list[str] = Field(default_factory=list)
    inspection_targets: list[str] = Field(default_factory=list)
    unresolved_items: list[str] = Field(default_factory=list)


class Finding(Model):
    description: str
    certainty: Literal["confirmed", "inference", "unknown"]
    evidence: list[Evidence] = Field(default_factory=list)


class RepositoryFindings(Model):
    context_available: bool
    findings: list[Finding] = Field(default_factory=list)
    missing_context: list[str] = Field(default_factory=list)


class Requirement(Model):
    id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    kind: Literal["functional", "nonfunctional"] = "functional"
    origin: Literal["user", "context", "suggestion"]
    source: str = Field(min_length=1, description="Exact idea/memory excerpt or evidence quote")
    acceptance_criteria: list[str]
    evidence: list[Evidence] = Field(default_factory=list)


class Requirements(Model):
    items: list[Requirement]
    constraints: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    unresolved_items: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)


class Task(Model):
    id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    requirement_ids: list[str]
    depends_on: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str]
    relevant_files: list[str] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)


Status = Literal["DRAFT_READY", "BLOCKED", "FAILED"]


class TaskPlan(Model):
    schema_version: Literal["1.0"] = "1.0"
    requirements: list[Requirement] = Field(default_factory=list)
    tasks: list[Task] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    unresolved_items: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    status: Status = "BLOCKED"


class TaskDraft(Model):
    tasks: list[Task]


class ValidationIssue(Model):
    issue_type: str
    severity: Literal["info", "warning", "critical"]
    affected_id: str | None = None
    explanation: str
    suggested_correction: str


class SemanticReview(Model):
    issues: list[ValidationIssue]


class Revision(Model):
    requirements: Requirements
    task_draft: TaskDraft


class ValidationResult(Model):
    issues: list[ValidationIssue]
    revisions: int
    status: Literal["DRAFT_READY", "BLOCKED"]


class PlanningModel(Model):
    original_idea: str
    analysis: IdeaAnalysis
    repository: RepositoryFindings
    plan: TaskPlan
    validation: ValidationResult


class Artifacts(Model):
    product_spec: str
    task_plan_json: str


class StepTrace(Model):
    workflow_name: str
    workflow_version: str
    step_id: str
    step_type: str
    status: Literal["SUCCEEDED", "FAILED"]
    started_at: datetime
    ended_at: datetime
    duration_seconds: float
    input_summary: list[str]
    output_summary: list[str]
    validation_issues: list[ValidationIssue] = Field(default_factory=list)
    error: str | None = None


class SpecAgentResult(Model):
    status: Status
    product_spec: str
    task_plan: TaskPlan
    task_plan_json: str
    unresolved_items: list[str]
    validation_issues: list[ValidationIssue]
    workflow_version: str
    execution_trace: list[StepTrace]
