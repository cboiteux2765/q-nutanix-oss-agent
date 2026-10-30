from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from evo_coding import gitops  # noqa: E402
from evo_coding.agent import CodingAgent  # noqa: E402
from evo_coding.config import CodingConfig, PolicyConfig, WorkspaceConfig  # noqa: E402
from evo_coding.implementers.scripted import ScriptedImplementer  # noqa: E402
from evo_coding.pilot import build_handoff, create_pilot_remote, load_scenario, pilot_dir  # noqa: E402
from evo_coding.publish import LocalGitPublisher  # noqa: E402

SCENARIOS = pilot_dir() / "scenarios"


@pytest.fixture
def pilot(tmp_path):
    """A fresh pilot 'origin' plus helpers to build handoffs and agents against it."""
    return Pilot(tmp_path)


class Pilot:
    def __init__(self, root: Path):
        self.root = root
        self.remote, self.sha = create_pilot_remote(root / "remote")
        self.publisher = LocalGitPublisher(self.remote)
        self.work_dir = root / "work"

    def handoff(self, scenario: str = "01-median-even", run_id: str = "run-001", **overrides) -> Path:
        source = overrides.pop("source_commit", self.sha)
        return build_handoff(SCENARIOS / f"{scenario}.toml", source, self.root / "handoffs" / run_id, run_id, **overrides)

    def script(self, scenario: str = "01-median-even") -> dict:
        return load_scenario(SCENARIOS / f"{scenario}.toml")["implementation"]

    def config(self, **policy) -> CodingConfig:
        return CodingConfig(work_dir=self.work_dir, policy=PolicyConfig(**policy),
                            workspace=WorkspaceConfig(command_timeout_seconds=120))

    def agent(self, script: dict | None = None, implementer=None, config: CodingConfig | None = None,
              hooks=None) -> CodingAgent:
        implementer = implementer or ScriptedImplementer(script if script is not None else self.script(), hooks)
        return CodingAgent(config or self.config(), implementer=implementer, publisher=self.publisher)

    def remote_log(self) -> list[str]:
        return gitops.git(self.remote, "log", "--all", "--format=%H %s").splitlines()

    def remote_file(self, ref: str, path: str) -> str:
        return gitops.git(self.remote, "show", f"{ref}:{path}")
