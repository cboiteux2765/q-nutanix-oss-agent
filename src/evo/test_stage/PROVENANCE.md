# Test-stage verification core

Imported unchanged from `test-agent-prototype/app` on branch `shanqin`, commit
`b20f9f331aacbe65a834b8905163ef207e1bec09` of this repository. Relative imports
allow the same core to run under the `evo.test_stage` namespace without a global
`app` package or a second checkout dependency. Keep fixes synchronized upstream.

Only the verification dependency closure is included. The legacy HTTP service,
MCP transport, benchmarks and optional evolver are not started by the main agent.
The host adapter is [test_stage_bridge.py](../test_stage_bridge.py).

The [acceptance harness](../../../scripts/test_stage_acceptance.py) comes from
the same commit's `demo_acceptance.py`, with only package imports and the default
output directory adapted. Source line endings are normalized in this checkout.
