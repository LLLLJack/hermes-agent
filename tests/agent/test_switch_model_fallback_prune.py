"""Regression tests for configured fallback policy across manual model switches.

The VPS uses an administrator-defined multi-provider chain.  A manual /model
switch changes the primary route but must not destructively edit that policy;
backend-identity checks skip the currently active route when fallback is needed.
"""

from unittest.mock import MagicMock, patch

from run_agent import AIAgent


def _make_agent(chain):
    agent = AIAgent.__new__(AIAgent)

    agent.provider = "openrouter"
    agent.model = "x-ai/grok-4"
    agent.base_url = "https://openrouter.ai/api/v1"
    agent.api_key = "or-key"
    agent.api_mode = "chat_completions"
    agent.client = MagicMock()
    agent._client_kwargs = {"api_key": "or-key", "base_url": "https://openrouter.ai/api/v1"}
    agent.context_compressor = None
    agent._anthropic_api_key = ""
    agent._anthropic_base_url = None
    agent._anthropic_client = None
    agent._is_anthropic_oauth = False
    agent._cached_system_prompt = "cached"
    agent._primary_runtime = {}
    agent._fallback_activated = False
    agent._fallback_index = 0
    agent._configured_fallback_chain = [dict(entry) for entry in chain]
    agent._fallback_chain = [dict(entry) for entry in chain]
    agent._fallback_model = chain[0] if chain else None

    return agent


def _switch_to_anthropic(agent):
    with (
        patch("agent.anthropic_adapter.build_anthropic_client", return_value=MagicMock()),
        patch("agent.anthropic_credentials.resolve_anthropic_token", return_value="sk-ant-xyz"),
        patch("agent.anthropic_credentials._is_oauth_token", return_value=False),
        patch("hermes_cli.timeouts.get_provider_request_timeout", return_value=None),
    ):
        agent.switch_model(
            new_model="claude-sonnet-4-5",
            new_provider="anthropic",
            api_key="sk-ant-xyz",
            base_url="https://api.anthropic.com",
            api_mode="anthropic_messages",
        )


def test_switch_preserves_configured_fallback_policy():
    chain = [
        {"provider": "openrouter", "model": "x-ai/grok-4"},
        {"provider": "nous", "model": "hermes-4"},
    ]
    agent = _make_agent(chain)

    _switch_to_anthropic(agent)

    assert agent._fallback_chain == chain
    assert agent._configured_fallback_chain == chain
    assert agent._fallback_model == chain[0]


def test_repeated_switch_bookkeeping_reseeds_from_configured_policy():
    from agent.agent_runtime_helpers import _finish_switch

    chain = [
        {"provider": "antigravity-cli", "model": "gemini-3.8-flash-high"},
        {"provider": "vertex", "model": "google/gemini-3.8-flash"},
        {"provider": "openai-codex", "model": "gpt-5.6-luna"},
    ]
    agent = _make_agent(chain)

    # Simulate an already-traversed/shortened runtime view, then another manual switch.
    agent._fallback_chain = [dict(chain[-1])]
    agent._fallback_model = chain[-1]
    _finish_switch(agent, "antigravity-cli", "openai-codex", "antigravity-cli")

    assert agent._fallback_chain == chain
    assert agent._fallback_model == chain[0]
    assert agent._fallback_index == 0


def test_switch_with_empty_chain_stays_empty():
    agent = _make_agent([])

    _switch_to_anthropic(agent)

    assert agent._fallback_chain == []
    assert agent._fallback_model is None


def test_manual_switch_clears_provider_fallback_provenance():
    agent = _make_agent([
        {"provider": "openrouter", "model": "x-ai/grok-4"},
        {"provider": "nous", "model": "hermes-4"},
    ])
    agent._provider_fallback_active = True
    agent._provider_fallback_route = ("fallback-model", "fallback-provider")

    _switch_to_anthropic(agent)

    assert agent._provider_fallback_active is False
    assert agent._provider_fallback_route is None




def test_switch_within_same_provider_preserves_chain():
    chain = [{"provider": "openrouter", "model": "x-ai/grok-4"}]
    agent = _make_agent(chain)

    with patch("hermes_cli.timeouts.get_provider_request_timeout", return_value=None):
        agent.switch_model(
            new_model="openai/gpt-5",
            new_provider="openrouter",
            api_key="or-key",
            base_url="https://openrouter.ai/api/v1",
        )

    assert agent._fallback_chain == chain
