import json
import subprocess
import sys
from pathlib import Path


def test_cli_writes_consistent_demo_artifacts(tmp_path):
    root = Path(__file__).resolve().parents[1]
    completed = subprocess.run([
        sys.executable, "-m", "spec_agent", str(root / "examples/request.json"),
        "--demo", "--output", str(tmp_path),
    ], cwd=root, capture_output=True, text=True, timeout=30)
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "BLOCKED"
    result = json.loads((tmp_path / "result.json").read_text())
    assert json.loads((tmp_path / "task_plan.json").read_text()) == result["task_plan"]
    assert (tmp_path / "product_spec.md").read_text() == result["product_spec"]
