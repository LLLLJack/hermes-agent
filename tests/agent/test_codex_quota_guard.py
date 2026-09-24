from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import yaml

from agent.account_usage import AccountUsageSnapshot, AccountUsageWindow
from agent.codex_quota_guard import QuotaDecision
import agent.codex_quota_guard as guard


@pytest.fixture(autouse=True)
def _reset_guard():
    guard.reset_for_tests()
    yield
    guard.reset_for_tests()


def _snapshot(session_used, weekly_used):
    from datetime import datetime, timezone
    return AccountUsageSnapshot(
        provider="openai-codex",
        source="usage_api",
        fetched_at=datetime.now(timezone.utc),
        windows=(
            AccountUsageWindow(label="Session", used_percent=session_used),
            AccountUsageWindow(label="Weekly", used_percent=weekly_used),
        ),
    )


def _policy(tmp_path, monkeypatch, *, threshold=30, hysteresis=5, refresh=5, stale=30):
    path = tmp_path / "policy.yaml"
    path.write_text(yaml.safe_dump({
        "enabled": True,
        "refresh_seconds": refresh,
        "stale_seconds": stale,
        "timeout_seconds": 1,
        "hysteresis_percent": hysteresis,
        "rules": [{
            "name": "protect-sol",
            "instances": ["h1"],
            "when": {
                "five_hour_remaining_lte": threshold,
                "weekly_remaining_lte": threshold,
                "logic": "any",
            },
            "block": [{"provider": "openai-codex", "models": ["gpt-5.6-sol", "gpt-6-sol"]}],
            "allow": [{"provider": "openai-codex", "models": ["gpt-5.6-luna", "gpt-6-luna"]}],
        }],
    }), encoding="utf-8")
    monkeypatch.setenv("HERMES_CODEX_QUOTA_POLICY", str(path))
    monkeypatch.setenv("HERMES_CODEX_QUOTA_INSTANCE", "h1")
    return path


def _agent(model="gpt-5.6-sol"):
    return SimpleNamespace(
        provider="openai-codex",
        model=model,
        api_key="live-token",
        base_url="https://chatgpt.com/backend-api/codex",
    )


def _mock_creds(monkeypatch):
    monkeypatch.setattr(
        "agent.account_usage._resolve_codex_usage_credentials",
        lambda base_url, api_key: ("resolved-token", base_url or "https://chatgpt.com/backend-api/codex", "acct"),
    )


def test_blocks_configured_sol_but_keeps_luna_allowed(tmp_path, monkeypatch):
    _policy(tmp_path, monkeypatch)
    _mock_creds(monkeypatch)
    monkeypatch.setattr("agent.account_usage._fetch_codex_account_usage", lambda **kw: _snapshot(71, 20))

    sol = guard.route_decision(_agent("gpt-5.6-sol"))
    luna = guard.route_decision(_agent("gpt-5.6-luna"))

    assert sol.blocked is True
    assert sol.five_hour_remaining == 29
    assert sol.rule == "protect-sol"
    assert luna.blocked is False


def test_instance_not_selected_is_fail_open(tmp_path, monkeypatch):
    _policy(tmp_path, monkeypatch)
    monkeypatch.setenv("HERMES_CODEX_QUOTA_INSTANCE", "h2")
    called = {"fetch": 0}
    monkeypatch.setattr(
        "agent.account_usage._fetch_codex_account_usage",
        lambda **kw: called.__setitem__("fetch", called["fetch"] + 1),
    )

    assert guard.route_decision(_agent()).blocked is False
    assert called["fetch"] == 0


def test_hysteresis_prevents_threshold_flapping(tmp_path, monkeypatch):
    _policy(tmp_path, monkeypatch, threshold=30, hysteresis=5, refresh=5)
    _mock_creds(monkeypatch)
    states = iter([_snapshot(71, 20), _snapshot(67, 20), _snapshot(64, 20)])
    monkeypatch.setattr("agent.account_usage._fetch_codex_account_usage", lambda **kw: next(states))
    clock = iter([0.0, 10.0, 20.0])
    monkeypatch.setattr(guard.time, "monotonic", lambda: next(clock))

    assert guard.route_decision(_agent()).blocked is True   # 29% remaining -> block
    assert guard.route_decision(_agent()).blocked is True   # 33% remains latched (<=35%)
    assert guard.route_decision(_agent()).blocked is False  # 36% clears hysteresis


def test_stale_last_known_good_then_fail_open(tmp_path, monkeypatch):
    _policy(tmp_path, monkeypatch, refresh=5, stale=15)
    _mock_creds(monkeypatch)
    results = iter([_snapshot(80, 20), None, None])
    monkeypatch.setattr("agent.account_usage._fetch_codex_account_usage", lambda **kw: next(results))
    clock = iter([0.0, 10.0, 30.0])
    monkeypatch.setattr(guard.time, "monotonic", lambda: next(clock))

    assert guard.route_decision(_agent()).blocked is True
    assert guard.route_decision(_agent()).blocked is True
    final = guard.route_decision(_agent())
    assert final.blocked is False
    assert "fail-open" in final.reason


def test_fallback_candidate_is_skipped_when_policy_blocks(monkeypatch):
    from agent.chat_completion_helpers import _should_skip_fallback_candidate

    agent = SimpleNamespace(
        provider="zai", model="glm-5.2", base_url="https://api.z.ai/v1",
        _entitlement_rejected_models=set(),
    )
    monkeypatch.setattr(
        "agent.codex_quota_guard.route_decision",
        lambda *a, **kw: QuotaDecision(blocked=True, reason="protected"),
    )
    fb = {"provider": "openai-codex", "model": "gpt-5.6-sol"}
    assert _should_skip_fallback_candidate(
        agent, fb, ("openai-codex", "gpt-5.6-sol", ""), "openai-codex", "gpt-5.6-sol", set()
    ) is True


def test_primary_restore_stays_on_fallback_while_policy_blocks(monkeypatch):
    from agent.agent_runtime_helpers import restore_primary_runtime

    agent = SimpleNamespace(
        _fallback_activated=True,
        _fallback_index=1,
        _rate_limited_until=0,
        _primary_runtime={
            "provider": "openai-codex",
            "model": "gpt-5.6-sol",
            "base_url": "https://chatgpt.com/backend-api/codex",
        },
    )
    monkeypatch.setattr(
        "agent.codex_quota_guard.route_decision",
        lambda *a, **kw: QuotaDecision(blocked=True, reason="protected"),
    )
    assert restore_primary_runtime(agent) is False


def test_pre_dispatch_guard_activates_existing_fallback(monkeypatch):
    from agent.error_classifier import FailoverReason
    from agent.turn_api_call import codex_quota_guard

    retry = SimpleNamespace(primary_recovery_attempted=True, restart_with_rebuilt_messages=False)
    agent = SimpleNamespace(
        provider="openai-codex", model="gpt-5.6-sol",
        _buffer_vprint=MagicMock(), _buffer_status=MagicMock(),
        _flush_status_buffer=MagicMock(), _persist_session=MagicMock(),
    )
    seen = {}
    def _activate(reason=None):
        seen["reason"] = reason
        return True
    agent._try_activate_fallback = _activate
    monkeypatch.setattr(
        "agent.codex_quota_guard.route_decision",
        lambda *a, **kw: QuotaDecision(blocked=True, reason="5h 20% remaining"),
    )
    monkeypatch.setattr(
        "agent.conversation_loop._arm_fallback_restart",
        lambda agent, api_messages, active_system_prompt, retry: setattr(retry, "restart_with_rebuilt_messages", True) or active_system_prompt,
    )

    verdict = codex_quota_guard(
        agent,
        _retry=retry,
        api_messages=[],
        messages=[],
        conversation_history=[],
        active_system_prompt="system",
        retry_count=2,
        compression_attempts=3,
        api_call_count=1,
    )

    assert verdict.action == "break"
    assert verdict.retry_count == 0
    assert verdict.compression_attempts == 0
    assert retry.restart_with_rebuilt_messages is True
    assert seen["reason"] is FailoverReason.quota_policy
