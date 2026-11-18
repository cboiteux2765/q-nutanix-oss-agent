import json
from pathlib import Path
import tempfile
import unittest

from agent.handoff import export, load_coding_input, prepare_mini_task
from agent.review import approve, fixture, inspect


class HandoffTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.directory = fixture(self.root)
        self.repo, self.commit = 'example/review-fixture', 'a' * 40

    def approved_packet(self):
        approve(self.directory, 'tester', inspect(self.directory)['bundle_hash'], True)
        return export(self.directory, self.repo, self.commit, self.root, True)

    def test_export_and_retrieve_complete_spec(self):
        path = self.approved_packet()
        result = load_coding_input(path, self.directory, self.repo, self.commit, True)
        self.assertEqual(result['handoff']['status'], 'APPROVED')
        self.assertIn('REQ-1', result['product_spec_markdown'])
        self.assertTrue(result['context']['sources'])

    def test_unapproved_export_blocked(self):
        with self.assertRaises(OSError):
            export(self.directory, self.repo, self.commit, self.root, True)
        self.assertFalse(list(self.root.glob('coding-input-*.json')))

    def test_mini_task_includes_spec_and_constraints(self):
        path = self.approved_packet()
        task = prepare_mini_task(path, self.directory, self.repo, self.commit, self.root, True)
        text = task.read_text(encoding='utf-8')
        self.assertIn('REQ-1', text)
        self.assertIn('Preserve behavior for nonempty input.', text)
        self.assertIn('product_spec_markdown', text)

    def test_packet_changes_rejected(self):
        path = self.approved_packet()
        original = path.read_text()
        for field, value in [('product_spec_markdown', 'Changed'), ('context', {}), ('approval', {})]:
            packet = json.loads(original)
            packet[field] = value
            path.write_text(json.dumps(packet))
            with self.subTest(field=field), self.assertRaises(ValueError):
                load_coding_input(path, self.directory, self.repo, self.commit, True)

    def test_source_change_invalidates_exported_packet(self):
        path = self.approved_packet()
        with (self.directory / 'product_spec.md').open('a') as stream:
            stream.write('Changed')
        with self.assertRaises(ValueError):
            load_coding_input(path, self.directory, self.repo, self.commit, True)


if __name__ == '__main__':
    unittest.main()
