import pytest

from spec_agent import SpecAgent, SpecAgentRequest, default_workflow, WorkflowConfig
from spec_agent.models import StepConfig
from spec_agent.steps import default_registry
from spec_agent.workflow import WorkflowStep, WorkflowValidator


def validate(config, registry=None):
    return WorkflowValidator().validate(config, registry or default_registry())


@pytest.mark.parametrize("mutation", [
    lambda w: w.steps.append(w.steps[0]),
    lambda w: setattr(w.steps[0], "type", "unknown"),
    lambda w: w.steps[1].depends_on.append("missing"),
    lambda w: w.steps[0].depends_on.append("tasks"),
    lambda w: w.steps[1].depends_on.clear(),
    lambda w: w.steps.pop(4),
    lambda w: w.steps[4].parameters.update(max_revisions=-1),
    lambda w: w.steps[0].parameters.update(python="exec('bad')"),
])
def test_invalid_workflows(mutation):
    config = default_workflow()
    mutation(config)
    with pytest.raises(ValueError):
        validate(config)


def test_reordered_json_and_yaml():
    config = default_workflow()
    config.steps.reverse()
    assert [s.id for s in validate(WorkflowConfig.from_text(config.model_dump_json()))] == [
        "analyze", "inspect", "requirements", "tasks", "validate", "artifacts"]
    with pytest.raises(Exception):
        WorkflowConfig.from_text("!!python/object/apply:os.system ['echo unsafe']")


class Extra(WorkflowStep):
    requires = frozenset({"analysis"})
    produces = frozenset({"note"})

    def run(self, inputs, parameters, llm):
        return {"note": "extension ran"}


def test_custom_workflow(llm):
    config = default_workflow()
    config.version = "core-custom-2"
    config.steps.append(StepConfig(id="extra", type="extra", depends_on=["analyze"]))
    config.steps[-2].depends_on.append("extra")
    registry = default_registry()
    registry.register("extra", Extra)
    result = SpecAgent(llm, registry).run(SpecAgentRequest(idea="Add health endpoint", workflow=config))
    assert result.status == "DRAFT_READY"
    assert len(result.execution_trace) == 7
    assert result.workflow_version == "core-custom-2"


@pytest.mark.parametrize("requires,produces", [({"missing"}, {"note"}), (set(), {"analysis"})])
def test_missing_inputs_and_output_conflicts(requires, produces):
    class InvalidExtra(Extra):
        pass
    InvalidExtra.requires = frozenset(requires)
    InvalidExtra.produces = frozenset(produces)
    registry = default_registry()
    registry.register("extra", InvalidExtra)
    config = default_workflow()
    config.steps.append(StepConfig(id="extra", type="extra"))
    config.steps[-2].depends_on.append("extra")
    with pytest.raises(ValueError):
        validate(config, registry)


def test_no_processing_after_artifacts():
    registry = default_registry()
    registry.register("extra", Extra)
    config = default_workflow()
    config.steps.append(StepConfig(id="extra", type="extra", depends_on=["artifacts"]))
    with pytest.raises(ValueError):
        validate(config, registry)


def test_cannot_replace_validation():
    registry = default_registry()
    registry._steps["validate_plan"] = Extra
    with pytest.raises(ValueError):
        validate(default_workflow(), registry)


def test_extension_output_contract_failure_has_trace(llm):
    class Broken(Extra):
        def run(self, inputs, parameters, llm):
            return {"planning": "unauthorized overwrite"}
    registry = default_registry()
    registry.register("broken", Broken)
    config = default_workflow()
    config.steps.append(StepConfig(id="broken", type="broken", depends_on=["analyze"]))
    config.steps[-2].depends_on.append("broken")
    result = SpecAgent(llm, registry).run(SpecAgentRequest(idea="Health", workflow=config))
    assert result.status == "FAILED"
    assert result.execution_trace[-1].step_id == "broken"
    assert result.execution_trace[-1].status == "FAILED"
