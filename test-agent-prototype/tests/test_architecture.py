"""Checks that also run without asyncio/socket permissions in the host sandbox."""
import ast
import os
from pathlib import Path
import subprocess
import sys
import tomllib


ROOT = Path(__file__).resolve().parents[1]


def test_all_core_modules_parse_and_no_tool_spawns_agents_or_shells():
    for path in (ROOT / "app").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                function = ast.unparse(node.func)
                assert not any(part in function.split(".") for part in
                               {"spawn", "spawn_agent", "create_session", "system", "create_subprocess_shell"}), function
                assert not any(keyword.arg == "shell" and isinstance(keyword.value, ast.Constant)
                               and keyword.value.value is True for keyword in node.keywords)


def test_tool_imports_have_no_http_dependency_or_storage_side_effect(tmp_path):
    # This imports only: no event loop, test subprocess, Docker, or MCP transport.
    code = (
        "import sys; from pathlib import Path; import app.prime_tools; "
        "assert 'app.main' not in sys.modules; assert 'fastapi' not in sys.modules; "
        "assert not Path('.evo-data').exists(); print('TOOL_IMPORT_OK')"
    )
    result = subprocess.run([sys.executable, "-c", code], cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(ROOT)}, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "TOOL_IMPORT_OK"


def test_package_defaults_to_tools_and_http_is_optional():
    configuration = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = configuration["project"]
    assert "mcp>=2,<3" in project["dependencies"]
    assert all(not dependency.startswith(("fastapi", "httpx", "uvicorn"))
               for dependency in project["dependencies"])
    assert project["scripts"]["evo-test-tools"] == "app.prime_mcp:main"
    assert "qa" in project["optional-dependencies"]


def test_review_api_has_no_queue_webhook_or_agent_context():
    tree = ast.parse((ROOT / "app/main.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            function = ast.unparse(node.func)
            assert not function.endswith(("create_run", "execute", "create_task", "spawn"))
        if isinstance(node, ast.Assign):
            assert all(not isinstance(target, ast.Name) or target.id != "app"
                       for target in node.targets) or not isinstance(node.value, ast.Call) or not (
                           isinstance(node.value.func, ast.Name) and node.value.func.id == "create_app")
