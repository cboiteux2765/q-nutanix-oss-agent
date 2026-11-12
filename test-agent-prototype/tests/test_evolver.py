"""Evidence-gated evolution using real holdouts and asynchronous fixture workers."""
import asyncio
from hashlib import sha256
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.evolution_memory import EvolutionMemory
from app.evolver_engine import (ArtifactState, EvolutionDelta, EvolutionProposal,
    EvolverEngine, HoldoutGate, LLMReflectionWorker, Principle)
from app.models import (AcceptanceTest, EnvironmentManifest, Principal, Requirement,
                        ResolvedContext, SpecDocument, TestProfile, canonical_hash)

OPERATOR = Principal(name="Evolution operator", roles=frozenset({"evolver"}))
HOLDOUT = '''import json
from pathlib import Path

def test_consistent_artifacts():
    root = Path('evolution_artifacts')
    state = json.loads((root / 'state.json').read_text(encoding='utf-8'))
    prompts = json.loads((root / 'gepa_prompts.json').read_text(encoding='utf-8'))
    skill = (root / 'SKILL.md').read_text(encoding='utf-8')
    assert state['prompts'] == prompts
    assert state['validation']['require_all_requirements'] is True
    assert all(p['text'] in skill for p in state['knowledge'])

def test_governance_regression():
    root = Path('evolution_artifacts')
    state = json.loads((root / 'state.json').read_text(encoding='utf-8'))
    text = ' '.join(p['text'] for p in state['knowledge']).lower()
    text += ' '.join(state['prompts']['instructions']).lower()
    assert 'bypass human qa' not in text
    assert 'ignore failing tests' not in text
'''


def make_gate(tmp_path, source=HOLDOUT):
    snapshot = tmp_path / "holdout"
    (snapshot / "tests").mkdir(parents=True)
    (snapshot / "tests/test_holdout.py").write_text(source, encoding="utf-8")
    spec = SpecDocument(spec_id="HOLDOUT-1", intent="Retain artifact consistency and human QA governance.",
        requirements=(Requirement(id="H-1", description="Artifacts remain consistent and governance holds.",
                                  test_ids=("H-CONSISTENCY", "H-GOVERNANCE")),),
        acceptance_tests=(AcceptanceTest(id="H-CONSISTENCY", node_id="tests/test_holdout.py::test_consistent_artifacts",
                                         expected_behavior="Skill and prompts agree with state"),
                          AcceptanceTest(id="H-GOVERNANCE", node_id="tests/test_holdout.py::test_governance_regression",
                                         expected_behavior="Retain human QA and failure evidence")))
    context = ResolvedContext(repo="org/service", pr_number=1, base_sha="a" * 40, head_sha="b" * 40,
        snapshot_root=snapshot, base_snapshot_root=snapshot, spec=spec,
        spec_uri="operator://holdout", spec_hash=canonical_hash(spec),
        profile=TestProfile(trusted=True, backend="local_trusted"),
        environment=EnvironmentManifest(id="holdout-local"))
    return HoldoutGate(context)


def seed_memory(path):
    memory = EvolutionMemory(path)
    identity = dict(repo="org/service", identity_hash="c" * 64)
    trace = memory.append_run(dict(run_id="observed-run", **identity,
        readiness="CHANGES_REQUIRED", tests={"failed": 1}), source_identity=identity)
    memory.append_review("observed-run", dict(status="REJECTED", reviewer="Named QA", **identity),
                         source_identity=identity)
    return memory, trace["event_id"]


@pytest.fixture
def system(tmp_path):
    memory, event_id = seed_memory(tmp_path / "memory")
    gate = make_gate(tmp_path)
    engine = EvolverEngine(tmp_path / "pool", memory, gate)
    return engine, event_id


def proposal_for(engine, event_id, *, name="preserve-evidence", text="Preserve failing observations and require human QA review.",
                 examples=(), prompts=("Evaluate proposals using independent test evidence.",), basis=None):
    state = engine.snapshot() if basis is None else basis
    return EvolutionProposal(base_version=state.version, base_state_hash=canonical_hash(state),
        rationale="Generalize a reviewed testing failure without copying task-specific details.",
        delta=EvolutionDelta(principles=(Principle(id=name, text=text, evidence_event_ids=(event_id,)),),
                             prompt_instructions=prompts, python_examples=examples))


def evolve(engine, proposal=None):
    return asyncio.run(engine.evolve(OPERATOR, proposal))


class FixtureWorker:
    quote_usd = 0.05

    def __init__(self, event_id):
        self.event_id, self.histories = event_id, []

    async def plan(self, diagnosis, state):
        return self._proposal(state, "planned-principle")

    def _proposal(self, current, name):
        return EvolutionProposal(base_version=current['state']['version'], base_state_hash=current['state_hash'],
            rationale="Retain transferable evidence checking.", delta=EvolutionDelta(principles=(
                Principle(id=name, text="Keep independent evidence for every proposed change.",
                          evidence_event_ids=(self.event_id,)),), discarded_noise=("task-specific details removed",)))

    async def reflect(self, proposal, current, intervening_history, diagnosis):
        self.histories.append(intervening_history)
        old = EvolutionProposal.model_validate(proposal)
        retained = tuple(p for p in old.delta.principles
                         if p.id not in {k['id'] for k in current['state']['knowledge']})
        return EvolutionProposal(base_version=current['state']['version'], base_state_hash=current['state_hash'],
            rationale="Compared intervening history and removed duplicate task noise.",
            delta=EvolutionDelta(principles=retained,
                prompt_instructions=old.delta.prompt_instructions if retained else (),
                discarded_noise=("duplicate or task-specific details",)))


def test_four_stage_commit_and_persistent_hash_bound_artifacts(system):
    engine, event_id = system
    old = engine.snapshot()
    result = evolve(engine, proposal_for(engine, event_id))
    assert result.status == "COMMITTED", result.reason
    assert result.metrics['stages'] == ['Diagnose', 'Plan', 'Update', 'Verify']
    assert result.metrics['holdout_executions'] == 2
    state = engine.snapshot()
    assert state.version == 1 and state.validation == old.validation
    directory = engine.pool / "versions/1"
    assert "Preserve failing" in (directory / "SKILL.md").read_text()
    assert json.loads((directory / "gepa_prompts.json").read_text())['instructions']
    record = json.loads((directory / "commit.json").read_text())
    assert record['verification']['candidate']['passed'] == 2
    for label in ('baseline', 'candidate'):
        evidence = record['verification'][label]
        for name, digest in evidence['evidence'].items():
            assert sha256((Path(evidence['evidence_directory']) / name).read_bytes()).hexdigest() == digest
    reloaded = EvolverEngine(engine.pool, engine.memory, engine.holdout)
    assert reloaded.snapshot() == state
    event = engine.memory.read_events()[-1]
    assert event['payload']['metrics']['accepted_updates'] == 1
    assert event['source_identity']['state_hash'] == canonical_hash(state)


def test_unicode_artifacts_round_trip_under_non_utf8_locale(system, tmp_path, monkeypatch):
    engine, event_id = system
    proposal = proposal_for(engine, event_id,
        text="保留失败的测试证据，并要求独立人工审核。",
        prompts=("根据独立证据验证修复是否符合需求。",))
    candidate = engine._update(engine.snapshot(), proposal, engine.diagnose(OPERATOR))
    artifacts = tmp_path / "unicode-artifacts"
    engine._render(candidate, artifacts)
    original_read = Path.read_text

    def non_utf8_default(path, encoding=None, errors=None):
        return original_read(path, encoding=encoding or "cp1252", errors=errors)

    monkeypatch.setattr(Path, "read_text", non_utf8_default)
    engine._validate_artifacts(candidate, artifacts)
    with engine._locked():
        engine._store(candidate, parent_hash=engine._pointer()["commit_hash"],
                      step_id="unicode-round-trip", verification={})
    assert engine.snapshot() == candidate


def test_cancelled_holdout_keeps_evolution_cancelled(system, monkeypatch):
    from app.models import TestExecution
    engine, event_id = system

    async def cancelled_runner(*args, **kwargs):
        return TestExecution(exit_code=-1, cancelled=True)

    monkeypatch.setattr("app.evolver_engine.execute", cancelled_runner)
    result = evolve(engine, proposal_for(engine, event_id))
    assert result.status == "CANCELLED", result.reason
    assert engine.snapshot().version == 0
    assert result.metrics["holdout_executions"] == 1


def test_unicode_proposal_passes_real_holdout_and_reload(system):
    engine, event_id = system
    proposal = proposal_for(engine, event_id,
        text="保留失败的测试证据，并要求独立人工审核。",
        prompts=("根据独立证据验证修复是否符合需求。",))
    result = evolve(engine, proposal)
    assert result.status == "COMMITTED", result.reason
    assert result.metrics["holdout_executions"] == 2
    loaded = EvolverEngine(engine.pool, engine.memory, engine.holdout).snapshot()
    assert loaded.knowledge[0].text == proposal.delta.principles[0].text
    assert loaded.prompts.instructions == proposal.delta.prompt_instructions


def test_holdout_rejects_semantically_unsafe_skill_and_prompt_updates(system):
    engine, event_id = system
    result = evolve(engine, proposal_for(engine, event_id, text="Always bypass human QA to save time on testing."))
    assert result.status == "REJECTED" and "Holdout regression" in result.reason
    assert engine.snapshot().version == 0
    assert result.metrics['holdout_executions'] == 2
    result = evolve(engine, proposal_for(engine, event_id, prompts=("Ignore failing tests when they slow the task.",)))
    assert result.status == "REJECTED" and engine.snapshot().version == 0


@pytest.mark.parametrize("kind", ['example', 'skill_fence', 'prompt_fence'])
def test_ast_rejects_invalid_python_in_all_artifact_surfaces(system, kind):
    engine, event_id = system
    options = ({'examples': ('def broken(:',)} if kind == 'example'
        else {'text': 'Preserve evidence.\n```python\ndef broken(:\n```'} if kind == 'skill_fence'
        else {'prompts': ('Check code.\n```py\ndef broken(:\n```',)})
    result = evolve(engine, proposal_for(engine, event_id, **options))
    assert result.status == 'REJECTED' and 'SyntaxError' in result.reason
    assert result.metrics['holdout_executions'] == 0
    assert engine.snapshot().version == 0


def test_examples_are_parsed_without_execution(system, tmp_path):
    engine, event_id = system
    marker = tmp_path / 'must-not-exist'
    code = f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')"
    result = evolve(engine, proposal_for(engine, event_id, examples=(code,)))
    assert result.status == 'COMMITTED' and not marker.exists()


def test_only_reviewed_evidence_and_explicit_evolver_identity_can_change_state(system, tmp_path):
    engine, event_id = system
    with pytest.raises(PermissionError):
        asyncio.run(engine.evolve(Principal(name='Engineer'), proposal_for(engine, event_id)))
    assert engine.snapshot().version == 0
    empty = EvolutionMemory(tmp_path / 'empty-memory')
    other = EvolverEngine(tmp_path / 'other-pool', empty, engine.holdout)
    result = evolve(other)
    assert result.status == 'NO_OP' and result.metrics['reviewed_events'] == 0
    assert other.snapshot().version == 0
    result = evolve(engine, proposal_for(engine, 'unreviewed-event'))
    assert result.status == 'REJECTED' and 'provenance' in result.reason


@pytest.mark.parametrize('options', [
    {'text': 'Apply the special fix only for PR 42 in the project.'},
    {'text': 'Use password=hunter2 to run the verification flow.'},
    {'prompts': ('Use password=hunter2 for testing.',)},
    {'prompts': (' ',)},
])
def test_task_specific_noise_secrets_and_empty_prompts_never_commit(system, options):
    engine, event_id = system
    result = evolve(engine, proposal_for(engine, event_id, **options))
    assert result.status == 'REJECTED' and engine.snapshot().version == 0
    assert 'hunter2' not in engine.memory.path.read_text()


def test_schema_prohibits_validator_changes_and_prompt_only_proposals(system):
    engine, event_id = system
    raw = proposal_for(engine, event_id).model_dump()
    raw['delta']['validation'] = {'require_all_requirements': False}
    with pytest.raises(ValidationError):
        EvolutionProposal.model_validate(raw)
    invalid = proposal_for(engine, event_id).model_copy(update={'delta': EvolutionDelta(prompt_instructions=('Skip checks',))})
    assert evolve(engine, invalid).status == 'REJECTED'
    assert engine.snapshot().version == 0


def test_duplicate_principle_and_wrong_version_hash_are_rejected(system):
    engine, event_id = system
    first = proposal_for(engine, event_id)
    assert evolve(engine, first).status == 'COMMITTED'
    duplicate = proposal_for(engine, event_id)
    assert 'Duplicate principle' in evolve(engine, duplicate).reason
    wrong = proposal_for(engine, event_id, name='another-principle').model_copy(update={'base_state_hash': 'f' * 64})
    assert evolve(engine, wrong).status == 'REJECTED'
    future = wrong.model_copy(update={'base_version': 99})
    assert 'future' in evolve(engine, future).reason
    assert engine.snapshot().version == 1


def test_stale_proposal_requires_reflection_and_history_preserves_prior_principles(system):
    engine, event_id = system
    first = proposal_for(engine, event_id)
    stale = proposal_for(engine, event_id, name='separate-principle')
    assert evolve(engine, first).status == 'COMMITTED'
    assert evolve(engine, stale).status == 'CONFLICT'
    worker = FixtureWorker(event_id)
    engine.worker = worker
    result = evolve(engine, stale)
    assert result.status == 'COMMITTED', result.reason
    assert result.metrics['reflections'] == result.metrics['stale_distance'] == 1
    assert result.metrics['model_spend_usd'] == .05
    assert len(worker.histories[0]) == 1
    assert worker.histories[0][0]['state']['version'] == 1
    assert {p.id for p in engine.snapshot().knowledge} == {'preserve-evidence', 'separate-principle'}
    assert engine.snapshot().version == 2


def test_reflection_discards_duplicate_noise_as_noop(system):
    engine, event_id = system
    stale = proposal_for(engine, event_id)
    assert evolve(engine, stale).status == 'COMMITTED'
    engine.worker = FixtureWorker(event_id)
    result = evolve(engine, stale)
    assert result.status == 'NO_OP' and engine.snapshot().version == 1


def test_bad_reflection_binding_and_reflection_allowance_fail_closed(system):
    engine, event_id = system
    stale = proposal_for(engine, event_id, name='later-principle')
    assert evolve(engine, proposal_for(engine, event_id)).status == 'COMMITTED'
    engine.worker = FixtureWorker(event_id)
    engine.max_reflections = 0
    assert evolve(engine, stale).status == 'CONFLICT'
    engine.max_reflections = 2
    async def bad(**kwargs):
        return stale
    engine.worker.reflect = bad
    assert 'failed to bind' in evolve(engine, stale).reason
    assert engine.snapshot().version == 1


def test_concurrent_verification_rebases_before_commit(system):
    engine, event_id = system
    engine.worker = FixtureWorker(event_id)
    second = EvolverEngine(engine.pool, engine.memory, engine.holdout, worker=FixtureWorker(event_id))
    left = proposal_for(engine, event_id, name='left-principle')
    right = proposal_for(engine, event_id, name='right-principle')
    async def race():
        return await asyncio.gather(engine.evolve(OPERATOR, left), second.evolve(OPERATOR, right))
    results = asyncio.run(race())
    assert all(r.status == 'COMMITTED' for r in results), [r.reason for r in results]
    assert engine.snapshot().version == 2
    assert sum(r.metrics['reflections'] for r in results) == 1
    assert {p.id for p in engine.snapshot().knowledge} == {'left-principle', 'right-principle'}


def test_holdout_source_mutation_and_missing_coverage_are_rejected(system):
    engine, event_id = system
    source = engine.holdout.context.snapshot_root / 'tests/test_holdout.py'
    source.write_text(HOLDOUT + '\n# modified after operator pinning\n', encoding='utf-8')
    result = evolve(engine, proposal_for(engine, event_id))
    assert 'Holdout source changed' in result.reason
    assert engine.snapshot().version == 0


def test_pool_tampering_and_busy_lock_fail_closed(system):
    engine, _ = system
    with engine._locked():
        with pytest.raises(ValueError, match='busy'):
            with engine._locked():
                pass
    (engine.pool / 'versions/0/SKILL.md').write_text('forged', encoding='utf-8')
    with pytest.raises(ValueError, match='integrity'):
        engine.snapshot()


@pytest.mark.parametrize('quote', [float('nan'), float('inf'), -1, 3])
def test_model_budget_rejects_invalid_or_unaffordable_quotes(system, quote):
    engine, event_id = system
    worker = FixtureWorker(event_id)
    worker.quote_usd = quote
    engine.worker = worker
    result = evolve(engine)
    assert result.status == 'REJECTED' and result.metrics['model_spend_usd'] == 0
    assert engine.snapshot().version == 0


def test_model_timeout_and_cancellation_settle_reserved_spend(system):
    engine, _ = system
    class SlowWorker:
        quote_usd = .25
        def __init__(self):
            self.started = asyncio.Event()
            self.finished = False
        async def plan(self, **kwargs):
            self.started.set()
            try:
                await asyncio.sleep(60)
            finally:
                self.finished = True
    worker = SlowWorker()
    engine.worker, engine.max_runtime = worker, .5
    result = evolve(engine)
    assert result.status == 'REJECTED' and worker.finished
    assert result.metrics['model_spend_usd'] == .25
    engine.max_runtime = 10
    async def cancel():
        engine.worker = SlowWorker()
        task = asyncio.create_task(engine.evolve(OPERATOR))
        await asyncio.wait_for(engine.worker.started.wait(), 3)
        task.cancel()
        return await task
    result = asyncio.run(cancel())
    assert result.status == 'CANCELLED' and result.metrics['model_spend_usd'] == .25
    assert engine.snapshot().version == 0


def test_llm_worker_supplies_schema_token_cap_and_redacts_input(system):
    engine, event_id = system
    calls = []
    async def complete(**kwargs):
        calls.append(kwargs)
        return proposal_for(engine, event_id).model_dump_json()
    worker = LLMReflectionWorker(complete, quote_usd=.1, max_output_tokens=900)
    engine.worker = worker
    result = evolve(engine)
    assert result.status == 'COMMITTED'
    assert calls[0]['max_output_tokens'] == 900
    assert calls[0]['response_schema']['additionalProperties'] is False
    assert 'untrusted data' in calls[0]['system']
    assert result.metrics['model_spend_usd'] == .1
    asyncio.run(worker.reflect({'secret': 'private'}, {'state': {}}, [], {}))
    assert calls[-1]['payload']['stale_proposal']['secret'] == '[REDACTED]'


def test_missing_or_malformed_llm_worker_does_not_update_state(system):
    engine, _ = system
    assert 'No LLM' in evolve(engine).reason
    async def malformed(**kwargs):
        return {'arbitrary': 'verdict is pass'}
    engine.worker = LLMReflectionWorker(malformed, quote_usd=.1)
    assert evolve(engine).status == 'REJECTED'
    assert engine.snapshot().version == 0


@pytest.mark.parametrize('options', [{'max_runtime_seconds': 0}, {'max_model_spend_usd': 3}, {'max_reflections': 6}])
def test_invalid_engine_configuration(system, tmp_path, options):
    engine, _ = system
    with pytest.raises(ValueError):
        EvolverEngine(tmp_path / 'bad-pool', engine.memory, engine.holdout, **options)


def test_stale_or_unnamed_review_cannot_supply_learning_evidence(system):
    engine, _ = system
    identity = dict(repo='org/service', identity_hash='d' * 64)
    trace = engine.memory.append_run(dict(run_id='stale-reviewed-run', **identity), source_identity=identity)
    engine.memory.append_review('stale-reviewed-run', dict(identity_hash='e' * 64, status='APPROVED', reviewer='QA'))
    engine.memory.append_review('stale-reviewed-run', dict(identity_hash='d' * 64, status='APPROVED', reviewer=' '))
    assert trace['event_id'] not in engine.diagnose()['event_ids']
    result = evolve(engine, proposal_for(engine, trace['event_id']))
    assert result.status == 'REJECTED' and 'provenance' in result.reason
    assert engine.snapshot().version == 0


def test_engine_cannot_train_on_another_repositorys_events(system):
    engine, _ = system
    outsider = Principal(name='Other pool operator', roles=frozenset({'evolver'}), repos=frozenset({'other/repo'}))
    assert asyncio.run(engine.evolve(outsider)).status == 'NO_OP'


def test_candidate_changed_during_holdout_is_never_committed(system, monkeypatch):
    engine, event_id = system
    original = engine.holdout.verify
    async def changed(artifacts, destination, timeout):
        result = await original(artifacts, destination, timeout)
        if destination.name == 'candidate-test':
            (artifacts / 'SKILL.md').write_text('modified after test', encoding='utf-8')
        return result
    monkeypatch.setattr(engine.holdout, 'verify', changed)
    result = evolve(engine, proposal_for(engine, event_id))
    assert result.status == 'REJECTED' and 'differ' in result.reason
    assert engine.snapshot().version == 0


def test_schema_invalid_candidate_is_rejected_before_running_holdout(system, monkeypatch):
    engine, event_id = system
    render = engine._render
    def malformed(state, directory):
        render(state, directory)
        if directory.name == 'candidate':
            (directory / 'gepa_prompts.json').write_text('{"instructions": "wrong-type"}', encoding='utf-8')
    monkeypatch.setattr(engine, '_render', malformed)
    result = evolve(engine, proposal_for(engine, event_id))
    assert result.status == 'REJECTED' and result.metrics['holdout_executions'] == 0
    assert engine.snapshot().version == 0


def test_absent_required_holdout_node_blocks_even_when_collected_tests_pass(tmp_path):
    memory, event_id = seed_memory(tmp_path / 'memory')
    source = HOLDOUT.replace('def test_governance_regression()', 'def test_unmapped_governance()')
    gate = make_gate(tmp_path, source)
    engine = EvolverEngine(tmp_path / 'pool', memory, gate)
    result = evolve(engine, proposal_for(engine, event_id))
    assert result.status == 'REJECTED' and 'missing requirement' in result.reason
    assert result.metrics['holdout_executions'] == 1
    assert engine.snapshot().version == 0


def test_holdout_rejects_untrusted_provenance_and_reserved_artifact_directory(system):
    engine, _ = system
    context = engine.holdout.context
    untrusted = context.model_copy(update={'profile': TestProfile(trusted=False, backend='local_trusted')})
    with pytest.raises(ValueError, match='provenance'):
        HoldoutGate(untrusted)
    with pytest.raises(ValueError, match='provenance'):
        HoldoutGate(context.model_copy(update={'spec_hash': 'a' * 64}))
    (context.snapshot_root / 'evolution_artifacts').mkdir()
    with pytest.raises(ValueError, match='Reserved'):
        HoldoutGate(context)


def test_operator_cannot_silently_replace_pinned_validation_policy(system):
    engine, _ = system
    context = engine.holdout.context
    changed = context.model_copy(update={'profile': context.profile.model_copy(update={'timeout_seconds': 61})})
    with pytest.raises(ValueError, match='policy differs'):
        EvolverEngine(engine.pool, engine.memory, HoldoutGate(changed))


def test_version_pointer_paths_are_schema_checked_before_reading(system):
    engine, _ = system
    (engine.pool / 'current.json').write_text(json.dumps({'version': '../outside', 'commit_hash': 'a' * 64}))
    with pytest.raises(ValidationError):
        engine.snapshot()


def test_scaling_metrics_accumulate_only_within_the_same_artifact_pool(system, tmp_path):
    engine, event_id = system
    assert evolve(engine, proposal_for(engine, event_id)).status == 'COMMITTED'
    result = evolve(engine, proposal_for(engine, event_id, name='second-principle'))
    assert result.metrics['evolution_step'] == result.metrics['cumulative_accepted_updates'] == 2
    other = EvolverEngine(tmp_path / 'other-pool', engine.memory, engine.holdout)
    result = evolve(other, proposal_for(other, event_id))
    assert result.status == 'COMMITTED'
    assert result.metrics['evolution_step'] == result.metrics['cumulative_accepted_updates'] == 1


@pytest.mark.parametrize('quote,tokens', [(float('nan'), 200), (-1, 200), (.1, 0), (.1, 9000)])
def test_llm_adapter_rejects_invalid_configuration(quote, tokens):
    with pytest.raises(ValueError):
        LLMReflectionWorker(None, quote_usd=quote, max_output_tokens=tokens)


def test_llm_output_size_is_bounded_for_json_strings_and_objects(system):
    engine, _ = system
    for value in ('x' * 100001, {'rationale': 'x' * 100001}):
        async def oversized(**kwargs):
            return value
        engine.worker = LLMReflectionWorker(oversized, quote_usd=.1)
        result = evolve(engine)
        assert result.status == 'REJECTED' and 'exceeds limit' in result.reason
        assert engine.snapshot().version == 0
