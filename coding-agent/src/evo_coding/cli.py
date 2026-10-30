"""`evo-code` command-line interface."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Optional

import typer

from .agent import CodingAgent, RunConflict, RunInProgress, RunNotFound
from .config import CodingConfig, SpendLedger
from .handoff import HandoffSchemaError, load_handoff
from .models import CodingResult, Handoff, RevisionRequest, RunState

app = typer.Typer(add_completion=False, no_args_is_help=True, pretty_exceptions_enable=False,
                  help="EVO Coding Agent: approved spec handoff -> scoped draft PR + work report.")

ConfigOpt = Annotated[Optional[Path], typer.Option("--config", "-c", help="TOML config file")]
WorkDirOpt = Annotated[Optional[Path], typer.Option("--work-dir", help="Override work_dir")]

EXIT_CODES = {RunState.TESTING: 0, RunState.CANCELLED: 4}


def _config(config: Path | None, work_dir: Path | None = None, **overrides) -> CodingConfig:
    cfg = CodingConfig.load(config)
    if work_dir:
        cfg.work_dir = work_dir
    for dotted, value in overrides.items():
        if value is None:
            continue
        section, key = dotted.split("__")
        setattr(getattr(cfg, section), key, value)
    return cfg


def _emit(result: CodingResult, agent: CodingAgent) -> None:
    artifacts = agent.run_root(result.run_id) / f"r{result.revision}" / "artifacts"
    typer.echo(json.dumps({
        "run_id": result.run_id, "revision": result.revision, "status": result.status,
        "next_owner": result.next_owner, "pr_number": result.pr_number, "pr_url": result.pr_url,
        "pr_head_sha": result.pr_head_sha, "blockers": [f"{b.code}: {b.message}" for b in result.blockers],
        "result": str(artifacts / "coding_result.json"), "report": str(artifacts / "work_report.md"),
    }, indent=2, default=str))
    raise typer.Exit(EXIT_CODES.get(result.status, 3))


@app.command()
def validate(handoff: Path, spec: Annotated[Optional[Path], typer.Option(help="product_spec.md path")] = None):
    """Check a handoff against the schema and the Coding Agent's intake rules."""
    try:
        loaded = load_handoff(handoff, spec)
    except HandoffSchemaError as exc:
        typer.echo(json.dumps({"valid": False, "schema_errors": exc.errors}, indent=2))
        raise typer.Exit(2) from exc
    typer.echo(json.dumps({"valid": loaded.ok, "issues": [i.__dict__ for i in loaded.issues]}, indent=2,
                          default=str))
    raise typer.Exit(0 if loaded.ok else 1)


@app.command()
def run(
    handoff: Path,
    config: ConfigOpt = None,
    work_dir: WorkDirOpt = None,
    implementer: Annotated[Optional[str], typer.Option(help="mini_swe | native | pi | rlm | scripted")] = None,
    script: Annotated[Optional[Path], typer.Option(help="Script for the scripted implementer")] = None,
    publisher: Annotated[Optional[str], typer.Option(help="local | github")] = None,
    remote: Annotated[Optional[Path], typer.Option(help="Local publisher remote repository")] = None,
    backend: Annotated[Optional[str], typer.Option(help="local | docker workspace")] = None,
    model: Annotated[Optional[str], typer.Option(help="LLM model name")] = None,
):
    """Implement an approved handoff and open a draft PR."""
    cfg = _config(config, work_dir, implementer__kind=implementer, implementer__script_path=script,
                  publisher__kind=publisher, publisher__local_remote=remote, workspace__backend=backend,
                  llm__model=model)
    agent = CodingAgent(cfg)
    try:
        result = agent.run(handoff)
    except HandoffSchemaError as exc:
        typer.echo(json.dumps({"valid": False, "schema_errors": exc.errors}, indent=2), err=True)
        raise typer.Exit(2) from exc
    except (RunConflict, RunInProgress) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc
    _emit(result, agent)


@app.command()
def revise(
    run_id: str,
    feedback: Annotated[Path, typer.Argument(help="RevisionRequest JSON, or a Test Agent verification.json")],
    config: ConfigOpt = None,
    work_dir: WorkDirOpt = None,
    script: Annotated[Optional[Path], typer.Option(help="Script for the scripted implementer")] = None,
    implementer: Annotated[Optional[str], typer.Option(help="mini_swe | native | pi | rlm | scripted")] = None,
    publisher: Annotated[Optional[str], typer.Option(help="local | github")] = None,
    remote: Annotated[Optional[Path], typer.Option(help="Local publisher remote repository")] = None,
):
    """Push a fix commit to the run's PR in response to CHANGES_REQUIRED feedback."""
    cfg = _config(config, work_dir, implementer__kind=implementer, implementer__script_path=script,
                  publisher__kind=publisher, publisher__local_remote=remote)
    data = json.loads(feedback.read_text(encoding="utf-8"))
    request = (RevisionRequest.from_test_agent_verification(data, run_id) if "readiness" in data
               else RevisionRequest.model_validate(data))
    agent = CodingAgent(cfg)
    try:
        result = agent.revise(run_id, request)
    except (RunNotFound, RunInProgress, ValueError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc
    _emit(result, agent)


@app.command()
def status(run_id: str, config: ConfigOpt = None, work_dir: WorkDirOpt = None):
    """Show every recorded attempt for a run."""
    agent = CodingAgent(_config(config, work_dir))
    results = agent.results(run_id)
    if not results:
        typer.echo(f"no results for run {run_id}", err=True)
        raise typer.Exit(2)
    typer.echo(json.dumps([{"revision": r.revision, "status": r.status, "next_owner": r.next_owner,
                            "pr_number": r.pr_number, "pr_head_sha": r.pr_head_sha,
                            "blockers": [b.code for b in r.blockers], "timestamp": r.timestamp}
                           for r in results], indent=2, default=str))


@app.command()
def spend(config: ConfigOpt = None, work_dir: WorkDirOpt = None):
    """Show total LLM spend and tokens across every attempt in the work dir (from spend_ledger.jsonl)."""
    cfg = _config(config, work_dir)
    ledger = SpendLedger(cfg.work_dir)
    entries = ledger.entries()
    keys = ("usd", "llm_calls", "input_tokens", "cached_input_tokens", "output_tokens", "reasoning_tokens")
    totals = {key: round(sum(e.get(key, 0) for e in entries), 6) for key in keys}
    typer.echo(json.dumps({"ledger": str(ledger.path), "attempts": len(entries), **totals,
                           "lifetime_max_usd": cfg.policy.lifetime_max_usd}, indent=2))


@app.command()
def cancel(run_id: str, config: ConfigOpt = None, work_dir: WorkDirOpt = None,
           reason: str = "cancelled by operator"):
    """Cancel a run: in-flight work stops at the next checkpoint and no new work starts."""
    CodingAgent(_config(config, work_dir)).cancel(run_id, reason)
    typer.echo(f"cancel requested for {run_id}")


@app.command("test-agent-request")
def test_agent_request(result: Path):
    """Print the Test Agent CreateRunRequest for a TESTING coding_result.json."""
    parsed = CodingResult.model_validate_json(result.read_text(encoding="utf-8"))
    if parsed.test_agent_request is None:
        typer.echo(f"result status is {parsed.status}; nothing to hand to the Test Agent", err=True)
        raise typer.Exit(1)
    typer.echo(json.dumps(parsed.test_agent_request, indent=2))


@app.command()
def schema(out: Annotated[Path, typer.Option(help="Directory to write JSON Schemas into")] = Path("schemas")):
    """Export JSON Schemas for the handoff, revision request and coding result."""
    out.mkdir(parents=True, exist_ok=True)
    for name, model in (("handoff", Handoff), ("revision-request", RevisionRequest),
                        ("coding-result", CodingResult)):
        path = out / f"{name}-v0.1.schema.json"
        path.write_text(json.dumps(model.model_json_schema(), indent=2) + "\n", encoding="utf-8")
        typer.echo(str(path))


@app.command("make-handoff")
def make_handoff(
    scenario: Annotated[Path, typer.Argument(help="Pilot scenario file, e.g. pilot/scenarios/01-median-even.toml")],
    repo: Annotated[Path, typer.Option(help="Git checkout whose HEAD becomes source_commit")],
    out: Annotated[Path, typer.Option(help="Output directory")],
    run_id: Annotated[str, typer.Option(help="Unique run identifier")],
    repository: Annotated[str, typer.Option(help="owner/name")] = "evo-pilot/pilot-stats",
    approved_by: Annotated[str, typer.Option(help="Named spec approver")] = "Pilot Spec Reviewer",
):
    """Spec Agent stand-in: build an approved, hash-bound handoff for a pilot scenario."""
    from . import gitops
    from .pilot import build_handoff

    path = build_handoff(scenario, gitops.head_sha(repo), out, run_id, repository, approved_by)
    typer.echo(str(path))


@app.command()
def demo(out: Annotated[Path, typer.Option(help="New directory for demo output")] = Path("demo-output")):
    """Run every pilot scenario offline (scripted implementer, local git remote) and report PASS/FAIL."""
    from .pilot import run_demo

    try:
        outcomes = run_demo(out)
    except FileExistsError:
        typer.echo(f"{out} already has demo output. Delete it or pass a new --out.", err=True)
        raise typer.Exit(2) from None
    for outcome in outcomes:
        statuses = " -> ".join(f"r{r.revision}:{r.status}" for r in outcome.results)
        line = f"{'PASS' if outcome.passed else 'FAIL'} {outcome.name}: {statuses}"
        if outcome.problems:
            line += f"  ({'; '.join(outcome.problems)})"
        typer.echo(line)
    typer.echo(f"output: {out.resolve()}")
    raise typer.Exit(0 if all(o.passed for o in outcomes) else 1)


if __name__ == "__main__":  # pragma: no cover
    app()
