import copy
import json
from pathlib import Path
import unittest

from agent.generate import prepare
from agent.mini_product import SpecShell, TextModel, run_mini, shape_errors
from agent.generate import SCHEMA

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = json.loads((ROOT / 'examples/learning-evidence.json').read_text())
REPO = EVIDENCE['input']['repo']
DRAFT = json.loads((ROOT / 'examples/product-replay.json').read_text())[-1]


def act(command):
    return f"THOUGHT: next step.\n\n```mswea_bash_command\n{command}\n```"


def ScriptedModel(replies):
    """TextModel over a canned chat function: the real parsing, metering and format errors."""
    replies = list(replies)

    def chat(messages, fmt=None, role=None):
        return replies.pop(0), {"input_tokens": 1, "output_tokens": 1}
    return TextModel(chat, "scripted")


class MiniProductTests(unittest.TestCase):
    def test_search_validate_repair_submit(self):
        broken = copy.deepcopy(DRAFT)
        broken['work_items'][0]['depends_on'] = ['REQ-1']
        model = ScriptedModel([
            act('evidence_search "ISSUE"'),
            'Here is the draft:\n```json\n{}\n```',  # wrong fence -> format error, not a crash
            act("validate_draft <<'EOF'\n" + json.dumps(broken) + "\nEOF"),
            act("submit <<'EOF'\n" + json.dumps(DRAFT) + "\nEOF"),
        ])
        draft, info = run_mini(EVIDENCE, REPO, model)
        self.assertEqual(info['exit_status'], 'DRAFT_READY')
        self.assertEqual(draft, DRAFT)
        self.assertEqual(info['model_calls'], 4)
        self.assertEqual(info['format_errors'], 1)
        self.assertEqual(info['input_tokens'], 4)
        self.assertEqual([c['tool'] for c in info['tool_calls']], ['evidence_search', 'validate_draft', 'submit'])
        observation = info['trajectory']['messages'][7]['content']
        self.assertIn("TASK-1 depends_on ['REQ-1']", observation)

    def test_step_limit_stops_without_draft(self):
        model = ScriptedModel([act('feedback_memory')] * 3)
        draft, info = run_mini(EVIDENCE, REPO, model, step_limit=3)
        self.assertIsNone(draft)
        self.assertEqual(info['exit_status'], 'LimitsExceeded')

    def test_shell_is_virtual_and_read_only(self):
        shell = SpecShell(prepare(EVIDENCE, REPO))
        self.assertEqual(shell.execute({'command': 'rm -rf /'})['returncode'], 127)
        self.assertEqual(shell.execute({'command': 'evidence_search "SRC-1"'})['returncode'], 0)
        self.assertIn('not valid JSON', shell.execute({'command': "submit <<'EOF'\n{oops\nEOF"})['output'])

    def test_guidance_reaches_the_system_prompt(self):
        model = ScriptedModel([act("submit <<'EOF'\n" + json.dumps(DRAFT) + "\nEOF")])
        _, info = run_mini(EVIDENCE, REPO, model, guidance='Ask the product owner about whitespace.')
        system = info['trajectory']['messages'][0]['content']
        self.assertIn('Supplemental guidance', system)
        self.assertIn('Ask the product owner about whitespace.', system)

    def test_shape_errors_reports_everything(self):
        errors = shape_errors({'problem': '', 'scope': 'x', 'extra': 1}, SCHEMA)
        self.assertTrue(any("unexpected field 'extra'" in e for e in errors))
        self.assertTrue(any('draft.scope: expected a list' in e for e in errors))
        self.assertGreater(len(errors), 10)


if __name__ == '__main__':
    unittest.main()
