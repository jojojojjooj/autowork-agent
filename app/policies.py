"""Offline safety policy profiles for AI-generated automation plans."""

from __future__ import annotations

from typing import Any


POLICY_PROFILES: dict[str, dict[str, Any]] = {
    "standard": {
        "label": "일반 업무",
        "allowed_actions": {"click", "double_click", "type", "hotkey", "scroll", "wait"},
        "blocked_risks": {"delete"},
        "require_confirmation_for": {"write", "submit", "delete", "unknown"},
    },
    "public_document": {
        "label": "공공문서·행정",
        "allowed_actions": {"click", "double_click", "scroll", "wait"},
        "blocked_risks": {"write", "submit", "delete", "unknown"},
        "require_confirmation_for": {"write", "submit", "delete", "unknown"},
    },
    "browser": {
        "label": "브라우저 조회",
        "allowed_actions": {"click", "double_click", "scroll", "wait", "hotkey"},
        "blocked_risks": {"submit", "delete", "unknown"},
        "require_confirmation_for": {"write", "submit", "delete", "unknown"},
    },
    "excel": {
        "label": "Excel 작업",
        "allowed_actions": {"click", "double_click", "type", "hotkey", "scroll", "wait"},
        "blocked_risks": {"delete", "unknown"},
        "require_confirmation_for": {"write", "submit", "delete", "unknown"},
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
    }


def validate_plan_policy(plan: dict[str, Any], profile_name: str | None = None) -> tuple[bool, str]:
    """Apply an explicit profile policy without performing any operating-system action."""
    policy = get_policy_profile(profile_name)
    for index, action in enumerate(plan.get("steps", []), start=1):
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
    return f"안전 프로필은 {policy['label']}입니다. 허용 동작은 {allowed}이며, 프로필에서 차단한 위험 동작은 계획하지 마십시오."
