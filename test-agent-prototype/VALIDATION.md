# Prototype validation

Validated on October 22, 2026 with Windows, Python 3.12, Git, and the dependency versions in `requirements.lock.txt`.

## Results

| Check | Result |
| --- | --- |
| API, authorization, webhook and real integration tests | 21 passed |
| RCA, patch policy and evolution memory tests | 42 passed |
| Real pytest runner, preflight and process cancellation tests | 18 passed |
| Budget, async proposal cancellation, QA and stale-result tests | 14 passed |
| Complete pytest suite | **95 passed** |
| Five executable acceptance scenarios | **5 passed** |
| Dependency consistency (`pip check`) | Passed |
| Local API seeding and OpenAPI generation | Passed |

The five scenarios execute real local Git and pytest operations. See `example-results/acceptance_summary.json` and each scenario's run directory for validated verification JSON, reports, JUnit, logs and checksummed manifests. Recreate the complete fixture repositories and source snapshots by running `python demo_acceptance.py`; the package includes evidence rather than the temporary Git repositories or virtual environment.

## Reproduce

From the project directory, install `requirements.lock.txt` in a local virtual environment, then run:

```text
python -m pytest -q
python demo_acceptance.py
```

In a restricted Windows execution environment, asyncio may require permission to use local loopback. This is an execution-host restriction; the offline demo performs no external network calls. The final suite emitted one non-failing Starlette/httpx deprecation warning.

## What these checks establish

The tests cover deterministic observations, no automatic approval of candidate branches, version-bound named QA decisions, hash/ACL checks for evidence, persistence recovery, immutable inputs, bounded attempts and invocation reservations, missing prerequisites, spec conflicts, cancellation of spawned test processes and model proposals, invalid patch paths, secret redaction, and stale invalidation.

No paid model calls, real GitHub write actions, cloud probes, GEPA optimization, SKILL.md modification, merge or deployment were performed. Docker execution is implemented but was not integration tested. Local fixtures are trusted; host subprocess mode is not containment for arbitrary code. See README for the local-mirror integration boundary and production extensions.
