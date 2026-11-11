from abc import ABC, abstractmethod
from typing import ClassVar

from spec_agent.models import Model


class NoParameters(Model):
    pass


class WorkflowStep(ABC):
    requires: ClassVar[frozenset[str]] = frozenset()
    produces: ClassVar[frozenset[str]] = frozenset()
    parameters_model: ClassVar[type[Model]] = NoParameters

    @abstractmethod
    def run(self, inputs: dict, parameters: Model, llm) -> dict:
        """Read declared inputs and return exactly the declared output fields."""


class StepRegistry:
    """Registration is trusted application code, never workflow configuration."""
    def __init__(self):
        self._steps: dict[str, type[WorkflowStep]] = {}

    def register(self, name: str, step: type[WorkflowStep]):
        if name in self._steps:
            raise ValueError(f"Step type already registered: {name}")
        if not issubclass(step, WorkflowStep):
            raise TypeError("Expected WorkflowStep implementation")
        self._steps[name] = step

    def get(self, name: str) -> type[WorkflowStep]:
        try:
            return self._steps[name]
        except KeyError:
            raise ValueError(f"Unknown step type: {name}") from None
