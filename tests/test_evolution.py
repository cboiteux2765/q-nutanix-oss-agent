"""Evolution experiments: GEPA search, held-out gate, budgets, isolation, activation and rollback."""

import asyncio
import json
import shutil

import pytest

from evo import runtime as runtime_module
from evo.evolve import service as service_module
from evo.evolve.adapter import SEED_PLACEHOLDER
from evo.evolve.service import ExperimentService
from evo.evolve.tasks import TaskBundles, replay_dir, task_python
from evo.runtime import Runtime

from .conftest import FakeProvider, python_call, reply

REAL_REFLECT = ExperimentService._reflect  # before the fixture replaces it

TASKS = [
    {"id": "t1", "split": "train"}, {"id": "t2", "split": "train"},
    {"id": "v1", "split": "validation"},
    {"id": "h1", "split": "heldout"}, {"id": "h2", "split": "heldout"},
]


class Fakes:
    def __init__(self):
        self.rollouts = []
        self.reflections = []
        self.providers = []
        self.limits = []
        self.override = None  # (task_id, candidate) -> fields to change in the fake record


@pytest.fixture
def fakes(monkeypatch):
    found = Fakes()

    async def fake_rollout(*, root, task, hidden, role, candidate, frozen, provider_settings, limits, python,
                           cancel=None, rollout_id=None):
        text = (candidate or {}).get("content", "")
        found.rollouts.append((task["id"], text))
        found.providers.append(dict(provider_settings))
        found.limits.append(dict(limits))
        good = "reproduce" in text
        extra = found.override(task["id"], candidate) if found.override else {}
        return {"task_id": task["id"], "outcome": "completed", "score": 1.0 if good else 0.0, "resolved": good,
                "tokens": 1000, "cost_usd": 0.0, "fail_to_pass": [int(good), 1], "pass_to_pass": [3, 3],
                "changed_files": ["src/x.py"], "diff": "+fix", "commands": [], "final_message": "done",
                "leakage": [], "latency_s": 1.0, "steps": 3} | extra

    monkeypatch.setattr(service_module, "run_rollout", fake_rollout)
    monkeypatch.setattr(service_module, "task_python", lambda: "python")
    monkeypatch.setattr(TaskBundles, "list", lambda self: [dict(t, title=f"Task {t['id']}") for t in TASKS])
    monkeypatch.setattr(TaskBundles, "load", lambda self, task_id: dict(
        next(t for t in TASKS if t["id"] == task_id), title=f"Task {task_id}", statement=f"STATEMENT-{task_id}",
        dir="unused"))
    monkeypatch.setattr(TaskBundles, "hidden_checks", lambda self, task_id: {})

    async def fake_reflect(self, experiment_id, messages):
        found.reflections.append(json.dumps(messages))
        self._spend(experiment_id, 100, 0, reflection=True)
        return "```\nAlways reproduce the bug with a failing test before changing code.\n```"

    monkeypatch.setattr(ExperimentService, "_reflect", fake_reflect)
    return found


def make_run(store, project, runtime_status="waiting", phase="code"):
    return store.put("runs", f"run-{phase}-{runtime_status}", {
        "project_id": project["id"], "title": "Earlier work", "status": runtime_status, "phase": phase,
        "root_agent_id": None})


async def finish(service, experiment):
    await service.jobs[experiment["id"]]
    return service.get(experiment["id"])


def test_gepa_experiment_gate_activation_and_rollback(store, project, fakes, fake_provider):
    source = make_run(store, project)

    async def scenario():
        runtime = Runtime(store)
        service = ExperimentService(store, runtime)
        old_run = await runtime.start_run(project["id"], "Work started before activation")
        experiment = service.create(project["id"], role="code", objective="Fix bugs more reliably",
                                    run_ids=[source["id"]], limits={"max_metric_calls": 6, "max_iterations": 2})
        result = await finish(service, experiment)
        candidate_id = result["summary"]["candidate_prompt_id"]
        activated = service.activate(project["id"], candidate_id)
        new_run = await runtime.start_run(project["id"], "Work started after activation")
        old_root = store.get("agents", old_run["root_agent_id"])
        new_root = store.get("agents", new_run["root_agent_id"])
        old_code = runtime._new_agent(store.get("runs", old_run["id"]), "code", "x", old_root)
        new_code = runtime._new_agent(store.get("runs", new_run["id"]), "code", "x", new_root)
        rolled = service.rollback(project["id"], "code")
        # A second identical experiment reuses finished rollouts instead of paying again.
        before = len(fakes.rollouts)
        repeat = await finish(service, service.create(project["id"], role="code", run_ids=[source["id"]],
                                                      limits={"max_metric_calls": 6, "max_iterations": 2}))
        await runtime.close()
        return result, activated, old_code, new_code, rolled, before, repeat

    result, activated, old_code, new_code, rolled, before, repeat = asyncio.run(scenario())
    assert result["status"] == "completed", result.get("error")
    gepa = result["gepa"]
    assert len(gepa["candidates"]) >= 2 and gepa["best_index"] > 0
    candidate = next(p for p in result["candidates"] if p["id"] == result["summary"]["candidate_prompt_id"])
    assert candidate["parent_prompt_ids"] == [None]  # descended from the built-in baseline
    assert "reproduce" in candidate["content"]
    gate = result["summary"]["gate"]
    assert gate["passed"] and gate["improved"] and gate["heldout"]["tasks"] == 2
    heldout = [r for r in result["rollouts"] if r["phase"] == "heldout"]
    assert sorted((r["task_id"], r["candidate_label"].split()[0]) for r in heldout) == [
        ("h1", "baseline"), ("h1", "candidate"), ("h2", "baseline"), ("h2", "candidate")]
    # Reflection only ever saw reflection (train) examples: no validation or held-out text.
    reflections = "\n".join(fakes.reflections)
    assert "STATEMENT-t" in reflections
    assert "STATEMENT-v1" not in reflections and "STATEMENT-h" not in reflections
    assert result["spent"]["rollouts"] >= 5 and result["spent"]["reflection_calls"] >= 1
    assert activated["activation_label"] == "passed held-out evaluation"
    old_system = store.messages(old_code["id"])[0]["content"]
    new_system = store.messages(new_code["id"])[0]["content"]
    assert "reproduce the bug" not in old_system  # the running assignment keeps its snapshot
    assert "reproduce the bug" in new_system
    assert rolled["active"] is None and store.get("prompts", candidate["id"])["status"] == "rolled_back"
    assert repeat["status"] == "completed"
    assert len(fakes.rollouts) == before
    assert all(r.get("reused_from") for r in repeat["rollouts"])


def test_search_cannot_spend_the_heldout_reserve(store, project, fakes, monkeypatch):
    """Every reflection call is admitted against its worst case, and the search stops while the
    tokens for the paired held-out evaluation are still untouched."""
    source = make_run(store, project)
    store.put("settings", "provider", {"max_output_tokens": 200})
    monkeypatch.setattr(ExperimentService, "_reflect", REAL_REFLECT)
    calls = []

    class Provider:
        def __init__(self, settings, key):
            pass

        async def complete(self, messages, tools=None, on_delta=None):
            calls.append(messages)
            # A different, unhelpful proposal each time: GEPA rejects them all.
            return reply(f"```\nWork carefully, attempt {len(calls)}.\n```", tokens=300)

        async def close(self):
            pass

    monkeypatch.setattr(service_module, "ModelProvider", Provider)

    async def scenario():
        runtime = Runtime(store)
        service = ExperimentService(store, runtime)
        with pytest.raises(ValueError, match="cannot cover the final paired evaluation"):
            service.create(project["id"], role="code", run_ids=[source["id"]],
                           limits={"rollout_tokens": 1000, "experiment_tokens": 3999})
        with pytest.raises(ValueError, match="testing one proposal"):
            service.create(project["id"], role="code", run_ids=[source["id"]],
                           limits={"rollout_tokens": 1000, "experiment_tokens": 6000})
        experiment = service.create(project["id"], role="code", run_ids=[source["id"]],
                                    limits={"rollout_tokens": 1000, "experiment_tokens": 16_000,
                                            "max_metric_calls": 40, "max_iterations": 10})
        result = await finish(service, experiment)
        await runtime.close()
        return result

    result = asyncio.run(scenario())
    budget, spent = result["budget"], result["spent"]
    assert budget["heldout_reserve_tokens"] == 4000 and budget["search_tokens"] == 12_000
    assert calls and spent["reflection_calls"] == len(calls) < 10  # stopped by tokens, not proposals
    assert spent["search_tokens"] <= budget["search_tokens"]
    assert budget["total_tokens"] - spent["tokens"] >= budget["heldout_reserve_tokens"]
    assert any("Search budget reached" in line for line in result.get("log", []))
    assert fakes.limits and all(limits["rollout_tokens"] == 1000 for limits in fakes.limits)


def test_budget_plan_counts():
    from evo.evolve.service import budget_plan
    splits = {"train": ["a"] * 8, "validation": ["b"] * 8, "heldout": ["c"] * 8}
    plan = budget_plan({"rollout_tokens": 400_000, "experiment_tokens": 14_000_000, "max_iterations": 2,
                        "reflection_minibatch_size": 1, "max_metric_calls": 14, "max_cost_usd": 10.0},
                       splits, {"max_output_tokens": 32_768})
    assert plan["heldout_reserve_tokens"] == 6_400_000 and plan["search_tokens"] == 7_600_000
    assert plan["search_rollouts"] == {"all_rejected": 12, "all_accepted": 28, "fit_in_search_tokens": 19,
                                       "max_metric_calls": 14, "rollouts_per_task": 1}
    # With prices, the spending limit is a dollar pool with the same held-out reserve.
    priced = budget_plan({"rollout_tokens": 400_000, "experiment_tokens": 14_000_000, "max_cost_usd": 20.0,
                          "rollout_cost_usd": 0.5}, splits, {"input_price": 1.0, "output_price": 5.0})
    assert priced["total_tokens"] == 14_000_000  # dollars no longer shrink the token budget
    assert (priced["total_usd"], priced["heldout_reserve_usd"], priced["search_usd"]) == (20.0, 8.0, 12.0)


def test_forced_activation_is_labelled(store, project, fakes):
    async def scenario():
        runtime = Runtime(store)
        service = ExperimentService(store, runtime)
        prompt = store.put("prompts", "p-manual", {"project_id": project["id"], "role": "code",
                                                   "status": "candidate", "version": 1, "content": "Be careful."})
        with pytest.raises(PermissionError):
            service.activate(project["id"], prompt["id"])
        forced = service.activate(project["id"], prompt["id"], force=True)
        await runtime.close()
        return forced

    forced = asyncio.run(scenario())
    assert forced["status"] == "active"
    assert forced["activation_label"] == "activated without evaluation"


def test_reflection_queue_ignores_evaluation_runs_and_respects_manual_mode(store, project, fakes):
    async def scenario():
        runtime = Runtime(store)
        service = ExperimentService(store, runtime)
        run = make_run(store, project, phase="test")
        service.on_run_event("verification_failed", run, {"handoff_id": "h"})
        service.on_run_event("verification_failed", run, {"handoff_id": "h"})  # deduplicated
        service.on_run_event("turn_finished", run, {"status": "waiting"})  # a pause, not an outcome
        service.on_run_event("turn_finished", run | {"evaluation": True}, {"status": "failed"})
        chat = make_run(store, project, phase="triage")
        service.on_run_event("turn_finished", chat, {"status": "waiting"})  # plain conversation
        queue = service.queue(project["id"])
        experiments = service.list(project["id"])
        await runtime.close()
        return queue, experiments

    queue, experiments = asyncio.run(scenario())
    assert [item["reason"] for item in queue] == ["verification failed"]
    assert experiments == []  # manual mode never starts paid work


def test_automatic_mode_starts_one_bounded_experiment(store, project, fakes):
    async def scenario():
        runtime = Runtime(store)
        service = ExperimentService(store, runtime)
        service.update_settings(project["id"], {"mode": "auto", "min_queued_runs": 2,
                                                "limits": {"max_metric_calls": 4, "max_iterations": 1}})
        for phase in ("code", "test"):
            run = make_run(store, project, phase=phase)
            service.on_run_event("turn_finished", run, {"status": "failed"})
        await asyncio.sleep(0.05)
        experiments = service.list(project["id"])
        for job in list(service.jobs.values()):
            await job
        await runtime.close()
        return experiments

    experiments = asyncio.run(scenario())
    assert len(experiments) == 1 and experiments[0]["origin"] == "auto"
    assert experiments[0]["limits"]["max_metric_calls"] == 4


# ---------------------------------------------------------------- the real harness, scripted model
needs_replay = pytest.mark.skipif(not (replay_dir() and task_python()),
                                  reason="needs the pytest replay mirror and task interpreter")


@pytest.fixture(scope="session")
def real_task(tmp_path_factory):
    bundles = TaskBundles(tmp_path_factory.mktemp("evo-tasks"))
    return bundles, bundles.load("pytest-14492")


@needs_replay
def test_real_rollout_scores_with_hidden_checks_in_isolation(store, project, real_task, monkeypatch, tmp_path):
    """The real EVO harness fixes a real historical bug (scripted model) and hidden tests score it."""
    from evo.evolve.rollout import run_rollout

    bundles, task = real_task
    monkeypatch.setattr(runtime_module, "ModelProvider", FakeProvider)
    fix = (
        "p = 'src/_pytest/_code/code.py'\n"
        "s = open(p, encoding='utf-8').read()\n"
        "old = 'argcount += raw.co_flags & CO_VARARGS\\n            argcount += raw.co_flags & CO_VARKEYWORDS'\n"
        "new = ('argcount += raw.co_kwonlyargcount\\n            argcount += bool(raw.co_flags & CO_VARARGS)'\n"
        "       '\\n            argcount += bool(raw.co_flags & CO_VARKEYWORDS)')\n"
        "assert old in s\n"
        "repo.write(p, s.replace(old, new))\n"
        "repo.commit('Count keyword-only and variadic arguments correctly')"
    )
    FakeProvider.calls = []
    FakeProvider.script = [
        python_call("try:\n    memory.write('notes.md', 'x')\nexcept Exception as e:\n    print(e)", "c1"),
        python_call("try:\n    web.search('pytest getargs fix')\nexcept Exception as e:\n    print(e)", "c2"),
        python_call(fix, "c3"),
        reply("Fixed Code.getargs and committed."),
    ]
    main_runs_before = len(store.list("runs"))
    candidate = {"version": "candidate 1", "content": "Always reproduce first."}
    monkeypatch.setenv("EVO_MODEL", "env-model")  # must not override the experiment's frozen settings

    record = asyncio.run(run_rollout(
        root=tmp_path / "rollouts", task=task, hidden=bundles.hidden_checks(task["id"]), role="code",
        candidate=candidate, frozen={"skills": [], "wiki_dir": None, "wiki_revision": None},
        provider_settings=store.get("settings", "provider"),
        limits={"rollout_tokens": 100_000, "max_steps": 10, "rollout_seconds": 300}, python=task_python()))

    assert record["outcome"] == "completed", record.get("error")
    assert record["resolved"] and record["score"] == 1.0, record.get("checks_output")
    assert record["fail_to_pass"] == [1, 1]
    assert record["changed_files"] == ["src/_pytest/_code/code.py"]
    assert record["leakage"] == []
    assert record["model"] == "test-model"
    system = FakeProvider.calls[0][0]["content"]
    assert "Project-approved role instructions (version candidate 1):\nAlways reproduce first." in system
    assert "Fix this issue" in FakeProvider.calls[0][1]["content"]
    assert len(store.list("runs")) == main_runs_before  # nothing written to the user's store
    from evo.store import Store
    isolated = Store(tmp_path / "rollouts" / record["data_dir"].split("\\")[-1].split("/")[-1] / "data")
    try:
        results = [e["data"].get("result") for e in isolated.events(record["run_id"], limit=5000)
                   if e["kind"] == "tool.finished" and e["data"].get("name") in {"memory.write", "web.search"}]
        assert all("disabled in this run" in json.dumps(r) for r in results) and len(results) == 2
    finally:
        isolated.close()


@needs_replay
def test_real_rollout_records_an_unresolved_attempt(store, real_task, monkeypatch, tmp_path):
    from evo.evolve.rollout import run_rollout

    bundles, task = real_task
    monkeypatch.setattr(runtime_module, "ModelProvider", FakeProvider)
    FakeProvider.script = [reply("I could not find the problem.")]
    record = asyncio.run(run_rollout(
        root=tmp_path / "rollouts", task=task, hidden=bundles.hidden_checks(task["id"]), role="code",
        candidate=None, frozen={"skills": [], "wiki_dir": None, "wiki_revision": None},
        provider_settings=store.get("settings", "provider"),
        limits={"rollout_tokens": 100_000, "max_steps": 10, "rollout_seconds": 300}, python=task_python()))
    assert record["outcome"] == "completed"
    assert not record["resolved"] and record["score"] == 0.0
    assert record["fail_to_pass"] == [0, 1] and record["changed_files"] == []


def test_seed_placeholder_is_not_applied_as_instructions():
    assert "No project-specific instructions" in SEED_PLACEHOLDER


# ---------------------------------------------------------------- review regressions
def heldout_record(task_id, **fields):
    return {"task_id": task_id, "split": "heldout", "outcome": "completed", "score": 1.0, "resolved": True,
            "fail_to_pass": [1, 1], "pass_to_pass": [3, 3], "tokens": 1000, "leakage": []} | fields


def test_gate_rejects_a_comparison_with_a_failed_baseline():
    results = {"baseline": [heldout_record("h1", outcome="error", score=0.0, resolved=False,
                                           error="Evaluation harness: git failed")],
               "candidate": [heldout_record("h1")]}
    gate = ExperimentService._gate(results, ["h1"])
    assert gate["complete"] is False and gate["passed"] is False and gate["improved"] is False
    assert "baseline on h1" in gate["reasons"][0]
    # A missing record or unscored checks in either arm also invalidates it.
    assert not ExperimentService._gate({"baseline": [heldout_record("h1")], "candidate": []}, ["h1"])["passed"]
    timed_out_checks = heldout_record("h1", checks_error="Hidden checks timed out.")
    assert not ExperimentService._gate({"baseline": [heldout_record("h1")], "candidate": [timed_out_checks]},
                                       ["h1"])["complete"]
    good = ExperimentService._gate({"baseline": [heldout_record("h1", score=0.0, resolved=False)],
                                    "candidate": [heldout_record("h1")]}, ["h1"])
    assert good["complete"] and good["passed"] and good["improved"]
    assert {k: v for k, v in good["paired"].items() if k != "tasks"} == {
        "both": 0, "baseline_only": 0, "candidate_only": 1, "neither": 0}


def test_experiment_runs_with_the_settings_frozen_at_creation(store, project, fakes, monkeypatch):
    source = make_run(store, project)
    seen = []

    class Provider:
        def __init__(self, settings, key):
            seen.append(dict(settings))

        async def complete(self, messages, tools=None, on_delta=None):
            return reply("```\nproposal\n```")

        async def close(self):
            pass

    monkeypatch.setattr(service_module, "ModelProvider", Provider)

    async def scenario():
        runtime = Runtime(store)
        service = ExperimentService(store, runtime)
        experiment = service.create(project["id"], role="code", run_ids=[source["id"]],
                                    limits={"max_metric_calls": 3, "max_iterations": 1})
        # Settings changed after creation (or before a resume) must not leak into the experiment.
        store.put("settings", "provider", {"model": "other-model", "api_style": "responses"})
        await service.jobs[experiment["id"]]
        await REAL_REFLECT(service, experiment["id"], [{"role": "user", "content": "reflect"}])
        await runtime.close()
        return experiment

    experiment = asyncio.run(scenario())
    assert experiment["provider"]["model"] == "test-model"
    assert fakes.providers and all(p["model"] == "test-model" and p["api_style"] == "chat_completions"
                                   for p in fakes.providers)
    assert seen and all(s["model"] == "test-model" for s in seen)


def test_held_out_tasks_stay_held_out_across_experiments(store, project, fakes):
    source = make_run(store, project)

    async def scenario():
        runtime = Runtime(store)
        service = ExperimentService(store, runtime)
        first = service.create(project["id"], role="code", run_ids=[source["id"]],
                               limits={"max_metric_calls": 3, "max_iterations": 1})
        await service.jobs[first["id"]]
        errors = []
        for splits in ({"train": ["h1"], "validation": ["v1"], "heldout": ["h2"]},   # held-out -> reflection
                       {"train": ["t1"], "validation": ["h1"], "heldout": ["h2"]},   # held-out -> validation
                       {"train": ["t2"], "validation": ["v1"], "heldout": ["t1"]}):  # reflection -> held-out
            try:
                service.create(project["id"], role="code", run_ids=[source["id"]], **splits)
            except ValueError as error:
                errors.append(str(error))
        await runtime.close()
        return errors

    errors = asyncio.run(scenario())
    assert len(errors) == 3
    assert "reserved as held-out" in errors[0] and "reserved as held-out" in errors[1]
    assert "cannot give a fresh held-out result" in errors[2]


def test_rollout_cache_respects_limits_and_retries_failures(store, project, fakes):
    source = make_run(store, project)
    fakes.override = lambda task_id, candidate: {"outcome": "timeout"} if task_id == "v1" else (
        {"outcome": "error", "error": "Evaluation harness: disk full"} if task_id == "t1" else {})
    small = {"max_metric_calls": 3, "max_iterations": 1}

    async def scenario():
        runtime = Runtime(store)
        service = ExperimentService(store, runtime)
        calls = []
        for limits in (small, small, {**small, "rollout_seconds": 2400}):
            before = len(fakes.rollouts)
            experiment = service.create(project["id"], role="code", run_ids=[source["id"]], limits=limits)
            await service.jobs[experiment["id"]]
            calls.append([task for task, _ in fakes.rollouts[before:]])
        await runtime.close()
        return calls

    first, same, longer = asyncio.run(scenario())
    assert "v1" in first and "t1" in first
    assert "v1" not in same       # a timeout under the same limits is a real outcome and is reused
    assert "t1" in same           # a harness error is retried, never reused
    assert "v1" in longer         # a longer timeout runs the task again
    assert fakes.limits[-1]["rollout_seconds"] == 2400


def test_gate_reason_when_every_proposal_loses_its_minibatch(store, project, fakes, monkeypatch):
    """A proposal that does not beat its parent on the minibatch is never scored on validation, and the
    gate says so instead of claiming a validation comparison happened."""
    source = make_run(store, project)

    async def weak_reflect(self, experiment_id, messages):
        self._spend(experiment_id, 100, 0, reflection=True)
        text = "```\nRead the code carefully.\n```"
        proposals = self.get(experiment_id).get("proposals") or []
        self._update(experiment_id, proposals=[*proposals, {"at": "now", "text": text}])
        return text

    monkeypatch.setattr(ExperimentService, "_reflect", weak_reflect)

    async def scenario():
        runtime = Runtime(store)
        service = ExperimentService(store, runtime)
        experiment = service.create(project["id"], role="code", run_ids=[source["id"]],
                                    limits={"max_metric_calls": 6, "max_iterations": 2})
        result = await finish(service, experiment)
        await runtime.close()
        return result

    result = asyncio.run(scenario())
    assert result["status"] == "completed", result.get("error")
    gate = result["summary"]["gate"]
    assert not gate["passed"] and result["summary"]["candidate_prompt_id"] is None
    assert len(result["gepa"]["candidates"]) == 1
    assert gate["reasons"] == [f"All {len(result['proposals'])} proposals were rejected on their reflection "
                               "minibatch, so none was scored on validation."]
    assert len(result["proposals"]) >= 1
    assert not [r for r in result["rollouts"] if r["phase"] == "heldout"]


def test_an_errored_rollout_is_retried_and_both_attempts_are_charged(store, project, fakes):
    source = make_run(store, project)
    seen = []

    def flaky(task_id, candidate):
        seen.append(task_id)
        if task_id == "h1" and seen.count("h1") == 1:
            return {"outcome": "error", "error": "Provider: response hit the output limit"}
        return {}

    fakes.override = flaky

    async def scenario():
        runtime = Runtime(store)
        service = ExperimentService(store, runtime)
        experiment = service.create(project["id"], role="code", run_ids=[source["id"]],
                                    limits={"max_metric_calls": 6, "max_iterations": 2})
        result = await finish(service, experiment)
        await runtime.close()
        return result

    result = asyncio.run(scenario())
    h1 = [r for r in result["rollouts"] if r["task_id"] == "h1" and r["candidate_label"] == "baseline"]
    assert [(r["outcome"], r["retry"]) for r in h1] == [("error", 0), ("completed", 1)]
    assert result["summary"]["gate"]["complete"]           # the retry, not the error, is compared
    assert result["spent"]["rollouts"] == len(result["rollouts"])
    assert any("Retrying h1" in line for line in result["log"])


def test_error_retries_can_be_turned_off(store, project, fakes):
    source = make_run(store, project)
    fakes.override = lambda task_id, candidate: {"outcome": "error", "error": "boom"} if task_id == "h1" else {}

    async def scenario():
        runtime = Runtime(store)
        service = ExperimentService(store, runtime)
        experiment = service.create(project["id"], role="code", run_ids=[source["id"]],
                                    limits={"max_metric_calls": 6, "max_iterations": 2, "error_retries": 0})
        result = await finish(service, experiment)
        await runtime.close()
        return result

    result = asyncio.run(scenario())
    h1 = [r for r in result["rollouts"] if r["task_id"] == "h1"]
    assert len(h1) == 2 and not result["summary"]["gate"]["complete"]   # one per arm, no retry


def test_repeated_rollouts_average_search_scores(store, project, fakes):
    source = make_run(store, project)
    seen = []

    def alternate(task_id, candidate):
        seen.append(task_id)
        if task_id == "v1" and candidate is None:  # the baseline solves v1 on its first attempt only
            good = seen.count("v1") % 2 == 1
            return {"score": float(good), "resolved": good, "fail_to_pass": [int(good), 1]}
        return {}

    fakes.override = alternate

    async def scenario():
        runtime = Runtime(store)
        service = ExperimentService(store, runtime)
        experiment = service.create(project["id"], role="code", run_ids=[source["id"]],
                                    limits={"max_metric_calls": 6, "max_iterations": 1, "rollouts_per_task": 2,
                                            "experiment_tokens": 6_000_000})
        result = await finish(service, experiment)
        await runtime.close()
        return result

    result = asyncio.run(scenario())
    assert result["status"] == "completed", result.get("error")
    v1 = [r for r in result["rollouts"] if r["task_id"] == "v1" and r["candidate_label"] == "baseline"]
    assert sorted(r["attempt"] for r in v1) == [0, 1]
    assert result["gepa"]["candidates"][0]["validation_score"] == 0.5
    # Held-out evaluation stays one rollout per arm and task.
    assert len([r for r in result["rollouts"] if r["phase"] == "heldout"]) == 4


def test_combine_attempts_and_budget_plan_with_repeats():
    from evo.evolve.service import budget_plan, combine_attempts

    done = {"outcome": "completed", "fail_to_pass": [1, 1], "pass_to_pass": [1, 1]}
    combined = combine_attempts([{**done, "score": 1.0, "resolved": True},
                                 {"outcome": "skipped", "score": 0.0},
                                 {**done, "score": 0.0, "resolved": False}])
    assert combined["score"] == 0.5 and combined["resolved"] is False and len(combined["attempts"]) == 3
    assert combine_attempts([{"outcome": "skipped", "score": 0.0}])["outcome"] == "skipped"
    plan = budget_plan({"rollout_tokens": 1000, "experiment_tokens": 100_000, "max_iterations": 2,
                        "reflection_minibatch_size": 1, "max_metric_calls": 14, "rollouts_per_task": 2},
                       {"train": ["a"], "validation": ["b", "c"], "heldout": ["d"]}, {})
    assert plan["search_rollouts"]["all_rejected"] == 2 * (2 + 2 * 2 * 1)
    assert plan["final_rollouts"] == 2   # held-out is not repeated
