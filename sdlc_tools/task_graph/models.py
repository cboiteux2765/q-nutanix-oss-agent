"""Serializable graph contracts. Dependencies exist only on the graph."""
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StringConstraints

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class Task(Model):
    id: Text
    title: Text
    description: Text
    requirement_ids: list[Text] = Field(default_factory=list)
    acceptance_criteria: list[Text] = Field(default_factory=list)
    relevant_files: list[Text] = Field(default_factory=list)
    metadata: dict[str, JsonValue] = Field(default_factory=dict)


class Dependency(Model):
    prerequisite_id: Text
    dependent_id: Text


class HistoryEntry(Model):
    operation: Text
    task_ids: list[str]
    previous_version: int = Field(ge=0)
    new_version: int = Field(ge=1)
    timestamp: datetime
    actor: Text | None = None
    success: Literal[True] = True


class GraphData(Model):
    schema_version: Literal["1.0"] = "1.0"
    tasks: list[Task] = Field(default_factory=list)
    dependencies: list[Dependency] = Field(default_factory=list)
    metadata: dict[str, JsonValue] = Field(default_factory=dict)
    version: int = Field(default=0, ge=0)
    history: list[HistoryEntry] = Field(default_factory=list)


class ValidationIssue(Model):
    code: str
    message: str
    task_id: str | None = None
    requirement_id: str | None = None
