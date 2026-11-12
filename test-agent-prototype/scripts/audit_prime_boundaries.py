"""Probe Test-stage boundaries; report the unverified caller-auth limitation."""
from __future__ import annotations

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from pydantic import ValidationError

from app.models import Principal
from app.prime_tools import StageTarget, TestStageTools, ToolBudget, ToolPolicy, VerifyInput
from app.providers import SourceRegistry, make_demo_repository
from app.rca_and_repair import PatchPolicyError, validate_patch
from app.run_manager import RunManager


def rejected(call, error_type: type[Exception] | tuple[type[Exception], ...]) -> bool:
    try:
        call()
    except error_type:
        return True
    return False


async def main() -> None:
    with TemporaryDirectory(prefix="evo-prime-audit-") as temporary:
        root = Path(temporary)
        _, registration = make_demo_repository(root / "repository")
        registry = SourceRegistry(root / "state/sources")
        registry.register(registration)
        manager = RunManager(root / "state", registry=registry)
        policy = ToolPolicy(operator_name="audit-operator", allowed_repos=("org/service",),
            allowed_backends=("local_trusted",), max_runtime_seconds=60)
        principal = Principal(name=policy.operator_name, roles=frozenset({"engineer"}),
                              repos=frozenset(policy.allowed_repos))
        tools = TestStageTools(manager, principal, policy)
        request = registry.request_for_pr("org/service", 42)
        context = registry.resolve(request)
        target = StageTarget(repo="org/service", pr_number=42,
            expected_base_sha=context.base_sha, expected_head_sha=context.head_sha,
            spec_id=request.spec_id, spec_hash=request.spec_hash,
            test_profile_id=request.test_profile_id,
            test_profile_hash=context.profile.fingerprint,
            environment_manifest_id=request.environment_manifest_id,
            environment_fingerprint=context.environment.fingerprint,
            policy_version=context.policy_version, correlation_id="security-audit")
        base = target.model_dump(mode="json")
        budget = ToolBudget(max_runtime_seconds=60).model_dump(mode="json")

        for extra in ({"role": "qa"}, {"approved": True}):
            assert rejected(lambda: VerifyInput.model_validate({**base, "budget": budget, **extra}),
                            ValidationError)
        assert rejected(lambda: TestStageTools(manager, None, policy), PermissionError)
        print("PASS: role='qa', approved=True and missing constructor identity refused")

        for key, value in (("expected_head_sha", "f" * 40),
                           ("spec_hash", "f" * 64),
                           ("environment_manifest_id", "wrong-environment")):
            altered = VerifyInput.model_validate({**base, key: value, "budget": budget})
            with patch("app.test_runner.asyncio.create_subprocess_exec") as launch:
                try:
                    await tools.verify_pr(altered)
                except ValueError as exc:
                    assert "STALE" in str(exc)
                else:
                    raise AssertionError(f"Stale {key} was accepted")
                assert not launch.called and not manager.runs
        print("PASS: wrong head SHA, spec hash and environment ID rejected before pytest spawn")

        run = await tools.verify_pr(VerifyInput.model_validate({**base, "budget": budget}))
        for name in ("../secrets.env", "tests/test_api.py"):
            assert rejected(lambda: tools.artifact(run.run_id, name),
                            (ValueError, PermissionError))
        print("PASS: ../secrets.env and tests/test_api.py artifact requests refused")

        for name in ("tests/test_service.py", ".github/workflows/ci.py",
                     "src/auth/login.py", "src/security.py"):
            try:
                validate_patch({name: "x = 1\n"}, (name,), context.snapshot_root)
            except PatchPolicyError as exc:
                assert any("Protected or non-source path" in issue for issue in exc.issues)
            else:
                raise AssertionError(f"Protected patch path was accepted: {name}")
        print("PASS: tests/, .github/, auth and security patch paths refused")

        # No signed caller assertion is checked by this constructor. This
        # illustrates why operator policy alone is not per-caller authentication.
        forged = Principal(name=policy.operator_name, roles=frozenset({"engineer"}),
                           repos=frozenset(policy.allowed_repos))
        TestStageTools(manager, forged, policy)
        print("FAIL: a forged Principal matching static policy is accepted in-process")
        print("Host-verified or cryptographic per-caller identity: NOT IMPLEMENTED")
        raise SystemExit(2)


if __name__ == "__main__":
    asyncio.run(main())
