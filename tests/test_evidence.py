import base64
import unittest

from agent.evidence import collect_evidence


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.run = {"input": {"repo": "org/repo", "source_commit": "a" * 40}}
        self.calls = []
        self.data = {"type": "file", "encoding": "base64", "size": 14, "sha": "b" * 40,
                     "content": base64.b64encode(b"one\ntwo\nthree\n").decode()}

    def get(self, path):
        self.calls.append(path)
        return self.data

    def test_exact_excerpt_and_pinned_citation(self):
        result = collect_evidence(self.run, ["doc/a b.md:2-3"], "org/repo", self.get)[0]
        self.assertEqual(result["text"], "two\nthree")
        self.assertEqual(result["start_line"], 2)
        self.assertTrue(result["is_excerpt"])
        self.assertEqual(self.calls, [f"/repos/org/repo/contents/doc/a%20b.md?ref={'a' * 40}"])
        self.assertEqual(result["url"], f"https://github.com/org/repo/blob/{'a' * 40}/doc/a%20b.md#L2-L3")

    def test_default_range_clamps_to_file_length(self):
        result = collect_evidence(self.run, ["README.md"], "org/repo", self.get)[0]
        self.assertEqual(result["end_line"], 3)
        self.assertFalse(result["is_excerpt"])

    def test_invalid_selections_rejected_before_fetch(self):
        for selections in (["../secret"], ["/absolute"], ["a:3-2"], ["a:1-201"], ["a"] * 6):
            with self.subTest(selections=selections), self.assertRaises(ValueError):
                collect_evidence(self.run, selections, "org/repo", self.get)
        self.assertEqual(self.calls, [])

    def test_repository_and_commit_checked_before_fetch(self):
        with self.assertRaises(ValueError):
            collect_evidence(self.run, ["README.md"], "other/repo", self.get)
        self.run["input"]["source_commit"] = "main"
        with self.assertRaises(ValueError):
            collect_evidence(self.run, ["README.md"], "org/repo", self.get)
        self.assertEqual(self.calls, [])

    def test_out_of_bounds_range_rejected(self):
        with self.assertRaisesRegex(ValueError, "outside"):
            collect_evidence(self.run, ["README.md:2-5"], "org/repo", self.get)

    def test_unsupported_files_rejected(self):
        for override in ({"type": "dir"}, {"size": 1_000_001}, {"encoding": "none"},
                         {"content": base64.b64encode(b"\x00").decode()},
                         {"content": base64.b64encode(b"\xff").decode()}):
            original = self.data.copy()
            self.data.update(override)
            with self.subTest(override=override), self.assertRaises(ValueError):
                collect_evidence(self.run, ["file"], "org/repo", self.get)
            self.data = original


if __name__ == "__main__":
    unittest.main()
