"""Record live model replies so every Spec Lab part can be demoed offline.

    uv run python -m evals.record --model qwen2.5:7b
    uv run python -m evals.record --backend aicommons --model claude-v5.5-haiku --case bundled-fixture --case undecided-whitespace

1. Every frozen-benchmark case through every harness with the base prompt. The graded rows
   are also saved to evals/results/ as that model's harness comparison.
2. The governed-evolution demo: the first case whose Prime-RLM cycle produced a candidate
   prompt becomes the demo cycle, and the mini-swe-agent harness is recorded on every case
   with that candidate prompt, so the Gate 5 benchmark replay can run offline.

Replays (Spec Lab "Recorded" mode, or run_case(mode="recorded")) feed these replies to the
real harness loop, tools and validators. Nothing is synthesized: a reply that was not
recorded makes the replay stop with a NotRecorded error.
"""
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
import time

from agent.backends import RECORDINGS, Recording
from agent.learning import BASE_PROMPT
from evals.cases import CASES
from evals.run_evals import HARNESSES, budget_left, make_chat, run_budgeted

CYCLE_CASES = ["bundled-fixture", "resolved-decision", "prompt-injection", "distractor-evidence"]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--backend", choices=["ollama", "aicommons"], default="ollama")
    parser.add_argument("--model", default="qwen2.5:7b")
    parser.add_argument("--case", action="append", choices=[c["id"] for c in CASES],
                        help="Record only these cases (default: all). Fewer cases spend fewer tokens.")
    parser.add_argument("--max-tokens", type=int, default=None,
                        help="Hard cap for this command (default: 300,000 on AI Commons, none on Ollama)")
    parser.add_argument("--run-tokens", type=int, default=40_000, help="Hard cap per case and harness")
    args = parser.parse_args()
    cases = [c for c in CASES if not args.case or c["id"] in args.case]
    cap = args.max_tokens or (300_000 if args.backend == "aicommons" else None)
    chat = make_chat(args.backend, args.model, cap)
    label = chat.label.replace(":", "-").replace("/", "-")
    recording = Recording(RECORDINGS / f"spec-lab-{label}.json")
    recording.data = {"model": chat.label, "recorded_at": datetime.now(timezone.utc).isoformat(),
                      "guidance": BASE_PROMPT, "streams": {}, "demo_cycle": None}

    def record(case, harness, guidance):
        if not budget_left(chat):
            print(f"Token budget used up; skipping {harness} on {case['id']}.", flush=True)
            return None
        print(f"[{harness}] {case['id']} ...", flush=True)
        row = run_budgeted(case, harness, chat, guidance, args.run_tokens, mode="record", recording=recording)
        recording.save()
        print(f"    -> {row['exit_status']} score={row['score']} calls={row['model_calls']} "
              f"tokens={row.get('input_tokens')}/{row.get('output_tokens')} t={row.get('elapsed_seconds')}s "
              f"{row.get('error') or ''}", flush=True)
        return row

    rows = [row for case in cases for harness in HARNESSES if (row := record(case, harness, BASE_PROMPT))]
    results = Path("evals/results") / f"results-{label}-{time.strftime('%Y%m%d-%H%M%S')}-recorded.json"
    results.write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Saved {results}", flush=True)

    cycle = next((r for c in CYCLE_CASES for r in rows
                  if r["case"] == c and r["harness"] == "prime-rlm" and r.get("candidate_prompt")), None)
    if cycle is None:
        print("No Prime-RLM cycle produced a candidate prompt; the governed demo needs a live model.")
        return
    recording.data["demo_cycle"] = {"case": cycle["case"], "harness": "prime-rlm",
                                    "candidate_prompt": cycle["candidate_prompt"]}
    print(f"Demo cycle: {cycle['case']}. Recording the Gate 5 candidate arm ...", flush=True)
    for case in cases:
        record(case, "mini", cycle["candidate_prompt"])
    recording.save()
    used = chat.state["used"] if getattr(chat, "state", None) else "?"
    print(f"Saved {recording.path} ({used} tokens used)")


if __name__ == "__main__":
    main()
