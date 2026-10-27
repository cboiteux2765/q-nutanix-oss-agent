import json

from app import fixtures as fx
from app.intake import load_handoff
from app.models import Verdict

BASE = "a" * 40
HEAD = "b" * 40


def write(tmp_path, **overrides):
    path = fx.write_verification(tmp_path / "verification.json", BASE, HEAD)
    if overrides:
        data = json.loads(path.read_text())
        for key, value in overrides.items():
            if value is None:
                data.pop(key, None)
            else:
                data[key] = value
        path.write_text(json.dumps(data))
    return path


def test_fresh_ready_handoff_starts_review(tmp_path):
    result = load_handoff(write(tmp_path), HEAD)
    assert result.ok
    assert result.handoff.run_id == "run-demo-1"
    assert len(result.verification_sha256) == 64


def test_moved_head_is_stale(tmp_path):
    result = load_handoff(write(tmp_path), "c" * 40)
    assert result.stop_verdict == Verdict.STALE
    assert "PR head moved" in result.reasons[0]


def test_changed_spec_is_stale(tmp_path):
    result = load_handoff(write(tmp_path), HEAD, expected_spec_hash="different")
    assert result.stop_verdict == Verdict.STALE


def test_changed_test_profile_is_stale(tmp_path):
    result = load_handoff(write(tmp_path), HEAD, expected_test_profile_hash="different")
    assert result.stop_verdict == Verdict.STALE


def test_test_agent_stale_stays_stale(tmp_path):
    result = load_handoff(write(tmp_path, readiness="STALE"), HEAD)
    assert result.stop_verdict == Verdict.STALE


def test_not_ready_is_blocked(tmp_path):
    for readiness in ("CHANGES_REQUIRED", "BLOCKED", "INCONCLUSIVE"):
        result = load_handoff(write(tmp_path, readiness=readiness), HEAD)
        assert result.stop_verdict == Verdict.BLOCKED, readiness


def test_missing_readiness_is_blocked(tmp_path):
    result = load_handoff(write(tmp_path, readiness=None), HEAD)
    assert result.stop_verdict == Verdict.BLOCKED


def test_missing_required_field_is_blocked(tmp_path):
    result = load_handoff(write(tmp_path, reviewed_head_sha=None), HEAD)
    assert result.stop_verdict == Verdict.BLOCKED
    assert "not a valid Test Agent handoff" in result.reasons[0]


def test_unsupported_schema_is_blocked(tmp_path):
    result = load_handoff(write(tmp_path, schema_version="9.9"), HEAD)
    assert result.stop_verdict == Verdict.BLOCKED


def test_invalid_json_is_blocked(tmp_path):
    path = tmp_path / "verification.json"
    path.write_text("{not json")
    result = load_handoff(path, HEAD)
    assert result.stop_verdict == Verdict.BLOCKED


def test_missing_file_is_blocked(tmp_path):
    result = load_handoff(tmp_path / "nope.json", HEAD)
    assert result.stop_verdict == Verdict.BLOCKED
    assert result.handoff is None
