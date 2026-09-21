"""VPS r34: idle/daily rollover keeps the legacy user + agent boundary notices."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.run_turn import GatewayTurnMixin


def _runner(policy=None):
    runner = object.__new__(GatewayTurnMixin)
    runner.session_store = SimpleNamespace(
        config=GatewayConfig.from_dict({
            "session_reset": policy or {
                "mode": "both",
                "idle_minutes": 1440,
                "at_hour": 4,
                "notify": True,
            }
        })
    )
    runner._reset_notice_session_info = lambda _source: None
    runner._thread_metadata_for_source = lambda _source: {}
    return runner

def _source(platform=Platform.WEIXIN):
    return SimpleNamespace(platform=platform, chat_type="dm", chat_id="wx-chat")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reason", "sidecar_fragment", "notice_fragment"),
    [
        ("idle", "expired due to inactivity", "inactive for 24h"),
        ("daily", "reset by the daily schedule", "daily schedule at 4:00"),
    ],
)
async def test_weixin_idle_daily_reset_notifies_once(
    reason, sidecar_fragment, notice_fragment, monkeypatch
):
    runner = _runner()
    adapter = SimpleNamespace(send=AsyncMock())
    runner._adapter_for_source = lambda _source: adapter
    monkeypatch.setattr(
        "gateway.run_turn.build_channel_continuity_note", lambda *_args: None
    )
    entry = SimpleNamespace(
        auto_reset_reason=reason,
        reset_had_activity=True,
        prev_session_id="old-session",
    )
    notes = []

    await runner._hmwa_deliver_auto_reset_notice(entry, _source(), notes)

    assert len(notes) == 1
    assert sidecar_fragment in notes[0]
    adapter.send.assert_awaited_once()
    sent = adapter.send.await_args.args[1]
    assert "Conversation history cleared." in sent
    assert notice_fragment in sent
    assert entry.auto_reset_reason is None


@pytest.mark.asyncio
async def test_idle_reset_without_prior_activity_stays_silent(monkeypatch):
    runner = _runner()
    adapter = SimpleNamespace(send=AsyncMock())
    runner._adapter_for_source = lambda _source: adapter
    monkeypatch.setattr(
        "gateway.run_turn.build_channel_continuity_note", lambda *_args: None
    )
    entry = SimpleNamespace(
        auto_reset_reason="idle",
        reset_had_activity=False,
        prev_session_id=None,
    )
    notes = []
    await runner._hmwa_deliver_auto_reset_notice(entry, _source(), notes)

    assert "fresh conversation with no prior context" in notes[0]
    adapter.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_excluded_api_server_does_not_get_user_notice(monkeypatch):
    runner = _runner()
    adapter = SimpleNamespace(send=AsyncMock())
    runner._adapter_for_source = lambda _source: adapter
    monkeypatch.setattr(
        "gateway.run_turn.build_channel_continuity_note", lambda *_args: None
    )
    entry = SimpleNamespace(
        auto_reset_reason="daily",
        reset_had_activity=True,
        prev_session_id="old",
    )
    notes = []

    await runner._hmwa_deliver_auto_reset_notice(
        entry, _source(Platform.API_SERVER), notes
    )

    assert "daily schedule" in notes[0]
    adapter.send.assert_not_awaited()
