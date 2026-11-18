"""Start the local EVO server."""

import argparse
from pathlib import Path
import shutil
import subprocess
import sys
import time
import webbrowser
from threading import Thread
from urllib.error import URLError
from urllib.request import urlopen

import uvicorn


def _open_browser(url: str) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            with urlopen(f"{url}/api/health", timeout=0.5) as response:
                if response.status == 200:
                    webbrowser.open(url)
                    return
        except (URLError, OSError):
            time.sleep(0.2)


FRONTEND = Path(__file__).resolve().parents[2] / "frontend"


def frontend_stale(frontend: Path = FRONTEND) -> bool:
    """True when the built UI is missing or older than any frontend source or config file."""
    built = frontend / "dist" / "index.html"
    if not built.is_file():
        return True
    sources = [p for p in (frontend / "src").rglob("*") if p.is_file()]
    sources += [frontend / name for name in ("index.html", "package.json", "package-lock.json", "vite.config.ts")]
    newest = max((p.stat().st_mtime for p in sources if p.is_file()), default=0)
    return newest > built.stat().st_mtime


def build_frontend(frontend: Path = FRONTEND) -> None:
    """Rebuild the UI after a pull or edit, so `uv run evo` always serves the current code."""
    if not (frontend / "package.json").is_file() or not frontend_stale(frontend):
        return
    npm = shutil.which("npm")
    if npm is None:
        print("The frontend build is out of date and npm was not found; serving the existing build. "
              "Install Node.js 20+ or run `npm --prefix frontend run build`.", file=sys.stderr)
        return
    print("Frontend changed since the last build; rebuilding (this takes a few seconds) ...", flush=True)
    try:
        if not (frontend / "node_modules").is_dir():
            subprocess.run([npm, "ci"], cwd=frontend, check=True)
        subprocess.run([npm, "run", "build"], cwd=frontend, check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"Frontend build failed ({exc}); serving the existing build if there is one.", file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(prog="evo", description="Run the local EVO workspace server.")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (default: 127.0.0.1).")
    parser.add_argument("--port", default=8765, type=int, help="Listen port (default: 8765).")
    parser.add_argument("--no-browser", action="store_true", help="Start without opening a browser.")
    parser.add_argument("--no-build", action="store_true",
                        help="Do not rebuild the frontend even if its sources changed.")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    if not args.no_build:
        build_frontend()

    if not args.no_browser:
        host = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(args.host, args.host)
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        Thread(target=_open_browser, args=(f"http://{host}:{args.port}",), daemon=True).start()
    uvicorn.run("evo.server:app", host=args.host, port=args.port, timeout_graceful_shutdown=2)


if __name__ == "__main__":
    main()
