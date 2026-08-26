"""End-to-end-ish user-flow scenarios using controlled local fakes.

The scenarios exercise persistence, validation, playback orchestration, recovery,
and AI safety gates without sending real OS input or external network traffic.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import engine
from app.engine import (
    Step,
    Workflow,
    WorkflowPlayer,
    build_execution_report_dashboard,
    clear_execution_checkpoint,
    load_execution_checkpoint,
    load_workflow,
    save_execution_checkpoint,
    save_workflow,
    validate_ai_steps,
    verify_workflow_signature,
    write_execution_report,
)
from app.adapters import update_approved_text_document
from app.policies import review_plan


class FakePyAutoGUI:
    PAUSE = 0.0
    FAILSAFE = True

    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def size(self) -> tuple[int, int]:
        return (1920, 1080)

    def click(self, x: int, y: int, button: str = "left") -> None:
        self.calls.append(("click", (x, y, button)))

    def keyDown(self, key: str) -> None:
        self.calls.append(("keyDown", key))

    def keyUp(self, key: str) -> None:
        self.calls.append(("keyUp", key))


def _isolate_engine_paths(tmp_path: Path, monkeypatch) -> None:
    app_dir = tmp_path / "app"
    monkeypatch.setattr(engine, "APP_DIR", app_dir)
    monkeypatch.setattr(engine, "WORKFLOW_DIR", app_dir / "workflows")
    monkeypatch.setattr(engine, "BACKUP_DIR", app_dir / "workflow_backups")
    monkeypatch.setattr(engine, "RUN_REPORT_DIR", app_dir / "run_reports")
    monkeypatch.setattr(engine, "CACHE_DIR", app_dir / "cache")
    monkeypatch.setattr(engine, "ERROR_DIR", app_dir / "errors")
    monkeypatch.setattr(engine, "DEBUG_DIR", app_dir / "debug_snapshots")
    monkeypatch.setattr(engine, "TEMPLATE_DIR", app_dir / "templates")
    monkeypatch.setattr(engine, "HISTORY_PATH", app_dir / "execution_history.jsonl")
    monkeypatch.setattr(engine, "LOG_PATH", app_dir / "autowork.log")
    monkeypatch.setattr(engine, "CHECKPOINT_PATH", app_dir / "execution_checkpoint.json")
    monkeypatch.setattr(engine, "WORKFLOW_KEY_PATH", app_dir / "workflow.key")


def test_scenario_record_save_validate_playback_and_report(tmp_path: Path, monkeypatch):
    _isolate_engine_paths(tmp_path, monkeypatch)
    fake = FakePyAutoGUI()
    monkeypatch.setattr(engine, "pyautogui", fake)
    monkeypatch.setattr(engine, "get_active_window_title", lambda: "")

    workflow_path = tmp_path / "workflows" / "daily_task.json"
    workflow = Workflow(
        name="일일 조회 시나리오",
        description="저장 후 검증하고 재생하는 사용자 흐름",
        recorded_screen_size=[1920, 1080],
        steps=[
            Step(type="click", x=120, y=240, button="left"),
            Step(type="key", event="down", kind="special", value="enter"),
            Step(type="key", event="up", kind="special", value="enter"),
            Step(type="wait", value="0"),
        ],
    )

    save_workflow(workflow, workflow_path)
    loaded = load_workflow(workflow_path, require_signature=True)
    assert verify_workflow_signature(loaded) is True
    assert loaded.name == "일일 조회 시나리오"
    assert len(loaded.steps) == 4

    seen_steps: list[int] = []
    player = WorkflowPlayer(on_step=lambda index, _step: seen_steps.append(index))
    player.play(loaded)
    assert player.failed is None
    assert seen_steps == [1, 2, 3, 4]
    assert ("click", (120, 240, "left")) in fake.calls
    assert ("keyDown", "enter") in fake.calls
    assert ("keyUp", "enter") in fake.calls

    report_path = write_execution_report(
        "scenario-playback",
        "playback_completed",
        loaded,
        start_step=1,
        last_step=4,
        duration_seconds=0.25,
        policy_profile="standard",
    )
    assert report_path is not None
    dashboard = build_execution_report_dashboard()
    assert dashboard["summary"]["completed_runs"] == 1
    assert dashboard["recent_reports"][0]["workflow"] == "일일 조회 시나리오"
    assert "keyDown" not in json.dumps(dashboard, ensure_ascii=False)


def test_scenario_checkpoint_recovery_then_clear(tmp_path: Path, monkeypatch):
    _isolate_engine_paths(tmp_path, monkeypatch)
    workflow_path = tmp_path / "workflows" / "recoverable.json"
    workflow_path.parent.mkdir(parents=True)
    workflow_path.write_text("{}", encoding="utf-8")
    save_execution_checkpoint(workflow_path, 2, "a" * 64, mode="playback", error_type="WindowGuardError")
    checkpoint = load_execution_checkpoint()
    assert checkpoint is not None
    assert checkpoint["next_index"] == 2
    assert checkpoint["mode"] == "playback"
    assert checkpoint["error_type"] == "WindowGuardError"
    clear_execution_checkpoint()
    assert load_execution_checkpoint() is None


def test_scenario_ai_preflight_approves_read_only_and_blocks_unsafe_plan():
    observation = {
        "screen_size": [1920, 1080],
        "elements": [
            {
                "id": "uia_search",
                "role": "button",
                "name": "검색",
                "control_type": "Button",
                "bbox": [10, 20, 110, 60],
                "source": "uia",
                "confidence": 1.0,
                "enabled": True,
                "visible": True,
            }
        ],
    }
    safe_plan = {
        "summary": "검색 버튼 확인",
        "risk": "low",
        "steps": [{"action": "click", "element_id": "uia_search", "confidence": 0.95, "risk": "read", "requires_confirmation": True}],
    }
    valid, reason = validate_ai_steps(safe_plan, (1920, 1080), observation, "standard")
    assert valid is True
    assert reason == "검증 완료"
    review = review_plan(safe_plan, "standard")
    assert review["ok"] is True
    unsafe_plan = {"summary": "위험 작업", "risk": "high", "steps": [{"action": "shell", "command": "del *"}]}
    unsafe_valid, unsafe_reason = validate_ai_steps(unsafe_plan, (1920, 1080), {"elements": []}, "standard")
    assert unsafe_valid is False
    assert "형식 오류" in unsafe_reason or "허용" in unsafe_reason


def test_scenario_confirmed_document_change_creates_backup_and_verifies_result(tmp_path: Path):
    approved = tmp_path / "approved"
    approved.mkdir()
    document = approved / "report.md"
    original = "# 월간 보고서\n상태: 초안\n담당: 내부 검토\n"
    document.write_text(original, encoding="utf-8")

    try:
        update_approved_text_document([str(approved)], "report.md", "상태: 초안", "상태: 승인 대기", confirmed=False)
    except PermissionError:
        pass
    else:
        raise AssertionError("명시적 확인 없는 문서 변경이 허용되었습니다.")

    result = update_approved_text_document(
        [str(approved)],
        "report.md",
        "상태: 초안",
        "상태: 승인 대기",
        confirmed=True,
    )
    assert document.read_text(encoding="utf-8") == original.replace("상태: 초안", "상태: 승인 대기")
    backup = approved / result["backup_relative_path"]
    assert backup.read_text(encoding="utf-8") == original
    assert result["before_sha256"] != result["after_sha256"]
    assert result["confirmed"] is True
    assert "상태: 초안" not in json.dumps(result, ensure_ascii=False)

    with pytest.raises(ValueError, match="루트 밖"):
        update_approved_text_document([str(approved)], "../outside.md", "상태", "상태", confirmed=True)
    unsupported = approved / "report.pdf"
    unsupported.write_text("상태", encoding="utf-8")
    with pytest.raises(ValueError, match="확장자"):
        update_approved_text_document([str(approved)], "report.pdf", "상태", "변경", confirmed=True)
    document.write_text("상태: 승인 대기\n상태: 승인 대기", encoding="utf-8")
    with pytest.raises(ValueError, match="정확히 한 번"):
        update_approved_text_document([str(approved)], "report.md", "상태: 승인 대기", "상태: 완료", confirmed=True)
