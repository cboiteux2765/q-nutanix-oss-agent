"""Seed an operator-owned local PR mirror and write a pinned Prime handoff."""
import argparse
import json
from pathlib import Path
from app.providers import SourceRegistry, make_demo_repository
from app.prime_tools import StageTarget

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
    request = registry.request_for_pr("org/service", 42)
    context = registry.resolve(request)
    target = StageTarget(repo=request.repo, pr_number=request.pr_number,
        expected_base_sha=context.base_sha, expected_head_sha=context.head_sha,
        spec_id=request.spec_id, spec_hash=request.spec_hash,
        test_profile_id=request.test_profile_id, test_profile_hash=context.profile.fingerprint,
        environment_manifest_id=request.environment_manifest_id,
        environment_fingerprint=context.environment.fingerprint,
        policy_version=context.policy_version, correlation_id="seed-demo")
    path = data / "test-target.json"
    path.write_text(json.dumps(target.model_dump(mode="json"), indent=2), encoding="utf-8")
    print(f"Registered org/service PR42. Prime Test-stage handoff: {path}")
    print("Set EVO_TEST_DATA_DIR and an external EVO_TEST_POLICY_FILE in the trusted MCP process.")
