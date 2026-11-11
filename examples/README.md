# Runnable example

After installing the project dependencies, run from the repository root:

```bash
python -m spec_agent examples/request.json --demo --output examples/output
```

This creates `product_spec.md`, `task_plan.json`, and `result.json` (including
execution traces). The fake provider deliberately returns `BLOCKED`, with a
critical `demo_only` issue stating that it cannot validate semantics. For a real
specification, inject an actual provider adapter through the Python interface.

Generated artifacts are not checked in: the implementation environment lacked
Pydantic and pytest, and network dependency installation failed. No end-to-end
execution or generated output is claimed. `tests/test_cli.py` checks output
generation and consistency once dependencies are installed.
