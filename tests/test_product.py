import copy
import json
from pathlib import Path
import tempfile
import unittest

from agent.product import add_documents, validate


class ProductTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1] / 'examples'
        self.run = json.loads((root / 'evidence.json').read_text())
        self.replay = json.loads((root / 'product-replay.json').read_text())

    def test_quote_must_exist_in_source(self):
        from agent.generate import prepare
        brief = copy.deepcopy(self.replay[0])
        brief['evidence_assessment'][0]['quote'] = 'invented code'
        with self.assertRaisesRegex(ValueError, 'quote'):
            validate(brief, prepare(self.run, 'example/review-fixture')['sources'])

    def test_documents_are_snapshotted_with_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'product.md'
            path.write_text('User outcome: predictable results.')
            result = add_documents(self.run, [path])
            self.assertEqual(result['product_documents'][0]['id'], 'DOC-1')
            self.assertEqual(len(result['product_documents'][0]['sha256']), 64)
            self.assertNotIn('product_documents', self.run)

    def test_product_loop_preserves_blocker_and_handoff_brief(self):
        try:
            import minisweagent
        except ImportError:
            self.skipTest('Install requirements-mini.txt')
        from agent.mini_spec import LocalModel, run_spec
        from agent.review import inspect
        with tempfile.TemporaryDirectory() as tmp:
            directory = run_spec(self.run, 'example/review-fixture', Path(tmp),
                                 LocalModel(replay=self.replay), product=True)
            trace = json.loads((directory / 'trace.json').read_text())
            self.assertEqual(trace['status'], 'BLOCKED')
            self.assertEqual(trace['model_calls'], 3)
            self.assertEqual(trace['correction_attempts'], 1)
            self.assertTrue((directory / 'product_brief.json').exists())
            handoff = json.loads((directory / 'handoff.json').read_text())
            self.assertEqual(handoff['risk_tier'], 'unknown')
            self.assertIn('product reviewer', handoff['unresolved_questions'][-1])
            self.assertTrue(inspect(directory)['blockers'])
