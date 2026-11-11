from spec_agent.services.artifact_renderer import render
from spec_agent.workflow.registry import WorkflowStep


class GenerateArtifacts(WorkflowStep):
    requires = frozenset({"planning"})
    produces = frozenset({"artifacts"})

    def run(self, inputs, parameters, llm):
        return {"artifacts": render(inputs["planning"])}
