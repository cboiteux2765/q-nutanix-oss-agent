"""Host-side lifecycle and RPC for a local Python agent process."""

import asyncio
import json
import os
import sys
from pathlib import Path

from .common import redact
from .procs import kill_tree, new_group


def _child_environment(api_key_env: str, api_key: str) -> dict:
    secret_words = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL", "AUTH", "COOKIE")
    return {
        name: value
        for name, value in os.environ.items()
        if name.upper() != api_key_env.upper()
        and not any(word in name.upper() for word in secret_words)
        and not (api_key and api_key in value)
    }


class PythonREPL:
    def __init__(self, cwd, settings, api_key, tools, dispatch):
        self.cwd = cwd
        self.settings = settings
        self.api_key = api_key
        self.tools = tools
        self.dispatch = dispatch
        self.process = None

    def redact(self, text: str) -> str:
        return redact(text, self.api_key)

    async def start(self):
        if self.process and self.process.returncode is None:
            return
        worker = Path(__file__).with_name("repl_worker.py").resolve()
        self.process = await asyncio.create_subprocess_exec(
            sys.executable, "-I", "-X", "utf8", "-u", str(worker),
            cwd=self.cwd, env=_child_environment(self.settings["api_key_env"], self.api_key),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, limit=64 * 1024 * 1024,
            **new_group(),
        )
        await self._write({"tools": self.tools})

    async def _write(self, data):
        payload = json.dumps(data, ensure_ascii=True, default=str, allow_nan=False)
        self.process.stdin.write(payload.encode() + b"\n")
        await self.process.stdin.drain()

    async def execute(self, code: str) -> dict:
        try:
            async with asyncio.timeout(self.settings["repl_timeout"]):
                await self.start()
                await self._write({"code": code})
                while True:
                    line = await self.process.stdout.readline()
                    if not line:
                        raise EOFError("Python process exited")
                    message = json.loads(line)
                    if message["type"] == "result":
                        return {key: self.redact(value) if isinstance(value, str) else value
                                for key, value in message.items() if key != "type"}
                    try:
                        value = await self.dispatch(message["name"], message["args"], message["kwargs"])
                        await self._write({"result": value})
                    except Exception as exc:
                        await self._write({"error": self.redact(str(exc) or type(exc).__name__)})
        except TimeoutError:
            await self.close()
            return {"ok": False, "error": "Python execution timed out. Namespace was reset."}
        except (EOFError, OSError, ValueError) as exc:
            await self.close()
            return {"ok": False, "error": self.redact(f"{exc}. Namespace was reset.")}
        except asyncio.CancelledError:
            await self.close()
            raise

    async def close(self):
        process, self.process = self.process, None
        if not process:
            return
        await kill_tree(process, _child_environment(self.settings["api_key_env"], self.api_key))
        await process.communicate()
