"""Fixed-command pytest execution with bounded process lifetime and real evidence.

The subprocess backend is intentionally limited to trusted local demo fixtures.
Docker isolates untrusted source; an untrusted profile's observations still cannot
establish readiness. Production must provision independently reviewed test suites.
"""
from __future__ import annotations

import asyncio
from collections import Counter
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
from uuid import uuid4
import xml.etree.ElementTree as ET

from .models import RequirementOutcome, RequirementResult, TestCase, TestExecution
from .preflight import prepare_workspace, validate_snapshot, validate_test_path


MAX_LOG_BYTES = 2 * 1024 * 1024
MAX_JUNIT_BYTES = 10 * 1024 * 1024
_SECRET_NAME = re.compile(r"(?:secret|token|password|api[_-]?key|private[_-]?key|credential)", re.I)


def redact_log(text: str) -> str:
    """Best-effort defense in depth; subprocesses never receive service secrets."""
    for name, secret in os.environ.items():
        if _SECRET_NAME.search(name) and len(secret) >= 4:
            text = text.replace(secret, "[REDACTED]")
    text = re.sub(r"\b(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9_]{12,}|AKIA[A-Z0-9]{16})\b",
                  "[REDACTED]", text)
    text = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+=*", "Bearer [REDACTED]", text)
    return re.sub(
        r'''(?ix)(\b(?:api[_-]?key|password|secret|access[_-]?token|authorization|token)\b["']?\s*[:=]\s*)["']?[^\s,;<>"']+''',
        lambda m: m.group(1) + "[REDACTED]", text)


def _minimal_environment(workspace: Path, *, docker: bool = False) -> dict[str, str]:
    # PATH is needed for Docker/OS process termination, not inherited credentials.
    allowed = ("PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP")
    env = {name: os.environ[name] for name in allowed if name in os.environ}
    if not docker:
        env.update(PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", PYTHONNOUSERSITE="1",
                   PYTHONDONTWRITEBYTECODE="1", PYTHONHASHSEED="0",
                   PYTHONPATH=str(workspace), HOME=str(workspace), USERPROFILE=str(workspace))
    return env


async def _bounded_control(command: list[str], env: dict[str, str]) -> None:
    """Invoke a fixed process-control utility without a command shell."""
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(*command, env=env,
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL)
        await asyncio.wait_for(proc.wait(), 3)
    except (OSError, asyncio.TimeoutError):
        if proc is not None and proc.returncode is None:
            proc.kill()
            try:
                await asyncio.wait_for(proc.wait(), 1)
            except asyncio.TimeoutError:
                pass


async def _kill_process_tree(proc, container_name: str | None, env: dict[str, str]) -> None:
    if container_name:
        await _bounded_control(["docker", "kill", container_name], env)
        await _bounded_control(["docker", "rm", "--force", container_name], env)
    if os.name == "nt":
        # taskkill /T terminates child processes even when a test spawns workers.
        await _bounded_control(["taskkill.exe", "/PID", str(proc.pid), "/T", "/F"], env)
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if proc.returncode is None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
    try:
        await asyncio.wait_for(proc.wait(), 3)
    except asyncio.TimeoutError:
        pass


def _case_node_id(element: ET.Element, workspace: Path) -> str:
    classname, name = element.get("classname", ""), element.get("name", "")
    file_name = element.get("file")
    if file_name:
        path = file_name.replace("\\", "/")
        try:
            candidate = Path(file_name)
            if candidate.is_absolute():
                path = candidate.resolve().relative_to(workspace).as_posix()
        except ValueError:
            pass
        # classname includes class names; retain the suffix after the module.
        module_name = path.removesuffix(".py").replace("/", ".")
        suffix = classname[len(module_name):].strip(".") if classname.startswith(module_name) else ""
        return path + ("::" + suffix.replace(".", "::") if suffix else "") + "::" + name
    # Pytest xunit2 omits `file`. Find the longest classname prefix that is a file.
    pieces = classname.split(".")
    for index in range(len(pieces), 0, -1):
        file_path = "/".join(pieces[:index]) + ".py"
        if (workspace / file_path).is_file():
            return file_path + "::" + "::".join(pieces[index:] + [name])
    return classname.replace(".", "/") + "::" + name


def _read_junit(path: Path, workspace: Path, evidence_id: str) -> tuple[list[TestCase], str | None]:
    try:
        if not path.is_file() or path.is_symlink() or path.stat().st_size > MAX_JUNIT_BYTES:
            raise ValueError("JUnit evidence is missing, linked, or exceeds the size limit.")
        raw = path.read_bytes()
        if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
            raise ValueError("JUnit cannot contain a document type or entity declaration.")
        root = ET.fromstring(raw)
        if root.tag not in {"testsuites", "testsuite"}:
            raise ValueError("JUnit root is not a testsuite.")
        cases = []
        for item in root.iter("testcase"):
            node_id = _case_node_id(item, workspace)
            outcome = ("failed" if item.find("failure") is not None or item.find("error") is not None
                       else "skipped" if item.find("skipped") is not None else "passed")
            cases.append(TestCase(node_id=node_id, outcome=outcome, evidence_id=evidence_id))
        # Collection errors appear directly under testsuite and must count as failure.
        for suite in root.iter("testsuite"):
            if suite.find("error") is not None:
                cases.append(TestCase(node_id="collection", outcome="failed", evidence_id=evidence_id))
        for element in root.iter():
            if element.text:
                element.text = redact_log(element.text)
            if element.tail:
                element.tail = redact_log(element.tail)
            for key, content in list(element.attrib.items()):
                element.set(key, redact_log(content))
        ET.indent(root)
        path.write_bytes(ET.tostring(root, encoding="utf-8", xml_declaration=True))
        return cases, None
    except (OSError, ValueError, ET.ParseError) as exc:
        # Do not retain malformed raw evidence, which could contain credentials.
        if path.exists() and path.is_file() and not path.is_symlink():
            path.write_text("<?xml version='1.0'?><testsuites />", encoding="utf-8")
        return [], str(exc)


async def execute(context, workspace: Path, evidence_dir: Path,
                  cancel_event: asyncio.Event, timeout_seconds: float) -> TestExecution:
    """Run the selected fixed pytest profile and persist bounded, redacted evidence."""
    started = time.monotonic()
    workspace_input = Path(workspace)
    workspace, evidence_dir = workspace_input.resolve(), Path(evidence_dir).resolve()
    evidence_dir.mkdir(parents=True, exist_ok=True)
    log_path, junit_path = evidence_dir / "pytest.log", evidence_dir / "junit.xml"
    log_parts: list[bytes] = []
    captured = 0
    command: list[str] = []
    container_name = None
    proc = None
    reader_task = process_task = cancel_task = None
    timed_out = cancelled = False
    blocked_reason = None
    code = 127
    profile = context.profile
    trusted = bool(profile.trusted)
    try:
        # Validate the supplied root; resolving first would hide a root symlink.
        validate_snapshot(workspace_input)
        targets = [validate_test_path(x) for x in profile.test_paths]
        if not targets:
            raise ValueError("Test profile has no explicit targets.")
        timeout = min(float(timeout_seconds), float(profile.timeout_seconds))
        if timeout <= 0:
            timed_out = True
            raise ValueError("No test execution runtime remains.")
        if cancel_event.is_set():
            cancelled = True
            raise ValueError("Cancelled before a process was started.")
        if profile.backend == "local_trusted":
            if not profile.trusted:
                raise ValueError("Local execution rejects untrusted source; select an isolated Docker profile.")
            config_path = evidence_dir / "pytest-runner.ini"
            config_path.write_text("[pytest]\njunit_family=xunit2\n", encoding="utf-8")
            command = [sys.executable, "-m", "pytest", "-c", str(config_path),
                       "--rootdir", str(workspace), "-q", "--tb=short",
                       "-p", "no:cacheprovider", "--junitxml", str(junit_path), *targets]
        elif profile.backend == "docker":
            image = profile.docker_image or ""
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/:\-]*@sha256:[a-f0-9]{64}", image):
                raise ValueError("Docker execution requires a prebuilt image pinned by SHA-256 digest.")
            container_name = "test-agent-" + uuid4().hex
            config_path = evidence_dir / "pytest-runner.ini"
            config_path.write_text("[pytest]\njunit_family=xunit2\n", encoding="utf-8")
            command = ["docker", "run", "--rm", "--pull=never", "--name", container_name,
                "--network", "none", "--read-only", "--cap-drop", "ALL",
                "--security-opt", "no-new-privileges", "--pids-limit", "128",
                "--memory", "512m", "--memory-swap", "512m", "--cpus", "1",
                "--user", "65534:65534", "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m",
                "--mount", f"type=bind,src={workspace},dst=/workspace",
                "--mount", f"type=bind,src={evidence_dir},dst=/evidence",
                "--workdir", "/workspace", "--env", "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1",
                "--env", "PYTHONNOUSERSITE=1", "--env", "PYTHONDONTWRITEBYTECODE=1",
                "--env", "PYTHONPATH=/workspace", "--env", "HOME=/tmp",
                "--env", "PYTHONHASHSEED=0", "--entrypoint", "python", image,
                "-m", "pytest", "-c", "/evidence/pytest-runner.ini", "--rootdir", "/workspace",
                "-q", "--tb=short", "-p", "no:cacheprovider", "--junitxml", "/evidence/junit.xml", *targets]
        else:
            raise ValueError("Unsupported test execution backend.")
        env = _minimal_environment(workspace, docker=profile.backend == "docker")
        options = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt"
                   else {"start_new_session": True})
        proc = await asyncio.create_subprocess_exec(*command, cwd=workspace, env=env,
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT, **options)

        async def collect():
            nonlocal captured
            while True:
                chunk = await proc.stdout.read(65536)
                if not chunk:
                    break
                room = MAX_LOG_BYTES - captured
                if room > 0:
                    log_parts.append(chunk[:room])
                    captured += min(room, len(chunk))

        reader_task = asyncio.create_task(collect())
        process_task = asyncio.create_task(proc.wait())
        cancel_task = asyncio.create_task(cancel_event.wait())
        done, _ = await asyncio.wait({process_task, cancel_task}, timeout=timeout,
                                     return_when=asyncio.FIRST_COMPLETED)
        cancelled = cancel_event.is_set()
        timed_out = not done
        if cancelled or timed_out:
            await _kill_process_tree(proc, container_name, env)
        else:
            # Terminate background descendants that outlive the pytest parent.
            if os.name != "nt":
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        code = proc.returncode if proc.returncode is not None else -1
        try:
            await asyncio.wait_for(reader_task, 2)
        except asyncio.TimeoutError:
            reader_task.cancel()
            blocked_reason = "Output pipe remained open after process termination."
    except asyncio.CancelledError:
        cancelled = True
        if proc is not None:
            await _kill_process_tree(proc, container_name, _minimal_environment(workspace, docker=True))
        code = -1
    except (ValueError, OSError) as exc:
        blocked_reason = str(exc)
        trusted = False
    finally:
        for task in (reader_task, process_task, cancel_task):
            if task is not None and not task.done():
                task.cancel()
        log = b"".join(log_parts).decode("utf-8", errors="replace")
        if captured >= MAX_LOG_BYTES:
            log += "\n[Output truncated at 2 MiB; complete logs are unavailable.]\n"
            blocked_reason = blocked_reason or "Test log exceeded the evidence capture limit."
        if blocked_reason:
            log += "\nRUNNER: " + blocked_reason + "\n"
        if cancelled:
            log += "\nRUNNER: execution cancelled; partial evidence only.\n"
        if timed_out:
            log += "\nRUNNER: execution deadline exceeded; partial evidence only.\n"
        log_path.write_text(redact_log(log), encoding="utf-8")

    cases, junit_error = _read_junit(junit_path, workspace, f"{evidence_dir.name}/junit.xml")
    counts = Counter(case.outcome for case in cases)
    if junit_error and not (cancelled or timed_out):
        blocked_reason = blocked_reason or junit_error
        trusted = False
    if code == 0 and (not cases or counts["failed"]):
        blocked_reason = blocked_reason or "Successful exit does not agree with complete JUnit evidence."
        trusted = False
    if code not in (0, 1) and not (cancelled or timed_out):
        blocked_reason = blocked_reason or f"Pytest could not complete normally (exit {code})."
    if blocked_reason:
        with log_path.open("a", encoding="utf-8") as output:
            output.write("\nRUNNER: " + redact_log(blocked_reason) + "\n")
    return TestExecution(exit_code=code, passed=counts["passed"], failed=counts["failed"],
        skipped=counts["skipped"], cases=cases, junit_path=junit_path if junit_path.exists() else None,
        log_path=log_path, command=command, timed_out=timed_out, cancelled=cancelled,
        trusted=trusted, blocked_reason=blocked_reason, duration_seconds=time.monotonic() - started)


def requirement_results(spec, executions) -> list[RequirementResult]:
    """Map the latest original-head execution to all required acceptance nodes.

    The orchestrator must pass original-head observations only. Base, flaky, and
    candidate executions are diagnostic evidence and cannot overwrite a failure.
    """
    if isinstance(executions, TestExecution):
        executions = [executions]
    execution = executions[-1] if executions else None
    acceptance = {test.id: test for test in spec.acceptance_tests}
    cases: dict[str, list[TestCase]] = {}
    if execution is not None:
        for case in execution.cases:
            cases.setdefault(case.node_id.replace("\\", "/"), []).append(case)
    results = []
    for requirement in spec.requirements:
        ids = list(requirement.test_ids)
        mapped = []
        complete = bool(ids)
        for test_id in ids:
            test = acceptance.get(test_id)
            observations = cases.get(test.node_id.replace("\\", "/"), []) if test else []
            if not observations:
                complete = False
            mapped.extend(observations)
        if execution is None or execution.blocked_reason or not execution.trusted or execution.cancelled or execution.timed_out:
            outcome, reason = RequirementOutcome.NOT_VERIFIED, "No complete trusted execution is available."
        elif any(case.outcome == "failed" for case in mapped):
            outcome, reason = RequirementOutcome.FAIL, "A mapped acceptance test failed."
        elif not complete or any(case.outcome != "passed" for case in mapped) or execution.exit_code != 0:
            outcome, reason = RequirementOutcome.NOT_VERIFIED, "Required tests were absent, skipped, incomplete, or the suite failed."
        else:
            outcome, reason = RequirementOutcome.PASS, "All mapped acceptance tests passed in this execution."
        results.append(RequirementResult(id=requirement.id, test_ids=ids, result=outcome,
            evidence_ids=sorted({case.evidence_id for case in mapped}), reason=reason))
    return results
