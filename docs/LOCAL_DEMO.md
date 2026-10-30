# Mini-SWE-agent checkpoint: local spec generation

This checkpoint uses mini-SWE-agent 2.4.6's actual agent loop, a custom no-tools
environment, and a local Ollama model adapter. No shell actions, code modification,
implementation tests, or coding-agent dispatch are exposed to the model.

## Test now: recorded demonstration

The project virtual environment is already installed on this machine. To recreate:

```powershell
uv venv --python 3.12 .venv
uv pip install --python .venv/Scripts/python.exe -r requirements-mini.txt
```

From the repository directory:

```powershell
$draftDir = .venv/Scripts/python.exe -m agent.mini_spec examples/evidence.json --allowed-repo example/review-fixture --replay examples/replay.json
if ($LASTEXITCODE -ne 0) { throw 'Generation failed' }
Get-Content "$draftDir/product_spec.md" -Raw -Encoding UTF8
Get-Content "$draftDir/trace.json" -Raw
```

Expected: attempt 1 fails because REQ-1 cites nonexistent TEST-999; attempt 2
supplies TEST-1 and passes. Trace reports `correction_attempts: 1`. The output
directory contains the spec, handoff, context, draft, trace, and agent_trace.json.
This is a recorded synthetic demonstration, not model inference or evidence of
model quality. Its `offline-replay` handoff cannot be approved.

## Real local inference: no OpenAI key

Install [Ollama for Windows](https://docs.ollama.com/windows), then open a new
PowerShell terminal. The installer normally starts the local service. Download
the [Qwen2.5 7B model](https://ollama.com/library/qwen2.5:7b) (about 4.7 GB):

```powershell
ollama pull qwen2.5:7b
ollama list
$draftDir = .venv/Scripts/python.exe -m agent.mini_spec examples/evidence.json --allowed-repo example/review-fixture --model qwen2.5:7b
if ($LASTEXITCODE -ne 0) { throw 'Generation failed' }
Get-Content "$draftDir/product_spec.md" -Raw -Encoding UTF8
Get-Content "$draftDir/trace.json" -Raw
```

This runs real inference on synthetic input. For a real issue, substitute the
evidence JSON path from `agent/evidence.py` and its allowed repository. Choose relevant,
small excerpts: local input is limited to 24 KB per call, including conversation
history; oversized inputs fail explicitly. Each call has a 300-second timeout.
There are at most three model calls: one draft and two structural corrections.
Local model speed and quality depend on hardware and model choice. A smaller
model may be substituted with `--model`, but must already be pulled locally.

If the server is not running, run `ollama serve` in another terminal. The adapter
only calls 127.0.0.1:11434, does not send an API key, and provides no tools. It uses
Ollama's [JSON schema output format](https://docs.ollama.com/api/chat).

## What this checkpoint proves

### Monitor the autonomous loop

Run with full live request/response logging:

```powershell
.venv/Scripts/python.exe -m agent.mini_spec examples/evidence.json --allowed-repo example/review-fixture --replay examples/replay.json --verbose --log runs/live-events.jsonl
```

For actual local inference, omit `--replay examples/replay.json` and add
`--model qwen2.5:7b`. Ollama must be installed and serving that model.
Console output shows stage names and metadata, including HTTP status, endpoint,
latency, model, and token counts. `--verbose` formats the same metadata as JSON;
neither mode logs request or response bodies. Each event is flushed immediately.
A response event appears when the call completes, not token by token.

To watch the same log in a second terminal after the file is created:

```powershell
Get-Content runs/live-events.jsonl -Tail 10 -Wait
```

The file appends across runs; `session_id` separates them. Stages include context
validation, drafting, HTTP calls, structural validation, correction, artifact
writing, and completion. Replay events are labeled `replay.*` and have no HTTP
status because no request occurs. Actual inference uses `api.*`. Full context and
model transcripts remain in artifact files (`context.json`, `agent_trace.json`),
not operational logs. Existing old logs are not rewritten. The agent autonomously
drafts/checks/retries supplied evidence; automatic evidence selection is pending.
The separate coding agent owns code changes and PR creation; this project exports
an approved spec without launching that agent.

Mini runs generation -> structural checks -> bounded correction -> draft artifacts.
It saves transcripts, check failures, timing, and model token counts when available.
Exhausted corrections or connection failures leave a failed-mini trace rather than
publishing a valid-looking handoff. Zero API cost excludes electricity/hardware.
It does not yet verify factual support, guarantee targeted edits, or implement the
updated PRD's complete requirements/handoff schema. Existing IDs remain REQ-N.

The updated DOCX makes the Coding Agent an independent workflow. Subsequent
checkpoints: align product/design fields and acceptance mapping with that PRD;
add factual verification; then improve version-specific review and artifact handoff.
Each major feature stops for a user end-to-end test.

Mini supports [custom model/environment components](https://mini-swe-agent.com/latest/advanced/cookbook/).
Jev's [documented API](https://api.typesafe.ai/redoc) requires credentials and returns
typed decisions, so it is not used to replace the local text generator here.

Tests:

```powershell
.venv/Scripts/python.exe -m unittest -v
```
