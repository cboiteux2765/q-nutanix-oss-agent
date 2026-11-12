"""Offline REPL-style MCP smoke test; not a live Prime Agent host session."""
from __future__ import annotations

import ast
import asyncio
from hashlib import sha256
import inspect
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from app.prime_tools import StageTarget, ToolPolicy
from app.providers import SourceRegistry, make_demo_repository


async def run_cell(source: str, namespace: dict) -> None:
    code = compile(source, "<prime-repl-example>", "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
    result = eval(code, namespace)
    if inspect.isawaitable(result):
        await result


async def main() -> None:
    with TemporaryDirectory(prefix="evo-prime-repl-") as temporary:
        root = Path(temporary)
        _, registration = make_demo_repository(root / "repository")
        registry = SourceRegistry(root / "state/sources")
        registry.register(registration)
        request = registry.request_for_pr("org/service", 42)
        context = registry.resolve(request)
        target = StageTarget(repo="org/service", pr_number=42,
            expected_base_sha=context.base_sha, expected_head_sha=context.head_sha,
            spec_id=request.spec_id, spec_hash=request.spec_hash,
            test_profile_id=request.test_profile_id,
            test_profile_hash=context.profile.fingerprint,
            environment_manifest_id=request.environment_manifest_id,
            environment_fingerprint=context.environment.fingerprint,
            policy_version=context.policy_version, correlation_id="repl-smoke")
        policy = ToolPolicy(operator_name="local-example-operator", allowed_repos=("org/service",),
            allowed_backends=("local_trusted",), max_runtime_seconds=120)
        policy_file = root / "operator-policy.json"
        policy_file.write_text(policy.model_dump_json(), encoding="utf-8")
        server = StdioServerParameters(command=sys.executable, args=["-m", "app.prime_mcp"],
            cwd=str(Path(__file__).resolve().parents[1]),
            env={**os.environ, "EVO_TEST_DATA_DIR": str(root / "state"),
                 "EVO_TEST_POLICY_FILE": str(policy_file)})

        async with stdio_client(server) as (reader, writer):
            async with ClientSession(reader, writer) as client:
                await client.initialize()

                class PrimeMcpFacade:
                    async def call_tool(self, connection: str, name: str, arguments: dict):
                        if connection != "evo-test":
                            raise ValueError("Unexpected MCP connection")
                        return await client.call_tool(name, arguments)

                namespace = {"mcp": PrimeMcpFacade(),
                             "handoff": {"test_target": target.model_dump(mode="json")}}
                await run_cell('target = handoff["test_target"]', namespace)
                await run_cell('check = await mcp.call_tool("evo-test", "test_preflight", '
                               '{"target": target})', namespace)
                assert not namespace["check"].is_error
                assert namespace["check"].structured_content["ready"] is True
                await run_cell('run = await mcp.call_tool("evo-test", "test_verify", '
                               '{"request": {**target, "budget": {'
                               '"max_debug_iterations": 0, "max_test_executions": 4, '
                               '"max_runtime_seconds": 120, "max_llm_cost_usd": 0}}})', namespace)
                result = namespace["run"].structured_content
                assert not namespace["run"].is_error and result["readiness"] == "READY_FOR_QA"
                await run_cell('evidence = await mcp.call_tool("evo-test", "test_manifest", '
                               '{"run_id": run.structured_content["run_id"]})', namespace)
                await run_cell('artifact = await mcp.call_tool("evo-test", "test_artifact", '
                               '{"run_id": run.structured_content["run_id"], '
                               '"name": "initial/junit.xml"})', namespace)
                manifest = namespace["evidence"].structured_content
                artifact = namespace["artifact"].structured_content
                assert not namespace["evidence"].is_error and not namespace["artifact"].is_error
                assert manifest["run_id"] == result["run_id"]
                assert manifest["artifact_manifest_hash"] == result["artifact_manifest_hash"]
                assert sha256(artifact["content_utf8"].encode()).hexdigest() == artifact["sha256"]
                assert {"target", "check", "run", "evidence", "artifact"} <= namespace.keys()
                print("REPL-style stdio MCP flow: PASS")
                print("Stages: preflight -> verify -> manifest -> artifact")
                print("Readiness:", result["readiness"], "tests passed:", result["tests"]["passed"])
                print("Persistent variables:", ", ".join(sorted(namespace.keys() - {"handoff", "mcp"})))
                print("Live Prime Agent host session: NOT TESTED")


if __name__ == "__main__":
    asyncio.run(main())
