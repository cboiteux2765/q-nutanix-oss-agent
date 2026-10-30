"""Stand-in for the `pi` CLI that speaks pi's documented JSON-mode event stream.

It checks the invocation contract the PiImplementer relies on (flags, isolated config dir, provider
definition, key passed by env reference) and then acts out a scenario chosen by FAKE_PI_SCENARIO:
  median  - fix median() like the pilot scenario, write implementation notes, exit 0
  loop    - keep producing expensive turns until killed (budget / cancel / step-limit tests)
  error   - assistant message ends with stopReason "error", exit 1
Invocation details are written to FAKE_PI_RECORD for assertions.
"""
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from fake_openai import FIXED_STATS, MEDIAN_TESTS, NOTES  # noqa: E402


def emit(event):
    sys.stdout.write(json.dumps(event) + "\n")
    sys.stdout.flush()


def assistant(text, input_tokens=1000, output_tokens=100, stop="toolUse"):
    return {"type": "message_end", "message": {
        "role": "assistant", "content": [{"type": "text", "text": text}], "stopReason": stop,
        "usage": {"input": input_tokens, "output": output_tokens, "cacheRead": 0, "cacheWrite": 0,
                  "totalTokens": input_tokens + output_tokens,
                  "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0}}}}


def tool(name, args, call_id):
    emit({"type": "tool_execution_start", "toolCallId": call_id, "toolName": name, "args": args})
    emit({"type": "tool_execution_end", "toolCallId": call_id, "toolName": name, "result": {}, "isError": False,
          "durationMs": 5})


def main():
    args = sys.argv[1:]
    if args == ["--version"]:
        print("1.4.0-fake")
        return 0
    agent_dir = Path(os.environ["PI_CODING_AGENT_DIR"])
    models = json.loads((agent_dir / "models.json").read_text(encoding="utf-8"))
    system_prompt = Path(args[args.index("--append-system-prompt") + 1]).read_text(encoding="utf-8")
    prompt = sys.stdin.read()
    record = {
        "args": args, "cwd": os.getcwd(), "models": models, "system_prompt": system_prompt, "prompt": prompt,
        "key_present": bool(os.environ.get("EVO_PI_API_KEY")),
        "secret_env": sorted(k for k in os.environ if "TOKEN" in k or k == "OPENAI_API_KEY"),
        "offline": os.environ.get("PI_OFFLINE"), "agent_dir_in_cwd": str(agent_dir).startswith(os.getcwd()),
    }
    Path(os.environ["FAKE_PI_RECORD"]).write_text(json.dumps(record), encoding="utf-8")

    emit({"type": "session", "version": 3, "id": "fake", "timestamp": "2026-10-27T00:00:00Z", "cwd": os.getcwd()})
    emit({"type": "agent_start"})
    scenario = os.environ.get("FAKE_PI_SCENARIO", "median")
    if scenario == "loop":
        n = 0
        while True:
            n += 1
            emit({"type": "turn_start"})
            emit(assistant(f"turn {n}", input_tokens=200_000, output_tokens=10_000))
            tool("bash", {"command": "ls"}, f"c{n}")
            emit({"type": "turn_end", "message": {}, "toolResults": []})
            time.sleep(0.2)
    if scenario == "error":
        emit({"type": "turn_start"})
        emit({"type": "message_end", "message": {"role": "assistant", "content": [], "stopReason": "error",
                                                 "errorMessage": "401 invalid api key", "usage": {}}})
        emit({"type": "agent_end", "messages": [], "willRetry": False})
        return 1

    steps = [
        ("read", {"path": "src/pilot_stats/stats.py"}, None),
        ("write", {"path": "src/pilot_stats/stats.py"}, ("src/pilot_stats/stats.py", FIXED_STATS)),
        ("write", {"path": "tests/test_median.py"}, ("tests/test_median.py", MEDIAN_TESTS)),
        ("bash", {"command": "python -m pytest -q"}, None),
        ("write", {"path": ".evo-artifacts/implementation_notes.json"},
         (".evo-artifacts/implementation_notes.json", NOTES)),
    ]
    for i, (name, tool_args, write) in enumerate(steps, 1):
        emit({"type": "turn_start"})
        emit(assistant(f"step {i}"))
        if write:
            path = Path(write[0])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(write[1], encoding="utf-8", newline="\n")
        tool(name, tool_args, f"c{i}")
        emit({"type": "turn_end", "message": {}, "toolResults": []})
    emit({"type": "turn_start"})
    emit(assistant("Done: median() fixed.", stop="stop"))
    emit({"type": "turn_end", "message": {}, "toolResults": []})
    emit({"type": "agent_end", "messages": [], "willRetry": False})
    emit({"type": "agent_settled", "aborted": False})
    return 0


if __name__ == "__main__":
    sys.exit(main())
