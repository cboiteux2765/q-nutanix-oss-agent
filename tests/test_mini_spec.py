import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

try:
    import minisweagent
except ImportError:
    minisweagent = None


@unittest.skipUnless(minisweagent, "Install requirements-mini.txt for mini integration tests")
class MiniTests(unittest.TestCase):
    def setUp(self):
        from agent.mini_spec import LocalModel, run_spec
        self.model_class, self.run_spec = LocalModel, run_spec
        root = Path(__file__).resolve().parents[1] / "examples"
        self.run = json.loads((root / "evidence.json").read_text())
        self.replay = json.loads((root / "replay.json").read_text())

    def test_replay_corrects_and_remains_unapprovable(self):
        from agent.review import inspect
        with tempfile.TemporaryDirectory() as tmp:
            directory = self.run_spec(self.run, "example/review-fixture", Path(tmp), self.model_class(replay=self.replay))
            trace = json.loads((directory / "trace.json").read_text())
            self.assertEqual(trace["correction_attempts"], 1)
            self.assertEqual([c["passed"] for c in trace["checks"]], [False, True])
            self.assertTrue(inspect(directory)["blockers"])
            self.assertTrue((directory / "agent_trace.json").exists())

    def test_event_log_records_responses_correction_and_completion(self):
        from agent.mini_spec import EventLog
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'events.jsonl'
            model = self.model_class(replay=self.replay, log=EventLog(path))
            self.run_spec(self.run, 'example/review-fixture', Path(tmp), model)
            records = [json.loads(line) for line in path.read_text().splitlines()]
            events = [r['event'] for r in records]
            self.assertIn('correction.requested', events)
            self.assertEqual(events.count('replay.response'), 2)
            self.assertEqual(events[-1], 'run.completed')
            self.assertEqual(len({r['session_id'] for r in records}), 1)
            response = next(r for r in records if r['event'] == 'replay.response')
            self.assertEqual(response["source"], "recorded")
            self.assertNotIn("response", response)
            self.assertNotIn("Not assessed in offline demo", path.read_text())

    def test_stops_after_two_corrections_and_saves_failure(self):
        model = self.model_class(replay=[self.replay[0]] * 4)
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                self.run_spec(self.run, "example/review-fixture", Path(tmp), model)
            self.assertEqual(model.calls, 3)
            self.assertEqual(len(list(Path(tmp).glob('failed-mini-*.json'))), 1)
            self.assertFalse(list(Path(tmp).glob('*/handoff.json')))

    def test_ollama_request_uses_loopback_without_credentials(self):
        response = {"done": True, "done_reason": "stop", "message": {"content": json.dumps(self.replay[1])}}
        events = []
        with patch('agent.mini_spec.urlopen', return_value=io.BytesIO(json.dumps(response).encode())) as transport:
            self.model_class(log=lambda event, **details: events.append((event, details))).query(
                [{"role": "user", "content": "Draft a spec"}])
        request = transport.call_args.args[0]
        self.assertEqual(request.full_url, 'http://127.0.0.1:11434/api/chat')
        self.assertFalse(request.has_header('Authorization'))
        self.assertNotIn('tools', json.loads(request.data))
        self.assertEqual(events[-1][1]['status'], 200)
        self.assertEqual(events[-1][1]['endpoint'], request.full_url)
        for event, details in events:
            self.assertFalse(set(details) & {'body', 'response', 'messages', 'response_body'})


if __name__ == '__main__':
    unittest.main()
