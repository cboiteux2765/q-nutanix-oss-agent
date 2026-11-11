from copy import deepcopy
from datetime import datetime, timezone
from time import perf_counter

from spec_agent.models import StepTrace
from spec_agent.services.llm_client import LLMResponseError
from spec_agent.services.validation import issue
from spec_agent.workflow.validator import WorkflowValidator


class ExecutionFailure(Exception):
    def __init__(self, traces):
        self.traces = traces
        super().__init__("Workflow step failed; see sanitized trace")


class WorkflowExecutor:
    def execute(self, config, registry, initial, llm):
        order = WorkflowValidator().validate(config, registry)
        context = deepcopy(initial)
        traces = []
        for step in order:
            impl = registry.get(step.type)
            start = datetime.now(timezone.utc)
            tick = perf_counter()
            outputs = {}
            error = None
            failure_issues = []
            try:
                inputs = deepcopy({key: context[key] for key in impl.requires})
                outputs = impl().run(inputs, impl.parameters_model.model_validate(step.parameters), llm)
                if not isinstance(outputs, dict) or set(outputs) != impl.produces:
                    raise ValueError("Step violated output contract")
                context.update(outputs)
            except Exception as exc:
                error = f"{type(exc).__name__}: step execution failed (details redacted)"
                failure_issues = [issue(
                    "schema_violation" if isinstance(exc, LLMResponseError) else "execution_failure",
                    "Structured model response violates the schema." if isinstance(exc, LLMResponseError)
                    else "Step execution failed; check the configured client and implementation.", step.id)]
            planning = outputs.get("planning") if not error else None
            traces.append(StepTrace(
                workflow_name=config.name, workflow_version=config.version,
                step_id=step.id, step_type=step.type,
                status="FAILED" if error else "SUCCEEDED", started_at=start,
                ended_at=datetime.now(timezone.utc), duration_seconds=perf_counter() - tick,
                input_summary=sorted(impl.requires), output_summary=sorted(outputs) if not error else [],
                validation_issues=planning.validation.issues if planning else failure_issues, error=error))
            if error:
                raise ExecutionFailure(traces)
        return context, traces
