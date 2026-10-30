import json
from pathlib import Path
import tempfile
import unittest

from agent.review import approve, consume, feedback, fixture, inspect, read


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = fixture(Path(self.temp.name))
        self.report = inspect(self.directory)
        self.repo = "example/review-fixture"
        self.commit = "a" * 40

    def approve(self):
        return approve(self.directory, "tester", self.report["bundle_hash"], True)

    def test_approval_consumption_and_feedback(self):
        self.approve()
        result = consume(self.directory, self.repo, self.commit, True)
        self.assertEqual(result["status"], "APPROVED")
        self.assertEqual(read(self.directory / "handoff.json")["status"], "DRAFT")
        path = feedback(self.directory, "test-agent", "ACK", "Contract accepted", self.repo, self.commit, True)
        self.assertEqual(read(Path(path))["decision"], "ACK")
        self.assertTrue(read(Path(path))["approval_hash"])

    def test_ack_requires_approval(self):
        with self.assertRaises(OSError):
            feedback(self.directory, "test-agent", "ACK", "Accepted", self.repo, self.commit, True)
        self.assertEqual(list(self.directory.glob("feedback-*.json")), [])

    def test_stale_review_and_overwrite_rejected(self):
        with self.assertRaises(ValueError):
            approve(self.directory, "tester", "stale", True)
        self.approve()
        with self.assertRaises(FileExistsError):
            self.approve()

    def test_any_reviewed_file_edit_invalidates_approval(self):
        self.approve()
        for name in self.report["file_hashes"]:
            path = self.directory / name
            original = path.read_bytes()
            path.write_bytes(original + b"\n")
            with self.subTest(name=name), self.assertRaises(ValueError):
                consume(self.directory, self.repo, self.commit, True)
            path.write_bytes(original)

    def test_wrong_consumer_repo_commit_and_fixture_mode(self):
        with self.assertRaises(ValueError):
            approve(self.directory, "tester", self.report["bundle_hash"])
        self.approve()
        for repo, commit, fixture_allowed in (("other/repo", self.commit, True),
                                              (self.repo, "b" * 40, True), (self.repo, self.commit, False)):
            with self.subTest(repo=repo, commit=commit), self.assertRaises(ValueError):
                consume(self.directory, repo, commit, fixture_allowed)

    def test_tampered_approved_contract_rejected(self):
        self.approve()
        path = self.directory / "approval.json"
        approval = read(path)
        approval["handoff"]["constraints"] = []
        path.write_text(json.dumps(approval), encoding="utf-8")
        with self.assertRaises(ValueError):
            consume(self.directory, self.repo, self.commit, True)

    def test_unresolved_questions_block_approval(self):
        for name in ("draft.json", "handoff.json"):
            path = self.directory / name
            data = read(path)
            data["unresolved_questions"] = ["What is the expected behavior?"]
            path.write_text(json.dumps(data), encoding="utf-8")
        report = inspect(self.directory)
        with self.assertRaisesRegex(ValueError, "blocked"):
            approve(self.directory, "tester", report["bundle_hash"], True)

    def test_reject_can_be_recorded_without_approval(self):
        path = feedback(self.directory, "test-agent", "REJECT", "Needs review", self.repo, self.commit)
        self.assertEqual(read(Path(path))["decision"], "REJECT")


if __name__ == "__main__":
    unittest.main()
