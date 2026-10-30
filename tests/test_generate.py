import copy
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from agent.generate import check_draft, demo_draft, parse_response, prepare, publish_specification, request_draft


class GenerationTests(unittest.TestCase):
    def setUp(self):
        self.run = {"run_id": "parent", "correlation_id": "correlation", "status": "EVIDENCE_COLLECTED",
                    "input": {"repo": "org/repo", "source_commit": "a" * 40, "issue_number": 1,
                              "title": "Handle empty input", "body": "Return an empty list.",
                              "issue_url": "https://github.com/org/repo/issues/1", "comments": []},
                    "evidence": [{"id": "SRC-1", "commit": "a" * 40, "text": "def parse(): pass",
                                  "url": f"https://github.com/org/repo/blob/{'a' * 40}/app.py#L1-L1"}]}
        self.payload = prepare(self.run, "org/repo")
        self.draft = demo_draft(self.payload)
        self.draft["requirements"] = [{"id": "REQ-1", "description": "Handle empty input.",
                                       "source_ids": ["ISSUE"], "test_ids": ["TEST-1"]}]
        self.draft["acceptance_tests"] = [{"id": "TEST-1", "expected_behavior": "Empty input returns []."}]
        self.draft["timeline"] = [{"id": "MILESTONE-1", "phase": "Implement and verify",
                                    "deliverable": "Empty-input handling and regression coverage.",
                                    "exit_criteria": "TEST-1 passes.", "depends_on": []}]

    def test_rejects_wrong_repo_and_mixed_commits(self):
        with self.assertRaises(ValueError):
            prepare(self.run, "other/repo")
        self.run["evidence"][0]["commit"] = "b" * 40
        with self.assertRaises(ValueError):
            prepare(self.run, "org/repo")

    def test_rejects_invented_citation_and_missing_test(self):
        for field, value in (("source_ids", ["SRC-999"]), ("test_ids", ["TEST-999"])):
            draft = copy.deepcopy(self.draft)
            draft["requirements"][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                check_draft(draft, self.payload["sources"])

    def test_rejects_approval_field_and_cyclic_tasks(self):
        draft = {**self.draft, "status": "APPROVED"}
        with self.assertRaises(ValueError):
            check_draft(draft, self.payload["sources"])
        self.draft["work_items"] = [{"id": "TASK-1", "objective": "Change code", "depends_on": ["TASK-1"]}]
        with self.assertRaises(ValueError):
            check_draft(self.draft, self.payload["sources"])

    def test_rejects_missing_or_forward_timeline(self):
        draft = copy.deepcopy(self.draft)
        draft["timeline"] = []
        with self.assertRaisesRegex(ValueError, "include a timeline"):
            check_draft(draft, self.payload["sources"])
        draft["timeline"] = [{"id": "MILESTONE-1", "phase": "Implement",
                              "deliverable": "The change", "exit_criteria": "Tests pass",
                              "depends_on": ["MILESTONE-2"]}]
        with self.assertRaisesRegex(ValueError, "earlier milestones"):
            check_draft(draft, self.payload["sources"])

    def test_refusal_incomplete_and_malformed_response(self):
        for response in ({"status": "incomplete"},
                         {"status": "completed", "output": [{"type": "message", "content": [{"type": "refusal"}]}]},
                         {"status": "completed", "output": []}):
            with self.subTest(response=response), self.assertRaises(ValueError):
                parse_response(response)

    @patch.dict("os.environ", {"OPENAI_API_KEY": "test-only"})
    def test_api_request_and_response_contract(self):
        response = {"status": "completed", "model": "test-model", "usage": {"input_tokens": 20},
                    "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(self.draft)}]}]}
        with patch("agent.generate.urlopen", return_value=io.BytesIO(json.dumps(response).encode())) as transport:
            draft, metadata = request_draft(self.payload, "test-model")
        body = json.loads(transport.call_args.args[0].data)
        self.assertFalse(body["store"])
        self.assertTrue(body["text"]["format"]["strict"])
        self.assertEqual(json.loads(body["input"]), self.payload)
        self.assertEqual(draft, self.draft)
        self.assertIsNone(metadata["llm_cost_usd"])

    def test_demo_cli_artifacts_and_exact_byte_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "evidence.json"
            source.write_text(json.dumps(self.run), encoding="utf-8")
            result = subprocess.run([sys.executable, "-m", "agent.generate",
                                     str(source), "--allowed-repo", "org/repo", "--demo",
                                     "--output", str(root / "out"),
                                     "--specifications", str(root / "specifications")],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            output = Path(result.stdout.strip())
            handoff = json.loads((output / "handoff.json").read_text(encoding="utf-8"))
            self.assertEqual(handoff["status"], "DRAFT")
            self.assertEqual(handoff["base_commit"], "a" * 40)
            self.assertEqual(handoff["spec_hash"], hashlib.sha256((output / "product_spec.md").read_bytes()).hexdigest())
            self.assertEqual(handoff["mode"], "offline-demo")
            self.assertTrue(handoff["unresolved_questions"])
            self.assertEqual(json.loads(source.read_text()), self.run)
            published = list((root / "specifications").rglob("*.md"))
            self.assertEqual(len(published), 1)
            self.assertEqual(published[0].read_bytes(), (output / "product_spec.md").read_bytes())

    def test_published_spec_is_exact_and_named_for_issue(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_directory = root / "run-id"
            run_directory.mkdir()
            source = run_directory / "product_spec.md"
            source.write_bytes(b"# Exact generated spec\n")
            published = publish_specification(
                run_directory, {"repo": "org/service", "issue_number": 42,
                                "title": "Handle Empty Input!"}, root / "specifications")
            self.assertEqual(published.parent.name, "issue-42-handle-empty-input")
            self.assertEqual(published.name, "product_spec-v1.md")
            self.assertEqual(published.read_bytes(), source.read_bytes())
            second = publish_specification(
                run_directory, {"repo": "org/service", "issue_number": 42,
                                "title": "Handle Empty Input!"}, root / "specifications")
            self.assertEqual(second.name, "product_spec-v2.md")
            self.assertEqual(second.read_bytes(), source.read_bytes())


if __name__ == "__main__":
    unittest.main()
