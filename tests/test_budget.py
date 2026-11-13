"""Strict token budgets: retries, unknown usage, restarts mid-rollout, cache writes, price tier."""

import asyncio
import json

import pytest

from evo.budget import PriceTierExceeded, confirm_price_tier
from evo.evolve import service as service_module
from evo.evolve.service import ExperimentService
from evo.providers import ModelProvider, ModelResponse, ProviderError
from evo.runtime import Runtime
from evo.store import Store

from .conftest import FakeProvider, reply, settle
from .test_evolution import TASKS, fakes, make_run  # noqa: F401  (fixture)


def run_async(coro):
    return asyncio.run(coro)


def usage_of(store, run_id):
    return store.get("runs", run_id)["token_usage"]


def test_a_retry_after_a_possibly_billed_failure_is_charged_and_admitted_again(store, project, fake_provider):
    """The reviewer's reproduction: a response is lost, the retry succeeds. The lost attempt keeps its
    reservation as an unresolved charge instead of vanishing."""
    reservations = []

    def lost_then_ok(messages):
        run = store.list("runs")[0]
        reservations.append(run["reserved_tokens"])
        FakeProvider.current.before_retry(True)  # what ModelProvider does before retrying
        reservations.append(store.get("runs", run["id"])["reserved_tokens"])
        return reply("Done.", tokens=550)

    original_init = FakeProvider.__init__

    def init(self, settings, api_key, transport=None):
        original_init(self, settings, api_key, transport)
        FakeProvider.current = self

    fake_provider.script = [lost_then_ok]
    FakeProvider.__init__ = init
    try:
        async def scenario():
            runtime = Runtime(store)
            run = await runtime.start_run(project["id"], "Go")
            await settle(runtime)
            await runtime.close()
            return run["id"]

        run_id = run_async(scenario())
    finally:
        FakeProvider.__init__ = original_init
    usage = usage_of(store, run_id)
    first, second = reservations
    assert first > 0 and second == first  # a fresh reservation for the retry
    assert usage["input"] + usage["output"] == 1100
    assert usage["unresolved"] == first  # the lost attempt, charged at its worst case
    assert store.get("runs", run_id)["reserved_tokens"] == 0


def test_unknown_usage_and_uncertain_failures_are_never_counted_as_zero(store, project, fake_provider):
    def no_usage(messages):
        return ModelResponse("Done.", usage={}, finish_reason="stop")

    def dropped(messages):
        raise ProviderError("Provider connection failed: ReadError", billing_uncertain=True)

    def rejected(messages):
        raise ProviderError("Provider returned HTTP 400: bad request")

    results = {}
    for name, step in (("no usage", no_usage), ("dropped", dropped), ("rejected", rejected)):
        fake_provider.script = [step]

        async def scenario():
            runtime = Runtime(store)
            run = await runtime.start_run(project["id"], name)
            await settle(runtime)
            await runtime.close()
            return run["id"]

        results[name] = usage_of(store, run_async(scenario()))
    assert results["no usage"]["unresolved"] > 0
    assert results["dropped"]["unresolved"] > 0
    assert results["rejected"]["unresolved"] == 0  # an HTTP error response was not processed


def test_reservations_left_by_a_crash_become_unresolved_charges(store, project):
    run = store.put("runs", "r1", {"project_id": project["id"], "status": "running", "reserved_tokens": 5000,
                                   "token_usage": {"input": 10, "output": 5}})
    runtime = Runtime(store)
    runtime.reconcile_reservations()
    saved = store.get("runs", run["id"])
    assert saved["reserved_tokens"] == 0 and saved["token_usage"]["unresolved"] == 5000
    runtime.reconcile_reservations()  # idempotent
    assert store.get("runs", run["id"])["token_usage"]["unresolved"] == 5000


def test_a_restart_mid_rollout_settles_recorded_usage_once(store, project, fakes, monkeypatch, tmp_path):
    """The reviewer's reproduction: usage recorded inside a rollout must reach the experiment even
    when the rollout is cancelled by a shutdown, and settling again must not double-charge."""
    source = make_run(store, project)
    started = asyncio.Event()

    async def hanging_rollout(*, root, task, rollout_id, **_):
        rollout_store = Store(root / rollout_id / "data")
        rollout_store.put("runs", "eval", {"token_usage": {"input": 2000, "output": 10, "unresolved": 0},
                                           "reserved_tokens": 0})
        rollout_store.close()
        started.set()
        await asyncio.Event().wait()  # cancelled by shutdown

    monkeypatch.setattr(service_module, "run_rollout", hanging_rollout)

    async def scenario():
        runtime = Runtime(store)
        service = ExperimentService(store, runtime)
        experiment = service.create(project["id"], role="code", run_ids=[source["id"]])
        await started.wait()
        await service.close()  # server shutdown
        await runtime.close()
        restarted = ExperimentService(store, Runtime(store))
        restarted.recover()
        restarted.recover()
        return experiment["id"]

    experiment_id = run_async(scenario())
    experiment = store.get("evolution_experiments", experiment_id)
    assert experiment["spent"]["tokens"] == 2010
    assert experiment["spent"].get("reserved_search", 0) == 0
    rollouts = store.list("evolution_rollouts", experiment_id=experiment_id)
    assert len(rollouts) == 1 and rollouts[0]["settled"] and rollouts[0]["outcome"] == "interrupted"
    assert experiment["status"] == "interrupted"


def test_an_unreadable_interrupted_rollout_is_charged_its_full_reservation(store, project, fakes):
    source = make_run(store, project)

    async def scenario():
        runtime = Runtime(store)
        service = ExperimentService(store, runtime)
        experiment = service.create(project["id"], role="code", run_ids=[source["id"]],
                                    limits={"max_metric_calls": 3, "max_iterations": 1})
        await service.jobs[experiment["id"]]
        before = store.get("evolution_experiments", experiment["id"])["spent"]["tokens"]
        # A crash left a rollout that never wrote its store.
        store.put("evolution_rollouts", "lost", {"experiment_id": experiment["id"], "task_id": "t1",
                                                 "outcome": "running", "settled": False, "pool": "search",
                                                 "reserved_tokens": 400_000, "data_dir": "Z:/missing"})
        service._reserve(experiment["id"], "reserved_search", 400_000)
        service.reconcile(experiment["id"])
        await runtime.close()
        return experiment["id"], before

    experiment_id, before = run_async(scenario())
    spent = store.get("evolution_experiments", experiment_id)["spent"]
    assert spent["tokens"] == before + 400_000 and spent["unresolved_tokens"] == 400_000
    assert spent["reserved_search"] == 0


def test_cache_writes_are_priced_separately():
    provider = ModelProvider({"api_style": "responses", "base_url": "http://127.0.0.1:9/v1", "model": "m",
                              "input_price": 1.0, "cached_input_price": 0.05, "output_price": 5.0,
                              "cache_write_price": 1.25}, "")
    response = provider._decode({"status": "completed", "output": [], "usage": {
        "input_tokens": 1_000_000, "output_tokens": 0,
        "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 1_000_000}}}, True)
    assert response.usage["cache_write"] == 1_000_000
    assert response.usage["cost_usd"] == pytest.approx(1.25)
    asyncio.run(provider.close())


def test_requests_that_could_cross_the_price_tier_are_counted_or_refused():
    settings = {"model": "gpt-6.1-terra", "base_url": "https://api.openai.com/v1", "context_tokens": 272_000}
    messages = [{"role": "user", "content": "x"}]

    class Client:
        def __init__(self, count):
            self.count = count

        async def count_input_tokens(self, messages, tools=None):
            return self.count

    async def check(client, bound, overrides=None):
        await confirm_price_tier(client, {**settings, **(overrides or {})}, messages, None, bound)

    asyncio.run(check(Client(None), 200_000))           # the byte bound alone settles it
    asyncio.run(check(Client(150_000), 900_000))        # counted below the threshold
    with pytest.raises(PriceTierExceeded, match="could not be counted"):
        asyncio.run(check(Client(None), 900_000))
    with pytest.raises(PriceTierExceeded, match="above"):
        asyncio.run(check(Client(300_000), 900_000))
    # A long-context input budget is an explicit choice of the higher tier.
    asyncio.run(check(Client(None), 900_000, {"context_tokens": 600_000}))
    json.dumps(settings)


def test_a_request_cancelled_in_flight_is_charged_its_reservation(store, project, fake_provider):
    started = asyncio.Event()

    async def hang(messages):
        started.set()
        await asyncio.Event().wait()

    class Hanging(FakeProvider):
        async def complete(self, messages, tools=None, on_delta=None):
            return await hang(messages)

    from evo import runtime as runtime_module
    runtime_module.ModelProvider = Hanging
    try:
        async def scenario():
            runtime = Runtime(store)
            run = await runtime.start_run(project["id"], "Go")
            await started.wait()
            await runtime.close()  # shutdown while the provider is working
            return run["id"]

        run_id = run_async(scenario())
    finally:
        runtime_module.ModelProvider = FakeProvider
    run = store.get("runs", run_id)
    assert run["token_usage"]["unresolved"] > 0 and run["reserved_tokens"] == 0


# ---------------------------------------------------------------- crash windows and classification
class Crash(Exception):
    """Stands in for the process stopping before a transaction commits."""


def test_a_crash_while_settling_a_call_keeps_its_reservation_for_recovery(store, project, fake_provider,
                                                                          monkeypatch):
    """The reviewer's fault injection: a crash between releasing the reservation and recording the
    usage left neither. Settlement is now one transaction, so the reservation survives."""
    fake_provider.script = [reply("Done.", tokens=1005)]
    original = Store._put

    def crash_on_agent_usage(self, db, collection, id, data):
        if collection == "agents" and "token_usage" in data:
            raise Crash
        return original(self, db, collection, id, data)

    monkeypatch.setattr(Store, "_put", crash_on_agent_usage)

    async def scenario():
        runtime = Runtime(store)
        run = await runtime.start_run(project["id"], "Go")
        await settle(runtime)
        await runtime.close()
        return run["id"]

    run_id = run_async(scenario())
    monkeypatch.setattr(Store, "_put", original)
    run = store.get("runs", run_id)
    assert run["reserved_tokens"] > 0  # nothing was half-written
    assert run["token_usage"]["input"] + run["token_usage"]["output"] == 0
    reserved = run["reserved_tokens"]
    Runtime(store).reconcile_reservations()  # what the next start does
    usage = store.get("runs", run_id)["token_usage"]
    assert usage["unresolved"] == reserved >= 2010  # at least the 2,010 tokens the provider reported


def test_a_crash_while_settling_a_rollout_cannot_charge_it_twice(store, project, fakes, monkeypatch):
    """The reviewer's fault injection: charged, then crashed before the rollout was marked settled,
    then charged again by recovery (2,010 became 4,020)."""
    source = make_run(store, project)
    original = Store._put
    armed = {"on": True}

    def crash_marking_settled(self, db, collection, id, data):
        if armed["on"] and collection == "evolution_rollouts" and data.get("settled") is True:
            armed["on"] = False
            raise Crash
        return original(self, db, collection, id, data)

    monkeypatch.setattr(Store, "_put", crash_marking_settled)
    fakes.override = lambda task_id, candidate: {"tokens": 2010}

    async def scenario():
        runtime = Runtime(store)
        service = ExperimentService(store, runtime)
        experiment = service.create(project["id"], role="code", run_ids=[source["id"]],
                                    limits={"max_metric_calls": 3, "max_iterations": 1})
        await service.jobs[experiment["id"]]  # fails at the injected crash
        restarted = ExperimentService(store, Runtime(store))
        restarted.recover()
        restarted.recover()
        await runtime.close()
        return experiment["id"]

    experiment_id = run_async(scenario())
    spent = store.get("evolution_experiments", experiment_id)["spent"]
    rollouts = store.list("evolution_rollouts", experiment_id=experiment_id)
    assert len(rollouts) == 1 and rollouts[0]["settled"]
    # Recovery found no usage on disk for the fake rollout, so it charged the full reservation once;
    # what matters is that the rolled-back charge was not applied in addition.
    assert spent["tokens"] == rollouts[0]["tokens"] and spent["reserved_search"] == 0


def test_server_and_gateway_errors_are_treated_as_possibly_billed():
    import httpx

    def provider_with(status):
        replies = iter([httpx.Response(status, json={"error": {"message": "upstream"}}),
                        httpx.Response(200, json={"status": "completed", "output": [], "usage": {
                            "input_tokens": 1000, "output_tokens": 100}})])
        provider = ModelProvider({"api_style": "responses", "base_url": "http://127.0.0.1:9/v1", "model": "m"}, "",
                                 transport=httpx.MockTransport(lambda request: next(replies)))
        seen = []
        provider.before_retry = seen.append
        return provider, seen

    async def run(status):
        provider, seen = provider_with(status)
        try:
            response = await provider.complete([{"role": "user", "content": "hi"}])
        finally:
            await provider.close()
        return seen, response.usage

    for status in (500, 502, 503, 504):
        seen, usage = asyncio.run(run(status))
        assert seen == [True], status
        assert usage["input"] + usage["output"] == 1100
    seen, _ = asyncio.run(run(429))  # a rate-limit rejection happens before the model runs
    assert seen == [False]


def test_incomplete_usage_keeps_a_conservative_charge(store, project, fake_provider):
    provider = ModelProvider({"api_style": "responses", "base_url": "http://127.0.0.1:9/v1", "model": "m"}, "")
    decoded = provider._decode({"status": "completed", "output": [], "usage": {"input_tokens": 100}}, True)
    asyncio.run(provider.close())
    assert decoded.usage["reported"] is False and decoded.usage["input"] == 100
    fake_provider.script = [ModelResponse("Done.", usage=decoded.usage, finish_reason="stop")]

    async def scenario():
        runtime = Runtime(store)
        run = await runtime.start_run(project["id"], "Go")
        await settle(runtime)
        await runtime.close()
        return run["id"]

    usage = usage_of(store, run_async(scenario()))
    assert usage["input"] == 100 and usage["output"] == 0
    assert usage["unresolved"] > 1000  # the rest of the reservation, not zero


def test_a_malformed_stream_after_submission_is_charged(store, project, monkeypatch):
    """The reviewer's reproduction, through the real provider: HTTP 200, then a stream event that
    breaks parsing (AttributeError). The submitted request keeps its reservation as unresolved."""
    import httpx
    from evo import runtime as runtime_module

    store.put("settings", "provider", {"api_style": "responses"})

    def transport(request):
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=b'data: {"type": "response.created"}\n\ndata: [1, 2]\n\n')

    class Provider(ModelProvider):
        def __init__(self, settings, api_key, transport_=None):
            super().__init__(settings, api_key, transport=httpx.MockTransport(transport))

    monkeypatch.setattr(runtime_module, "ModelProvider", Provider)

    async def scenario():
        runtime = Runtime(store)
        run = await runtime.start_run(project["id"], "Go")
        await settle(runtime)
        await runtime.close()
        return run["id"]

    run = store.get("runs", run_async(scenario()))
    assert run["status"] == "failed"
    assert run["token_usage"]["unresolved"] > 0 and run["reserved_tokens"] == 0


def test_an_unexpected_reflection_failure_is_charged(store, project, fakes, monkeypatch):
    from .test_evolution import REAL_REFLECT

    source = make_run(store, project)

    class Broken:
        def __init__(self, settings, key):
            self.before_retry = None

        async def complete(self, messages, tools=None, on_delta=None):
            raise RuntimeError("callback failed after submission")

        async def close(self):
            pass

    monkeypatch.setattr(service_module, "ModelProvider", Broken)

    async def scenario():
        runtime = Runtime(store)
        service = ExperimentService(store, runtime)
        experiment = service.create(project["id"], role="code", run_ids=[source["id"]],
                                    limits={"max_metric_calls": 3, "max_iterations": 1})
        await service.jobs[experiment["id"]]
        with pytest.raises(RuntimeError):
            await REAL_REFLECT(service, experiment["id"], [{"role": "user", "content": "reflect"}])
        await runtime.close()
        return experiment["id"]

    spent = store.get("evolution_experiments", run_async(scenario()))["spent"]
    assert spent["unresolved_tokens"] > 0 and spent.get("reserved_reflection", 0) == 0


# ---------------------------------------------------------------- hard dollar limits
PRICES = {"input_price": 1.0, "cached_input_price": 0.05, "cache_write_price": 1.25, "output_price": 5.0}


def test_a_run_refuses_a_call_whose_worst_case_cost_does_not_fit(store, project, fake_provider):
    store.put("settings", "provider", {**PRICES, "max_output_tokens": 1000})
    fake_provider.script = [reply("never")]

    async def scenario():
        runtime = Runtime(store)
        run_id = (await runtime.start_run(project["id"], "Go"))["id"]
        # A run with a 1-cent limit: the first call's worst case (input at $1.25/M plus 1,000 output
        # tokens at $5/M) is above it, so nothing is sent.
        store.put("runs", run_id, {"settings_snapshot": store.get("runs", run_id)["settings_snapshot"]
                                   | {"max_run_cost_usd": 0.001}})
        await settle(runtime)
        await runtime.close()
        return run_id

    run = store.get("runs", run_async(scenario()))
    assert run["status"] == "failed" and "spending limit" in run["error"]


def test_the_experiment_never_commits_more_dollars_than_its_limit(store, project, fakes):
    """$20 limit: rollouts get the remaining dollars of their pool as a hard allowance, the held-out
    reserve is kept, and a limit too small for the reserve is refused up front."""
    source = make_run(store, project)
    store.put("settings", "provider", PRICES)
    fakes.override = lambda task_id, candidate: {"cost_usd": 0.12}

    async def scenario():
        runtime = Runtime(store)
        service = ExperimentService(store, runtime)
        with pytest.raises(ValueError, match="cannot cover the final paired evaluation"):
            service.create(project["id"], role="code", run_ids=[source["id"]],
                           limits={"max_cost_usd": 1.0, "rollout_cost_usd": 0.5})
        experiment = service.create(project["id"], role="code", run_ids=[source["id"]],
                                    limits={"max_cost_usd": 20.0, "rollout_cost_usd": 0.5,
                                            "max_metric_calls": 6, "max_iterations": 2})
        await service.jobs[experiment["id"]]
        await runtime.close()
        return experiment["id"]

    experiment = store.get("evolution_experiments", run_async(scenario()))
    budget, spent = experiment["budget"], experiment["spent"]
    assert (budget["total_usd"], budget["heldout_reserve_usd"]) == (20.0, 2.0)
    assert spent["cost_usd"] <= 20.0 and spent["reserved_cost_search"] == spent["reserved_cost_heldout"] == 0
    allowances = [limits["max_run_cost_usd"] for limits in fakes.limits]
    assert allowances and all(0 < a <= 2.0 for a in allowances)  # min(pool left, 400k tokens at $5/M)
