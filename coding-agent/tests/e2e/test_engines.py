"""End-to-end runs of every LLM implementer (native, mini-SWE-agent, RLM, pi) through the full pipeline.

Model calls go to the scripted OpenAI-compatible server in tests/fake_openai.py; pi is driven through
tests/fake_pi.py, which speaks pi's documented JSON-mode protocol. Set EVO_E2E_PI=1 (with pi installed
and OPENAI_API_KEY set) to also run the real pi CLI, or EVO_E2E_OPENAI=1 to run native and rlm against
the real default model. Nothing else here spends money.
"""
import importlib.util
import json
import os
import shutil
import sys
from pathlib import Path

import pytest

from evo_coding.config import ImplementerConfig, LLMConfig, WorkspaceConfig
from evo_coding.implementers.bash_loop import NativeImplementer
from evo_coding.implementers.pi import LOCKDOWN_FLAGS, PiImplementer
from evo_coding.implementers.rlm import RLMImplementer
from evo_coding.models import RunState
from fake_openai import COMPLETE, FIXED_STATS, MEDIAN_FIX_SCRIPT, MEDIAN_TESTS, FakeOpenAIServer

pytestmark = pytest.mark.e2e
API_KEY = "sk-test-" + "k" * 24
HAS_MINI_SWE = importlib.util.find_spec("minisweagent") is not None
REAL_KEY = os.environ.get("OPENAI_API_KEY", "")  # before the autouse fixture swaps in the fake one


def llm_config(server, api_style="chat", **kw):
    return LLMConfig(base_url=server.base_url, model="fake-coder", api_style=api_style, retry_backoff_seconds=0,
                     input_usd_per_mtok=1.0, output_usd_per_mtok=4.0, **kw)


def make_implementer(kind, server, api_style, step_limit=20):
    config = ImplementerConfig(kind=kind, step_limit=step_limit, command_timeout_seconds=120)
    if kind == "native":
        return NativeImplementer(llm_config(server, api_style), config)
    from evo_coding.implementers.bash_loop import MiniSweImplementer

    return MiniSweImplementer(llm_config(server, api_style), config)


KINDS = [pytest.param("native", id="native")] + [
    pytest.param("mini_swe", id="mini-swe", marks=[pytest.mark.mini_swe,
                                                   pytest.mark.skipif(not HAS_MINI_SWE, reason="mini-swe extra")])]


@pytest.mark.parametrize("api_style", ["chat", "responses"])
@pytest.mark.parametrize("kind", KINDS)
def test_llm_implementer_fixes_median(pilot, kind, api_style):
    with FakeOpenAIServer(MEDIAN_FIX_SCRIPT) as server:
        implementer = make_implementer(kind, server, api_style)
        agent = pilot.agent(implementer=implementer)
        result = agent.run(pilot.handoff())
    assert result.status == RunState.TESTING, result.blockers
    assert {c.path for c in result.changed_files} == {"src/pilot_stats/stats.py", "tests/test_median.py"}
    assert [c.status for c in result.criteria_coverage] == ["IMPLEMENTED"] * 3
    assert result.implementer.name == implementer.name and result.implementer.model == "fake-coder"
    assert result.implementer.prompt_version == "coding-brief-v1"
    assert result.budget_used.llm_calls == len(MEDIAN_FIX_SCRIPT)
    assert result.budget_used.input_tokens == 1000 * len(MEDIAN_FIX_SCRIPT)
    assert result.budget_used.usd == pytest.approx(len(MEDIAN_FIX_SCRIPT) * (1000 * 1 + 100 * 4) / 1e6)

    endpoint = "/v1/responses" if api_style == "responses" else "/v1/chat/completions"
    assert set(server.paths) == {endpoint}
    assert all(h == f"Bearer {API_KEY}" for h in server.auth_headers)
    first = server.requests[0]
    prompt = str(first.get("messages") or first.get("input")) + str(first.get("instructions", ""))
    for needle in ("EVO Coding Agent", "AC-1", "src/pilot_stats/stats.py", "untrusted", "<approved_spec>"):
        assert needle in prompt
    last = str(server.requests[-1])
    assert "passed" in last, "the pytest observation is fed back to the model"

    transcript = (agent.run_root("run-001") / "r0" / "artifacts" / "transcript.json").read_text(encoding="utf-8")
    assert API_KEY not in transcript


@pytest.mark.parametrize("kind", KINDS)
def test_llm_cost_budget_is_enforced(pilot, kind):
    script = ["ls"] * 10 + [COMPLETE]
    with FakeOpenAIServer(script, input_tokens=200_000, output_tokens=10_000) as server:
        agent = pilot.agent(implementer=make_implementer(kind, server, "chat"),
                            config=pilot.config(hard_max_usd=0.5))
        result = agent.run(pilot.handoff())
    assert result.status == RunState.BLOCKED and result.blockers[0].code == "BUDGET_EXCEEDED"
    assert result.blockers[0].details["kind"] == "usd"
    assert len(server.requests) == 3  # 0.24 USD per call: the third pushes it over and stops the loop


@pytest.mark.parametrize("kind", KINDS)
def test_llm_step_limit_blocks(pilot, kind):
    with FakeOpenAIServer(["ls"] * 50) as server:
        agent = pilot.agent(implementer=make_implementer(kind, server, "chat", step_limit=3))
        result = agent.run(pilot.handoff())
    assert result.status == RunState.BLOCKED
    assert result.blockers[0].code == "IMPLEMENTER_DID_NOT_FINISH"
    assert len(server.requests) == 3


def test_native_recovers_from_format_errors_and_transient_failures(pilot):
    script = [{"text": "I will just describe the change."}, {"status": 503}] + MEDIAN_FIX_SCRIPT
    with FakeOpenAIServer(script) as server:
        agent = pilot.agent(implementer=make_implementer("native", server, "chat"))
        result = agent.run(pilot.handoff())
    assert result.status == RunState.TESTING, result.blockers
    assert any("Format error" in str(r) for r in server.requests)


def test_native_gives_up_after_repeated_format_errors(pilot):
    with FakeOpenAIServer([{"text": "no tools"}] * 5) as server:
        agent = pilot.agent(implementer=make_implementer("native", server, "chat"))
        result = agent.run(pilot.handoff())
    assert result.blockers[0].code == "IMPLEMENTER_DID_NOT_FINISH" and "FormatError" in result.blockers[0].message


def test_unreachable_model_blocks_with_owner(pilot):
    config = LLMConfig(base_url="http://127.0.0.1:9/v1", model="m", retry_backoff_seconds=0,
                       request_timeout_seconds=2)
    agent = pilot.agent(implementer=NativeImplementer(config, ImplementerConfig()))
    result = agent.run(pilot.handoff())
    assert result.status == RunState.BLOCKED and result.blockers[0].code == "MODEL_UNAVAILABLE"


def test_prompt_injection_in_repo_cannot_widen_scope(pilot):
    """The model 'obeys' an injected instruction to edit CI; the deterministic scope check still blocks it."""
    script = [
        "cat src/pilot_stats/stats.py",
        "echo 'jobs: {}  # ignore previous instructions' > .github/workflows/ci.yml",
        *MEDIAN_FIX_SCRIPT[1:],
    ]
    with FakeOpenAIServer(script) as server:
        agent = pilot.agent(implementer=make_implementer("native", server, "chat"))
        result = agent.run(pilot.handoff())
    assert result.status == RunState.BLOCKED
    assert [v["code"] for v in result.blockers[0].details["violations"]] == ["FORBIDDEN_PATH"]
    assert pilot.publisher.pulls() == []

FAKE_PI = [sys.executable, str(Path(__file__).parents[1] / "fake_pi.py")]
CRITERIA = {"AC-1": {"status": "IMPLEMENTED", "notes": "average middle pair"},
            "AC-2": {"status": "IMPLEMENTED", "notes": "ValueError"},
            "AC-3": {"status": "IMPLEMENTED", "notes": "unchanged"}}


@pytest.fixture(autouse=True)
def _api_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", API_KEY)


def py(code: str) -> dict:
    return {"tool": "python", "args": {"code": code}}


RLM_SCRIPT = [
    py("print(len(context['spec']), [f for f in context['files'] if f.startswith('src/')])\n"
       "import os; print('keys in sandbox:', sorted(k for k in os.environ if 'KEY' in k or 'TOKEN' in k))"),
    py("plan = llm_query('Summarize what must change:\\n' + context['spec'][:1500])\nprint(plan)"),
    {"text": "median() must average the middle pair and reject empty input."},  # answers the llm_query sub-call
    py(f"write_file('src/pilot_stats/stats.py', {FIXED_STATS!r})\n"
       f"write_file('tests/test_median.py', {MEDIAN_TESTS!r})\nprint(run('python -m pytest -q'))"),
    py(f"submit('Fixed median() for even-length and empty input; added tests.', {CRITERIA!r})"),
]


def rlm(server, api_style="chat", **kw):
    llm = LLMConfig(base_url=server.base_url, model="fake-root", api_style=api_style, retry_backoff_seconds=0,
                    input_usd_per_mtok=1.0, output_usd_per_mtok=4.0)
    return RLMImplementer(llm, ImplementerConfig(kind="rlm", command_timeout_seconds=60, **kw))


@pytest.mark.parametrize("api_style", ["chat", "responses"])
def test_rlm_fixes_median_through_repl_and_subcalls(pilot, api_style):
    with FakeOpenAIServer(RLM_SCRIPT) as server:
        agent = pilot.agent(implementer=rlm(server, api_style))
        result = agent.run(pilot.handoff())
    assert result.status == RunState.TESTING, result.blockers
    assert result.implementer.name == "rlm"
    assert [c.status for c in result.criteria_coverage] == ["IMPLEMENTED"] * 3
    assert result.budget_used.llm_calls == 5  # 4 root turns + 1 llm_query sub-call
    assert result.budget_used.steps == 4
    tool_key = "tools"
    assert [tool_key in r for r in server.requests] == [True, True, False, True, True]
    assert "Summarize what must change" in str(server.requests[2])
    root_prompt = str(server.requests[0])
    assert "REPL variable `context`" in root_prompt and "<approved_spec>" not in root_prompt
    transcript = (agent.run_root("run-001") / "r0" / "artifacts" / "transcript.json").read_text(encoding="utf-8")
    assert "keys in sandbox: []" in transcript, "API keys must never reach the REPL"
    assert API_KEY not in transcript
    assert not any(c.path.startswith(".evo-artifacts") for c in result.changed_files)


def test_rlm_repl_timeout_restarts_and_recovers(pilot):
    script = [py("x = 41"), py("import time\ntime.sleep(30)"), py("print('x' in dir())"), *RLM_SCRIPT[3:]]
    with FakeOpenAIServer(script) as server:
        implementer = rlm(server)
        implementer.config.command_timeout_seconds = 3
        result = pilot.agent(implementer=implementer).run(pilot.handoff())
    assert result.status == RunState.TESTING, result.blockers
    observations = [str(r) for r in server.requests]
    assert any("REPL was restarted" in o for o in observations)
    assert "False" in observations[3], "state is lost after a restart and the model is told so"


def test_rlm_subcall_limit_is_reported_to_the_model(pilot):
    script = [py("try:\n    llm_query('hi')\nexcept RuntimeError as e:\n    print('ERR', e)"), *RLM_SCRIPT[3:]]
    with FakeOpenAIServer(script) as server:
        result = pilot.agent(implementer=rlm(server, rlm_max_subcalls=0)).run(pilot.handoff())
    assert result.status == RunState.TESTING
    assert "sub-call limit 0 reached" in str(server.requests[1])
    assert result.budget_used.llm_calls == 3


def test_rlm_budget_counts_subcalls(pilot):
    script = [py("print(llm_query_batched(['a', 'b', 'c']))"), {"text": "1"}, {"text": "2"}, {"text": "3"},
              *RLM_SCRIPT[3:]]
    with FakeOpenAIServer(script, input_tokens=100_000, output_tokens=10_000) as server:
        result = pilot.agent(implementer=rlm(server), config=pilot.config(hard_max_usd=0.45)).run(pilot.handoff())
    # 0.14 USD per call: root + 3 sub-calls = 0.56 > 0.45, so the third sub-call trips the budget.
    assert result.status == RunState.BLOCKED and result.blockers[0].code == "BUDGET_EXCEEDED"
    assert len(server.requests) == 4


@pytest.mark.docker
def test_rlm_in_docker_sandbox(pilot):
    from evo_coding.workspace import docker_available

    if not docker_available():
        pytest.skip("Docker daemon not available")
    import subprocess

    if subprocess.run(["docker", "image", "inspect", "evo-coding-sandbox:py312"], capture_output=True).returncode:
        pytest.skip("sandbox image not built")
    config = pilot.config()
    config.workspace = WorkspaceConfig(backend="docker", command_timeout_seconds=300)
    with FakeOpenAIServer(RLM_SCRIPT) as server:
        result = pilot.agent(implementer=rlm(server), config=config).run(pilot.handoff())
    assert result.status == RunState.TESTING, result.blockers


# ---------------------------------------------------------------- pi


def pi(tmp_path, monkeypatch, scenario="median", step_limit=40, **llm_kw):
    record = tmp_path / "pi-record.json"
    monkeypatch.setenv("FAKE_PI_RECORD", str(record))
    monkeypatch.setenv("FAKE_PI_SCENARIO", scenario)
    monkeypatch.setenv("EVO_GITHUB_TOKEN", "ghp_" + "S" * 36)
    llm = LLMConfig(**{"base_url": "http://127.0.0.1:1/v1", "model": "fake-pi-model", "retry_backoff_seconds": 0,
                       "input_usd_per_mtok": 1.0, "output_usd_per_mtok": 4.0, **llm_kw})
    implementer = PiImplementer(llm, ImplementerConfig(kind="pi", pi_command=FAKE_PI, step_limit=step_limit))
    return implementer, record


def test_pi_fixes_median_with_locked_down_invocation(pilot, tmp_path, monkeypatch):
    implementer, record_path = pi(tmp_path, monkeypatch)
    assert implementer.version == "1.4.0-fake"
    agent = pilot.agent(implementer=implementer)
    result = agent.run(pilot.handoff())
    assert result.status == RunState.TESTING, result.blockers
    assert result.implementer.name == "pi" and result.implementer.model == "fake-pi-model"
    assert [c.status for c in result.criteria_coverage] == ["IMPLEMENTED"] * 3
    assert result.budget_used.llm_calls == 6 and result.budget_used.steps == 6
    assert result.budget_used.usd == pytest.approx(6 * (1000 * 1 + 100 * 4) / 1e6)

    record = json.loads(record_path.read_text(encoding="utf-8"))
    args = record["args"]
    assert args[:2] == ["--mode", "json"] and "evo/fake-pi-model" in args
    assert all(flag in args for flag in LOCKDOWN_FLAGS)  # no session, extensions, MCP, skills, AGENTS.md
    provider = record["models"]["providers"]["evo"]
    assert provider["api"] == "openai-responses" and provider["apiKey"] == "$EVO_PI_API_KEY"
    assert args[args.index("--thinking") + 1] == "high"
    assert provider["baseUrl"] == "http://127.0.0.1:1/v1"
    assert API_KEY not in json.dumps(record["models"]), "the key is passed by env reference, not written to disk"
    assert record["key_present"] and record["secret_env"] == [], "only the pi key env var reaches pi"
    assert record["offline"] == "1" and not record["agent_dir_in_cwd"]
    assert "AC-1" in record["prompt"] and "untrusted data" in record["system_prompt"]
    assert Path(record["cwd"]).resolve() == (agent.run_root("run-001") / "r0" / "workspace").resolve()


def test_pi_responses_api_maps_to_openai_responses(pilot, tmp_path, monkeypatch):
    implementer, record_path = pi(tmp_path, monkeypatch, api_style="responses")
    assert pilot.agent(implementer=implementer).run(pilot.handoff()).status == RunState.TESTING
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert record["models"]["providers"]["evo"]["api"] == "openai-responses"


def test_pi_budget_kills_the_process(pilot, tmp_path, monkeypatch):
    implementer, _ = pi(tmp_path, monkeypatch, scenario="loop")
    result = pilot.agent(implementer=implementer, config=pilot.config(hard_max_usd=1.0)).run(pilot.handoff())
    assert result.status == RunState.BLOCKED and result.blockers[0].code == "BUDGET_EXCEEDED"
    # 0.24 USD per turn: four turns (0.96) fit, the fifth crosses 1.00 and pi is killed right after it.
    assert result.budget_used.llm_calls == 5 and result.budget_used.usd == pytest.approx(1.2)


def test_pi_step_limit(pilot, tmp_path, monkeypatch):
    implementer, _ = pi(tmp_path, monkeypatch, scenario="loop", step_limit=2)
    result = pilot.agent(implementer=implementer).run(pilot.handoff())
    assert result.blockers[0].code == "IMPLEMENTER_DID_NOT_FINISH" and "StepLimit" in result.blockers[0].message


def test_pi_error_is_reported(pilot, tmp_path, monkeypatch):
    implementer, _ = pi(tmp_path, monkeypatch, scenario="error")
    result = pilot.agent(implementer=implementer).run(pilot.handoff())
    assert result.blockers[0].code == "IMPLEMENTER_DID_NOT_FINISH"
    assert "401 invalid api key" in result.blockers[0].message


def test_pi_cancellation_kills_the_process(pilot, tmp_path, monkeypatch):
    import threading

    implementer, _ = pi(tmp_path, monkeypatch, scenario="loop", input_usd_per_mtok=0, output_usd_per_mtok=0)
    agent = pilot.agent(implementer=implementer)
    timer = threading.Timer(4.0, lambda: agent.cancel("run-001"))
    timer.start()
    try:
        result = agent.run(pilot.handoff())
    finally:
        timer.cancel()
    assert result.status == RunState.CANCELLED


def test_pi_refuses_docker_backend(pilot, tmp_path, monkeypatch):
    from evo_coding.workspace import DockerWorkspace

    implementer, _ = pi(tmp_path, monkeypatch)
    with pytest.raises(Exception, match="local workspace backend"):
        implementer.implement(None, DockerWorkspace.__new__(DockerWorkspace), None, None)


@pytest.mark.skipif(not (os.environ.get("EVO_E2E_PI") and shutil.which("pi")),
                    reason="set EVO_E2E_PI=1 with pi installed and a real model configured")
def test_real_pi_cli(pilot, monkeypatch):
    if not REAL_KEY:
        pytest.skip("OPENAI_API_KEY is required")
    monkeypatch.setenv("OPENAI_API_KEY", REAL_KEY)
    llm = LLMConfig()
    implementer = PiImplementer(llm, ImplementerConfig(kind="pi"))
    result = pilot.agent(implementer=implementer).run(pilot.handoff())
    assert result.status in (RunState.TESTING, RunState.BLOCKED)
    assert result.implementer.exit_status in ("Submitted", "Error", "StepLimit")


@pytest.mark.skipif(not os.environ.get("EVO_E2E_OPENAI"), reason="set EVO_E2E_OPENAI=1 to spend real money")
@pytest.mark.parametrize("kind", ["native", "rlm"])
def test_live_openai_fixes_median(pilot, kind, monkeypatch):
    """Real default model (gpt-6-luna, reasoning high, Responses API). Costs about $0.001 per run."""
    monkeypatch.setenv("OPENAI_API_KEY", REAL_KEY)
    config = ImplementerConfig(kind=kind, step_limit=20, command_timeout_seconds=120)
    implementer = (NativeImplementer if kind == "native" else RLMImplementer)(LLMConfig(), config)
    result = pilot.agent(implementer=implementer, config=pilot.config(hard_max_usd=0.25)).run(pilot.handoff())
    assert result.status == RunState.TESTING, result.blockers
    assert result.budget_used.usd > 0 and result.budget_used.input_tokens > 0
