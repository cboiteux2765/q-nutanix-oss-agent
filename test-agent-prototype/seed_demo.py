"""Seed an operator-owned local PR mirror and write a valid API request."""
import argparse
import json
from pathlib import Path
from app.providers import SourceRegistry, make_demo_repository

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path(".evo-data"))
    parser.add_argument("--regression", action="store_true")
    args = parser.parse_args()
    data = args.data_dir.resolve()
    data.mkdir(parents=True, exist_ok=True)
    registry = SourceRegistry(data / "sources")
    if ("org/service", 42) in registry.registrations:
        raise SystemExit("This data directory is already seeded; choose another --data-dir.")
    _, entry = make_demo_repository(data / "demo-repository", regression=args.regression)
    registry.register(entry)
    path = data / "create-request.json"
    path.write_text(json.dumps(registry.request_for_pr("org/service", 42).model_dump(mode="json"), indent=2), encoding="utf-8")
    print(f"Registered org/service PR42. API request: {path}")
    print("Set EVO_DATA_DIR to this data directory before starting the service.")
