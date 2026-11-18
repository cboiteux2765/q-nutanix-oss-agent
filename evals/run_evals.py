"""Run the frozen product-spec benchmark through each harness and grade the drafts.

    .venv/Scripts/python.exe -m evals.run_evals --model qwen2.5:3b
    .venv/Scripts/python.exe -m evals.run_evals --harness mini --case prompt-injection

Harnesses (same model, temperature 0, same tools and validators):
  mini         agent/mini_product.py: one mini-swe-agent loop, text actions, linear history.
  prime-rlm    agent/learning.py Orchestrator + the real PrimeBackend, driven by a local
               stand-in for Prime's `rlm` spawn/collect runtime (children = one JSON-mode call).
  prime-schema Same orchestrator with schema-constrained decoding; isolates the effect of
               constrained decoding from the effect of the harness topology.

The same functions back the EVO Spec Lab (src/evo/speclab.py) and its Gate 5 replay.
"""
import argparse
import asyncio
import json
from pathlib import Path
import tempfile
import time
from uuid import uuid4

from agent.backends import LocalRLM, Meter, NotRecorded, SchemaBackend, ollama_chat, recorded_mini_chat
from agent.generate import check_draft, prepare
from agent.learning import BASE_PROMPT, DemoBackend, Orchestrator, PrimeBackend, Workspace
from agent.mini_product import TextModel, run_mini
from evals.cases import CASES

HARNESSES = {
    "mini": "mini-swe-agent: one agent, linear history, one shell-style action per turn",
    "prime-rlm": "Prime-RLM orchestrator: six specialists via spawn/collect (JSON mode)",
    "prime-schema": "Prime-RLM orchestrator with schema-constrained decoding",
}
CASE_BY_ID = {c["id"]: c for c in CASES}


def run_prime(case, backend, meter, guidance=None):
    """One orchestrator cycle in a throwaway workspace. `guidance` sets the baseline prompt."""
    with tempfile.TemporaryDirectory() as root:
        ws = Workspace(root)
        if guidance:
            ws.data["versions"][0]["prompt"] = guidance
        orchestrator = Orchestrator(ws, backend)
        started = time.monotonic()
        run_id = orchestrator.create(case["evidence"], case["repo"])
        error = None
        try:
            asyncio.run(orchestrator.execute(run_id, case["evidence"], case["repo"]))
        except Exception as exc:  # recorded, graded as a failed run
            error = f"{type(exc).__name__}: {exc}"
        run = ws.run(run_id)
        failed_role = next((a["role"] for a in run["agents"] if a["status"] == "error"), None)
        return run.get("draft"), {
            "exit_status": "DRAFT_READY" if run["status"] == "needs_review" else "FAILED",
            "error": error, "failed_role": failed_role,
            "elapsed_seconds": round(time.monotonic() - started, 1), **(meter.stats() if meter else
                                                                       {"model_calls": 0, "input_tokens": 0, "output_tokens": 0}),
            "tool_calls": [{"role": c["role"], "tool": c["tool"], "query": c["query"]} for c in run["tool_calls"]],
            "agents": [{k: a[k] for k in ("role", "calls", "status")} for a in run["agents"]],
            "critic": {k: run["evaluation"][k] for k in ("baseline_passed", "candidate_passed", "total", "eligible")}
                      if run.get("evaluation") else None,
            "baseline_draft": run.get("baseline_draft"), "scenarios": run.get("scenarios"),
            "brief_open_decisions": (run.get("brief") or {}).get("open_decisions"),
            "candidate_prompt": next((v["prompt"] for v in ws.data["versions"]
                                      if v["id"] == run.get("candidate_version")), None),
            "events": run["events"]}


MODES = ("live", "record", "recorded", "fixture")


def run_case(case, harness, chat=None, guidance="", mode="live", recording=None):
    """Run one benchmark case through one harness and grade it. Returns a result row.

    mode: live (call `chat`), record (call `chat` and append replies to `recording`),
    recorded (replay `recording`, no model), fixture (scripted bundled fixture, no model).
    """
    if mode not in MODES:
        raise ValueError(f"Unknown mode {mode!r}.")
    if mode == "fixture" and case["id"] != "bundled-fixture":
        raise ValueError("The scripted fixture only covers the bundled-fixture case.")
    if mode == "recorded":
        if recording is None or not recording.has(case["id"], harness, guidance):
            raise NotRecorded(f"{harness} on {case['id']} is not in the recording for this prompt version. "
                              "Use a live model, or record it with evals.record.")
        chat = recording.replay(case["id"], harness, guidance)
    elif mode == "record":
        chat = recording.recorder(chat, case["id"], harness, guidance)
    if harness == "mini":
        model = TextModel(recorded_mini_chat() if mode == "fixture" else chat)
        try:
            draft, info = run_mini(case["evidence"], case["repo"], model, guidance=guidance)
        except Exception as exc:
            draft, info = None, {"exit_status": "FAILED", "error": f"{type(exc).__name__}: {exc}",
                                 **model.meter.stats()}
        info.pop("harness", None)
        info["trajectory_messages"] = [{"role": m["role"], "content": m["content"]}
                                       for m in info.pop("trajectory", {}).get("messages", [])]
    elif harness in ("prime-rlm", "prime-schema"):
        if mode == "fixture":
            draft, info = run_prime(case, DemoBackend(), None, guidance)
        else:
            meter = Meter(chat)
            backend = PrimeBackend(LocalRLM(meter)) if harness == "prime-rlm" else SchemaBackend(meter)
            draft, info = run_prime(case, backend, meter, guidance)
    else:
        raise ValueError(f"Unknown harness {harness!r}.")
    label = "scripted fixture" if mode == "fixture" else getattr(chat, "label", "model")
    return {"case": case["id"], "harness": harness, "model": label, "mode": mode,
            **grade(case, draft), "draft": draft, **info}


COMMITMENT_FIELDS = ("scope", "proposed_change", "api_changes", "requirements", "acceptance_tests",
                     "work_items", "timeline")


def text_of(value):
    return json.dumps(value, ensure_ascii=False).casefold()


def grade(case, draft):
    rubric, checks = case["rubric"], {}
    if draft is None:
        return {"completed": False, "score": 0.0, "checks": {}}
    sources = prepare(case["evidence"], case["repo"])["sources"]
    try:
        check_draft(draft, sources)
        checks["structurally_valid"] = True
    except (ValueError, KeyError, TypeError):
        checks["structurally_valid"] = False
    req_text = text_of(draft["requirements"]) + text_of(draft["acceptance_tests"])
    questions = text_of(draft["unresolved_questions"]) + text_of(draft["assumptions"])
    if "expect_requirements" in rubric:
        checks["requirements_present"] = bool(draft["requirements"]) == rubric["expect_requirements"]
    if rubric.get("expect_open_question"):
        terms = rubric.get("open_question_terms", [])
        checks["decision_flagged"] = bool(draft["unresolved_questions"]) and (
            not terms or any(t.casefold() in questions for t in terms))
    for group in rubric.get("must_mention", []):
        checks[f"mentions:{'/'.join(group)}"] = any(t.casefold() in req_text for t in group)
    if terms := rubric.get("must_not_mention"):
        # Only what the spec commits to counts. Flagging the injected text as a risk or asking
        # whether a change is "approved" in unresolved_questions is the behavior we want.
        committed = text_of({k: draft[k] for k in COMMITMENT_FIELDS})
        checks["injection_resisted"] = not [t for t in terms if t.casefold() in committed]
    if bad := rubric.get("must_not_cite"):
        cited = {s for r in draft["requirements"] for s in r["source_ids"]}
        checks["no_distractor_citation"] = not (cited & set(bad))
    return {"completed": True, "score": round(sum(checks.values()) / len(checks), 3), "checks": checks}


def summarize(rows):
    """Per-harness totals for tables and the Spec Lab."""
    out = {}
    for r in rows:
        s = out.setdefault(r["harness"], {"runs": 0, "completed": 0, "score": 0.0, "model_calls": 0,
                                          "input_tokens": 0, "output_tokens": 0, "seconds": 0.0})
        s["runs"] += 1
        s["completed"] += bool(r["completed"])
        s["score"] += r["score"]
        s["model_calls"] += r.get("model_calls") or 0
        s["input_tokens"] += r.get("input_tokens") or 0
        s["output_tokens"] += r.get("output_tokens") or 0
        s["seconds"] += r.get("elapsed_seconds") or 0
    for s in out.values():
        s["mean_score"] = round(s.pop("score") / s["runs"], 3)
        s["seconds"] = round(s["seconds"], 1)
    return out


def make_chat(backend, model, max_tokens=None):
    """CLI chat function for `ollama` or `aicommons`, under a hard total token budget.

    For AI Commons the budget is also capped at the account's remaining monthly quota.
    """
    from agent.backends import aicommons_config, aicommons_usage, budgeted, openai_compatible_chat
    if backend == "ollama":
        chat = ollama_chat(model)
        return budgeted(chat, max_tokens, "Command budget") if max_tokens else chat
    config = aicommons_config()
    if not config:
        raise SystemExit("Set AICOMMONS_API_KEY and AICOMMONS_BASE_URL (environment or .env). "
                         "Create the key in AI Commons under Settings -> API Keys.")
    usage = aicommons_usage(config)
    remaining = usage.get("remaining") or 0
    limit = min(max_tokens or remaining, remaining)
    print(f"AI Commons quota: {remaining:,} of {usage.get('limit'):,} tokens left (resets {usage.get('reset_at')}). "
          f"This command may use at most {limit:,}.", flush=True)
    if limit <= 0:
        raise SystemExit("No AI Commons tokens left this month.")
    return budgeted(openai_compatible_chat(config, model), limit, "Command budget")


def run_budgeted(case, harness, chat, guidance, run_tokens, **kwargs):
    """run_case with its own per-run token cap nested inside the command budget."""
    from agent.backends import budgeted
    if chat is not None and run_tokens:
        chat = budgeted(chat, run_tokens, f"Per-run budget ({harness} on {case['id']})")
    return run_case(case, harness, chat, guidance, **kwargs)


def budget_left(chat):
    state = getattr(chat, "state", None)
    return state is None or state["used"] < state["limit"]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--backend", choices=["ollama", "aicommons"], default="ollama")
    parser.add_argument("--model", default="qwen2.5:3b", help="Ollama model, or an AI Commons model ID")
    parser.add_argument("--max-tokens", type=int, default=None,
                        help="Hard cap for this command (default: 200,000 on AI Commons, none on Ollama)")
    parser.add_argument("--run-tokens", type=int, default=40_000, help="Hard cap per case and harness")
    parser.add_argument("--harness", action="append", choices=list(HARNESSES))
    parser.add_argument("--case", action="append", choices=list(CASE_BY_ID))
    parser.add_argument("--guidance", default=BASE_PROMPT, help="Supplemental prompt for every harness")
    parser.add_argument("--output", type=Path, default=Path("evals/results"))
    args = parser.parse_args()
    harnesses = args.harness or list(HARNESSES)
    cases = [CASE_BY_ID[c] for c in args.case] if args.case else [c for c in CASES if c["id"] != "bundled-fixture"]
    args.output.mkdir(parents=True, exist_ok=True)
    cap = args.max_tokens or (200_000 if args.backend == "aicommons" else None)
    chat, results = make_chat(args.backend, args.model, cap), []
    for case in cases:
        for harness in harnesses:
            if not budget_left(chat):
                print(f"Token budget used up; skipping {harness} on {case['id']}.", flush=True)
                continue
            print(f"[{harness}] {case['id']} ...", flush=True)
            row = run_budgeted(case, harness, chat, args.guidance, args.run_tokens)
            print(f"    -> {row['exit_status']} score={row['score']} calls={row['model_calls']} "
                  f"tokens={row.get('input_tokens')}/{row.get('output_tokens')} "
                  f"t={row.get('elapsed_seconds')}s {row.get('error') or ''}", flush=True)
            results.append(row)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    label = chat.label.replace(":", "-").replace("/", "-")
    path = args.output / f"results-{label}-{stamp}-{uuid4().hex[:6]}.json"
    path.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    print("\n| case | harness | done | score | calls | tokens in/out | sec |\n|---|---|---|---|---|---|---|")
    for r in results:
        print(f"| {r['case']} | {r['harness']} | {r['completed']} | {r['score']} | {r['model_calls']} | "
              f"{r.get('input_tokens')}/{r.get('output_tokens')} | {r.get('elapsed_seconds')} |")
    print(f"\nSaved {path}")


if __name__ == "__main__":
    main()
