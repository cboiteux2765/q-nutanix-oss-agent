"""Regenerate the hosted recorded fixture from the tested Python workflow."""
import asyncio
import json
from pathlib import Path
import tempfile

from agent.learning import Workspace, Orchestrator, DemoBackend

ROOT = Path(__file__).resolve().parents[1]


def main():
    evidence = json.loads((ROOT / 'examples/learning-evidence.json').read_text())
    with tempfile.TemporaryDirectory() as directory:
        workspace = Workspace(directory)
        orchestrator = Orchestrator(workspace, DemoBackend())
        run_id = orchestrator.create(evidence, evidence['input']['repo'])
        asyncio.run(orchestrator.execute(run_id, evidence, evidence['input']['repo']))
        snapshot = workspace.snapshot()
        run = snapshot['runs'][0]
        run.pop('artifact_directory')
        fixture = {'baseline': snapshot['versions'][0], 'candidate': snapshot['versions'][1], 'run': run}
        (ROOT / 'site/product-spec-demo/dist/demo.json').write_text(
            json.dumps(fixture, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
