"""One-shot continuation for a progress-only stop after a real provider fallback.

This is deliberately narrow. We do *not* verify arbitrary assistant promises or
replay tools: the next model call gets the existing transcript and must inspect
already-made artifacts before taking any action.
"""
from __future__ import annotations

import re
from typing import Any

_ARTIFACT = re.compile(
    r"(?i)(?:\b(?:pdf|docx?|xlsx?|pptx?|zip|epub)\b|"
    r"路书|文档|文件|图册|地图册|手册|讲义|课件|简历|报告|表格|工作簿|演示文稿)"
)
_ACTION = re.compile(
    r"(?:制作|生成|编写|撰写|写成|做成|导出|转换|转成|填写|填入|"
    r"完善|优化|更新|补充|加入|添加|细化|整理|排版|修改|做一本|"
    r"create|generate|export|convert|revise|update|edit|produce)",
    re.I,
)
_PROGRESS = re.compile(
    r"(?:正在|仍在|稍后|随后|稍候|请稍等|接下来|下一步|后续将|"
    r"还会|将会|即将|待.{0,12}(?:汇总|整理|完成|生成|检查)|"
    r"(?:still|currently) (?:working|preparing|compiling)|"
    r"(?:will|i'll) (?:continue|prepare|finish|send))",
    re.I,
)
_DELIVERED = re.compile(
    r"(?:@file:|sandbox:|\[[^\]]{1,90}\]\((?:https?://|/opt/data/)|"
    r"(?:已完成(?:路书|文档|文件|报告|讲义|课件)|已做好|做好了|已经生成|下载|附件|交付|无法|不能|抱歉|出错|失败))",
    re.I,
)

RECOVERY_NUDGE = (
    "[System: One-time fallback artifact completion check. The user requested a "
    "deliverable, but your last reply was only a progress update. Continue the "
    "EXISTING task instead of stopping. FIRST inspect the current workspace and "
    "prior tool results for files already created. Reuse those exact files; do "
    "not recreate completed work, rerun side-effecting commands, or resend any "
    "previously delivered files. If the deliverable is ready, provide its exact "
    "file link/path and a brief completion summary now. Otherwise finish only "
    "the missing safe steps, validate the result, and deliver once. If blocked, "
    "state the specific blocker truthfully instead of another progress promise.]"
)
INCOMPLETE_NOTICE = (
    "⚠️ 本轮文件交付仍未完成：模型在自动继续后仍只返回进度说明。"
    "请勿将上述进度当作已完成结果，可在本会话继续处理。"
)


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return " ".join(_text(item) for item in value)
    if isinstance(value, dict):
        # Native multimodal user content can be text + image/file parts.
        return _text(value.get("text") or value.get("content") or "")
    return ""


def progress_only_artifact_stop(user_message: Any, final_response: Any) -> bool:
    """Fail closed on ambiguity: explicit file-producing work + short progress only."""
    request = _text(user_message)[:12000]
    answer = _text(final_response).strip()
    if not request or not answer or len(answer) > 280:
        return False
    if not (_ARTIFACT.search(request) and _ACTION.search(request)):
        return False
    if _DELIVERED.search(answer):
        return False
    return bool(_PROGRESS.search(answer))


def fallback_artifact_nudge(agent: Any, user_message: Any, final_response: Any) -> str | None:
    """Called only from the text-stop gate, never from tool execution or finalizer."""
    if not bool(getattr(agent, "_provider_fallback_active", False)):
        return None
    if not getattr(agent, "valid_tool_names", None):
        return None
    if getattr(agent, "_fallback_artifact_completion_attempts", 0) >= 1:
        return None
    if not progress_only_artifact_stop(user_message, final_response):
        return None
    return RECOVERY_NUDGE
