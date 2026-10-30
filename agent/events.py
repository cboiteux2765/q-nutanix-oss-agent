"""Shared JSONL action logging without agent framework dependencies."""

from datetime import datetime, timezone
import json
import sys
from uuid import uuid4

MESSAGES = {
    'monitor.started': 'Progress tracking is ready.',
    'run.started': 'Starting the product specification.',
    'context.validation.started': 'Checking the issue and source information.',
    'context.ready': 'Source information is ready.',
    'product.sources.loaded': 'Loaded the additional product documents.',
    'product.analysis.started': 'Identifying users, goals, evidence, and missing decisions.',
    'product.analysis.completed': 'Finished the product discovery brief.',
    'drafting.started': 'Writing the specification.',
    'api.request': 'Waiting for the local model to respond.',
    'api.response': 'Received the model response.',
    'api.error': 'The model request failed.',
    'replay.request': 'Reading the next recorded demo response; no model request is being made.',
    'replay.response': 'Loaded the recorded demo response.',
    'validation.started': 'Checking the draft and its requirement-to-test links.',
    'validation.failed': 'The draft needs a correction.',
    'validation.passed': 'The draft passed the structural checks.',
    'correction.requested': 'Asking the agent to fix the identified problem.',
    'correction.limit_reached': 'Stopped: the two correction attempts have been used.',
    'product.readiness.checked': 'Checked whether product questions remain.',
    'artifacts.writing': 'Saving the spec and handoff files.',
    'artifacts.saved': 'Saved the output files.',
    'specification.published': 'Published a readable Markdown copy of the specification.',
    'run.completed': 'Finished this specification run.',
    'review.automatic': 'The spec qualifies for automatic handoff; no human approval was recorded.',
    'review.required': 'A person needs to resolve the listed issues before handoff.',
    'handoff.exported': 'The spec package is ready for the separate coding workflow.',
    'github.request': 'Fetching information from GitHub.',
    'github.response': 'Received the GitHub response.',
    'evidence.file.started': 'Reading a source file.',
    'evidence.file.ready': 'Saved the selected source excerpt.',
}


def describe(event, details):
    message = MESSAGES.get(event, event.replace('_', ' ').replace('.', ' ').capitalize() + '.')
    labels = {'status': 'Result', 'endpoint': 'Endpoint', 'elapsed_seconds': 'Seconds',
              'attempt': 'Attempt', 'error': 'Problem', 'path': 'File', 'directory': 'Folder',
              'open_questions': 'Unanswered questions', 'open_decisions': 'Decisions needed',
              'source_count': 'Sources', 'reasons': 'Needs attention', 'correction_attempts': 'Corrections',
              'risk_tier': 'Assessed risk', 'next_owner': 'Next person responsible'}
    info = [f'{label}: {details[key]}' for key, label in labels.items() if key in details]
    return message + (' ' + '; '.join(info) if info else '')

class EventLog:
    """Flush each JSONL event to disk; optionally show structured metadata on stderr."""
    def __init__(self, path, verbose=False):
        self.path, self.verbose = path, verbose
        self.session_id = str(uuid4())

    def __call__(self, event, **details):
        # Payloads belong in the artifact files, not the operational event log.
        details = {key: value for key, value in details.items()
                   if key not in {"body", "response_body", "response", "messages", "payload", "result", "headers"}}
        record = {"time": datetime.now(timezone.utc).isoformat(), "session_id": self.session_id,
                  "event": event, **details}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record) + "\n")
        print(json.dumps(record, indent=2) if self.verbose else
              f"[{record['time'][11:19]}] {describe(event, details)}",
              file=sys.stderr, flush=True)


