"""Subprocess helpers: bounded command capture and process-tree cleanup."""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
from pathlib import Path
from tempfile import TemporaryFile

from .common import MAX_OUTPUT_CHARS


def bounded(value: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(value) > limit:
        return value[:limit] + f"\n[truncated {len(value) - limit} characters]"
    return value


def run(argv: list[str], cwd: Path, timeout: float, env: dict[str, str],
        input: bytes | None = None) -> dict:
    """Run a command to completion, keeping at most MAX_OUTPUT_CHARS of each stream."""
    with TemporaryFile() as output, TemporaryFile() as errors:
        timed_out = False
        try:
            result = subprocess.run(
                argv, cwd=cwd, env=env, input=input,
                stdin=subprocess.DEVNULL if input is None else None,
                stdout=output, stderr=errors, timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            timed_out = True
            result = None
        streams = []
        for stream in (output, errors):
            stream.seek(0)
            streams.append(bounded(stream.read(MAX_OUTPUT_CHARS + 1).decode("utf-8", errors="replace")))
    return {"exit_code": result.returncode if result else None,
            "stdout": streams[0], "stderr": streams[1], "timed_out": timed_out}


def new_group() -> dict:
    """Options that start a child in its own process group, so its whole tree can be stopped."""
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


async def kill_tree(process: asyncio.subprocess.Process, env: dict[str, str] | None = None) -> None:
    """Stop a process started with `new_group()` and everything it spawned.

    On POSIX the group is signalled even after the leader exits, to stop orphaned children.
    """
    if os.name == "nt" and process.returncode is None:
        try:
            killer = await asyncio.create_subprocess_exec(
                "taskkill.exe", "/PID", str(process.pid), "/T", "/F",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW, env=env,
            )
            await killer.wait()
        except OSError:
            pass
    elif os.name != "nt":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
