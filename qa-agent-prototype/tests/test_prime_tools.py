import json
from pathlib import Path

import pytest

import demo_acceptance as demo
from app import fixtures as fx
from app.models import Policy
from app.prime_tools import OperatorConfig, QATools, Target, load_tools


def service(tmp_path, scenario=demo.scenario_clean, allowed=None):
    repo, verification, policy = scenario(tmp_path / 'fixture')
    data = json.loads(verification.read_text())
    target = Target(repo=repo, head_ref='pr', base_sha=data['base_sha'],
                    head_sha=data['reviewed_head_sha'], verification=verification,
                    spec_hash=data['spec_hash'], test_profile_hash=data['test_profile_hash'])
    config = OperatorConfig(targets={'pr-7': target}, output_dir=tmp_path/'qa-service', policy=policy)
    if allowed is not None:
        config.allowed_tools = allowed
    return QATools(config), target


def test_inspection_and_scan_need_no_test_handoff(tmp_path):
    tools, _ = service(tmp_path)
    tools.config.targets['pr-7'].verification = None
    assert tools.call('qa_inspect', 'pr-7')['status'] == 'INSPECTED'
    scan = tools.call('qa_scan', 'pr-7')
    assert scan['status'] == 'SCANNED' and scan['approval'] is False
    assert tools.call('qa_review', 'pr-7')['status'] == 'BLOCKED'


@pytest.mark.parametrize('scenario,expected', [(s[1], s[2].value) for s in demo.SCENARIOS])
def test_registered_reviews(tmp_path, scenario, expected):
    tools, target = service(tmp_path, scenario)
    # Stale scenario intentionally registers the Test-reviewed commit.
    result = tools.call('qa_review', 'pr-7')
    assert result['status'] == expected


@pytest.mark.parametrize('tool,target', [('qa_scan', 'unknown'), ('shell_run', 'pr-7'), ('qa_review', 'pr-7')])
def test_operator_grants_are_enforced(tmp_path, tool, target):
    tools, _ = service(tmp_path, allowed=['qa_inspect'])
    with pytest.raises(PermissionError):
        tools.call(tool, target)
    event = json.loads((tools.config.output_dir/'events.jsonl').read_text())
    assert event['status'] == 'denied'


def test_tampered_evidence_is_blocked(tmp_path):
    tools, target = service(tmp_path)
    (target.verification.parent/'test_report.md').write_text('changed')
    assert tools.call('qa_review', 'pr-7')['status'] == 'BLOCKED'


def test_upstream_confirmation_survives_nonpilot(tmp_path):
    tools, _ = service(tmp_path)
    tools.config.policy = Policy(pilot_mode=False)
    result = tools.call('qa_review', 'pr-7')
    assert result['decision']['human_confirmation_required'] is True


def test_scan_redacts_secrets_and_logs_metadata_only(tmp_path):
    tools, _ = service(tmp_path, demo.scenario_leaked_secret)
    result = tools.call('qa_scan', 'pr-7')
    assert any(f['blocking'] for c in result['checks'] for f in c['findings'])
    assert fx.fake_aws_key() not in json.dumps(result)
    events = (tools.config.output_dir/'events.jsonl').read_text()
    assert fx.fake_aws_key() not in events
    assert [json.loads(line)['status'] for line in events.splitlines()] == ['started', 'completed']


def test_head_movement_during_review_invalidates_saved_report(tmp_path, monkeypatch):
    import app.prime_tools as module
    tools, target = service(tmp_path)
    original = module.review
    def moving_review(*args, **kwargs):
        decision = original(*args, **kwargs)
        fx.push_to_pr(target.repo, {'README.md': '# changed\n'})
        return decision
    monkeypatch.setattr(module, 'review', moving_review)
    result = tools.call('qa_review', 'pr-7')
    assert result['status'] == 'STALE'
    report = tools.config.output_dir/result['call_id']/'qa_decision.json'
    assert json.loads(report.read_text())['verdict'] == 'STALE'


def test_operator_config_cannot_live_in_reviewed_repo(tmp_path):
    tools, target = service(tmp_path)
    path = target.repo/'operator.json'
    path.write_text(tools.config.model_dump_json())
    with pytest.raises(ValueError, match='outside'):
        load_tools(path)


def test_mcp_stdio_roundtrip(tmp_path):
    pytest.importorskip('mcp')
    import asyncio
    import os
    import sys
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    tools, _ = service(tmp_path)
    config = tmp_path/'operator.json'
    config.write_text(tools.config.model_dump_json())
    async def run():
        params = StdioServerParameters(command=sys.executable, args=['-m', 'app.prime_mcp'],
            cwd=str(Path(__file__).resolve().parents[1]), env={**os.environ, 'EVO_QA_CONFIG': str(config)})
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                names = {t.name for t in (await session.list_tools()).tools}
                assert names == {'qa_inspect', 'qa_scan', 'qa_review'}
                response = await session.call_tool('qa_review', {'target_id': 'pr-7'})
                assert not response.is_error
                # SDK structured output wraps plain dicts on some releases.
                data = response.structured_content
                if 'result' in data:
                    data = data['result']
                assert data['status'] == 'APPROVED'
                denied = await session.call_tool('qa_scan', {'target_id': 'not-registered'})
                assert denied.is_error
    asyncio.run(run())
