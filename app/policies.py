"""Offline safety policy profiles for AI-generated automation plans."""

from __future__ import annotations

from typing import Any


POLICY_PROFILES: dict[str, dict[str, Any]] = {
    "standard": {
        "label": "일반 업무",
        "allowed_actions": {"click", "double_click", "type", "hotkey", "scroll", "wait"},
        "blocked_risks": {"delete"},
        "require_confirmation_for": {"write", "submit", "delete", "unknown"},
        "max_steps": 30,
        "max_total_wait": 300.0,
    },
    "public_document": {
        "label": "공공문서·행정",
        "allowed_actions": {"click", "double_click", "scroll", "wait"},
        "blocked_risks": {"write", "submit", "delete", "unknown"},
        "require_confirmation_for": {"write", "submit", "delete", "unknown"},
        "max_steps": 12,
        "max_total_wait": 120.0,
    },
    "browser": {
        "label": "브라우저 조회",
        "allowed_actions": {"click", "double_click", "scroll", "wait", "hotkey"},
        "blocked_risks": {"submit", "delete", "unknown"},
        "require_confirmation_for": {"write", "submit", "delete", "unknown"},
        "max_steps": 20,
        "max_total_wait": 300.0,
    },
    "excel": {
        "label": "Excel 작업",
        "allowed_actions": {"click", "double_click", "type", "hotkey", "scroll", "wait"},
        "blocked_risks": {"delete", "unknown"},
        "require_confirmation_for": {"write", "submit", "delete", "unknown"},
        "max_steps": 30,
        "max_total_wait": 600.0,
    },
}
DEFAULT_POLICY_PROFILE = "standard"


def get_policy_profile(name: str | None) -> dict[str, Any]:
    """Return a copy-like policy view, falling back to the standard profile."""
    profile = POLICY_PROFILES.get(str(name or "").strip(), POLICY_PROFILES[DEFAULT_POLICY_PROFILE])
    return {
        "name": next(key for key, value in POLICY_PROFILES.items() if value is profile),
        "label": profile["label"],
        "allowed_actions": set(profile["allowed_actions"]),
        "blocked_risks": set(profile["blocked_risks"]),
        "require_confirmation_for": set(profile["require_confirmation_for"]),
        "max_steps": int(profile["max_steps"]),
        "max_total_wait": float(profile["max_total_wait"]),
    }


def validate_plan_policy(plan: dict[str, Any], profile_name: str | None = None) -> tuple[bool, str]:
    """Apply an explicit profile policy without performing any operating-system action."""
    policy = get_policy_profile(profile_name)
    steps = list(plan.get("steps", []))
    if len(steps) > policy["max_steps"]:
        return False, f"현재 안전 프로필({policy['label']})에서는 최대 {policy['max_steps']}단계까지만 허용합니다."
    try:
        total_wait = sum(float(action.get("seconds", 0) or 0) for action in steps if str(action.get("action")) == "wait")
    except (AttributeError, TypeError, ValueError):
        return False, "계획의 대기시간 형식이 올바르지 않습니다."
    if total_wait > policy["max_total_wait"]:
        return False, f"현재 안전 프로필({policy['label']})의 총 대기시간 예산을 초과했습니다."
    for index, action in enumerate(steps, start=1):
        action_name = str(action.get("action", "none"))
        risk = str(action.get("risk", "unknown"))
        if action_name not in policy["allowed_actions"] and action_name != "none":
            return False, f"현재 안전 프로필({policy['label']})에서는 {index}번째 {action_name} 동작을 허용하지 않습니다."
        if risk in policy["blocked_risks"]:
            return False, f"현재 안전 프로필({policy['label']})에서는 위험도 {risk} 동작을 차단합니다."
        if risk in policy["require_confirmation_for"] and not bool(action.get("requires_confirmation", True)):
            return False, f"현재 안전 프로필({policy['label']})에서는 {index}번째 동작에 사용자 확인이 필요합니다."
    return True, "정책 검증 완료"


def profile_prompt_rules(profile_name: str | None) -> str:
    """Return short policy instructions suitable for a local model prompt."""
    policy = get_policy_profile(profile_name)
    allowed = ", ".join(sorted(policy["allowed_actions"]))
    return f"안전 프로필은 {policy['label']}입니다. 허용 동작은 {allowed}이며, 최대 {policy['max_steps']}단계·총 대기 {policy['max_total_wait']:.0f}초 이내로 계획하고, 프로필에서 차단한 위험 동작은 계획하지 마십시오."
