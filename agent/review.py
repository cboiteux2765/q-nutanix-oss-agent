"""Local review, approval and simulated coding-agent receipt. No network calls."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
from uuid import uuid4

from agent.generate import check_draft, demo_draft, write_artifacts
from agent.events import EventLog

FILES = ("product_spec.md", "handoff.json", "draft.json", "context.json")
FIELDS = ("requirements", "acceptance_tests", "work_items", "timeline", "constraints", "unresolved_questions")


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def save(path, value):
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, ensure_ascii=False)
        stream.write("\n")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def inspect(directory):
    raw = {name: (directory / name).read_bytes() for name in FILES}
    handoff, draft, context = (json.loads(raw[name]) for name in FILES[1:])
    check_draft(draft, context["sources"])
    if handoff["schema_version"] != "0.1-draft" or handoff["status"] != "DRAFT":
        raise ValueError("Expected a version 0.1 draft handoff.")
    if handoff["spec_uri"] != "product_spec.md" or handoff["spec_hash"] != digest(raw["product_spec.md"]):
        raise ValueError("Spec hash or artifact path does not match the draft handoff.")
    if handoff["issue"] != {"repo": context["repo"], "number": context["issue_number"]}:
        raise ValueError("Handoff issue does not match context.")
    if handoff["base_commit"] != context["commit"] or not re.fullmatch(r"[0-9a-f]{40}", context["commit"]):
        raise ValueError("Handoff commit does not match context.")
    if any(handoff[key] != draft[key] for key in FIELDS):
        raise ValueError("Handoff fields differ from the structured draft.")
    if 'product_analysis' in context:
        from agent.product import validate
        validate(context['product_analysis'], context['sources'])
        if handoff.get('product_analysis') != context['product_analysis']:
            raise ValueError('Handoff product analysis differs from approved context.')
    hashes = {name: digest(content) for name, content in raw.items()}
    bundle_hash = digest(json.dumps(hashes, sort_keys=True).encode())
    blockers = list(draft["unresolved_questions"])
    if 'product_analysis' in context:
        blockers += [item['question'] for item in context['product_analysis']['open_decisions']]
    for key in ("requirements", "acceptance_tests", "work_items", "timeline"):
        if not draft[key]:
            blockers.append(f"No {key} supplied.")
    if handoff["mode"] in ("offline-demo", "offline-replay"):
        blockers.append("Offline generation placeholder cannot be approved.")
    return {"bundle_hash": bundle_hash, "file_hashes": hashes, "blockers": blockers,
            "handoff": handoff, "review_note": "Human must review factual support and scope; checks are structural."}


def approve(directory, reviewer, expected_hash, allow_fixture=False):
    report = inspect(directory)
    if not reviewer.strip():
        raise ValueError("Reviewer name is required.")
    if report["bundle_hash"] != expected_hash:
        raise ValueError("Bundle changed since review; inspect it again.")
    if report["blockers"]:
        raise ValueError("Approval blocked: " + "; ".join(report["blockers"]))
    if report["handoff"]["mode"] == "review-fixture" and not allow_fixture:
        raise ValueError("Synthetic fixture requires --allow-fixture; not a real spec approval.")
    record = {"schema_version": "0.1", "reviewer": reviewer,
              "approved_at": datetime.now(timezone.utc).isoformat(),
              "bundle_hash": report["bundle_hash"], "file_hashes": report["file_hashes"],
              "handoff": {**report["handoff"], "status": "APPROVED"}}
    # Exclusive creation preserves previous approvals instead of overwriting them.
    save(directory / "approval.json", record)
    return record


def automatic_reasons(directory, report):
    context = read(directory / 'context.json')
    brief = context.get('product_analysis')
    reasons = list(report['blockers'])
    if report['handoff']['mode'] not in ('live-local', 'live'):
        reasons.append('Automatic handoff is only available for live-generated specs.')
    if not brief:
        reasons.append('Product discovery has not been performed.')
    else:
        if brief['risk_tier'] != 'low':
            reasons.append('The assessed risk is not low.')
        if brief['assumptions'] or read(directory / 'draft.json')['assumptions']:
            reasons.append('Assumptions still need confirmation.')
        if any(not brief[key] for key in ('users', 'desired_outcomes', 'user_stories', 'evidence_assessment')):
            reasons.append('Product context or quoted evidence is incomplete.')
    return reasons


def auto_clear(directory):
    report = inspect(directory)
    reasons = automatic_reasons(directory, report)
    if reasons:
        return {'status': 'NEEDS_REVIEW', 'reasons': reasons}
    record = {'schema_version': '0.1', 'reviewer': 'automatic-policy-v1',
              'decision_type': 'AUTOMATIC', 'approved_at': datetime.now(timezone.utc).isoformat(),
              'bundle_hash': report['bundle_hash'], 'file_hashes': report['file_hashes'],
              'handoff': {**report['handoff'], 'status': 'AUTO_CLEARED'}}
    save(directory / 'approval.json', record)
    return {'status': 'AUTO_CLEARED', 'reasons': []}


def consume(directory, repo, commit, allow_fixture=False):
    report = inspect(directory)
    approval = read(directory / "approval.json")
    if report["blockers"]:
        raise ValueError("Handoff has unresolved blockers.")
    if approval["schema_version"] != "0.1" or not approval["reviewer"].strip():
        raise ValueError("Invalid approval record.")
    if approval["bundle_hash"] != report["bundle_hash"] or approval["file_hashes"] != report["file_hashes"]:
        raise ValueError("Artifacts changed after approval; a new reviewed bundle is required.")
    automatic = approval.get('decision_type') == 'AUTOMATIC'
    if automatic and automatic_reasons(directory, report):
        raise ValueError('Spec no longer qualifies for automatic handoff.')
    expected = {**report["handoff"], "status": "AUTO_CLEARED" if automatic else "APPROVED"}
    if approval["handoff"] != expected:
        raise ValueError("Approved handoff differs from reviewed artifacts.")
    if expected["issue"]["repo"].casefold() != repo.casefold() or expected["base_commit"] != commit:
        raise ValueError("Consumer repository or commit does not match the handoff.")
    if expected["mode"] == "review-fixture" and not allow_fixture:
        raise ValueError("Synthetic fixture requires --allow-fixture.")
    return expected


def feedback(directory, actor, decision, reason, repo, commit, allow_fixture=False):
    if not actor.strip() or not reason.strip():
        raise ValueError("Actor and feedback reason are required.")
    if decision not in ("ACK", "REJECT"):
        raise ValueError("Feedback must be ACK or REJECT.")
    # ACK must pass consumption checks. REJECT may describe an invalid/unapproved bundle.
    if decision == "ACK":
        consume(directory, repo, commit, allow_fixture)
    observed = {name: digest((directory / name).read_bytes()) for name in FILES}
    record = {"actor": actor, "decision": decision, "reason": reason,
              "created_at": datetime.now(timezone.utc).isoformat(),
              "expected_repo": repo, "expected_commit": commit, "file_hashes": observed,
              "approval_hash": digest((directory / "approval.json").read_bytes())
              if (directory / "approval.json").exists() else None}
    path = directory / f"feedback-{uuid4()}.json"
    save(path, record)
    return str(path.resolve())


def fixture(output):
    """Build a complete synthetic review example without GitHub or an LLM."""
    context = {"repo": "example/review-fixture", "issue_number": 1, "source_commit": "a" * 40,
               "title": "Synthetic fixture: empty input returns an empty list"}
    payload = {"repo": context["repo"], "issue_number": 1, "commit": context["source_commit"],
               "title": context["title"], "sources": [{"id": "ISSUE", "url": "https://example.invalid/issue/1",
                                                       "text": "Empty input should return []."}]}
    draft = demo_draft(payload)
    draft.update(problem="Synthetic fixture: define empty-input behavior.", scope=["Empty input only."],
                 non_goals=["Other parser behavior."], assumptions=["Synthetic example, not repository evidence."],
                 proposed_change="Return [] when the input is empty.", api_changes="No signature changes.",
                 alternatives=["Raise an error; excluded by the synthetic issue."],
                 risks=["Real parser behavior has not been examined."],
                 rollout_rollback="For the hypothetical implementation, run tests; revert the change if needed.",
                 requirements=[{"id": "REQ-1", "description": "Empty input returns [].",
                                "source_ids": ["ISSUE"], "test_ids": ["TEST-1"]}],
                 acceptance_tests=[{"id": "TEST-1", "expected_behavior": "Calling parse('') returns []."}],
                 work_items=[{"id": "TASK-1", "objective": "Add the empty-input case and test.", "depends_on": []}],
                 timeline=[{"id": "MILESTONE-1", "phase": "Implementation and verification",
                            "deliverable": "Empty-input behavior and its regression test.",
                            "exit_criteria": "TEST-1 passes and nonempty-input behavior remains green.",
                            "depends_on": []}],
                 constraints=["Preserve behavior for nonempty input."], unresolved_questions=[])
    return write_artifacts({"input": context, "run_id": "synthetic", "correlation_id": "synthetic"},
                           payload, draft, {"mode": "review-fixture", "llm_cost_usd": 0}, output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", type=Path, default=Path("runs/actions.jsonl"))
    commands = parser.add_subparsers(dest="command", required=True)
    demo = commands.add_parser("fixture")
    demo.add_argument("--output", type=Path, default=Path("runs"))
    for name in ("inspect", "approve", "consume", "feedback"):
        command = commands.add_parser(name)
        command.add_argument("directory", type=Path)
        if name != "inspect":
            command.add_argument("--allow-fixture", action="store_true")
        if name == "approve":
            command.add_argument("--reviewer", required=True)
            command.add_argument("--expected-hash", required=True)
        if name in ("consume", "feedback"):
            command.add_argument("--repo", required=True)
            command.add_argument("--commit", required=True)
        if name == "feedback":
            command.add_argument("--actor", required=True)
            command.add_argument("--decision", choices=("ACK", "REJECT"), required=True)
            command.add_argument("--reason", required=True)
    args = parser.parse_args()
    log = EventLog(args.log)
    try:
        log("review.started", action=args.command, directory=str(getattr(args, "directory", "")))
        if args.command == "fixture":
            directory = fixture(args.output)
            log("review.fixture.created", directory=str(directory))
            print(directory)
            return 0
        if args.command == "inspect":
            result = inspect(args.directory)
        elif args.command == "approve":
            result = approve(args.directory, args.reviewer, args.expected_hash, args.allow_fixture)
        elif args.command == "consume":
            result = consume(args.directory, args.repo, args.commit, args.allow_fixture)
        else:
            result = feedback(args.directory, args.actor, args.decision, args.reason,
                              args.repo, args.commit, args.allow_fixture)
        log("review.completed", action=args.command, directory=str(args.directory))
        print(json.dumps(result, indent=2))
    except (ValueError, OSError, KeyError, TypeError) as exc:
        log("review.failed", action=args.command, error=str(exc))
        print(f"Review failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
