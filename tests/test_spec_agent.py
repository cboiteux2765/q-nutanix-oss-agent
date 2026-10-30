import unittest
from agent.spec_agent import collect


class IntakeTests(unittest.TestCase):
    def fake_get(self, path):
        self.paths.append(path)
        if "/commits/" in path:
            return {"sha": "a" * 40}
        if "/comments?" in path:
            comment = {"id": 1, "user": None, "body": "Details", "html_url": "url", "updated_at": "today"}
            return [comment] * 100 if path.endswith("&page=1") else [comment]
        if "/issues/" in path:
            return {"title": "Fix a bug", "body": None, "user": {"login": "author"},
                    "html_url": "url", "updated_at": "today", "labels": [], **self.extra}
        return {"default_branch": "main"}

    def setUp(self):
        self.paths = []
        self.extra = {}

    def test_intake_pins_commit_and_collects_all_comment_pages(self):
        result = collect("https://github.com/org/repo/issues/42", "org/repo", get=self.fake_get)
        self.assertEqual(result["source_commit"], "a" * 40)
        self.assertEqual(len(result["comments"]), 101)
        self.assertEqual(result["body"], "")
        self.assertIn("/repos/org/repo/commits/main", self.paths)

    def test_wrong_repo_rejected_before_network(self):
        with self.assertRaisesRegex(ValueError, "not allowed"):
            collect("https://github.com/other/repo/issues/1", "org/repo", get=self.fake_get)
        self.assertEqual(self.paths, [])

    def test_non_github_url_rejected(self):
        with self.assertRaises(ValueError):
            collect("https://example.com/org/repo/issues/1", "org/repo", get=self.fake_get)
        self.assertEqual(self.paths, [])

    def test_pull_request_rejected(self):
        self.extra = {"pull_request": {}}
        with self.assertRaisesRegex(ValueError, "pull request"):
            collect("https://github.com/org/repo/issues/1", "org/repo", get=self.fake_get)

    def test_explicit_ref(self):
        collect("https://github.com/org/repo/issues/1", "org/repo", "feature/test", self.fake_get)
        self.assertIn("/repos/org/repo/commits/feature%2Ftest", self.paths)


if __name__ == "__main__":
    unittest.main()
