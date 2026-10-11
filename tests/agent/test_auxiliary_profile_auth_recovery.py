"""Profile-safe cache retirement and explicitly configured auth fallback."""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent import auxiliary_client as aux


def test_rotation_rebuilds_only_current_profile_without_closing_inflight(monkeypatch, tmp_path):
    monkeypatch.setattr(aux, "_client_cache", {})
    monkeypatch.setattr(aux, "_peek_pool_entry", lambda provider: None)
    stale, fresh, other = MagicMock(), MagicMock(), MagicMock()
    clients = iter([stale, other, fresh])
    monkeypatch.setattr(aux, "resolve_provider_client", lambda *a, **kw: (next(clients), "m"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "a"))
    assert aux._get_cached_client("vertex", "m")[0] is stale
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "b"))
    assert aux._get_cached_client("vertex", "m")[0] is other
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "a"))
    aux._evict_cached_clients("vertex")  # forced stale-token rotation
    assert aux._get_cached_client("vertex", "m")[0] is fresh
    stale.close.assert_not_called()
    stale.chat.completions.create.return_value = "inflight completes"
    assert stale.chat.completions.create() == "inflight completes"
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "b"))
    assert aux._get_cached_client("vertex", "m")[0] is other
    other.close.assert_not_called()


@pytest.mark.parametrize("chain", [[], [{"provider": "custom:backup", "model": "backup"}]])
def test_explicit_vertex_auth_uses_only_task_chain(monkeypatch, tmp_path, chain):
    import yaml
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({"auxiliary": {"compression": {
        "provider": "vertex", "fallback_chain": chain}}}))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    # Real task config and fallback-chain selection; stub only client construction.
    candidate = MagicMock()
    monkeypatch.setattr(aux, "_resolve_fallback_entry", lambda *a, **kw: (candidate, "backup"))
    for name in ("_try_main_fallback_chain", "_try_main_agent_model_fallback", "_try_payment_fallback"):
        monkeypatch.setattr(aux, name, lambda *a, **kw: pytest.fail("unconfigured provider hop"))
    route = SimpleNamespace(task="compression", tag="", resolved_provider="vertex", final_model="primary",
                            base_info="https://vertex.test", client=None, main_runtime=None, route_info=None)
    err = Exception("401 Unauthorized")
    err.status_code = 401
    ladder = aux._ladder_provider_fallback(err, route)
    if not chain:
        assert list(ladder) == []
        return
    step = next(ladder)
    assert step.args[0] is candidate
    # A quarantined candidate cannot escape to discovery/main when its chain exhausts.
    monkeypatch.setattr(aux, "_try_configured_fallback_chain", lambda *a, **kw: (None, None, ""))
    with pytest.raises(StopIteration):
        ladder.send(None)
