"""Primary stdio MCP entry point. Run as an operator-owned tool process."""
from __future__ import annotations

import os
from pathlib import Path

from mcp.server import MCPServer

from .prime_tools import (ArtifactOutput, ManifestOutput, PreflightOutput, StageTarget,
                          TestStageTools, VerifyInput, VerifyOutput, load_operator_tools)


def build_server(tools: TestStageTools) -> MCPServer:
    server = MCPServer("evo-test", instructions=(
        "Test-stage tools for one operator-registered local PR mirror. "
        "All inputs are pinned and permission-scoped by this server. "
        "READY_FOR_QA requires a separate human QA decision."))

    @server.tool(name="test_preflight", structured_output=True)
    def test_preflight(target: StageTarget) -> PreflightOutput:
        """Check registered versions, execution permissions and prerequisites."""
        return tools.preflight_pr(target)

    @server.tool(name="test_verify", structured_output=True)
    async def test_verify(request: VerifyInput) -> VerifyOutput:
        """Run bounded verification against a pinned, registered PR snapshot."""
        return await tools.verify_pr(request)

    @server.tool(name="test_manifest", structured_output=True)
    def test_manifest(run_id: str) -> ManifestOutput:
        """Read the checksum-verified evidence manifest for an authorized run."""
        return tools.manifest(run_id)

    @server.tool(name="test_artifact", structured_output=True)
    def test_artifact(run_id: str, name: str) -> ArtifactOutput:
        """Read a bounded UTF-8 artifact listed in the verified manifest."""
        return tools.artifact(run_id, name)

    return server


def main() -> None:
    data = os.getenv("EVO_TEST_DATA_DIR")
    policy = os.getenv("EVO_TEST_POLICY_FILE")
    if not data or not policy:
        raise RuntimeError("Operator must configure EVO_TEST_DATA_DIR and EVO_TEST_POLICY_FILE")
    build_server(load_operator_tools(Path(data), Path(policy))).run(transport="stdio")


if __name__ == "__main__":
    main()
