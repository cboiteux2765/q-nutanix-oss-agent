import json
from pathlib import Path
import tempfile
import unittest

from agent.events import describe
from agent.generate import prepare, write_artifacts
from agent.review import auto_clear, consume


class ClearanceTests(unittest.TestCase):
    def make_bundle(self, root, risk='low', questions=False):
        examples = Path(__file__).resolve().parents[1] / 'examples'
        run = json.loads((examples / 'evidence.json').read_text())
        brief, _, draft = json.loads((examples / 'product-replay.json').read_text())
        brief.update(risk_tier=risk, assumptions=[], open_decisions=[] if not questions else brief['open_decisions'])
        draft['assumptions'] = []
        draft['unresolved_questions'] = []
        payload = prepare(run, 'example/review-fixture')
        payload['product_analysis'] = brief
        return write_artifacts(run, payload, draft, {'mode':'live-local'}, root)

    def test_auto_clear_is_not_human_approval(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = self.make_bundle(Path(tmp))
            self.assertEqual(auto_clear(directory)['status'], 'AUTO_CLEARED')
            contract = consume(directory, 'example/review-fixture', 'a'*40)
            self.assertEqual(contract['status'], 'AUTO_CLEARED')
            with (directory / 'product_spec.md').open('a') as stream:
                stream.write('Changed')
            with self.assertRaises(ValueError):
                consume(directory, 'example/review-fixture', 'a'*40)

    def test_unknown_risk_or_decisions_require_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            for risk, questions in [('unknown', False), ('high', False), ('low', True)]:
                directory = self.make_bundle(Path(tmp), risk, questions)
                self.assertEqual(auto_clear(directory)['status'], 'NEEDS_REVIEW')
                self.assertFalse((directory / 'approval.json').exists())

    def test_plain_language_http_log(self):
        message = describe('api.response', {'status':200, 'endpoint':'http://localhost/api/chat'})
        self.assertIn('Received the model response.', message)
        self.assertIn('200', message)
        self.assertIn('http://localhost/api/chat', message)
