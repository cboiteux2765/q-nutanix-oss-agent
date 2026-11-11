import pytest

from spec_agent.services.llm_client import DemoLLM


class PassingLLM(DemoLLM):
    """Scripted successful review for infrastructure tests, not actual reasoning."""
    def complete(self, *, operation, **kwargs):
        if operation == "semantic_review":
            return {"issues": []}
        return super().complete(operation=operation, **kwargs)


@pytest.fixture
def llm():
    return PassingLLM()
