from graphlib import TopologicalSorter, CycleError

from spec_agent.models import WorkflowConfig

INITIAL_FIELDS = frozenset({"idea", "repository_files", "memory"})


class WorkflowValidator:
    def validate(self, config: WorkflowConfig, registry) -> list:
        # Core can change edges and add trusted steps, but cannot replace the
        # contracts/implementations of the six mandatory stages.
        from spec_agent.steps import BUILTINS
        ids = [s.id for s in config.steps]
        if len(set(ids)) != len(ids):
            raise ValueError("Duplicate step IDs")
        by_id = {s.id: s for s in config.steps}
        producers = {}
        for step in config.steps:
            impl = registry.get(step.type)
            impl.parameters_model.model_validate(step.parameters)
            if set(step.depends_on) - by_id.keys():
                raise ValueError(f"Missing dependencies for {step.id}")
            for field in impl.produces:
                if field in producers or field in INITIAL_FIELDS:
                    raise ValueError(f"Conflicting output producer: {field}")
                producers[field] = step.id
        for name, impl in BUILTINS.items():
            if sum(s.type == name for s in config.steps) != 1 or registry.get(name) is not impl:
                raise ValueError(f"Missing or replaced mandatory stage: {name}")
        try:
            order = list(TopologicalSorter({s.id: s.depends_on for s in config.steps}).static_order())
        except CycleError:
            raise ValueError("Circular workflow dependencies") from None
        ancestors = {}
        for sid in order:
            step = by_id[sid]
            ancestors[sid] = set(step.depends_on)
            for dep in step.depends_on:
                ancestors[sid].update(ancestors[dep])
            for field in registry.get(step.type).requires - INITIAL_FIELDS:
                if field not in producers:
                    raise ValueError(f"Missing required input: {field}")
                if producers[field] not in ancestors[sid]:
                    raise ValueError(f"Invalid ordering: {sid} requires dependency on {producers[field]}")
        # Artifact generation is terminal: no processing can run after publication.
        artifact_id = next(s.id for s in config.steps if s.type == "generate_artifacts")
        if set(ids) - {artifact_id} != ancestors[artifact_id]:
            raise ValueError("All stages must precede artifact generation")
        return [by_id[sid] for sid in order]
