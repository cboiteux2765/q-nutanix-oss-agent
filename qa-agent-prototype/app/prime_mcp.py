"""Optional MCP transport. Prime owns sessions and recursive delegation."""
import os
from pathlib import Path
from typing import Any

from .prime_tools import QATools, load_tools


def build_server(tools: QATools):
    from mcp.server import MCPServer
    server = MCPServer("evo-qa", instructions="QA tools for operator-registered targets. Scanning is not approval.")

    @server.tool(name="qa_inspect", structured_output=True)
    def qa_inspect(target_id: str) -> dict[str, Any]:
        """Inspect pinned changes and classify risk for an authorized target."""
        return tools.call("qa_inspect", target_id)

    @server.tool(name="qa_scan", structured_output=True)
    def qa_scan(target_id: str) -> dict[str, Any]:
        """Run fixed security, secret and demo dependency checks; does not approve."""
        return tools.call("qa_scan", target_id)

    @server.tool(name="qa_review", structured_output=True)
    def qa_review(target_id: str) -> dict[str, Any]:
        """Validate registered Test evidence and return a commit-bound QA recommendation."""
        return tools.call("qa_review", target_id)

    return server


def main():
    value = os.environ.get("EVO_QA_CONFIG")
    if not value:
        raise RuntimeError("Operator must set EVO_QA_CONFIG")
    build_server(load_tools(Path(value))).run(transport="stdio")


if __name__ == "__main__":
    main()
