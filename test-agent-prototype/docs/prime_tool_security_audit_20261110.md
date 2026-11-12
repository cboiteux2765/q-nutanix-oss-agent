# Prime Test-stage tool security audit — 2026-11-10

Historical snapshot: the November 12 tool-first migration removed standalone
HTTP intake, webhook scheduling and legacy adapters. Line numbers and test
counts below describe the pre-migration code. Its caller-authentication and
live Prime Host limitations still apply; see [current validation](../VALIDATION.md).

This audit executed the repository commands and inspected the current files.
`python -m pytest -v` emitted **203 passed in 187.02s**, with no warning or
error summary under the repository's pytest configuration. The original 95
tests, subsequent benchmark/evolver/audit tests and 36 Prime-tool tests are
included in that collected suite. `python demo_acceptance.py` printed five
PASS lines. An independent manifest walk found **8 bound manifests and 57
matching artifact hashes** across those scenarios.

| Requested deliverable | Implementation | Exact verification | Observed terminal result | Audit status |
| --- | --- | --- | --- | --- |
| 1. Preflight, verify and evidence tools | [`prime_mcp.py`](../app/prime_mcp.py) lines 13, 19, 24, 29, 34; [`prime_tools.py`](../app/prime_tools.py) lines 242, 261, 309, 318 | `test_preflight_and_verify_real_registered_pr_with_bound_evidence`; `test_prime_mcp_exposes_only_four_scoped_tools`; `test_stdio_mcp_process_uses_operator_policy_not_model_identity` in [`test_prime_tools.py`](../tests/test_prime_tools.py) | `python -m pytest -v` → `203 passed`; direct REPL-style smoke → `preflight -> verify -> manifest -> artifact`, `READY_FOR_QA`, 2 tests passed | **PASS** for MCP protocol and registered local fixture |
| 2. Strict pinned input/output contracts | [`prime_tools.py`](../app/prime_tools.py) lines 53, 76, 88, 110, 182, 189; [exported schemas](../schemas/prime-test-tools-v1.json) | `test_stale_or_mismatched_handoff_is_rejected_before_run`; `test_head_change_between_handoff_and_execution_is_rejected`; `test_checked_in_contracts_and_permissions_match_runtime_models` | Boundary command → `PASS: wrong head SHA, spec hash and environment ID rejected before pytest spawn` | **PASS** for exercised fields and snapshots |
| 3. Permissions and caller identity | [`prime_tools.py`](../app/prime_tools.py) lines 31, 198, 216, 221, 253, 338; [permissions manifest](../schemas/prime-test-permissions-v1.json) | `test_model_cannot_supply_permissions_commands_or_approval`; `test_missing_or_excessive_operator_identity_fails_closed`; `test_repository_scope_and_backend_are_enforced_before_execution`; `test_artifacts_are_allowlisted_and_paths_cannot_escape`; `test_manifest_named_artifact_cannot_be_replaced_by_link`; `test_patch_policy_rejects_protected_and_unsafe_paths_even_if_allowlisted` | Boundary command → role/approval, version, escape and protected-patch checks `PASS`; static `rg` → `NO_ORCHESTRATION_OR_UNRESTRICTED_SHELL_CALLS`; same command → `FAIL: a forged Principal matching static policy is accepted in-process` | **FAIL for host-verified caller identity**; other exercised restrictions pass |
| 4. Prime REPL integration example | [`prime_test_tools.md`](prime_test_tools.md) and executable [`prime_repl_example.py`](../scripts/prime_repl_example.py) lines 20, 27 | Direct `python -m scripts.prime_repl_example`; `test_stdio_mcp_process_uses_operator_policy_not_model_identity` | `REPL-style stdio MCP flow: PASS`; persistent `target`, `check`, `run`, `evidence`, `artifact` variables; `Live Prime Agent host session: NOT TESTED` | **PARTIAL**: real stdio and Python REPL semantics tested; no installed Prime Host |
| 5. Regression and acceptance verification | [`test_prime_tools.py`](../tests/test_prime_tools.py), [`test_repair_memory.py`](../tests/test_repair_memory.py), [`demo_acceptance.py`](../demo_acceptance.py) | Full `pytest -v`, focused protected-path test, acceptance demo and [`audit_acceptance_manifests.py`](../scripts/audit_acceptance_manifests.py) | `203 passed`; focused `19 passed`; acceptance `5 PASS`; `8 bound manifests; 57 verified artifacts` | **PASS** for offline/local scope |

The adversarial command was `python -m scripts.audit_prime_boundaries`.
It prints individual PASS results but exits nonzero because the current MCP
server binds a static operator identity rather than verifying a signed or
Host-issued identity per call. Rejecting model-supplied `role="qa"` or
`approved=True` is therefore necessary but insufficient to establish caller
authentication. The stdio child process also normally inherits the Prime
kernel's OS user unless an operator deploys a separate protected identity;
the permission JSON alone is not an OS security boundary.

The documented Prime import was corrected to `from rlm import mcp`; plain
`import mcp` refers to the MCP SDK and is not the Prime kernel-side client.
The standalone example uses a client façade over a real stdio server to run
the same calls as top-level-await cells. No `prime-agent` executable was found
on this machine, so an actual Harness-managed session and its host identity
cannot be certified here. The existing Docker backend and live GitHub
registration were also outside this offline audit.

To close the two gaps, provide a Prime Host environment with an authenticated
caller assertion that the tool process can verify, protect the process/policy
with OS-level isolation, and rerun the documented flow in a real Prime REPL.
