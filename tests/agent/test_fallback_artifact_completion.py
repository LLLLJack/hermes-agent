"""Regression: post-fallback progress-only stop on explicit file creation is not success."""
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent.fallback_artifact_completion import (
    progress_only_artifact_stop, fallback_artifact_nudge, RECOVERY_NUDGE,
)
from run_agent import AIAgent


@pytest.mark.parametrize("user_text,reply", [
    ("请在路书里加入地图，包括总的和每天的。", "已完成票务检索，后台三项研究仍在推进中。"),
    ("@file:attachments/罗汉寺参观路书.docx 请细化和优化这本路书",
     "正在为您深入考订，稍后为您呈现全面的参观路书。"),
    ("Please create a PDF report", "I'm currently preparing the report."),
])
def test_real_incident_shapes_are_recognized(user_text, reply):
    assert progress_only_artifact_stop(user_text, reply)


@pytest.mark.parametrize("user_text,reply", [
    ("请问 PDF 是什么格式？", "我正在解释 PDF 格式。"),
    ("帮我查天气", "正在为您查询。"),
    ("请制作PDF", "文件已生成，请下载：@file:/opt/data/report.pdf"),
    ("请制作路书", "抱歉，地图服务失败了，无法核实路线。"),
    ("请制作路书", "路书已经做好了。"),
    ("请制作路书", "已完成全部工作，附件已交付。"),
    ("请制作路书", "这是旅行指南的完整内容：" + "详细正文。" * 140),
])
def test_avoid_false_positive(user_text, reply):
    assert not progress_only_artifact_stop(user_text, reply)


def _response(content: str):
    msg = SimpleNamespace(content=content, tool_calls=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason="stop")],
                           model="test/model", usage=None)


@pytest.fixture
def agent(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setenv("HERMES_VERIFY_ON_STOP", "0")
    with (patch("model_tools.get_tool_definitions", return_value=[]),
          patch("model_tools.check_toolset_requirements", return_value={}),
          patch("agent.process_bootstrap.OpenAI")):
        instance = AIAgent(
            session_id="artifact-fallback-test", api_key="test",
            base_url="https://example.invalid/v1", provider="openai-compat",
            model="test/model", max_iterations=4, quiet_mode=True,
            skip_context_files=True, skip_memory=True,
        )
    instance._cached_system_prompt = "stable test prompt"
    instance._session_db = None
    instance.save_trajectories = False
    instance.compression_enabled = False
    instance._cleanup_task_resources = lambda *_a, **_kw: None
    instance._save_trajectory = lambda *_a, **_kw: None
    instance.valid_tool_names = ["terminal", "read_file"]
    return instance


def test_one_bounded_recovery_reuses_artifact_and_succeeds(agent):
    outputs = iter([
        "已经完成研究，接下来将汇总成路书。",
        "已经制作好：@file:/opt/data/roadbook.pdf",
    ])
    prompts = []

    def completion(_api_kwargs):
        prompts.append([dict(m) for m in agent._session_messages])
        agent._provider_fallback_active = True  # Simulate the switch in this turn.
        return _response(next(outputs))

    agent._interruptible_api_call = completion
    with (patch("hermes_cli.plugins.has_hook", return_value=False),
          patch("hermes_cli.plugins.invoke_hook", return_value=[])):
        result = agent.run_conversation("请制作一本路书PDF并提供下载")
    assert result["completed"] is True
    assert result["failed"] is False
    assert len(prompts) == 2
    assert result["final_response"] == "已经制作好：@file:/opt/data/roadbook.pdf"
    assert any(RECOVERY_NUDGE in str(msg.get("content")) for msg in prompts[1])
    assert not any(msg.get("_fallback_artifact_synthetic") for msg in result["messages"])
    assert len([m for m in result["messages"] if m["role"] == "assistant" and
                "@file:" in str(m.get("content"))]) == 1


def test_second_progress_does_not_silently_complete(agent):
    outputs = iter([
        "正在整理路书内容，稍后发送。",
        "接下来继续制作文档，稍后会给您。",
    ])
    calls = []

    def completion(_api_kwargs):
        calls.append(1)
        agent._provider_fallback_active = True
        return _response(next(outputs))

    agent._interruptible_api_call = completion
    with (patch("hermes_cli.plugins.has_hook", return_value=False),
          patch("hermes_cli.plugins.invoke_hook", return_value=[])):
        result = agent.run_conversation("请制作路书文档")
    assert len(calls) == 2
    assert result["completed"] is False
    assert result["failed"] is True
    assert result["failure_reason"] == "fallback_artifact_incomplete"
    assert "仍未完成" in result["final_response"]
    assert result["turn_exit_reason"] == "fallback_artifact_incomplete"
    assert not any(msg.get("_fallback_artifact_synthetic") for msg in result["messages"])


def test_without_real_fallback_no_extra_inference(agent):
    calls = []

    def completion(_api_kwargs):
        calls.append(1)
        return _response("正在整理路书内容，稍后发送。")

    agent._interruptible_api_call = completion
    with (patch("hermes_cli.plugins.has_hook", return_value=False),
          patch("hermes_cli.plugins.invoke_hook", return_value=[])):
        result = agent.run_conversation("请制作路书文档")
    assert len(calls) == 1
    assert result["completed"] is True


def test_nudge_require_real_tools_and_real_fallback():
    a = SimpleNamespace(_provider_fallback_active=True, valid_tool_names=[],
                        _fallback_artifact_completion_attempts=0)
    assert fallback_artifact_nudge(a, "制作PDF", "正在整理") is None
    a.valid_tool_names = ["terminal"]
    a._provider_fallback_active = False
    assert fallback_artifact_nudge(a, "制作PDF", "正在整理") is None
    a._provider_fallback_active = True
    a._fallback_artifact_completion_attempts = 1
    assert fallback_artifact_nudge(a, "制作PDF", "正在整理") is None
