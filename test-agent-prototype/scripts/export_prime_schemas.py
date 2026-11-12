"""Export the checked-in Prime Test-stage JSON Schema bundle."""
from __future__ import annotations

import json
from pathlib import Path

from app.prime_tools import (ArtifactOutput, ManifestOutput, PreflightOutput,
                             StageTarget, ToolPolicy, VerifyInput, VerifyOutput)


def contract() -> dict:
    run_id = {"type": "string", "pattern": r"^run-[a-f0-9]{32}$"}
    return {
        "schema_version": "1.0",
        "operator_policy": ToolPolicy.model_json_schema(),
        "tools": {
            "test_preflight": {"input": StageTarget.model_json_schema(),
                               "output": PreflightOutput.model_json_schema()},
            "test_verify": {"input": VerifyInput.model_json_schema(),
                            "output": VerifyOutput.model_json_schema()},
            "test_manifest": {"input": {"type": "object", "additionalProperties": False,
                "required": ["run_id"], "properties": {"run_id": run_id}},
                "output": ManifestOutput.model_json_schema()},
            "test_artifact": {"input": {"type": "object", "additionalProperties": False,
                "required": ["run_id", "name"], "properties": {"run_id": run_id,
                    "name": {"type": "string", "minLength": 1, "maxLength": 500}}},
                "output": ArtifactOutput.model_json_schema()},
        },
    }


if __name__ == "__main__":
    output = Path(__file__).resolve().parents[1] / "schemas/prime-test-tools-v1.json"
    output.write_text(json.dumps(contract(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(output)
