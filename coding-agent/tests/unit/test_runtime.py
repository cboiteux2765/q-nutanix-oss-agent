"""Budgets, cancellation, JUnit parsing, workspaces."""
import os
import textwrap

import pytest

from evo_coding.config import (
    BudgetExceeded,
    BudgetTracker,
    Cancelled,
    CancelToken,
    CodingConfig,
    EffectiveBudget,
    PolicyConfig,
    WorkspaceConfig,
)
from evo_coding.models import BudgetLimits, LocalTestEvidence
from evo_coding.workspace import LocalWorkspace, evidence_for_hints, find_bash, normalize_test_id, parse_junit


def test_handoff_can_only_lower_operator_caps():
    policy = PolicyConfig(hard_max_usd=2.0, hard_max_seconds=600, hard_max_steps=30, hard_max_revisions=2)
    lowered = EffectiveBudget.resolve(policy, BudgetLimits(max_usd=0.5, max_steps=10))
    assert (lowered.max_usd, lowered.max_seconds, lowered.max_steps, lowered.max_revisions) == (0.5, 600, 10, 2)
    raised = EffectiveBudget.resolve(policy, BudgetLimits(max_usd=50, max_seconds=99999, max_revisions=9))
    assert (raised.max_usd, raised.max_seconds, raised.max_revisions) == (2.0, 600, 2)


def test_budget_tracker_limits():
    now = [0.0]
    tracker = BudgetTracker(EffectiveBudget(1.0, 10, 2, 1), clock=lambda: now[0])
    tracker.charge_llm(0.4, 100, 10)
    tracker.charge_step()
    tracker.check()
    tracker.charge_llm(0.7)
    with pytest.raises(BudgetExceeded) as exc:
        tracker.check()
    assert exc.value.kind == "usd"
    snap = tracker.snapshot()
    assert snap.llm_calls == 2 and snap.input_tokens == 100 and snap.usd == pytest.approx(1.1)
    timed = BudgetTracker(EffectiveBudget(1.0, 10, 5, 1), clock=lambda: now[0])
    now[0] = 11
    with pytest.raises(BudgetExceeded, match="time"):
        timed.check()


def test_cancel_token_file_is_cross_process(tmp_path):
    path = tmp_path / "CANCEL"
    other = CancelToken(path)
    assert not other.cancelled
    CancelToken(path).cancel("stop")
    assert other.cancelled
    with pytest.raises(Cancelled):
        other.raise_if_cancelled()


JUNIT = textwrap.dedent("""\
    <?xml version="1.0" encoding="utf-8"?>
    <testsuites><testsuite name="pytest" tests="5">
      <testcase classname="tests.test_a" name="test_ok"/>
      <testcase classname="tests.test_a" name="test_param[1]"/>
      <testcase classname="tests.test_a" name="test_param[2]"><failure message="x"/></testcase>
      <testcase classname="tests.test_b.TestCls" name="test_m"/>
      <testcase classname="tests.test_b" name="test_skip"><skipped/></testcase>
    </testsuite></testsuites>
""")


def test_junit_parsing_and_hint_matching(tmp_path):
    path = tmp_path / "junit.xml"
    path.write_text(JUNIT)
    results = parse_junit(path)
    assert (results.count("passed"), results.count("failed"), results.count("skipped")) == (3, 1, 1)
    assert normalize_test_id("tests/test_b.py::TestCls::test_m") == normalize_test_id("tests.test_b.TestCls::test_m")
    assert evidence_for_hints(results, ["tests/test_a.py::test_ok"]) == (LocalTestEvidence.PASS,
                                                                         ["tests/test_a.py::test_ok"])
    assert evidence_for_hints(results, ["tests/test_a.py::test_param"])[0] == LocalTestEvidence.FAIL
    assert evidence_for_hints(results, ["tests/test_b.py::TestCls::test_m"])[0] == LocalTestEvidence.PASS
    assert evidence_for_hints(results, ["tests/test_a.py::test_missing"])[0] == LocalTestEvidence.NOT_FOUND
    assert evidence_for_hints(results, [])[0] == LocalTestEvidence.NO_HINTS
    assert evidence_for_hints(None, ["x"])[0] == LocalTestEvidence.NOT_FOUND


def test_find_bash_is_not_wsl():
    assert "system32" not in find_bash().lower()


def test_local_workspace_runs_commands_and_strips_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("EVO_GITHUB_TOKEN", "should-not-leak-123")
    monkeypatch.setenv("HARMLESS_SETTING", "visible")
    ws = LocalWorkspace(tmp_path, WorkspaceConfig())
    result = ws.run('echo "${EVO_GITHUB_TOKEN:-missing} $HARMLESS_SETTING" && python -c "import pytest"')
    assert result.returncode == 0, result.output
    assert "missing visible" in result.output
    assert ws.run("exit 3").returncode == 3


def test_local_workspace_timeout_kills_command(tmp_path):
    ws = LocalWorkspace(tmp_path, WorkspaceConfig())
    result = ws.run('python -c "import time; time.sleep(30)"', timeout=2)
    assert result.timed_out and result.returncode == -1
    assert result.duration_seconds < 20


def test_config_loads_toml_with_relative_paths(tmp_path):
    (tmp_path / "evo.toml").write_text(textwrap.dedent("""\
        work_dir = "state"
        [implementer]
        kind = "scripted"
        script_path = "script.json"
        [publisher]
        kind = "local"
        local_remote = "origin.git"
        [policy]
        hard_max_usd = 0.25
    """))
    cfg = CodingConfig.load(tmp_path / "evo.toml")
    assert cfg.work_dir == tmp_path / "state"
    assert cfg.implementer.script_path == tmp_path / "script.json"
    assert cfg.publisher.local_remote == tmp_path / "origin.git"
    assert cfg.policy.hard_max_usd == 0.25


def test_config_rejects_unknown_keys(tmp_path):
    (tmp_path / "evo.toml").write_text("[policy]\nmerge_automatically = true\n")
    with pytest.raises(Exception, match="merge_automatically"):
        CodingConfig.load(tmp_path / "evo.toml")


@pytest.mark.skipif(os.name != "nt", reason="Windows-specific path handling")
def test_local_workspace_python_is_agent_interpreter(tmp_path):
    import sys

    ws = LocalWorkspace(tmp_path, WorkspaceConfig())
    out = ws.run('python -c "import sys; print(sys.executable)"').output.strip()
    assert os.path.normcase(out) == os.path.normcase(sys.executable)
