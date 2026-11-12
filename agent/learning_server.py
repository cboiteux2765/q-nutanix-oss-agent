"""Serve the EVO learning workspace locally: python -m agent.learning_server."""
import argparse
import asyncio
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
import webbrowser

from agent.learning import Workspace, Orchestrator, DemoBackend, OllamaBackend

ROOT = Path(__file__).resolve().parents[1]


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, workspace, backend, **kwargs):
        self.workspace, self.backend = workspace, backend
        super().__init__(*args, directory=str(ROOT / 'site/product-spec-demo/dist'), **kwargs)

    def reply(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def valid_host(self):
        return self.headers.get('Host') in (f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}')

    def do_GET(self):
        if not self.valid_host():
            return self.reply({'error': 'Invalid local host.'}, 403)
        if self.path == '/api/state':
            return self.reply({**self.workspace.snapshot(), 'mode': self.backend.mode})
        if self.path == '/api/fixture':
            return self.reply(json.loads((ROOT / 'examples/learning-evidence.json').read_text()))
        super().do_GET()

    def do_POST(self):
        origin = self.headers.get('Origin')
        if not self.valid_host() or (origin and origin != f'http://{self.headers.get("Host")}'):
            return self.reply({'error': 'Only same-origin local requests are accepted.'}, 403)
        try:
            if self.headers.get('Content-Type') != 'application/json':
                raise ValueError('JSON content type required.')
            length = int(self.headers.get('Content-Length', 0))
            if not 0 < length <= 250000:
                raise ValueError('Request must be 1–250000 bytes.')
            data = json.loads(self.rfile.read(length))
            if self.path == '/api/runs':
                evidence = data.get('evidence')
                if evidence is None and self.backend.mode == 'offline-demo':
                    evidence = json.loads((ROOT / 'examples/learning-evidence.json').read_text())
                if not isinstance(evidence, dict):
                    raise ValueError('Live mode requires an evidence JSON snapshot from the existing intake/evidence workflow.')
                allowed = data.get('allowed_repo') or ('example/review-fixture' if self.backend.mode == 'offline-demo' else '')
                orchestrator = Orchestrator(self.workspace, self.backend)
                run_id = orchestrator.create(evidence, allowed)
                def work():
                    try:
                        asyncio.run(orchestrator.execute(run_id, evidence, allowed))
                    except Exception:
                        pass  # The orchestrator persists the failure for the UI.
                threading.Thread(target=work, daemon=True).start()
                return self.reply({'run_id': run_id}, 202)
            if self.path == '/api/feedback':
                self.workspace.feedback(data['run_id'], data['text'], data['reviewer'])
            elif self.path == '/api/decision':
                self.workspace.decision(data['run_id'], data['action'], data['reviewer'])
            elif self.path == '/api/rollback':
                self.workspace.rollback(data['version_id'], data['reviewer'])
            else:
                return self.reply({'error': 'Unknown endpoint.'}, 404)
            self.reply({'ok': True})
        except (ValueError, KeyError, TypeError, StopIteration) as exc:
            self.reply({'error': str(exc) or 'Record not found.'}, 400)


def main(open_browser=False):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', help='Use local Ollama inference instead of recorded demo')
    parser.add_argument('--model', default='qwen2.5:7b')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--data', type=Path, default=None)
    parser.add_argument('--no-browser', action='store_true', help='Print the local URL without opening a browser')
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error('--port must be between 0 and 65535')
    # Keep simulated learning separate from real prompt history.
    workspace = Workspace(args.data or ROOT / 'runs' / ('learning-live' if args.live else 'learning-demo'))
    with workspace.lock:
        for run in workspace.data['runs']:
            if run['status'] == 'running':
                run.update(status='failed', error='Server restarted during this run. Start a new evaluation.')
        workspace.save()
    backend = OllamaBackend(args.model) if args.live else DemoBackend()
    server = ThreadingHTTPServer(('127.0.0.1', args.port), partial(Handler, workspace=workspace, backend=backend))
    url = f'http://127.0.0.1:{server.server_port}'
    print(f'EVO product specs: {url} ({backend.mode})', flush=True)
    print(f'Workspace: {workspace.root.resolve()}\nPress Ctrl+C to stop.', flush=True)
    if open_browser and not args.no_browser:
        def launch_browser():
            try:
                webbrowser.open(url)
            except webbrowser.Error:
                print(f'Open {url} in your browser.', flush=True)
        threading.Thread(target=launch_browser, daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
