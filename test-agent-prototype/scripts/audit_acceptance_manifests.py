"""Independently check saved manifests in one demo_acceptance output."""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
import xml.etree.ElementTree as ET

from app.models import Verification, canonical_hash


def audit(output: Path) -> tuple[int, int, int]:
    summary = json.loads((output / "acceptance_summary.json").read_text(encoding="utf-8"))
    if summary["passed"] != 5 or len(summary["scenarios"]) != 5:
        raise ValueError("Acceptance summary does not contain five passed scenarios")
    manifests = list(output.glob("*/state/runs/*/artifacts/evidence_manifest.json"))
    if len(manifests) < 5:
        raise ValueError("Expected evidence for at least five runs")
    entry_count = 0
    for manifest_path in manifests:
        directory = manifest_path.parent
        if manifest_path.is_symlink() or (directory / "verification.json").is_symlink():
            raise ValueError(f"Linked manifest or verification envelope: {directory}")
        verification = Verification.model_validate_json(
            (directory / "verification.json").read_text(encoding="utf-8"))
        raw = manifest_path.read_bytes()
        manifest = json.loads(raw)
        if (sha256(raw).hexdigest() != verification.artifact_manifest_hash
                or manifest["run_id"] != verification.run_id
                or manifest["identity_hash"] != verification.identity_hash
                or canonical_hash(manifest["identity"]) != verification.identity_hash):
            raise ValueError(f"Manifest/verification identity mismatch: {manifest_path}")
        for item in manifest["evidence"]:
            name = item["path"]
            if (name != item["id"] or name.startswith("/") or "\\" in name
                    or any(part in {"", ".", ".."} for part in name.split("/"))):
                raise ValueError(f"Unsafe artifact name: {name}")
            target = directory.joinpath(*name.split("/"))
            for ancestor in (target, *target.parents):
                if ancestor == directory:
                    break
                if ancestor.is_symlink() or (hasattr(ancestor, "is_junction") and ancestor.is_junction()):
                    raise ValueError(f"Linked evidence path: {target}")
            if (not target.resolve().is_relative_to(directory.resolve()) or not target.is_file()
                    or sha256(target.read_bytes()).hexdigest() != item["sha256"]
                    or target.stat().st_size != item["size"]):
                raise ValueError(f"Damaged evidence: {target}")
            if name.endswith(".xml"):
                ET.parse(target)
            entry_count += 1
    return summary["passed"], len(manifests), entry_count


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    scenarios, runs, entries = audit(args.output.resolve())
    print(f"PASS: {scenarios} acceptance scenarios; {runs} bound manifests; {entries} verified artifacts")
