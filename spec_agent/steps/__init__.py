from .processing import AnalyzeIdea, InspectRepository, GenerateRequirements, GenerateTasks
from .validate_plan import ValidatePlan
from .generate_artifacts import GenerateArtifacts
from spec_agent.workflow.registry import StepRegistry

BUILTINS = {
    "analyze_idea": AnalyzeIdea,
    "inspect_repository": InspectRepository,
    "generate_requirements": GenerateRequirements,
    "generate_tasks": GenerateTasks,
    "validate_plan": ValidatePlan,
    "generate_artifacts": GenerateArtifacts,
}


def default_registry():
    registry = StepRegistry()
    for name, implementation in BUILTINS.items():
        registry.register(name, implementation)
    return registry
