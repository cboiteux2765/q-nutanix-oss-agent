from importlib.resources import files
import warnings

from spec_agent.models import SpecAgentRequest, SpecAgentResult, TaskPlan, WorkflowConfig
from spec_agent.services.llm_client import LLMClient, UnconfiguredLLM
from spec_agent.services.validation import issue
from spec_agent.steps import default_registry
from spec_agent.workflow.executor import WorkflowExecutor, ExecutionFailure


def default_workflow() -> WorkflowConfig:
    return WorkflowConfig.from_text(files("spec_agent.workflow").joinpath("default.yaml").read_text())


class SpecAgent:
    def __init__(self, llm: LLMClient | None = None, registry=None):
        warnings.warn("The fixed SpecAgent pipeline is deprecated; use sdlc_tools.task_graph and the task-planning skill.",
                      DeprecationWarning, stacklevel=2)
        self.llm = llm if llm is not None else UnconfiguredLLM()
        self.registry = registry if registry is not None else default_registry()

    def run(self, request: SpecAgentRequest) -> SpecAgentResult:
        workflow = request.workflow or default_workflow()
        try:
            context, traces = WorkflowExecutor().execute(workflow, self.registry, {
                "idea": request.idea, "repository_files": request.repository_files,
                "memory": request.memory}, self.llm)
        except Exception as exc:
            traces = exc.traces if isinstance(exc, ExecutionFailure) else []
            failure = traces[-1].validation_issues[0] if traces else issue("invalid_workflow",
                            "Workflow configuration is invalid; validate it with WorkflowValidator for details.")
            plan = TaskPlan(status="FAILED", unresolved_items=[failure.explanation])
            return SpecAgentResult(status="FAILED", product_spec="", task_plan=plan,
                task_plan_json=plan.model_dump_json(indent=2), unresolved_items=plan.unresolved_items,
                validation_issues=[failure], workflow_version=workflow.version, execution_trace=traces)
        planning, artifacts = context["planning"], context["artifacts"]
        return SpecAgentResult(status=planning.plan.status, product_spec=artifacts.product_spec,
            task_plan=planning.plan, task_plan_json=artifacts.task_plan_json,
            unresolved_items=planning.plan.unresolved_items,
            validation_issues=planning.validation.issues, workflow_version=workflow.version,
            execution_trace=traces)


def run_spec_agent(request: SpecAgentRequest, *, llm: LLMClient | None = None) -> SpecAgentResult:
    """Core entry point. Inject a provider client; absence fails explicitly."""
    return SpecAgent(llm=llm).run(request)
