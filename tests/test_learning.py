import asyncio
import copy
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace

from agent.learning import (Workspace, Orchestrator, DemoBackend, PrimeBackend,
                            evaluate, validate_scenarios)
from agent.spec_tools import SpecTools

ROOT = Path(__file__).resolve().parents[1]


class LearningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.ws = Workspace(self.temp.name)
        self.evidence = json.loads((ROOT / 'examples/learning-evidence.json').read_text())
        self.repo = self.evidence['input']['repo']

    def run_workflow(self, backend=None):
        agent = Orchestrator(self.ws, backend or DemoBackend())
        ident = agent.create(self.evidence, self.repo)
        asyncio.run(agent.execute(ident, self.evidence, self.repo))
        return self.ws.run(ident)

    def test_complete_loop_feedback_promotion_rollback_and_reload(self):
        run = self.run_workflow()
        self.assertEqual(run['status'], 'needs_review')
        self.assertEqual(len(run['agents']), 6)
        self.assertEqual(run['handoff']['status'], 'DRAFT')
        self.assertEqual(self.ws.data['active_version'], 'v1')
        self.ws.feedback(run['id'], 'Preserve whitespace behavior.', 'Product owner')
        self.ws.decision(run['id'], 'promote', 'Product owner')
        self.assertEqual(self.ws.data['active_version'], 'v2')
        second = self.run_workflow()
        prompt = self.ws.data['versions'][-1]['prompt']
        self.assertIn('Preserve whitespace behavior.', prompt)
        self.assertEqual(second['baseline_version'], 'v2')
        self.ws.rollback('v1', 'Product owner')
        self.assertEqual(Workspace(self.temp.name).data['active_version'], 'v1')
        with self.assertRaises(ValueError):
            self.ws.decision(second['id'], 'promote', 'Product owner')

    def test_regressions_block_promotion(self):
        run = self.run_workflow()
        judgment = {'results': copy.deepcopy(run['evaluation']['results'])}
        judgment['results'][0]['candidate'] = 'fail'
        report = evaluate(judgment, run['scenarios'], run['draft'], run['draft'])
        self.assertFalse(report['eligible'])
        self.assertEqual(report['regressions'], ['SC-1'])
        run['evaluation'] = report
        with self.assertRaises(ValueError):
            self.ws.decision(run['id'], 'promote', 'Reviewer')

    def test_tools_are_role_scoped_and_snapshot_only(self):
        calls = []
        tool = SpecTools('research', {'sources': [{'id': 'S1', 'text': 'empty input'}]}, calls.append)
        self.assertEqual(tool.call('evidence_search', 'empty')['sources'][0]['id'], 'S1')
        self.assertEqual(tool.call('evidence_search', 'unknown')['total_matches'], 0)
        with self.assertRaises(ValueError):
            tool.call('feedback_memory')
        with self.assertRaises(ValueError):
            tool.call('shell', 'anything')
        self.assertEqual(len(calls), 2)

    def test_specialist_tool_request_and_continuation(self):
        class ToolBackend(DemoBackend):
            requested = False
            async def query(self, role, instruction, payload, schema):
                if role == 'research' and not self.requested:
                    self.requested = True
                    return {'tool_requests': [{'name': 'evidence_search', 'query': 'empty'}]}
                return await super().query(role, instruction, payload, schema)
        run = self.run_workflow(ToolBackend())
        self.assertEqual(run['status'], 'needs_review')
        self.assertEqual(len(run['tool_calls']), 7)
        trace = next(c for c in run['tool_calls'] if c['tool'] == 'requirement_trace')
        self.assertTrue(trace['result']['candidate'][0]['tests'])
        self.assertEqual(next(a for a in run['agents'] if a['role'] == 'research')['calls'], 2)

    def test_repeated_tool_requests_cannot_loop(self):
        class LoopBackend(DemoBackend):
            async def query(self, role, instruction, payload, schema):
                if role == 'research':
                    return {'tool_requests': [{'name': 'evidence_search', 'query': ''}]}
                return await super().query(role, instruction, payload, schema)
        with self.assertRaises(ValueError):
            self.run_workflow(LoopBackend())
        run = self.ws.data['runs'][0]
        self.assertEqual(run['status'], 'failed')
        self.assertEqual(next(a for a in run['agents'] if a['role'] == 'research')['calls'], 3)

    def test_mutated_prompt_cannot_be_promoted(self):
        run = self.run_workflow()
        self.ws.data['versions'][-1]['prompt'] = 'Changed after evaluation'
        with self.assertRaises(ValueError):
            self.ws.decision(run['id'], 'promote', 'Reviewer')

    def test_missing_judgments_and_duplicate_scenarios_rejected(self):
        run = self.run_workflow()
        with self.assertRaises(ValueError):
            evaluate({'results': run['evaluation']['results'][:-1]}, run['scenarios'], run['draft'], run['draft'])
        cases = copy.deepcopy(run['scenarios'])
        cases[1]['id'] = cases[0]['id']
        with self.assertRaises(ValueError):
            validate_scenarios({'scenarios': cases}, [{'id': 'ISSUE'}])

    def test_failed_children_are_bounded_and_preserved(self):
        class Broken(DemoBackend):
            async def query(self, role, *args):
                return {} if role == 'prompt' else await super().query(role, *args)
        agent = Orchestrator(self.ws, Broken())
        ident = agent.create(self.evidence, self.repo)
        with self.assertRaises(ValueError):
            asyncio.run(agent.execute(ident, self.evidence, self.repo))
        run = self.ws.run(ident)
        self.assertEqual(run['status'], 'failed')
        self.assertEqual(next(a for a in run['agents'] if a['role'] == 'prompt')['calls'], 2)

    def test_single_active_run_and_allowlist(self):
        agent = Orchestrator(self.ws, DemoBackend())
        with self.assertRaises(ValueError):
            agent.create(self.evidence, 'wrong/repo')
        agent.create(self.evidence, self.repo)
        with self.assertRaises(ValueError):
            agent.create(self.evidence, self.repo)

    def test_prime_adapter_uses_real_spawn_collect_contract(self):
        class Runtime:
            async def spawn(self, prompt, *, name):
                self.prompt, self.name = prompt, name
                return 'child-handle'
            async def collect(self, handle, *, timeout_ms):
                assert handle == 'child-handle' and timeout_ms == 180000
                return [SimpleNamespace(settled=True, status='done', answer_text='{"ok": true}')]
        runtime = Runtime()
        result = asyncio.run(PrimeBackend(runtime).query('research', 'Inspect sources', {}, {}))
        self.assertTrue(result['ok'])
        self.assertTrue(runtime.name.startswith('spec-research-'))


if __name__ == '__main__':
    unittest.main()
