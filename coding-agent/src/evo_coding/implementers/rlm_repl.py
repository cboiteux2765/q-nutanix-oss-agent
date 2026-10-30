"""Persistent REPL server for the RLM implementer. Runs INSIDE the workspace (host or container).

Standard library only. It never sees API keys: llm_query() is proxied to the host over stdin/stdout
as JSON lines, and the host enforces budgets on every sub-call.

    host -> repl: {"type": "exec", "code": "..."}
    repl -> host: {"type": "llm_query", "prompts": [...]}      (zero or more, during exec)
    host -> repl: {"type": "llm_result", "texts": [...]} | {"type": "llm_result", "error": "..."}
    repl -> host: {"type": "result", "output": "...", "submitted": bool}
"""
import contextlib
import io
import json
import os
import subprocess
import sys
import traceback

PROTO_IN, PROTO_OUT = sys.stdin, sys.stdout
for stream in (PROTO_IN, PROTO_OUT):
    stream.reconfigure(encoding="utf-8", errors="replace")
BASH = os.environ.get("EVO_BASH") or "bash"
MAX_OUTPUT = int(os.environ.get("EVO_RLM_MAX_OUTPUT", "20000"))
NOTES_PATH = ".evo-artifacts/implementation_notes.json"
SKIP_DIRS = {".git", ".evo-artifacts", "__pycache__", ".pytest_cache", ".venv", "node_modules"}
STATE = {"submitted": False}


def _send(message):
    PROTO_OUT.write(json.dumps(message) + "\n")
    PROTO_OUT.flush()


def _recv():
    line = PROTO_IN.readline()
    if not line:
        sys.exit(0)
    return json.loads(line)


def llm_query_batched(prompts):
    _send({"type": "llm_query", "prompts": [str(p) for p in prompts]})
    reply = _recv()
    if reply.get("error"):
        raise RuntimeError(f"llm_query failed: {reply['error']}")
    return reply["texts"]


def llm_query(prompt):
    return llm_query_batched([prompt])[0]


def run(command, timeout=120):
    try:
        proc = subprocess.run([BASH, "-c", command], capture_output=True, text=True, timeout=timeout,
                              encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return f"[timeout after {timeout}s]"
    output = (proc.stdout or "") + (proc.stderr or "")
    return f"[exit {proc.returncode}]\n{output[-MAX_OUTPUT:]}"


def read_file(path):
    with open(path, encoding="utf-8", errors="replace") as handle:
        return handle.read()


def write_file(path, content):
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(content)


def list_files():
    found = []
    for root, dirs, files in os.walk("."):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        found += [os.path.relpath(os.path.join(root, f), ".").replace(os.sep, "/") for f in sorted(files)]
    return found


def submit(summary, criteria=None):
    write_file(NOTES_PATH, json.dumps({"summary": str(summary), "criteria": criteria or {}}, indent=2))
    STATE["submitted"] = True
    print("submitted")


def main():
    with open(".evo-artifacts/rlm_context.json", encoding="utf-8") as handle:
        context = json.load(handle)
    context["files"] = list_files()
    namespace = {"__name__": "__repl__", "context": context, "llm_query": llm_query,
                 "llm_query_batched": llm_query_batched, "run": run, "read_file": read_file,
                 "write_file": write_file, "list_files": list_files, "submit": submit}
    _send({"type": "ready"})
    while True:
        message = _recv()
        if message.get("type") != "exec":
            continue
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
            try:
                exec(compile(message["code"], "<repl>", "exec"), namespace)  # noqa: S102 - this is the sandbox
            except SystemExit:
                print("SystemExit is not allowed in the REPL")
            except BaseException:  # noqa: BLE001 - report everything to the model
                traceback.print_exc()
        output = buffer.getvalue()
        if len(output) > MAX_OUTPUT:
            output = output[: MAX_OUTPUT // 2] + f"\n... [{len(output) - MAX_OUTPUT} chars truncated] ...\n" \
                + output[-MAX_OUTPUT // 2:]
        _send({"type": "result", "output": output, "submitted": STATE["submitted"]})


if __name__ == "__main__":
    main()
