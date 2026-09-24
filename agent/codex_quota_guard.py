"""Configurable pre-dispatch quota protection for ChatGPT Codex OAuth routes.

Policy is intentionally external to normal Hermes model config so one shared file can
select which VPS instances and Codex model slugs are protected.  The guard reads the
same official /wham/usage endpoint that Hermes' /usage command already uses, keyed by
the credential that will actually make the model request.

Failure policy is fail-open: a fresh/last-known-good snapshot can block a route, but an
unavailable quota endpoint never becomes a synthetic outage after the configured stale
hold expires.
"""

from __future__ import annotations

import fnmatch
import hashlib
import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import yaml

logger = logging.getLogger(__name__)

_DEFAULT_POLICY_PATH = "/run/codex-quota-guard/policy.yaml"
_LOCK = threading.RLock()
_POLICY_CACHE: tuple[str, int, dict[str, Any]] | None = None
_USAGE_CACHE: dict[str, dict[str, Any]] = {}
_BLOCK_LATCH: dict[tuple[str, str, str, str, str], bool] = {}


@dataclass(frozen=True)
class QuotaDecision:
    blocked: bool = False
    reason: str = ""
    rule: str = ""
    five_hour_remaining: Optional[float] = None
    weekly_remaining: Optional[float] = None
    source: str = ""


def _policy_path() -> str:
    return str(os.environ.get("HERMES_CODEX_QUOTA_POLICY") or _DEFAULT_POLICY_PATH).strip()


def _instance_id() -> str:
    return str(os.environ.get("HERMES_CODEX_QUOTA_INSTANCE") or "").strip().lower()


def _load_policy() -> dict[str, Any]:
    global _POLICY_CACHE
    path = _policy_path()
    try:
        stat = Path(path).stat()
        stamp = int(stat.st_mtime_ns)
    except OSError:
        return {}
    with _LOCK:
        if _POLICY_CACHE and _POLICY_CACHE[0] == path and _POLICY_CACHE[1] == stamp:
            return _POLICY_CACHE[2]
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        policy = raw if isinstance(raw, dict) else {}
    except Exception:
        logger.warning("Codex quota policy could not be read from %s; fail-open", path, exc_info=True)
        return {}
    with _LOCK:
        _POLICY_CACHE = (path, stamp, policy)
    return policy


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _patterns(entry: dict[str, Any]) -> list[str]:
    raw = entry.get("models")
    if raw is None:
        raw = [entry.get("model")] if entry.get("model") else ["*"]
    elif isinstance(raw, str):
        raw = [raw]
    return [str(v).strip().lower() for v in raw if str(v).strip()]


def _route_matches(entry: Any, provider: str, model: str) -> bool:
    if not isinstance(entry, dict):
        return False
    p = str(entry.get("provider") or "openai-codex").strip().lower()
    if p not in {"*", provider}:
        return False
    m = model.lower()
    return any(fnmatch.fnmatchcase(m, pat) for pat in _patterns(entry))


def _instance_matches(rule: dict[str, Any], instance: str) -> bool:
    raw = rule.get("instances", ["*"])
    if isinstance(raw, str):
        raw = [raw]
    values = {str(v).strip().lower() for v in raw if str(v).strip()}
    return "*" in values or instance in values


def _remaining(snapshot, label: str) -> Optional[float]:
    wanted = label.lower()
    for window in getattr(snapshot, "windows", ()) or ():
        if str(getattr(window, "label", "")).strip().lower() == wanted:
            used = getattr(window, "used_percent", None)
            if isinstance(used, (int, float)):
                return max(0.0, min(100.0, 100.0 - float(used)))
    return None


def _resolve_usage_credentials(agent: Any, provider: str, base_url: Optional[str], api_key: Optional[str]):
    from agent.account_usage import _resolve_codex_usage_credentials

    if api_key is None and str(getattr(agent, "provider", "") or "").strip().lower() == provider:
        api_key = str(getattr(agent, "api_key", "") or "").strip() or None
        base_url = base_url or str(getattr(agent, "base_url", "") or "").strip() or None
    return _resolve_codex_usage_credentials(base_url, api_key)


def _fetch_snapshot(agent: Any, provider: str, base_url: Optional[str], api_key: Optional[str], policy: dict[str, Any]):
    try:
        token, resolved_base_url, _account_id = _resolve_usage_credentials(agent, provider, base_url, api_key)
    except Exception:
        logger.debug("Codex quota credential resolution failed; fail-open", exc_info=True)
        return None, ""
    token_key = hashlib.sha256(token.encode("utf-8")).hexdigest()[:24]
    refresh_s = max(5.0, _as_float(policy.get("refresh_seconds"), 120.0))
    stale_s = max(refresh_s, _as_float(policy.get("stale_seconds"), 600.0))
    timeout_s = max(0.5, _as_float(policy.get("timeout_seconds"), 3.0))
    now = time.monotonic()
    with _LOCK:
        cached = _USAGE_CACHE.get(token_key)
        if cached and now - cached["fetched_at"] <= refresh_s:
            return cached["snapshot"], token_key
        if cached and cached.get("refreshing"):
            if now - cached["fetched_at"] <= stale_s:
                return cached["snapshot"], token_key
            return None, token_key
        if cached:
            cached["refreshing"] = True
        else:
            _USAGE_CACHE[token_key] = {"snapshot": None, "fetched_at": 0.0, "refreshing": True}
    try:
        from agent.account_usage import _fetch_codex_account_usage
        snapshot = _fetch_codex_account_usage(
            base_url=resolved_base_url, api_key=token, timeout=timeout_s
        )
    except Exception:
        snapshot = None
        logger.debug("Codex quota refresh failed; retaining bounded last-known-good state", exc_info=True)
    with _LOCK:
        entry = _USAGE_CACHE[token_key]
        old = entry.get("snapshot")
        old_at = float(entry.get("fetched_at") or 0.0)
        if snapshot is not None and getattr(snapshot, "available", False):
            entry.update(snapshot=snapshot, fetched_at=now, refreshing=False)
            return snapshot, token_key
        entry["refreshing"] = False
        if old is not None and now - old_at <= stale_s:
            return old, token_key
    return None, token_key


def _condition(rule: dict[str, Any], five: Optional[float], weekly: Optional[float], *, latched: bool, hysteresis: float) -> bool:
    when = rule.get("when") if isinstance(rule.get("when"), dict) else {}
    limits: list[tuple[Optional[float], float]] = []
    if when.get("five_hour_remaining_lte") is not None:
        limits.append((five, _as_float(when.get("five_hour_remaining_lte"), -1.0)))
    if when.get("weekly_remaining_lte") is not None:
        limits.append((weekly, _as_float(when.get("weekly_remaining_lte"), -1.0)))
    if not limits:
        return False
    logic = str(when.get("logic") or "any").strip().lower()
    thresholded = []
    for remaining, threshold in limits:
        if remaining is None or threshold < 0:
            if logic == "all":
                return False
            continue
        thresholded.append(remaining <= threshold + (hysteresis if latched else 0.0))
    if not thresholded:
        return False
    return all(thresholded) if logic == "all" else any(thresholded)


def route_decision(
    agent: Any, provider: Optional[str] = None, model: Optional[str] = None,
    *, base_url: Optional[str] = None, api_key: Optional[str] = None,
) -> QuotaDecision:
    provider = str(provider if provider is not None else getattr(agent, "provider", "") or "").strip().lower()
    model = str(model if model is not None else getattr(agent, "model", "") or "").strip()
    if provider != "openai-codex" or not model:
        return QuotaDecision()
    policy = _load_policy()
    instance = _instance_id()
    if not policy.get("enabled") or not instance:
        return QuotaDecision()
    rules = policy.get("rules")
    if not isinstance(rules, list):
        return QuotaDecision()

    candidates: list[dict[str, Any]] = []
    for rule in rules:
        if not isinstance(rule, dict) or not _instance_matches(rule, instance):
            continue
        allow = rule.get("allow") if isinstance(rule.get("allow"), list) else []
        if any(_route_matches(entry, provider, model) for entry in allow):
            continue
        block = rule.get("block") if isinstance(rule.get("block"), list) else []
        if any(_route_matches(entry, provider, model) for entry in block):
            candidates.append(rule)
    if not candidates:
        return QuotaDecision()

    snapshot, account_key = _fetch_snapshot(agent, provider, base_url, api_key, policy)
    if snapshot is None:
        return QuotaDecision(reason="quota state unavailable; fail-open")
    five = _remaining(snapshot, "Session")
    weekly = _remaining(snapshot, "Weekly")
    hysteresis = max(0.0, _as_float(policy.get("hysteresis_percent"), 5.0))

    for index, rule in enumerate(candidates):
        name = str(rule.get("name") or f"rule-{index + 1}")
        key = (account_key, instance, name, provider, model.lower())
        with _LOCK:
            latched = bool(_BLOCK_LATCH.get(key))
        blocked = _condition(
            rule, five, weekly, latched=latched,
            hysteresis=max(0.0, _as_float(rule.get("hysteresis_percent"), hysteresis)),
        )
        with _LOCK:
            _BLOCK_LATCH[key] = blocked
        if blocked:
            bits = []
            if five is not None:
                bits.append(f"5h {five:.0f}% remaining")
            if weekly is not None:
                bits.append(f"weekly {weekly:.0f}% remaining")
            return QuotaDecision(
                blocked=True,
                reason=f"Codex quota guard {name}: " + ", ".join(bits),
                rule=name,
                five_hour_remaining=five,
                weekly_remaining=weekly,
                source=str(getattr(snapshot, "source", "") or ""),
            )
    return QuotaDecision(
        five_hour_remaining=five, weekly_remaining=weekly,
        source=str(getattr(snapshot, "source", "") or ""),
    )


def reset_for_tests() -> None:
    global _POLICY_CACHE
    with _LOCK:
        _POLICY_CACHE = None
        _USAGE_CACHE.clear()
        _BLOCK_LATCH.clear()
