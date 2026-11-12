"""Start the local EVO server."""

import argparse
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


def main() -> None:
    parser = argparse.ArgumentParser(prog="evo", description="Run the local EVO workspace server.")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (default: 127.0.0.1).")
    parser.add_argument("--port", default=8765, type=int, help="Listen port (default: 8765).")
    parser.add_argument("--no-browser", action="store_true", help="Start without opening a browser.")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")

    if not args.no_browser:
        host = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(args.host, args.host)
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        Thread(target=_open_browser, args=(f"http://{host}:{args.port}",), daemon=True).start()
    uvicorn.run("evo.server:app", host=args.host, port=args.port, timeout_graceful_shutdown=2)


if __name__ == "__main__":
    main()
