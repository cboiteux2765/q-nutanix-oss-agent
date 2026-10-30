"""Where implementer and test commands run (the host for trusted repos, or a Docker sandbox),
and how the approved test command's JUnit output becomes structured evidence.

Git operations always run on the host via gitops; workspaces only execute commands
inside the checked-out repository.
"""
from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

from .config import WorkspaceConfig
from .models import EnvironmentSpec, LocalTestEvidence, TestRunSummary
from .safety import redact, sensitive_env_names

MAX_CAPTURED_CHARS = 200_000


class WorkspaceError(Exception):
    pass


@dataclass
class CommandResult:
    command: str
    returncode: int
    output: str
    timed_out: bool
    duration_seconds: float


def _truncate(text: str) -> str:
    if len(text) <= MAX_CAPTURED_CHARS:
        return text
    half = MAX_CAPTURED_CHARS // 2
    return f"{text[:half]}\n... [{len(text) - MAX_CAPTURED_CHARS} characters truncated] ...\n{text[-half:]}"


def _kill_tree(proc: subprocess.Popen) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _run_process(argv: list[str], *, cwd: Path | None, env: dict[str, str] | None, timeout: float,
                 label: str) -> CommandResult:
    started = time.monotonic()
    proc = subprocess.Popen(argv, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, encoding="utf-8", errors="replace",
                            start_new_session=os.name == "posix")
    timed_out = False
    try:
        output, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_tree(proc)
        output, _ = proc.communicate()
        output = (output or "") + f"\n[evo] command timed out after {timeout:.0f}s"
    return CommandResult(label, proc.returncode if not timed_out else -1, _truncate(output or ""), timed_out,
                         round(time.monotonic() - started, 3))


def find_bash() -> str:
    """Locate a real bash. On Windows prefer Git Bash and never pick WSL's System32 shim."""
    configured = os.environ.get("EVO_BASH")
    if configured:
        return configured
    if os.name != "nt":
        return shutil.which("bash") or "/bin/bash"
    git_exe = shutil.which("git")
    if git_exe:
        for parent in Path(git_exe).resolve().parents:
            candidate = parent / "bin" / "bash.exe"
            if candidate.is_file():
                return str(candidate)
    for candidate in (r"C:\Program Files\Git\bin\bash.exe", r"C:\Program Files (x86)\Git\bin\bash.exe"):
        if Path(candidate).is_file():
            return candidate
    found = shutil.which("bash")
    if found and "system32" not in found.lower():
        return found
    raise WorkspaceError("bash not found; install Git for Windows or set EVO_BASH")


class Workspace(ABC):
    backend: str

    def __init__(self, root: Path, config: WorkspaceConfig):
        self.root = Path(root).resolve()
        self.config = config

    def __enter__(self) -> Workspace:
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def start(self) -> None:  # noqa: B027 - optional hook
        pass

    def close(self) -> None:  # noqa: B027 - optional hook
        pass

    def enter_isolation(self) -> None:  # noqa: B027 - optional hook
        """Called after setup commands, before the implementer runs (e.g. drop network)."""

    @property
    def display_root(self) -> str:
        """Repository path as seen by commands running inside the workspace."""
        return str(self.root)

    @abstractmethod
    def run(self, command: str, timeout: float | None = None) -> CommandResult: ...

    @abstractmethod
    def spawn_python(self, script: str, stderr_path: Path) -> subprocess.Popen:
        """Start a long-lived `python -u <script>` (path relative to the repo root) with stdin/stdout pipes."""


def _popen_pipes(argv: list[str], cwd: Path | None, env: dict[str, str] | None, stderr_path: Path) -> subprocess.Popen:
    stderr_path.parent.mkdir(parents=True, exist_ok=True)
    with stderr_path.open("ab") as stderr:
        return subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=stderr, encoding="utf-8", errors="replace", bufsize=1,
                                start_new_session=os.name == "posix")


def kill_process_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        _kill_tree(proc)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass


class LocalWorkspace(Workspace):
    """Runs commands on the host in the cloned repository. NOT a security boundary."""

    backend = "local"

    def __init__(self, root: Path, config: WorkspaceConfig):
        super().__init__(root, config)
        self.bash = find_bash()

    def _env(self) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if k not in set(sensitive_env_names())}
        # Resolve `python` to the agent's interpreter so pytest is available without installing.
        env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
        env.update({"PYTHONDONTWRITEBYTECODE": "1", "PAGER": "cat", "GIT_PAGER": "cat",
                    "PIP_PROGRESS_BAR": "off", "TQDM_DISABLE": "1", "PYTHONIOENCODING": "utf-8"})
        env.pop("VIRTUAL_ENV", None)
        return env

    def run(self, command: str, timeout: float | None = None) -> CommandResult:
        return _run_process([self.bash, "-c", command], cwd=self.root, env=self._env(),
                            timeout=timeout or self.config.command_timeout_seconds, label=command)

    def host_env(self) -> dict[str, str]:
        """Secret-stripped environment for host processes started in this workspace (e.g. pi)."""
        return self._env()

    def spawn_python(self, script: str, stderr_path: Path) -> subprocess.Popen:
        env = {**self._env(), "EVO_BASH": self.bash}
        return _popen_pipes([sys.executable, "-u", "-I", script], self.root, env, stderr_path)


def docker_available(timeout: float = 15) -> bool:
    if not shutil.which("docker"):
        return False
    try:
        proc = subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"], capture_output=True,
                              timeout=timeout)
    except (subprocess.TimeoutExpired, OSError):
        return False
    return proc.returncode == 0


class DockerWorkspace(Workspace):
    """Long-lived hardened container with the repository bind-mounted at /workspace."""

    backend = "docker"
    MOUNT = "/workspace"

    def __init__(self, root: Path, config: WorkspaceConfig):
        super().__init__(root, config)
        self.container = f"evo-coding-{uuid.uuid4().hex[:12]}"
        self._started = False

    @property
    def display_root(self) -> str:
        return self.MOUNT

    def start(self) -> None:
        network = "bridge" if self.config.docker_network_during_setup else "none"
        argv = [
            "docker", "run", "-d", "--rm", "--name", self.container,
            "--network", network,
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--pids-limit", str(self.config.docker_pids_limit),
            "--memory", self.config.docker_memory, "--cpus", self.config.docker_cpus,
            "-e", "PYTHONDONTWRITEBYTECODE=1", "-e", "PAGER=cat", "-e", "PIP_PROGRESS_BAR=off",
            "-v", f"{self.root}:{self.MOUNT}", "-w", self.MOUNT,
        ]
        if self.config.docker_user:
            argv += ["--user", self.config.docker_user]
        argv += [self.config.docker_image, "sleep", "infinity"]
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=180)
        if proc.returncode != 0:
            raise WorkspaceError(f"could not start sandbox container: {proc.stderr.strip()[:500]}")
        self._started = True

    def enter_isolation(self) -> None:
        if self.config.docker_network_during_setup:
            subprocess.run(["docker", "network", "disconnect", "bridge", self.container], capture_output=True,
                           timeout=60)

    def run(self, command: str, timeout: float | None = None) -> CommandResult:
        if not self._started:
            raise WorkspaceError("docker workspace not started")
        timeout = timeout or self.config.command_timeout_seconds
        result = _run_process(["docker", "exec", "-w", self.MOUNT, self.container, "bash", "-c", command],
                              cwd=None, env=None, timeout=timeout, label=command)
        if result.timed_out:
            # Kill everything except PID 1 inside the container so runaway children don't linger.
            subprocess.run(["docker", "exec", self.container, "sh", "-c", "kill -9 -1"], capture_output=True,
                           timeout=30)
        return result

    def spawn_python(self, script: str, stderr_path: Path) -> subprocess.Popen:
        if not self._started:
            raise WorkspaceError("docker workspace not started")
        return _popen_pipes(["docker", "exec", "-i", "-w", self.MOUNT, self.container, "python", "-u", "-I",
                             script], None, None, stderr_path)

    def close(self) -> None:
        if self._started:
            subprocess.run(["docker", "rm", "-f", self.container], capture_output=True, timeout=60)
            self._started = False


def make_workspace(root: Path, config: WorkspaceConfig) -> Workspace:
    if config.backend == "docker":
        return DockerWorkspace(root, config)
    return LocalWorkspace(root, config)


ARTIFACT_DIR = ".evo-artifacts"


@dataclass
class JUnitResults:
    cases: dict[str, str] = field(default_factory=dict)  # normalized test id -> passed|failed|error|skipped

    def count(self, outcome: str) -> int:
        return sum(1 for value in self.cases.values() if value == outcome)


def normalize_test_id(test_id: str) -> str:
    """Make pytest node IDs and JUnit classname/name pairs comparable.

    tests/test_x.py::TestA::test_b  and  classname=tests.test_x.TestA name=test_b
    both normalize to tests/test_x/TestA/test_b.
    """
    test_id = test_id.strip().replace("\\", "/")
    params = ""
    if "[" in test_id:
        test_id, params = test_id.split("[", 1)
        params = "[" + params
    test_id = re.sub(r"\.py(?=::|$)", "", test_id)
    return test_id.replace("::", "/").replace(".", "/") + params


def parse_junit(path: Path) -> JUnitResults:
    results = JUnitResults()
    root = ET.parse(path).getroot()
    for case in root.iter("testcase"):
        classname = case.get("classname", "")
        name = case.get("name", "")
        test_id = normalize_test_id(f"{classname}::{name}" if classname else name)
        if case.find("failure") is not None:
            outcome = "failed"
        elif case.find("error") is not None:
            outcome = "error"
        elif case.find("skipped") is not None:
            outcome = "skipped"
        else:
            outcome = "passed"
        results.cases[test_id] = outcome
    return results


def evidence_for_hints(results: JUnitResults | None, hints: list[str]) -> tuple[LocalTestEvidence, list[str]]:
    if not hints:
        return LocalTestEvidence.NO_HINTS, []
    if results is None:
        return LocalTestEvidence.NOT_FOUND, []
    outcomes: list[str] = []
    found_hints: list[str] = []
    missing = False
    for hint in hints:
        key = normalize_test_id(hint)
        found = [out for tid, out in results.cases.items() if tid == key or tid.startswith(key + "[")]
        if found:
            found_hints.append(hint)
            outcomes += found
        else:
            missing = True
    if any(out in ("failed", "error") for out in outcomes):
        return LocalTestEvidence.FAIL, found_hints
    if missing:
        return LocalTestEvidence.NOT_FOUND, found_hints
    return LocalTestEvidence.PASS, found_hints


def run_tests(workspace: Workspace, env: EnvironmentSpec, label: str, artifacts_dir: Path, timeout: float,
              commit: str | None = None, secrets: list[str] = ()) -> tuple[TestRunSummary, JUnitResults | None]:
    rel_junit = f"{ARTIFACT_DIR}/junit-{label}.xml"
    host_junit = workspace.root / rel_junit
    host_junit.parent.mkdir(parents=True, exist_ok=True)
    host_junit.unlink(missing_ok=True)
    command = f"{env.test_command} --junitxml={rel_junit} -p no:cacheprovider"
    result = workspace.run(command, timeout=timeout)

    artifacts_dir.mkdir(parents=True, exist_ok=True)
    log_name = f"tests-{label}.log"
    (artifacts_dir / log_name).write_text(redact(result.output, secrets), encoding="utf-8")
    junit: JUnitResults | None = None
    junit_name = None
    if host_junit.is_file():
        junit_name = f"junit-{label}.xml"
        shutil.copyfile(host_junit, artifacts_dir / junit_name)
        try:
            junit = parse_junit(host_junit)
        except ET.ParseError:
            junit = None
    failing = sorted(t for t, o in (junit.cases.items() if junit else []) if o in ("failed", "error"))
    summary = TestRunSummary(
        label=label,
        command=command,
        commit=commit,
        returncode=result.returncode,
        timed_out=result.timed_out,
        passed=junit.count("passed") if junit else 0,
        failed=junit.count("failed") if junit else 0,
        errors=(junit.count("error") if junit else 0) + (0 if junit or result.returncode == 0 else 1),
        skipped=junit.count("skipped") if junit else 0,
        duration_seconds=result.duration_seconds,
        failing_tests=failing,
        junit_artifact=junit_name,
        log_artifact=log_name,
    )
    return summary, junit
