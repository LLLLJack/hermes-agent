"""H6: changing pagination and unrelated calls cannot evade short-path stalls."""
import json
import os

from agent.tool_guardrails import ToolCallGuardrailConfig, ToolCallGuardrailController
from tools import file_tools
from tools.file_operations import ShellFileOperations
from tools.environments.local import LocalEnvironment
from tools.file_tools_read_tracking import (
    _read_tracker, notify_other_tool_call, reset_file_dedup, _invalidate_dedup_for_path,
)
from tools.registry import registry


def _setup(monkeypatch):
    _read_tracker.clear()
    ops = ShellFileOperations(LocalEnvironment())
    monkeypatch.setattr(file_tools, "_get_file_ops", lambda task_id: ops)


def _read(path, task="h6", offset=1, limit=2000):
    return registry.dispatch("read_file", {"path": str(path), "offset": offset, "limit": limit}, task_id=task)


def test_h6_varying_reads_halt_without_general_hard_stop(monkeypatch, tmp_path):
    _setup(monkeypatch)
    path = tmp_path / "render.py"
    path.write_text("a = 1\nb = 2\n")
    controller = ToolCallGuardrailController(ToolCallGuardrailConfig(hard_stop_enabled=False))
    assert "content" in json.loads(_read(path))
    stalled = []
    for i in range(1, 12):
        notify_other_tool_call("h6")
        controller.after_call("terminal", {"command": f"echo {i}"}, "ok")
        args = {"path": str(path), "offset": i + 5, "limit": i + 20}
        result = _read(path, offset=args["offset"], limit=args["limit"])
        if json.loads(result).get("status") == "stalled_path":
            stalled.append(result)
        decision = controller.after_call("read_file", args, result)
        if decision.should_halt:
            break
    assert len(stalled) == 3
    assert len(set(stalled)) == 1
    assert decision.code == "stalled_path"
    assert i <= 8
    # Neither another file nor another task/profile inherits the path block.
    other = tmp_path / "other.py"
    other.write_text("different = 1\n")
    assert "content" in json.loads(_read(other))
    assert "content" in json.loads(_read(path, task="other-tenant"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile-b"))
    assert json.loads(_read(path, offset=1, limit=1999)).get("status") != "stalled_path"


def test_progress_pagination_edit_compaction_and_bounds(monkeypatch, tmp_path):
    _setup(monkeypatch)
    path = tmp_path / "short.py"
    path.write_text("one\ntwo\nthree\n")
    assert "one" in json.loads(_read(path, limit=1))["content"]
    for i in range(6):
        _read(path, offset=1, limit=1)
    # An uncovered line is always legitimate progress even at the block boundary.
    assert "two" in json.loads(_read(path, offset=2, limit=1))["content"]
    _read(path, limit=100)
    for i in range(6):
        _read(path, offset=50+i)
    assert json.loads(_read(path, offset=70))["status"] == "stalled_path"
    reset_file_dedup("h6")
    assert "one" in json.loads(_read(path))["content"]
    for i in range(6):
        result = json.loads(_read(path, offset=80+i))
    assert result["status"] == "stalled_path"
    old_stat = path.stat()
    path.write_text("changed version\n")
    os.utime(path, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))
    assert "changed version" in json.loads(_read(path))["content"]
    _invalidate_dedup_for_path(str(path), "h6")
    assert "changed version" in json.loads(_read(path))["content"]
    long = tmp_path / "long.txt"
    long.write_text("line\n" * 500)
    for offset in range(1, 501, 20):
        assert "content" in json.loads(_read(long, offset=offset, limit=20))
    from tools.file_tools_read_tracking import _PATH_PROGRESS_CAP, _cap_read_tracker_data
    data = _read_tracker["h6"]
    data["path_progress"].update({("p", str(i)): {} for i in range(_PATH_PROGRESS_CAP + 1)})
    _cap_read_tracker_data(data)
    assert len(data["path_progress"]) <= _PATH_PROGRESS_CAP
