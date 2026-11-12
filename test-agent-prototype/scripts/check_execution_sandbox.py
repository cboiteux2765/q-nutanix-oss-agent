"""Bounded host diagnostics; never execute repository code or pull an image."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys


def probe(command: list[str], timeout: float = 5) -> dict:
    try:
        result = subprocess.run(command, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=timeout)
        return {"available": result.returncode == 0, "exit_code": result.returncode,
                "detail": (result.stdout + result.stderr).strip()[:2000]}
    except subprocess.TimeoutExpired:
        return {"available": False, "detail": f"Timed out after {timeout:g} seconds"}
    except OSError as error:
        return {"available": False, "detail": str(error)}


def main() -> int:
    loop = probe([sys.executable, "-c",
        "import asyncio; loop = asyncio.new_event_loop(); loop.close(); print('ASYNCIO_OK')"])
    docker = shutil.which("docker")
    container = (probe([docker, "version", "--format", "{{.Server.Version}}"])
                 if docker else {"available": False, "detail": "Docker CLI not installed"})
    print(json.dumps({"python": sys.executable, "asyncio_host": loop,
                      "docker_daemon": container, "repository_code_executed": False,
                      "images_pulled": False}, indent=2))
    return 0 if loop["available"] and container["available"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
