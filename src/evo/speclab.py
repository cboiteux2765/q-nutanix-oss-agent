"""Spec Lab: the Product Spec Agent toolchain inside EVO.

Wraps the product-spec harnesses in `agent/` and the frozen benchmark in `evals/`:

* **Spec runs**: one issue snapshot through either harness (mini-swe-agent loop or the
  Prime-RLM specialist orchestrator), with the tool trace, grade and rendered spec.
* **Harness comparison**: the frozen benchmark across harnesses on one model, plus the
  recorded results committed under `evals/results/`.
* **Governed evolution**: prompt-revision cycles (learning.Orchestrator), human feedback,
  the governance gates in `agent/governance.py` (including a frozen-benchmark replay),
  promotion, rejection and rollback, with a hash-chained ledger.

Models: "recorded" (replays real model replies captured by `evals.record`, no model needed),
"fixture" (a scripted reply for the bundled fixture), "ollama" (local), or "evo" (the
provider configured in EVO Settings). Jobs run one at a time per project in a worker
thread and are stored as `spec_jobs` records, like experiments.
"""

import asyncio
import json
from pathlib import Path
import threading
import time
from uuid import uuid4

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from agent.backends import (LocalRLM, Meter, Recording, SchemaBackend, aicommons_config, aicommons_models,
                            aicommons_usage, budgeted, evo_chat, ollama_chat, ollama_models, openai_compatible_chat)
from agent.generate import prepare, write_artifacts
from agent.governance import GATES, check_gates, compare_replay, seal, verify_ledger
from agent.learning import DemoBackend, Orchestrator, PrimeBackend, Workspace
from evals.cases import CASES, MUST_STAY_BLOCKED
from evals.run_evals import CASE_BY_ID, HARNESSES, run_case, summarize

from .common import Conflict, NotFound
from .config import get_api_key, get_settings
from .store import utc_now

ROOT = Path(__file__).resolve().parents[2]
RESULTS_DIR = ROOT / "evals" / "results"
FINAL = {"completed", "failed", "interrupted"}
BACKENDS = ("recorded", "fixture", "aicommons", "ollama", "evo")
AICOMMONS_DEFAULT = "claude-v5.5-haiku"
JOB_TOKEN_CAP = 300_000  # hard cap per Spec Lab job on hosted AI Commons models
OFFLINE = ("recorded", "fixture")


class LocalPrimeBackend(PrimeBackend):
    mode = "prime-rlm-local"


class SpecLabService:
    def __init__(self, store, data_dir: Path):
        self.store, self.root = store, Path(data_dir) / "speclab"
        self.root.mkdir(parents=True, exist_ok=True)
        self.tasks: dict[str, asyncio.Task] = {}
        self.workspaces: dict[str, Workspace] = {}
        self.lock = threading.RLock()

    # ---------------------------------------------------------------- state
    def recover(self) -> None:
        for job in self.store.list("spec_jobs"):
            if job["status"] not in FINAL:
                self.store.put("spec_jobs", job["id"], {**job, "status": "interrupted",
                               "error": "EVO stopped while this job was running.", "finished_at": utc_now()})
        for path in self.root.glob("*/learning/workspace.json"):
            ws = self.workspace(path.parent.parent.name)
            with ws.lock:
                for run in ws.data["runs"]:
                    if run["status"] == "running":
                        run.update(status="failed", error="Interrupted by restart.")
                ws.save()

    async def close(self) -> None:
        for task in self.tasks.values():
            task.cancel()
        for task in self.tasks.values():
            try:
                await task
            except BaseException:
                pass

    def workspace(self, project_id: str) -> Workspace:
        with self.lock:
            if project_id not in self.workspaces:
                self.workspaces[project_id] = Workspace(self.root / project_id / "learning")
            return self.workspaces[project_id]

    def jobs(self, project_id: str) -> list[dict]:
        return self.store.list("spec_jobs", project_id=project_id)

    def job(self, project_id: str, job_id: str) -> dict:
        job = self.store.get("spec_jobs", job_id)
        if not job or job["project_id"] != project_id:
            raise NotFound("Spec job not found.")
        return job

    def overview(self, project_id: str) -> dict:
        ws = self.workspace(project_id)
        settings = get_settings(self.store)
        jobs = [{k: v for k, v in j.items() if k not in ("rows", "result")} for j in self.jobs(project_id)]
        snapshot = ws.snapshot()
        return {
            "cases": [{"id": c["id"], "repo": c["repo"], "why": c["why"], "title": c["evidence"]["input"]["title"],
                       "rubric": c["rubric"], "must_stay_blocked": c["id"] in MUST_STAY_BLOCKED} for c in CASES],
            "harnesses": [{"id": k, "description": v} for k, v in HARNESSES.items()],
            "backends": {"recorded": self._recording_view(project_id), "aicommons": self._aicommons_view(),
                         "ollama": ollama_models(), "evo": {"model": settings.get("model"),
                         "configured": bool(get_api_key(settings)) or "127.0.0.1" in settings.get("base_url", "")
                         or "localhost" in settings.get("base_url", "")}},
            "jobs": jobs[::-1],
            "recorded": self.recorded_list(),
            "governance": {
                "active_version": snapshot["active_version"], "versions": snapshot["versions"][::-1],
                "cycles": [{k: r.get(k) for k in ("id", "title", "status", "mode", "created_at", "candidate_version",
                                                  "baseline_version", "error", "case_id")}
                           | {"critic": (r.get("evaluation") or {}).get("eligible"),
                              "replay": (r.get("benchmark_replay") or {}).get("status")}
                           for r in snapshot["runs"][::-1]],
                "feedback": snapshot["feedback"][::-1][:20], "audit": snapshot["audit"][::-1][:40],
                "ledger_ok": verify_ledger(snapshot["audit"]),
                "gates": [{"id": g, "name": n, "rule": r} for g, n, r in GATES],
            },
        }

    # ------------------------------------------------------------- recorded
    def recorded_list(self) -> list[dict]:
        out = []
        for path in sorted(RESULTS_DIR.glob("results-*.json"), reverse=True):
            try:
                rows = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            out.append({"name": path.name, "models": sorted({r.get("model", "?") for r in rows}),
                        "cases": len({r["case"] for r in rows}), "summary": summarize(rows)})
        return out

    def recorded(self, name: str) -> dict:
        path = (RESULTS_DIR / name).resolve()
        if path.parent != RESULTS_DIR.resolve() or not path.name.startswith("results-") or not path.is_file():
            raise NotFound("Recorded result not found.")
        rows = json.loads(path.read_text(encoding="utf-8"))
        return {"name": name, "rows": rows, "summary": summarize(rows)}

    # ------------------------------------------------------------- backends
    def _recording_view(self, project_id: str) -> dict | None:
        rec = Recording.default()
        if rec is None:
            return None
        guidance = self._active_prompt(project_id)
        return {"model": rec.data.get("model"), "recorded_at": rec.data.get("recorded_at"),
                "demo_cycle": (rec.data.get("demo_cycle") or {}).get("case"),
                "coverage": {h: [c["id"] for c in CASES if rec.has(c["id"], h, guidance)] for h in HARNESSES}}

    def _aicommons_view(self) -> dict | None:
        """Configured UCSB AI Commons models and quota (both unmetered; cached for a minute)."""
        config = aicommons_config()
        if not config:
            return None
        cached = getattr(self, "_aicommons_cache", None)
        if cached and time.monotonic() - cached[0] < 60:
            return cached[1]
        try:
            view = {"models": aicommons_models(config), "usage": aicommons_usage(config),
                    "default": AICOMMONS_DEFAULT, "job_token_cap": JOB_TOKEN_CAP, "error": None}
        except (ValueError, OSError) as exc:
            view = {"models": [], "usage": None, "default": AICOMMONS_DEFAULT, "job_token_cap": JOB_TOKEN_CAP,
                    "error": str(exc)}
        self._aicommons_cache = (time.monotonic(), view)
        return view

    @staticmethod
    def _mode(backend: str) -> str:
        return backend if backend in OFFLINE else "live"

    @staticmethod
    def _label(backend: str, chat, rec) -> str:
        if backend == "recorded":
            return f"recorded:{rec.data.get('model')}"
        return "scripted fixture" if backend == "fixture" else chat.label

    def _chat(self, backend: str, model: str):
        if backend == "aicommons":
            config = aicommons_config()
            if not config:
                raise Conflict("Set AICOMMONS_API_KEY and AICOMMONS_BASE_URL in .env to use AI Commons.")
            remaining = aicommons_usage(config).get("remaining") or 0
            if remaining <= 0:
                raise Conflict("No AI Commons tokens left this month.")
            self._aicommons_cache = None  # refresh the displayed quota after this job
            return budgeted(openai_compatible_chat(config, model or AICOMMONS_DEFAULT),
                            min(JOB_TOKEN_CAP, remaining), "Spec Lab job budget")
        if backend == "ollama":
            installed = ollama_models()
            if installed is None:
                raise Conflict("Ollama is not running. Start it, or use recorded mode.")
            if model not in installed:
                raise ValueError(f"Ollama model {model!r} is not installed. Installed: {', '.join(installed) or 'none'}.")
            return ollama_chat(model)
        if backend == "evo":
            settings = get_settings(self.store)
            key = get_api_key(settings)
            local = any(h in settings.get("base_url", "") for h in ("127.0.0.1", "localhost"))
            if not key and not local:
                raise Conflict("Set an API key in Settings to use EVO's model.")
            return evo_chat(settings, key)
        return None

    # ----------------------------------------------------------------- jobs
    def _start(self, project_id: str, kind: str, spec: dict, work, stats=None) -> dict:
        with self.lock:
            if any(j["status"] in ("queued", "running") for j in self.jobs(project_id)):
                raise Conflict("A Spec Lab job is already running for this project. Wait for it to finish.")
            job_id = f"spec_{uuid4().hex[:12]}"
            job = self.store.put("spec_jobs", job_id, {"project_id": project_id, "kind": kind, "status": "running",
                                 "started_at": utc_now(), "progress": [], **spec})

        def progress(text):
            current = self.store.get("spec_jobs", job_id)
            self.store.put("spec_jobs", job_id, {**current, "progress": [*current["progress"], text][-50:]})

        async def runner():
            started = time.monotonic()
            try:
                result = await asyncio.to_thread(work, progress)
                status, error = "completed", None
            except Exception as exc:  # recorded on the job
                result, status, error = {}, "failed", f"{type(exc).__name__}: {exc}"
            current = self.store.get("spec_jobs", job_id)
            usage = stats() if stats else {}  # model usage is recorded for failed jobs too
            self.store.put("spec_jobs", job_id, {**current, **usage, **result, "status": status, "error": error,
                           "finished_at": utc_now(), "seconds": round(time.monotonic() - started, 1)})
            self.tasks.pop(job_id, None)

        self.tasks[job_id] = asyncio.get_running_loop().create_task(runner())
        return job

    def _check_backend(self, backend: str, pairs: list[tuple[str, str]], guidance: str = ""):
        """Validate (case, harness) pairs for the backend. Returns the recording in recorded mode."""
        if backend not in BACKENDS:
            raise ValueError(f"Backend must be one of {', '.join(BACKENDS)}.")
        if backend == "fixture" and any(c != "bundled-fixture" for c, _ in pairs):
            raise ValueError("The scripted fixture only covers the bundled-fixture case. Use Recorded mode or a "
                             "live model for the other cases.")
        if backend != "recorded":
            return None
        rec = Recording.default()
        if rec is None:
            raise Conflict("No recording found in examples/recorded. Record one with `python -m evals.record`, "
                           "or use a live model.")
        missing = [f"{h} on {c}" for c, h in pairs if not rec.has(c, h, guidance)]
        if missing:
            raise ValueError(f"Not in the {rec.data.get('model')} recording for the prompt version in use: "
                             f"{', '.join(missing[:4])}{' ...' if len(missing) > 4 else ''}. Use a live model for "
                             "these, or roll back to the recorded prompt version.")
        return rec

    def start_spec(self, project_id: str, harness: str, case_id: str | None, backend: str, model: str = "",
                   evidence: dict | None = None) -> dict:
        if harness not in HARNESSES:
            raise ValueError("Unknown harness.")
        if evidence is not None:
            if backend in OFFLINE:
                raise ValueError("Custom evidence needs a live model.")
            repo = evidence.get("input", {}).get("repo", "")
            prepare(evidence, repo)  # validates the snapshot before anything runs
            case = {"id": "custom", "repo": repo, "evidence": evidence, "rubric": {}}
        else:
            case = CASE_BY_ID.get(case_id or "")
            if not case:
                raise ValueError("Choose a benchmark case or paste an evidence snapshot.")
        guidance = self._active_prompt(project_id)
        rec = self._check_backend(backend, [(case["id"], harness)], guidance)
        chat = self._chat(backend, model)

        def work(progress):
            progress(f"Running {harness} on {case['id']}")
            row = run_case(case, harness, chat, guidance, self._mode(backend), rec)
            if row.get("draft"):
                directory = write_artifacts(case["evidence"], prepare(case["evidence"], case["repo"]), row["draft"],
                                            {"mode": backend, "agent": harness, "workflow": "speclab-1"},
                                            self.root / project_id / "artifacts")
                row["markdown"] = (directory / "product_spec.md").read_text(encoding="utf-8")
                row["handoff"] = json.loads((directory / "handoff.json").read_text(encoding="utf-8"))
            return {"result": row}

        return self._start(project_id, "spec", {"harness": harness, "case_id": case["id"], "backend": backend,
                                                "model": self._label(backend, chat, rec),
                                                "title": case["evidence"]["input"]["title"]}, work)

    def start_benchmark(self, project_id: str, harnesses: list[str], case_ids: list[str], backend: str,
                        model: str = "") -> dict:
        if not harnesses or any(h not in HARNESSES for h in harnesses):
            raise ValueError("Choose at least one known harness.")
        if not case_ids or any(c not in CASE_BY_ID for c in case_ids):
            raise ValueError("Choose at least one benchmark case.")
        guidance = self._active_prompt(project_id)
        rec = self._check_backend(backend, [(c, h) for c in case_ids for h in harnesses], guidance)
        chat = self._chat(backend, model)

        def work(progress):
            rows = []
            for case_id in case_ids:
                for harness in harnesses:
                    progress(f"{harness} · {case_id}")
                    row = run_case(CASE_BY_ID[case_id], harness, chat, guidance, self._mode(backend), rec)
                    rows.append(row)
                    progress(f"{harness} · {case_id}: {row['exit_status']} score {row['score']}")
            if backend not in OFFLINE:  # keep live comparisons with the committed evidence
                RESULTS_DIR.mkdir(parents=True, exist_ok=True)
                label = chat.label.replace(":", "-").replace("/", "-")
                path = RESULTS_DIR / f"results-{label}-{time.strftime('%Y%m%d-%H%M%S')}-speclab.json"
                path.write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")
            return {"rows": rows, "summary": summarize(rows)}

        return self._start(project_id, "benchmark", {"harnesses": harnesses, "case_ids": case_ids,
                           "backend": backend, "model": self._label(backend, chat, rec),
                           "title": f"{len(harnesses)} harness(es) × {len(case_ids)} case(s)"}, work)

    # ------------------------------------------------------------ governance
    def _active_prompt(self, project_id: str) -> str:
        ws = self.workspace(project_id)
        return next(v["prompt"] for v in ws.data["versions"] if v["id"] == ws.data["active_version"])

    def _audit(self, ws: Workspace, entry: dict) -> None:
        with ws.lock:
            ws.data["audit"].append({**entry, "at": utc_now()})
            seal(ws)
            ws.save()

    def start_cycle(self, project_id: str, case_id: str, backend: str, model: str = "",
                    harness: str = "prime-rlm") -> dict:
        case = CASE_BY_ID.get(case_id)
        if not case:
            raise ValueError("Choose a benchmark case.")
        if harness not in ("prime-rlm", "prime-schema"):
            raise ValueError("Evolution cycles use the Prime-RLM orchestrator (prime-rlm or prime-schema).")
        guidance = self._active_prompt(project_id)
        rec = self._check_backend(backend, [(case_id, harness)], guidance)
        chat = rec.replay(case_id, harness, guidance) if rec else self._chat(backend, model)
        ws = self.workspace(project_id)
        meter = Meter(chat) if chat else None
        if backend == "fixture":
            backend_impl = DemoBackend()
        else:
            backend_impl = LocalPrimeBackend(LocalRLM(meter)) if harness == "prime-rlm" else SchemaBackend(meter)
            if rec:
                backend_impl.mode = f"recorded-{harness}"
        orchestrator = Orchestrator(ws, backend_impl)
        run_id = orchestrator.create(case["evidence"], case["repo"])
        with ws.lock:
            ws.run(run_id)["case_id"] = case_id
            ws.save()

        def work(progress):
            progress("Research, prompt design and scenarios (3 specialists in parallel)")
            try:
                asyncio.run(orchestrator.execute(run_id, case["evidence"], case["repo"]))
            finally:
                run = ws.run(run_id)
                if run.get("candidate_version"):
                    self._audit(ws, {"action": "proposal", "run_id": run_id, "version": run["candidate_version"],
                                     "mode": run["mode"]})
            run = ws.run(run_id)
            progress(f"Critic: candidate {run['evaluation']['candidate_passed']}/{run['evaluation']['total']}")
            return {"cycle_id": run_id}

        try:
            return self._start(project_id, "cycle", {"cycle_id": run_id, "case_id": case_id, "backend": backend,
                               "harness": harness, "model": self._label(backend, chat, rec),
                               "title": f"Evolution cycle · {case_id}"}, work, meter.stats if meter else None)
        except Exception:
            with ws.lock:
                ws.run(run_id).update(status="failed", error="Not started: another job is running.")
                ws.save()
            raise

    def cycle(self, project_id: str, cycle_id: str, reviewer: str = "") -> dict:
        ws = self.workspace(project_id)
        try:
            run = ws.run(cycle_id)
        except StopIteration:
            raise NotFound("Evolution cycle not found.") from None
        versions = {v["id"]: v for v in ws.data["versions"]}
        return {"run": run, "candidate": versions.get(run.get("candidate_version")),
                "baseline": versions.get(run.get("baseline_version")),
                "gates": check_gates(ws, cycle_id, reviewer) if run["status"] != "running" else []}

    def start_replay(self, project_id: str, cycle_id: str, backend: str, model: str = "",
                     harness: str = "mini") -> dict:
        """Gate 5: replay the frozen benchmark with the baseline and the candidate prompt."""
        ws = self.workspace(project_id)
        detail = self.cycle(project_id, cycle_id)
        run, candidate, baseline = detail["run"], detail["candidate"], detail["baseline"]
        if run["status"] != "needs_review" or not candidate:
            raise Conflict("Only a finished cycle that awaits a decision can be replayed.")
        if harness not in HARNESSES:
            raise ValueError("Unknown harness.")
        if backend == "fixture":
            case_ids = ["bundled-fixture"]
        elif backend == "recorded":
            recording = Recording.default()
            case_ids = [c["id"] for c in CASES if recording and recording.has(c["id"], harness, baseline["prompt"])
                        and recording.has(c["id"], harness, candidate["prompt"])]
            if not case_ids:
                raise ValueError("This candidate's benchmark replay was not recorded. Use a live model, or start "
                                 "the recorded demo cycle from its recorded case.")
        else:
            case_ids = [c["id"] for c in CASES]
        rec = self._check_backend(backend, [(c, harness) for c in case_ids], baseline["prompt"])
        if rec:
            self._check_backend(backend, [(c, harness) for c in case_ids], candidate["prompt"])
        chat = self._chat(backend, model)

        def work(progress):
            arms = {}
            for arm, version in (("baseline", baseline), ("candidate", candidate)):
                arms[arm] = []
                for case_id in case_ids:
                    progress(f"{arm} ({version['id']}) · {case_id}")
                    arms[arm].append(run_case(CASE_BY_ID[case_id], harness, chat, version["prompt"],
                                              self._mode(backend), rec))
            replay = {**compare_replay(arms["baseline"], arms["candidate"], MUST_STAY_BLOCKED),
                      "status": "completed", "harness": harness, "recorded": backend in OFFLINE,
                      "candidate_version": candidate["id"], "baseline_version": baseline["id"],
                      "model": self._label(backend, chat, rec), "at": utc_now()}
            with ws.lock:
                ws.run(cycle_id)["benchmark_replay"] = replay
                ws.save()
            self._audit(ws, {"action": "benchmark_replay", "run_id": cycle_id, "version": candidate["id"],
                             "regressions": replay["regressions"] + replay["unblocked"], "model": replay["model"]})
            return {"replay": replay}

        return self._start(project_id, "replay", {"cycle_id": cycle_id, "backend": backend, "harness": harness,
                           "model": self._label(backend, chat, rec),
                           "title": f"Benchmark replay · {candidate['id']} vs {baseline['id']}"}, work)

    def feedback(self, project_id: str, cycle_id: str, text: str, reviewer: str) -> dict:
        self.cycle(project_id, cycle_id)  # 404 for unknown cycles
        ws = self.workspace(project_id)
        ws.feedback(cycle_id, text, reviewer)
        self._audit(ws, {"action": "feedback", "run_id": cycle_id, "reviewer": reviewer.strip()})
        return self.cycle(project_id, cycle_id)

    def decide(self, project_id: str, cycle_id: str, action: str, reviewer: str) -> dict:
        self.cycle(project_id, cycle_id)  # 404 for unknown cycles
        ws = self.workspace(project_id)
        if action == "promote":
            failed = [g for g in check_gates(ws, cycle_id, reviewer) if not g["passed"]]
            if failed:
                raise Conflict("Promotion blocked by governance: " +
                               "; ".join(f"{g['name']}: {g['detail']}" for g in failed))
        ws.decision(cycle_id, action, reviewer)
        seal(ws)
        ws.save()
        return self.cycle(project_id, cycle_id, reviewer)

    def rollback(self, project_id: str, version_id: str, reviewer: str) -> dict:
        ws = self.workspace(project_id)
        try:
            ws.rollback(version_id, reviewer)
        except StopIteration:
            raise NotFound("Prompt version not found.") from None
        seal(ws)
        ws.save()
        return self.overview(project_id)["governance"]


# ------------------------------------------------------------------ HTTP
class SpecRunInput(BaseModel):
    harness: str
    case_id: str | None = None
    backend: str = "recorded"
    model: str = ""
    evidence: dict | None = None


class BenchmarkInput(BaseModel):
    harnesses: list[str] = Field(min_length=1)
    case_ids: list[str] = Field(min_length=1)
    backend: str = "recorded"
    model: str = ""


class CycleInput(BaseModel):
    case_id: str = "bundled-fixture"
    backend: str = "recorded"
    model: str = ""
    harness: str = "prime-rlm"


class ReplayInput(BaseModel):
    backend: str = "recorded"
    model: str = ""
    harness: str = "mini"


class FeedbackInput(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    reviewer: str = Field(min_length=1, max_length=200)


class DecisionInput(BaseModel):
    action: str
    reviewer: str = Field(min_length=1, max_length=200)


class RollbackVersionInput(BaseModel):
    reviewer: str = Field(min_length=1, max_length=200)


def router(project_dep) -> APIRouter:
    """Routes under /api/projects/{project_id}/spec. `project_dep` resolves the project or 404s."""
    api = APIRouter(prefix="/api/projects/{project_id}/spec")
    svc = lambda request: request.app.state.speclab  # noqa: E731

    @api.get("")
    async def overview(request: Request, project: project_dep):
        return svc(request).overview(project["id"])

    @api.get("/jobs/{job_id}")
    async def read_job(request: Request, project: project_dep, job_id: str):
        return svc(request).job(project["id"], job_id)

    @api.post("/runs", status_code=201)
    async def spec_run(request: Request, project: project_dep, body: SpecRunInput):
        return svc(request).start_spec(project["id"], **body.model_dump())

    @api.post("/benchmarks", status_code=201)
    async def benchmark(request: Request, project: project_dep, body: BenchmarkInput):
        return svc(request).start_benchmark(project["id"], **body.model_dump())

    @api.get("/recorded/{name}")
    async def recorded(request: Request, project: project_dep, name: str):
        return svc(request).recorded(name)

    @api.post("/cycles", status_code=201)
    async def cycle(request: Request, project: project_dep, body: CycleInput):
        return svc(request).start_cycle(project["id"], **body.model_dump())

    @api.get("/cycles/{cycle_id}")
    async def read_cycle(request: Request, project: project_dep, cycle_id: str, reviewer: str = ""):
        return svc(request).cycle(project["id"], cycle_id, reviewer)

    @api.post("/cycles/{cycle_id}/replay", status_code=201)
    async def replay(request: Request, project: project_dep, cycle_id: str, body: ReplayInput):
        return svc(request).start_replay(project["id"], cycle_id, **body.model_dump())

    @api.post("/cycles/{cycle_id}/feedback")
    async def feedback(request: Request, project: project_dep, cycle_id: str, body: FeedbackInput):
        return svc(request).feedback(project["id"], cycle_id, body.text, body.reviewer)

    @api.post("/cycles/{cycle_id}/decision")
    async def decision(request: Request, project: project_dep, cycle_id: str, body: DecisionInput):
        return svc(request).decide(project["id"], cycle_id, body.action, body.reviewer)

    @api.post("/versions/{version_id}/rollback")
    async def rollback(request: Request, project: project_dep, version_id: str, body: RollbackVersionInput):
        return svc(request).rollback(project["id"], version_id, body.reviewer)

    return api
