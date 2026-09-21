"""VPS compatibility: top-level session_reset rolls messaging routes on the next real user turn."""
from datetime import datetime, timedelta

from gateway.config import GatewayConfig, Platform
from gateway.session import SessionSource, SessionStore


def _store(tmp_path, policy):
    return SessionStore(
        tmp_path / "sessions",
        GatewayConfig.from_dict({"session_reset": policy}),
    )


def _age(entry, *, minutes):
    entry.updated_at = datetime.now() - timedelta(minutes=minutes)


def test_top_level_session_reset_idle_rotates_on_next_user_activity(tmp_path):
    store = _store(tmp_path, {"mode": "idle", "idle_minutes": 1, "at_hour": 4})
    source = SessionSource(platform=Platform.WEIXIN, chat_id="wx-user", user_id="wx-user")
    old = store.get_or_create_session(source)
    _age(old, minutes=2)
    store._save()

    new = store.get_or_create_session(source)

    assert new.session_id != old.session_id
    assert new.was_auto_reset is True
    assert new.auto_reset_reason == "idle"
    assert store._db.get_session(old.session_id)["end_reason"] == "idle"
    store._db.close()


def test_internal_activity_does_not_time_rotate(tmp_path):
    store = _store(tmp_path, {"mode": "idle", "idle_minutes": 1, "at_hour": 4})
    source = SessionSource(platform=Platform.WEIXIN, chat_id="wx-user", user_id="wx-user")
    old = store.get_or_create_session(source)
    _age(old, minutes=2)
    store._save()

    same = store.get_or_create_session(source, touch_activity=False)

    assert same.session_id == old.session_id
    assert store._db.get_session(old.session_id)["end_reason"] is None
    store._db.close()


def test_api_server_explicit_conversation_is_not_time_rotated(tmp_path):
    store = _store(tmp_path, {"mode": "idle", "idle_minutes": 1, "at_hour": 4})
    source = SessionSource(platform=Platform.API_SERVER, chat_id="desk", user_id="desk")
    old = store.get_or_create_session(source)
    _age(old, minutes=2)
    store._save()

    same = store.get_or_create_session(source)

    assert same.session_id == old.session_id
    assert store._db.get_session(old.session_id)["end_reason"] is None
    store._db.close()


def test_active_background_work_defers_time_rotation(tmp_path):
    config = GatewayConfig.from_dict({"session_reset": {"mode": "idle", "idle_minutes": 1}})
    store = SessionStore(tmp_path / "sessions", config, has_active_processes_fn=lambda _key: True)
    source = SessionSource(platform=Platform.WEIXIN, chat_id="wx-user", user_id="wx-user")
    old = store.get_or_create_session(source)
    _age(old, minutes=2)
    store._save()

    same = store.get_or_create_session(source)

    assert same.session_id == old.session_id
    store._db.close()


def test_legacy_upstream_reset_keys_remain_inert(tmp_path):
    config = GatewayConfig.from_dict({
        "default_reset_policy": {"mode": "idle", "idle_minutes": 1},
        "reset_by_type": {"dm": {"mode": "idle", "idle_minutes": 1}},
    })
    store = SessionStore(tmp_path / "sessions", config)
    source = SessionSource(platform=Platform.WEIXIN, chat_id="wx-user", user_id="wx-user")
    old = store.get_or_create_session(source)
    _age(old, minutes=2)
    store._save()

    same = store.get_or_create_session(source)

    assert same.session_id == old.session_id
    store._db.close()


def test_daily_boundary_rotates_after_local_four_am(monkeypatch, tmp_path):
    import gateway.session_lifecycle as lifecycle

    fixed_now = datetime(2026, 9, 21, 8, 0, 0)
    monkeypatch.setattr(lifecycle, "_now", lambda: fixed_now)
    store = _store(tmp_path, {"mode": "daily", "idle_minutes": 999999, "at_hour": 4})
    source = SessionSource(platform=Platform.WEIXIN, chat_id="wx-user", user_id="wx-user")
    old = store.get_or_create_session(source)
    old.updated_at = datetime(2026, 9, 20, 23, 30, 0)
    store._save()

    new = store.get_or_create_session(source)

    assert new.session_id != old.session_id
    assert new.was_auto_reset is True
    assert new.auto_reset_reason == "daily"
    assert store._db.get_session(old.session_id)["end_reason"] == "daily"
    store._db.close()


def test_daily_boundary_before_four_am_uses_previous_boundary(monkeypatch, tmp_path):
    import gateway.session_lifecycle as lifecycle

    fixed_now = datetime(2026, 9, 21, 3, 0, 0)
    monkeypatch.setattr(lifecycle, "_now", lambda: fixed_now)
    store = _store(tmp_path, {"mode": "daily", "idle_minutes": 999999, "at_hour": 4})
    source = SessionSource(platform=Platform.WEIXIN, chat_id="wx-user", user_id="wx-user")
    old = store.get_or_create_session(source)
    old.updated_at = datetime(2026, 9, 20, 5, 0, 0)
    store._save()

    same = store.get_or_create_session(source)

    assert same.session_id == old.session_id
    assert store._db.get_session(old.session_id)["end_reason"] is None
    store._db.close()
