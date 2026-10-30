"""Deterministic safety checks: scope enforcement, secret detection and log redaction.

The LLM cannot argue its way past these checks.
"""
from __future__ import annotations

import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache

from .models import ChangedFile, ChangeType, ScopePolicy

DEFAULT_FORBIDDEN = [
    ".git/**",
    ".github/**",
    ".gitmodules",
    "**/.env",
    "**/.env.*",
    "**/*.pem",
    "**/*.key",
    "**/id_rsa*",
    ".evo-artifacts/**",
]


@dataclass(frozen=True)
class ScopeViolation:
    code: str
    path: str | None
    message: str


@lru_cache(maxsize=512)
def glob_regex(pattern: str) -> re.Pattern[str]:
    """Gitignore-like globs: `*` stays in one segment, `**` spans segments, trailing `/` means a directory."""
    pattern = pattern.strip().replace("\\", "/").removeprefix("./")
    if pattern.endswith("/"):
        pattern += "**"
    parts = pattern.split("/")
    out = []
    for index, part in enumerate(parts):
        last = index == len(parts) - 1
        if part == "**":
            out.append(".*" if last else "(?:[^/]+/)*")
            continue
        segment = "".join("[^/]*" if c == "*" else "[^/]" if c == "?" else re.escape(c) for c in part)
        out.append(segment if last else segment + "/")
    return re.compile("^" + "".join(out) + "$")


def matches(path: str, patterns: list[str]) -> bool:
    path = path.replace("\\", "/")
    return any(glob_regex(p).match(path) for p in patterns)


def check_scope(changes: list[ChangedFile], policy: ScopePolicy, extra_forbidden: list[str] = (),
                symlinks: list[str] = ()) -> list[ScopeViolation]:
    forbidden = DEFAULT_FORBIDDEN + list(policy.forbidden_paths) + list(extra_forbidden)
    violations: list[ScopeViolation] = []
    changed_paths = {c.path for c in changes}
    for change in changes:
        path = change.path
        if matches(path, forbidden):
            violations.append(ScopeViolation("FORBIDDEN_PATH", path, f"{path} is a forbidden path"))
            continue
        if change.is_test:
            if change.change == ChangeType.ADDED and not (policy.may_add_tests or policy.may_modify_tests):
                violations.append(ScopeViolation("TEST_ADD_NOT_ALLOWED", path, f"adding test {path} is not allowed"))
            elif change.change == ChangeType.MODIFIED and not policy.may_modify_tests:
                violations.append(ScopeViolation("TEST_MODIFIED", path,
                                                 f"existing test {path} was modified without permission"))
            elif change.change == ChangeType.DELETED and not policy.may_modify_tests:
                violations.append(ScopeViolation("TEST_DELETED", path,
                                                 f"existing test {path} was deleted without permission"))
            continue
        if not matches(path, policy.allowed_paths):
            violations.append(ScopeViolation("OUT_OF_SCOPE", path, f"{path} is outside the authorized scope"))
    for link in symlinks:
        if link in changed_paths:
            violations.append(ScopeViolation("SYMLINK", link, f"{link} is a symlink; symlinks are not allowed"))
    if len(changes) > policy.max_changed_files:
        violations.append(ScopeViolation("TOO_MANY_FILES", None,
                                         f"{len(changes)} files changed, limit is {policy.max_changed_files}"))
    lines = sum(c.additions + c.deletions for c in changes)
    if lines > policy.max_changed_lines:
        violations.append(ScopeViolation("TOO_MANY_LINES", None,
                                         f"{lines} lines changed, limit is {policy.max_changed_lines}"))
    return violations


def scan_for_secrets(added_lines: dict[str, list[str]]) -> list[ScopeViolation]:
    violations = []
    for path, lines in added_lines.items():
        kinds = sorted({kind for line in lines for kind in find_secrets(line)})
        if kinds:
            violations.append(ScopeViolation("SECRET_DETECTED", path,
                                             f"possible secret ({', '.join(kinds)}) added in {path}"))
    return violations


SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("github_token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}\b")),
    ("github_pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{40,}\b")),
    ("openai_key", re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{20,}\b")),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("private_key", re.compile(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----")),
    ("slack_token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}\b")),
]

_BEARER = re.compile(r"(?i)(authorization:\s*(?:bearer|basic|token)\s+)[^\s\"']+")
_SENSITIVE_ENV = re.compile(r"(?i)(TOKEN|SECRET|PASSWORD|API_KEY|PRIVATE_KEY|CREDENTIAL)")


def sensitive_env_names(environ: dict[str, str] | None = None) -> list[str]:
    environ = os.environ if environ is None else environ
    return [name for name in environ if _SENSITIVE_ENV.search(name)]


def find_secrets(text: str) -> list[str]:
    return [name for name, pattern in SECRET_PATTERNS if pattern.search(text)]


def redact(text: str, extra_secrets: Iterable[str] = ()) -> str:
    if not text:
        return text
    for secret in sorted({s for s in extra_secrets if s and len(s) >= 6}, key=len, reverse=True):
        text = text.replace(secret, "[REDACTED]")
    for name, pattern in SECRET_PATTERNS:
        text = pattern.sub(f"[REDACTED:{name}]", text)
    return _BEARER.sub(r"\1[REDACTED]", text)


def env_secret_values(environ: dict[str, str] | None = None) -> list[str]:
    environ = os.environ if environ is None else environ
    return [environ[name] for name in sensitive_env_names(environ) if environ.get(name)]
