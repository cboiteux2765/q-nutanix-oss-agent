"""Shared statuses, limits, error types and secret redaction."""

from __future__ import annotations

from typing import Any

# Agent and run statuses. Agents and runs move queued -> running -> (waiting | completed | failed |
# cancelled | interrupted); "waiting" means the root agent finished a turn before the workflow was ready.
ACTIVE = frozenset({"queued", "running"})
RESUMABLE = frozenset({"failed", "cancelled", "interrupted", "waiting"})
FINISHED = frozenset({"completed", "failed", "cancelled", "interrupted"})

# Size limits shared by tools, artifacts and the runtime.
MAX_TEXT_BYTES = 8 * 1024 * 1024  # one file read or write, patch, or artifact
INLINE_RESULT_CHARS = 24_000  # larger tool results and shared values are stored as artifacts
RESULT_PREVIEW_CHARS = 12_000  # preview kept inline when a result moves to artifacts
MAX_OUTPUT_CHARS = 32_000  # command output and file excerpts returned to an agent


class NotFound(KeyError):
    """A record does not exist (HTTP 404)."""

    def __str__(self) -> str:
        return str(self.args[0]) if self.args else "Resource not found."


class Conflict(ValueError):
    """The request is valid but conflicts with the current state (HTTP 409)."""


REDACTED = "[redacted]"


def redact(text: str, secret: str) -> str:
    return text.replace(secret, REDACTED) if secret else text


def redact_all(value: Any, secret: str) -> Any:
    """Redact a secret from every string, including keys, in nested JSON-like data."""
    if not secret:
        return value
    if isinstance(value, str):
        return redact(value, secret)
    if isinstance(value, dict):
        return {redact(str(k), secret) if isinstance(k, str) else k: redact_all(v, secret)
                for k, v in value.items()}
    if isinstance(value, list):
        return [redact_all(item, secret) for item in value]
    return value


class StreamRedactor:
    """Redact a secret from text that arrives in chunks.

    A secret split across chunks would slip past per-chunk redaction, so `feed` holds back any
    trailing text that could be the start of the secret until the next chunk decides it.
    """

    def __init__(self, secret: str):
        self.secret = secret
        self.pending = ""

    def feed(self, text: str) -> str:
        self.pending = redact(self.pending + text, self.secret)
        retain = 0
        if self.secret:
            for size in range(min(len(self.pending), len(self.secret) - 1), 0, -1):
                if self.pending.endswith(self.secret[:size]):
                    retain = size
                    break
        end = len(self.pending) - retain
        ready, self.pending = self.pending[:end], self.pending[end:]
        return ready

    def flush(self) -> str:
        """Return the held-back text once no more input will arrive."""
        ready, self.pending = self.pending, ""
        return ready
