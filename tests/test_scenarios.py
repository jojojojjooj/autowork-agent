"""End-to-end-ish user-flow scenarios using controlled local fakes.

The scenarios exercise persistence, validation, playback orchestration, recovery,
and AI safety gates without sending real OS input or external network traffic.
"""

from __future__ import annotations

import io
import json
import zipfile
from html import escape as xml_escape
from pathlib import Path

import pytest
from defusedxml import ElementTree as SafeElementTree

from app import adapters, engine
from app.adapters import (
    restore_document_backup,
    update_approved_office_document,
    update_approved_text_document,
    verify_document_change,
)
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
from app.policies import review_plan


def _write_office_fixture(path: Path, suffix: str, text: str) -> None:
    escaped = xml_escape(text, quote=False)
    if suffix == ".docx":
        parts = {
            "[Content_Types].xml": "<?xml version=\"1.0\"?><Types xmlns=\"http://schemas.openxmlformats.org/package/2006/content-types\"><Default Extension=\"xml\" ContentType=\"application/xml\"/></Types>",
            "word/document.xml": f"<?xml version=\"1.0\"?><w:document xmlns:w=\"http://schemas.openxmlformats.org/wordprocessingml/2006/main\"><w:body><w:p><w:r><w:t>{escaped}</w:t></w:r></w:p></w:body></w:document>",
        }
    else:
        parts = {
            "[Content_Types].xml": "<?xml version=\"1.0\"?><Types xmlns=\"http://schemas.openxmlformats.org/package/2006/content-types\"><Default Extension=\"xml\" ContentType=\"application/xml\"/></Types>",
            "xl/workbook.xml": "<?xml version=\"1.0\"?><workbook xmlns=\"http://schemas.openxmlformats.org/spreadsheetml/2006/main\"><sheets/></workbook>",
            "xl/worksheets/sheet1.xml": f"<?xml version=\"1.0\"?><worksheet xmlns=\"http://schemas.openxmlformats.org/spreadsheetml/2006/main\"><sheetData><row r=\"1\"><c r=\"A1\" t=\"inlineStr\"><is><t>{escaped}</t></is></c></row></sheetData></worksheet>",
        }
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in parts.items():
            archive.writestr(name, content.encode("utf-8"))


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


def test_scenario_real_docx_mutation_reopens_and_preserves_backup(tmp_path: Path):
    approved = tmp_path / "approved"
    approved.mkdir()
    document = approved / "report.docx"
    _write_office_fixture(document, ".docx", "상태: 초안")
    original = document.read_bytes()

    result = update_approved_office_document([str(approved)], "report.docx", "상태: 초안", "상태: 승인 대기", confirmed=True)
    changed = document.read_bytes()
    assert changed != original
    assert result["extension"] == ".docx"
    assert result["changed_part"] == "word/document.xml"
    assert (approved / result["backup_relative_path"]).read_bytes() == original
    with zipfile.ZipFile(io.BytesIO(changed), "r") as archive:
        assert archive.testzip() is None
        xml = archive.read("word/document.xml")
        SafeElementTree.fromstring(xml)
        assert "상태: 초안" not in xml.decode("utf-8")
        assert "상태: 승인 대기" in xml.decode("utf-8")
    verified = verify_document_change([str(approved)], "report.docx", result["after_sha256"], expected_backup_sha256=result["before_sha256"])
    assert verified["verified"] is True
    restored = restore_document_backup([str(approved)], "report.docx", result["after_sha256"], result["before_sha256"], confirmed=True)
    assert restored["restored_sha256"] == result["before_sha256"]
    assert document.read_bytes() == original


def test_scenario_real_xlsx_mutation_reopens_and_preserves_backup(tmp_path: Path):
    approved = tmp_path / "approved"
    approved.mkdir()
    document = approved / "budget.xlsx"
    _write_office_fixture(document, ".xlsx", "합계: 100")
    original = document.read_bytes()

    result = update_approved_office_document([str(approved)], "budget.xlsx", "합계: 100", "합계: 120", confirmed=True)
    changed = document.read_bytes()
    assert changed != original
    assert result["extension"] == ".xlsx"
    assert result["changed_part"] == "xl/worksheets/sheet1.xml"
    assert (approved / result["backup_relative_path"]).read_bytes() == original
    with zipfile.ZipFile(io.BytesIO(changed), "r") as archive:
        assert archive.testzip() is None
        xml = archive.read("xl/worksheets/sheet1.xml")
        SafeElementTree.fromstring(xml)
        assert "합계: 120" in xml.decode("utf-8")


def test_scenario_document_audit_rejects_wrong_hash_and_requires_confirmed_restore(tmp_path: Path):
    approved = tmp_path / "approved"
    approved.mkdir()
    document = approved / "audit.md"
    document.write_text("상태: 초안\n", encoding="utf-8")

    result = update_approved_text_document(
        [str(approved)], "audit.md", "상태: 초안", "상태: 완료", confirmed=True
    )
    with pytest.raises(ValueError, match="SHA-256"):
        verify_document_change([str(approved)], "audit.md", "0" * 64)
    with pytest.raises(ValueError, match="백업"):
        verify_document_change(
            [str(approved)],
            "audit.md",
            result["after_sha256"],
            expected_backup_sha256="1" * 64,
        )
    with pytest.raises(PermissionError, match="명시적인 사용자 확인"):
        restore_document_backup(
            [str(approved)],
            "audit.md",
            result["after_sha256"],
            result["before_sha256"],
            confirmed=False,
        )
    with pytest.raises(ValueError, match="백업 SHA-256"):
        restore_document_backup(
            [str(approved)],
            "audit.md",
            result["after_sha256"],
            "invalid",
            confirmed=True,
        )


def test_scenario_document_size_guard_runs_before_text_read(tmp_path: Path, monkeypatch):
    approved = tmp_path / "approved"
    approved.mkdir()
    document = approved / "large.md"
    document.write_text("상태: 초안\n", encoding="utf-8")
    monkeypatch.setattr(adapters, "MAX_DOCUMENT_BYTES", 1)

    with pytest.raises(ValueError, match="허용 크기"):
        update_approved_text_document(
            [str(approved)], "large.md", "상태: 초안", "상태: 완료", confirmed=True
        )
    assert not list(approved.glob("*.bak"))


def test_scenario_office_mutation_rejects_malformed_changed_xml(tmp_path: Path):
    approved = tmp_path / "approved"
    approved.mkdir()
    document = approved / "broken.docx"
    with zipfile.ZipFile(document, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", "<w:document>상태: 초안")
    original = document.read_bytes()

    with pytest.raises(ValueError, match="다시 열 수"):
        update_approved_office_document(
            [str(approved)], "broken.docx", "상태: 초안", "상태: 완료", confirmed=True
        )
    assert document.read_bytes() == original
    assert not list(approved.glob("*.bak"))


def test_scenario_document_path_and_backup_edge_cases(tmp_path: Path):
    approved = tmp_path / "approved"
    outside = tmp_path / "outside"
    approved.mkdir()
    outside.mkdir()
    outside_document = outside / "outside.md"
    outside_document.write_text("상태: 외부", encoding="utf-8")
    linked_document = approved / "linked.md"
    linked_document.symlink_to(outside_document)

    with pytest.raises(ValueError, match="루트 밖에|symlink"):
        update_approved_text_document(
            [str(approved)], "linked.md", "상태: 외부", "상태: 차단", confirmed=True
        )

    document = approved / "report[1].md"
    document.write_text("상태: 초안\n", encoding="utf-8")
    result = update_approved_text_document(
        [str(approved)], "report[1].md", "상태: 초안", "상태: 완료", confirmed=True
    )
    verified = verify_document_change(
        (str(approved) for _ in range(1)),
        "report[1].md",
        result["after_sha256"],
        expected_backup_sha256=result["before_sha256"],
    )
    assert verified["backup_relative_path"] == result["backup_relative_path"]
    restored = restore_document_backup(
        (str(approved) for _ in range(1)),
        "report[1].md",
        result["after_sha256"],
        result["before_sha256"],
        confirmed=True,
    )
    assert restored["restored_sha256"] == result["before_sha256"]
    assert document.read_text(encoding="utf-8") == "상태: 초안\n"


def test_scenario_office_size_guard_and_zip_traversal_are_rejected(tmp_path: Path, monkeypatch):
    approved = tmp_path / "approved"
    approved.mkdir()
    oversized = approved / "oversized.docx"
    _write_office_fixture(oversized, ".docx", "상태: 초안")
    monkeypatch.setattr(adapters, "MAX_OFFICE_PACKAGE_BYTES", 1)

    with pytest.raises(ValueError, match="허용 크기"):
        update_approved_office_document(
            [str(approved)], "oversized.docx", "상태: 초안", "상태: 완료", confirmed=True
        )

    monkeypatch.setattr(adapters, "MAX_OFFICE_PACKAGE_BYTES", 20 * 1024 * 1024)
    unsafe = approved / "unsafe.docx"
    with zipfile.ZipFile(unsafe, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("../outside.xml", "<outside/>")
        archive.writestr("word/document.xml", "<document>상태: 초안</document>")
    original = unsafe.read_bytes()
    with pytest.raises(ValueError, match="내부 경로"):
        update_approved_office_document(
            [str(approved)], "unsafe.docx", "상태: 초안", "상태: 완료", confirmed=True
        )
    assert unsafe.read_bytes() == original
