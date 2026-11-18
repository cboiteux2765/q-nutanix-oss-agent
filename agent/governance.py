"""Governed self-evolution gates for product-spec prompt versions (docs/GOVERNED_EVOLUTION.md).

The immutable core (base instructions, evidence rules, schema, validators, tool allowlist,
budgets) lives in code and changes only by pull request. What evolves is the supplemental
prompt stored as versions in `learning.Workspace`. A candidate is promoted only when every
gate below passes and a named human approves it. All gates are deterministic: none asks a
model whether the change is safe.
"""
import difflib
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re

from agent.learning import BASE_PROMPT, digest

LESSON_MAX_CHARS = 300
MAX_LESSONS = 8
MAX_PROMOTIONS_PER_DAY = 3
VERSION_KEYS = {"id", "parent", "prompt", "rationale", "hash", "status", "created_at", "run_id", "mode"}
# Text that would weaken a core rule if it reached the prompt.
BANNED = [r"skip (the )?citations?", r"\bno (need for )?citations?\b", r"auto-?approve", r"approve (it|this|the spec)",
          r"ignore (the )?(validators?|evidence|rules|instructions)", r"\bassume\b", r"\binvent\b",
          r"(resolve|close|drop|remove) (all |any )?(open|unresolved) (decisions|questions)",
          r"mark (it|this|the spec) (as )?approved", r"run (shell|code|commands)", r"spawn"]
GATES = [
    ("scope", "Scope", "Only the supplemental prompt changes; core rules, schema and validators are code."),
    ("lint", "Policy lint", "No added text that would weaken core rules (citations, validators, approval, open decisions)."),
    ("size_rate", "Size and rate", f"Each lesson ≤ {LESSON_MAX_CHARS} chars, ≤ {MAX_LESSONS} lessons, ≤ {MAX_PROMOTIONS_PER_DAY} promotions per day."),
    ("provenance", "Provenance", "The candidate names the run and rationale (and feedback) that justify it."),
    ("critic", "Scenario check", "The independent critic passed every generated scenario with no regressions."),
    ("benchmark", "Frozen benchmark replay", "Same pinned snapshots, current vs candidate prompt: zero regressions; must-stay-blocked cases stay blocked."),
    ("approval", "Human approval", "Lesson tier: one named reviewer approves. New roles or tools would need two (not available here)."),
]


def lessons(prompt, base=BASE_PROMPT):
    """Lines a prompt adds on top of the built-in base prompt (each one is a 'lesson')."""
    return [line.strip() for line in prompt.splitlines() if line.strip() and line.strip() not in base]


def added_lines(candidate, baseline):
    diff = difflib.ndiff(baseline.splitlines(), candidate.splitlines())
    return [line[2:].strip() for line in diff if line.startswith("+ ") and line[2:].strip()]


def check_gates(ws, run_id, reviewer="", now=None):
    """Evaluate every gate for a run's candidate version. Returns [{id, name, rule, passed, detail}]."""
    now = now or datetime.now(timezone.utc)
    run = ws.run(run_id)
    versions = {v["id"]: v for v in ws.data["versions"]}
    candidate = versions.get(run.get("candidate_version"))
    baseline = versions.get(run.get("baseline_version"))
    results = {}
    if not candidate or not baseline:
        return [dict(id=g, name=n, rule=r, passed=False, detail="The run did not produce a candidate.")
                for g, n, r in GATES]
    added = added_lines(candidate["prompt"], baseline["prompt"])

    extra = set(candidate) - VERSION_KEYS
    results["scope"] = (not extra and candidate["hash"] == digest(candidate["prompt"]),
                        f"Unexpected fields {sorted(extra)}." if extra else
                        "Prompt changed after evaluation." if candidate["hash"] != digest(candidate["prompt"]) else
                        "Only the supplemental prompt differs.")

    hits = sorted({line for line in added for pattern in BANNED if re.search(pattern, line, re.I)})
    results["lint"] = (not hits, f"Rejected text: {hits[0][:120]!r}" if hits else f"{len(added)} added line(s) checked.")

    long = [line for line in added if len(line) > LESSON_MAX_CHARS]
    count = len(lessons(candidate["prompt"]))
    since = now - timedelta(days=1)
    recent = [a for a in ws.data["audit"] if a.get("action") == "promote"
              and datetime.fromisoformat(a["at"]) > since]
    results["size_rate"] = (not long and count <= MAX_LESSONS and len(recent) < MAX_PROMOTIONS_PER_DAY,
                            f"{len(long)} lesson(s) over {LESSON_MAX_CHARS} chars." if long else
                            f"{count} lessons (max {MAX_LESSONS})." if count > MAX_LESSONS else
                            f"{len(recent)} promotions in the last 24 h (max {MAX_PROMOTIONS_PER_DAY})."
                            if len(recent) >= MAX_PROMOTIONS_PER_DAY else
                            f"{count} lesson(s), longest added line {max(map(len, added), default=0)} chars, "
                            f"{len(recent)} promotion(s) in 24 h.")

    feedback = [f["id"] for f in ws.data["feedback"] if f["run_id"] == run_id]
    results["provenance"] = (bool(candidate.get("run_id") == run_id and candidate.get("rationale", "").strip()),
                             f"Run {run_id[:8]}, rationale given, {len(feedback)} feedback record(s) cited."
                             if candidate.get("rationale", "").strip() else "Missing rationale.")

    evaluation = run.get("evaluation") or {}
    results["critic"] = (bool(evaluation.get("eligible")),
                         f"Critic: candidate {evaluation.get('candidate_passed')}/{evaluation.get('total')}, "
                         f"baseline {evaluation.get('baseline_passed')}/{evaluation.get('total')}, "
                         f"regressions {evaluation.get('regressions') or 'none'}." if evaluation else "No evaluation.")

    replay = run.get("benchmark_replay")
    if not replay or replay.get("candidate_version") != candidate["id"] or replay.get("baseline_version") != baseline["id"]:
        results["benchmark"] = (False, "Run the frozen benchmark replay for this candidate first.")
    elif replay.get("status") != "completed":
        results["benchmark"] = (False, f"Replay {replay.get('status')}.")
    else:
        bad = replay["regressions"] + replay["unblocked"]
        results["benchmark"] = (not bad, (f"Regressions: {', '.join(bad)}." if bad else
                                f"{len(replay['cases'])} case(s), no regressions "
                                f"(mean {replay['baseline_mean']:.2f} → {replay['candidate_mean']:.2f})")
                                + (f" Offline replay ({replay.get('model')})." if replay.get("recorded") else ""))

    results["approval"] = (bool(reviewer.strip()), f"Approver: {reviewer.strip()}." if reviewer.strip()
                           else "Waiting for a named reviewer.")
    return [dict(id=g, name=n, rule=r, passed=results[g][0], detail=results[g][1]) for g, n, r in GATES]


def compare_replay(baseline_rows, candidate_rows, must_stay_blocked):
    """Turn paired benchmark rows into the Gate 5 record."""
    base = {r["case"]: r for r in baseline_rows}
    cand = {r["case"]: r for r in candidate_rows}
    cases = sorted(base)
    regressions = [c for c in cases if cand[c]["score"] < base[c]["score"]]
    unblocked = [c for c in cases if c in must_stay_blocked
                 and base[c]["checks"].get("decision_flagged") and not cand[c]["checks"].get("decision_flagged")]
    mean = lambda rows: sum(r["score"] for r in rows) / len(rows) if rows else 0.0
    return {"cases": [{"case": c, "baseline": base[c]["score"], "candidate": cand[c]["score"],
                       "baseline_status": base[c]["exit_status"], "candidate_status": cand[c]["exit_status"]}
                      for c in cases],
            "regressions": regressions, "unblocked": [c for c in unblocked if c not in regressions],
            "baseline_mean": mean(baseline_rows), "candidate_mean": mean(candidate_rows)}


def seal(ws):
    """Hash-chain every audit entry that is not chained yet (append-only ledger)."""
    previous = "0" * 64
    for entry in ws.data["audit"]:
        if "hash" not in entry:
            body = {k: v for k, v in entry.items() if k not in ("hash", "prev")}
            entry["prev"] = previous
            entry["hash"] = hashlib.sha256((previous + json.dumps(body, sort_keys=True)).encode()).hexdigest()
        previous = entry["hash"]


def verify_ledger(audit):
    """True when no chained entry was edited, removed or reordered."""
    previous = "0" * 64
    for entry in audit:
        if "hash" not in entry:
            return False
        body = {k: v for k, v in entry.items() if k not in ("hash", "prev")}
        if entry["prev"] != previous or entry["hash"] != hashlib.sha256(
                (previous + json.dumps(body, sort_keys=True)).encode()).hexdigest():
            return False
        previous = entry["hash"]
    return True
