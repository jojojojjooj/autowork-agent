import json
import threading
from pathlib import Path

import pytest

from app import engine
from app.adapters import build_adapter_context, build_document_context, detect_application, normalize_document_roots, normalize_excel_cell_reference, search_approved_documents, validate_pdf_page_number
from app.policies import review_plan, validate_plan_policy
from app.engine import (
    LocalAIClient,
    LocalScheduler,
    Step,
    Workflow,
    WorkflowPlayer,
    append_execution_history,
    backup_workflow,
    cleanup_workflow_backups,
    list_workflow_backups,
    mask_sensitive_text,
    read_execution_history,
    sign_workflow,
    summarize_execution_history,
    trim_execution_history,
    inspect_workflow,
    load_workflow,
    save_workflow,
    validate_ai_steps,
    validate_local_endpoint,
    validate_timeout,
    validate_schedule_interval,
    validate_workflow,
    resolve_observed_element,
    verify_workflow_signature,
    save_prompt_template,
    load_prompt_template,
    render_prompt_template,
    write_error_report,
)


def test_workflow_roundtrip(tmp_path: Path):
    original = Workflow(
        name="엑셀 반복 작업",
        description="테스트",
        steps=[
            Step(type="click", delay=0.2, x=100, y=200, button="left"),
            Step(type="key", delay=0.1, event="down", kind="char", value="a"),
        ],
        recorded_screen_size=[1920, 1080],
    )
    path = tmp_path / "workflow.json"
    save_workflow(original, path)
    loaded = load_workflow(path)
    assert loaded.name == original.name
    assert len(loaded.steps) == 2
    assert loaded.steps[0].x == 100
    assert loaded.steps[1].value == "a"
    assert loaded.recorded_screen_size == [1920, 1080]


def test_workflow_backup_is_created_and_bounded(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(engine, "APP_DIR", tmp_path / "app")
    monkeypatch.setattr(engine, "BACKUP_DIR", tmp_path / "app" / "workflow_backups")
    workflow_path = tmp_path / "job.json"
    workflow_path.write_text("{}", encoding="utf-8")
    first = backup_workflow(workflow_path)
    assert first is not None and first.exists()
    for index in range(4):
        workflow_path.write_text(str(index), encoding="utf-8")
        backup_workflow(workflow_path)
    cleanup_workflow_backups(2)
    assert len(list_workflow_backups()) == 2


def test_workflow_signature_detects_tampering(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(engine, "APP_DIR", tmp_path / "app")
    monkeypatch.setattr(engine, "WORKFLOW_KEY_PATH", tmp_path / "app" / "workflow.key")
    workflow = Workflow(steps=[Step(type="wait", value="1")])
    signature = sign_workflow(workflow)
    assert len(signature) == 64
    assert verify_workflow_signature(workflow) is True
    workflow.steps[0].value = "2"
    assert verify_workflow_signature(workflow) is False


def test_workflow_remove_step():
    workflow = Workflow(steps=[Step(type="wait", value="1"), Step(type="wait", value="2")])
    removed = workflow.remove_step(0)
    assert removed.value == "1"
    assert len(workflow.steps) == 1
    with pytest.raises(IndexError):
        workflow.remove_step(3)


def test_workflow_edit_operations():
    workflow = Workflow(steps=[Step(type="wait", value="1"), Step(type="wait", value="2")])
    workflow.move_step(1, -1)
    assert [step.value for step in workflow.steps] == ["2", "1"]
    workflow.duplicate_step(0)
    assert [step.value for step in workflow.steps] == ["2", "2", "1"]
    workflow.update_step_delay(1, "1.23456")
    assert workflow.steps[1].delay == 1.235
    with pytest.raises(IndexError):
        workflow.move_step(0, -1)
    with pytest.raises(ValueError):
        workflow.update_step_delay(0, 61)


def test_workflow_rejects_invalid_screen_metadata():
    with pytest.raises(ValueError, match="화면 크기"):
        Workflow.from_dict({"version": 2, "recorded_screen_size": [0, 1080], "steps": []})
    with pytest.raises(ValueError, match="화면 크기"):
        Workflow.from_dict({"version": 2, "recorded_screen_size": [1920], "steps": []})


def test_offline_application_adapters():
    assert detect_application("Book1 - Excel") == "excel"
    assert normalize_excel_cell_reference("$b$12") == "B12"
    assert validate_pdf_page_number("3") == 3
    context = build_adapter_context("report.pdf - Acrobat", "페이지 3")
    assert context["application"] == "pdf"
    assert context["page_numbers"] == [3]
    with pytest.raises(ValueError):
        normalize_excel_cell_reference("XFE1")
    with pytest.raises(ValueError):
        validate_pdf_page_number(0)


def test_approved_local_document_search_is_bounded(tmp_path: Path):
    approved = tmp_path / "approved"
    approved.mkdir()
    (approved / "report.txt").write_text("2026년 예산 보고서 합계 100", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("예산 보고서 외부 파일", encoding="utf-8")
    assert normalize_document_roots([str(approved), str(outside)]) == [approved.resolve()]
    link = tmp_path / "approved-link"
    link.symlink_to(approved, target_is_directory=True)
    assert normalize_document_roots([str(link)]) == []
    (approved / "contact.txt").write_text("예산 담당자 010-1234-5678 test@example.com", encoding="utf-8")
    results = search_approved_documents([str(approved)], "담당자")
    assert len(results) == 1
    assert results[0]["relative_path"] == "contact.txt"
    assert "010-1234-5678" not in results[0]["snippet"]
    assert "<전화번호 마스킹>" in results[0]["snippet"]
    context = build_document_context([str(approved)], "담당자")
    assert context["result_count"] == 1
    assert str(approved.resolve()) in context["approved_roots"]


def test_policy_profiles_block_unsafe_actions():
    valid, message = validate_plan_policy({"steps": [{"action": "type", "risk": "write", "requires_confirmation": True}]}, "public_document")
    assert valid is False
    assert "허용하지 않습니다" in message
    valid, message = validate_plan_policy({"steps": [{"action": "click", "risk": "read", "requires_confirmation": True}]}, "browser")
    assert valid is True
    too_many = {"steps": [{"action": "wait", "seconds": 1, "risk": "read", "requires_confirmation": True}] * 13}
    valid, message = validate_plan_policy(too_many, "public_document")
    assert valid is False
    assert "최대 12단계" in message


def test_review_plan_summarizes_actions_risks_wait_and_expected_checks():
    plan = {
        "steps": [
            {"action": "click", "risk": "read", "requires_confirmation": True, "expected_texts": ["완료"]},
            {"action": "wait", "seconds": 2.5, "risk": "read", "requires_confirmation": True},
            {"action": "scroll", "amount": -3, "risk": "read", "requires_confirmation": True},
        ]
    }
    review = review_plan(plan, "browser")
    assert review["ok"] is True
    assert review["step_count"] == 3
    assert review["action_counts"] == {"click": 1, "scroll": 1, "wait": 1}
    assert review["risk_counts"] == {"read": 3}
    assert review["total_wait_seconds"] == 2.5
    assert review["expected_check_count"] == 1
    assert review["warnings"] == []


def test_review_plan_explains_policy_violation():
    plan = {"steps": [{"action": "type", "risk": "write", "requires_confirmation": True}]}
    review = review_plan(plan, "public_document")
    assert review["ok"] is False
    assert review["policy_message"]
    assert review["warnings"]
    assert "허용하지 않습니다" in review["warnings"][0]


def test_review_plan_rejects_malformed_steps_and_waits():
    malformed = review_plan({"steps": ["not-an-action"]}, "standard")
    assert malformed["ok"] is False
    assert "형식이 올바르지 않습니다" in malformed["policy_message"]
    invalid_wait = review_plan({"steps": [{"action": "wait", "seconds": "nan", "risk": "read"}]}, "standard")
    assert invalid_wait["ok"] is False
    assert "대기시간" in invalid_wait["policy_message"]


def test_validate_ai_steps_applies_policy_profile():
    observation = {
        "screen_size": [1920, 1080],
        "elements": [{"id": "uia_edit", "role": "edit", "name": "문서", "control_type": "Edit", "bbox": [10, 20, 110, 60], "source": "uia"}],
    }
    valid, message = validate_ai_steps(
        {"steps": [{"action": "type", "element_id": "uia_edit", "text": "확인", "confidence": 0.95, "risk": "write", "requires_confirmation": True}]},
        (1920, 1080),
        observation,
        "public_document",
    )
    assert valid is False
    assert "안전 프로필" in message


def test_prompt_template_roundtrip_and_fail_closed(tmp_path: Path):
    path = tmp_path / "prompt.json"
    save_prompt_template(path, "월간 보고서", "{{month}} 보고서를 {{target}}에서 확인해 줘", "public_document", [str(tmp_path)])
    data = load_prompt_template(path)
    assert data["placeholders"] == ["month", "target"]
    assert render_prompt_template(data["goal_template"], {"month": "3월", "target": "문서함"}) == "3월 보고서를 문서함에서 확인해 줘"
    with pytest.raises(ValueError):
        render_prompt_template(data["goal_template"], {"month": "3월"})


def test_ai_plan_validation():
    observation = {
        "screen_size": [1920, 1080],
        "elements": [{"id": "uia_0001", "role": "button", "name": "검색", "bbox": [10, 20, 110, 60], "source": "uia", "confidence": 1.0, "enabled": True, "visible": True}],
    }
    valid, message = validate_ai_steps(
        {
            "summary": "검색 버튼 클릭",
            "risk": "low",
            "steps": [{"action": "click", "element_id": "uia_0001", "confidence": 0.95, "risk": "read", "requires_confirmation": True}],
        },
        (1920, 1080),
        observation,
    )
    assert valid is True
    assert message == "검증 완료"


def test_ai_plan_rejects_unsafe_action():
    valid, message = validate_ai_steps({"steps": [{"action": "shell", "command": "del *"}]}, (1920, 1080), {"elements": []})
    assert valid is False
    assert "구조화 계획 형식 오류" in message


def test_ai_plan_rejects_direct_coordinates():
    valid, message = validate_ai_steps({"steps": [{"action": "click", "x": 10, "y": 20}]}, (1920, 1080), {"elements": []})
    assert valid is False
    assert "구조화 계획 형식 오류" in message or "element_id" in message


def test_ai_plan_semantic_rebinding_after_element_id_change():
    observation = {
        "screen_size": [1920, 1080],
        "elements": [{
            "id": "uia_new_search",
            "role": "button",
            "name": "검색",
            "control_type": "Button",
            "bbox": [10, 20, 110, 60],
            "source": "uia",
            "enabled": True,
            "visible": True,
        }],
    }
    action = engine.UIAction(
        action="click",
        element_id="uia_old_search",
        element_role="button",
        element_name="검색",
        element_control_type="Button",
        confidence=0.95,
        risk="read",
    )
    resolved = resolve_observed_element(action, observation)
    assert resolved is not None
    assert resolved.id == "uia_new_search"
    valid, message = validate_ai_steps(
        {"steps": [action.model_dump()]},
        (1920, 1080),
        observation,
    )
    assert valid is True
    assert message == "검증 완료"


def test_ai_plan_semantic_rebinding_rejects_ambiguous_elements():
    observation = {
        "screen_size": [1920, 1080],
        "elements": [
            {"id": "uia_a", "role": "button", "name": "확인", "control_type": "Button", "bbox": [0, 0, 50, 30], "source": "uia"},
            {"id": "uia_b", "role": "button", "name": "확인", "control_type": "Button", "bbox": [60, 0, 110, 30], "source": "uia"},
        ],
    }
    action = engine.UIAction(action="click", element_id="uia_missing", element_role="button", element_name="확인", element_control_type="Button", confidence=0.95, risk="read")
    resolved = resolve_observed_element(action, observation)
    assert resolved is None
    valid, message = validate_ai_steps({"steps": [action.model_dump()]}, (1920, 1080), observation)
    assert valid is False
    assert "하나로 특정" in message


def test_workflow_inspection_reports_counts_and_screen_warning():
    workflow = Workflow(
        steps=[
            Step(type="click", x=100, y=100, button="left", window_title="문서 - 메모장"),
            Step(type="key", event="down", kind="char", value="a"),
            Step(type="wait", value="1"),
        ],
        recorded_screen_size=[1920, 1080],
    )
    report = inspect_workflow(workflow, (1280, 720))
    assert report["valid"] is True
    assert report["step_count"] == 3
    assert report["click_count"] == 1
    assert report["key_count"] == 1
    assert report["wait_count"] == 1
    assert report["window_titles"] == ["문서 - 메모장"]
    assert report["warnings"]


def test_workflow_rejects_out_of_bounds_click():
    valid, message = validate_workflow(Workflow(steps=[Step(type="click", x=-1, y=10, button="left")]), (1920, 1080))
    assert valid is False
    assert "화면 밖" in message


def test_ai_plan_rejects_unconfirmed_text_input():
    observation = {
        "screen_size": [1920, 1080],
        "elements": [{"id": "uia_edit", "role": "edit", "name": "메모", "control_type": "Edit", "bbox": [10, 20, 110, 60], "source": "uia", "enabled": True, "visible": True}],
    }
    valid, message = validate_ai_steps(
        {"steps": [{"action": "type", "element_id": "uia_edit", "text": "업무자료", "confidence": 0.95, "risk": "read", "requires_confirmation": False}]},
        (1920, 1080),
        observation,
    )
    assert valid is False
    assert "확인" in message


def test_error_report_redacts_sensitive_context(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(engine, "APP_DIR", tmp_path / "app")
    monkeypatch.setattr(engine, "WORKFLOW_DIR", tmp_path / "app" / "workflows")
    monkeypatch.setattr(engine, "CACHE_DIR", tmp_path / "app" / "cache")
    monkeypatch.setattr(engine, "ERROR_DIR", tmp_path / "app" / "errors")
    monkeypatch.setattr(engine, "DEBUG_DIR", tmp_path / "app" / "debug_snapshots")
    monkeypatch.setattr(engine, "LOG_PATH", tmp_path / "app" / "autowork.log")
    try:
        raise RuntimeError("test failure")
    except RuntimeError as exc:
        report_path = write_error_report({"component": "test", "step": {"text": "비밀번호123"}}, exc, capture_screen=False)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    serialized = json.dumps(report, ensure_ascii=False)
    assert "비밀번호123" not in serialized
    assert "redacted" in serialized


def test_ai_plan_rejects_unknown_element():
    valid, message = validate_ai_steps(
        {"steps": [{"action": "click", "element_id": "uia_missing", "confidence": 0.95}]},
        (1920, 1080),
        {"screen_size": [1920, 1080], "elements": []},
    )
    assert valid is False
    assert "현재 화면에 없습니다" in message


def test_json_parser_accepts_markdown_fence():
    parsed = LocalAIClient._parse_json('```json\n{"summary":"ok","risk":"low","steps":[]}\n```')
    assert parsed["summary"] == "ok"
    assert parsed["steps"] == []


def test_local_endpoint_allows_loopback_only():
    assert validate_local_endpoint("http://127.0.0.1:11434/v1").startswith("http://127.0.0.1")
    assert validate_local_endpoint("localhost:1234/v1").startswith("http://localhost")
    try:
        validate_local_endpoint("https://example.com/v1")
    except ValueError as exc:
        assert "로컬 LLM" in str(exc)
    else:
        raise AssertionError("외부 endpoint가 허용되었습니다.")


def test_local_ai_health_check_reads_models_only(monkeypatch):
    import requests

    calls = []

    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"data": [{"id": "local-model"}]}

    def fake_get(url, timeout):
        calls.append((url, timeout))
        return FakeResponse()

    monkeypatch.setattr(requests, "get", fake_get)
    result = LocalAIClient(endpoint="http://127.0.0.1:11434/v1", model="local-model", timeout=120).health_check()
    assert result["reachable"] is True
    assert result["models"] == ["local-model"]
    assert calls == [("http://127.0.0.1:11434/v1/models", 5)]


def test_workflow_is_saved_as_version_2_and_rejects_unsafe_steps(tmp_path: Path):
    path = tmp_path / "workflow.json"
    save_workflow(Workflow(steps=[Step(type="wait", value="1")]), path)
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["version"] == 2
    assert load_workflow(path).steps[0].value == "1"

    with pytest.raises(ValueError, match="동작 유형"):
        Workflow.from_dict({"version": 2, "steps": [{"type": "shell"}]})


def test_scheduler_and_schedule_interval():
    triggered = threading.Event()
    scheduler = LocalScheduler(triggered.set)
    assert validate_schedule_interval(60) == 60
    with pytest.raises(ValueError):
        validate_schedule_interval(59)
    scheduler.start(60, run_immediately=True)
    assert triggered.wait(1.0)
    scheduler.stop()
    assert scheduler.running is False


def test_sensitive_text_masking_and_history_summary():
    masked = mask_sensitive_text("연락처 010-1234-5678, 이메일 test@example.com, 주민번호 900101-1234567")
    assert "010-1234-5678" not in masked
    assert "test@example.com" not in masked
    assert "900101-1234567" not in masked
    summary = summarize_execution_history([
        {"event": "playback_completed"},
        {"event": "playback_failed"},
        {"event": "scheduler_started"},
    ])
    assert summary["completed_runs"] == 1
    assert summary["failed_runs"] == 1
    assert summary["success_rate"] == 50.0


def test_local_endpoint_and_timeout_are_strictly_bounded():
    assert validate_local_endpoint("localhost:1234/v1/") == "http://localhost:1234/v1"
    with pytest.raises(ValueError):
        validate_local_endpoint("http://127.0.0.1:11434/v1?token=secret")
    with pytest.raises(ValueError):
        validate_local_endpoint("http://127.0.0.1:11434/other")
    assert validate_timeout("30") == 30
    with pytest.raises(ValueError):
        validate_timeout("601")


def test_ai_plan_rejects_zero_scroll_and_oversized_text():
    observation = {
        "screen_size": [1920, 1080],
        "elements": [{"id": "uia_0001", "role": "edit", "bbox": [10, 20, 110, 60], "enabled": True, "visible": True}],
    }
    valid, message = validate_ai_steps(
        {"steps": [{"action": "scroll", "amount": 0, "confidence": 0.9}]},
        (1920, 1080), observation,
    )
    assert valid is False
    assert "스크롤 양" in message
    valid, message = validate_ai_steps(
        {"steps": [{"action": "type", "element_id": "uia_0001", "text": "x" * 100_001, "confidence": 0.9}]},
        (1920, 1080), observation,
    )
    assert valid is False
    assert "너무 깁니다" in message


def test_execution_history_is_bounded(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(engine, "APP_DIR", tmp_path / "app")
    monkeypatch.setattr(engine, "WORKFLOW_DIR", tmp_path / "app" / "workflows")
    monkeypatch.setattr(engine, "CACHE_DIR", tmp_path / "app" / "cache")
    monkeypatch.setattr(engine, "ERROR_DIR", tmp_path / "app" / "errors")
    monkeypatch.setattr(engine, "DEBUG_DIR", tmp_path / "app" / "debug_snapshots")
    monkeypatch.setattr(engine, "TEMPLATE_DIR", tmp_path / "app" / "templates")
    monkeypatch.setattr(engine, "HISTORY_PATH", tmp_path / "app" / "history.jsonl")
    for index in range(20):
        append_execution_history("test_event", Workflow(name=f"workflow-{index}"), detail="x" * 100)
    trim_execution_history(1024)
    assert engine.HISTORY_PATH.stat().st_size <= 1024
    assert read_execution_history(100)


def test_execution_history_is_local_and_redacted(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(engine, "APP_DIR", tmp_path / "app")
    monkeypatch.setattr(engine, "TEMPLATE_DIR", tmp_path / "app" / "templates")
    monkeypatch.setattr(engine, "HISTORY_PATH", tmp_path / "app" / "history.jsonl")
    workflow = Workflow(name="이력 테스트", steps=[Step(type="wait", value="1")])
    append_execution_history("completed", workflow, goal="비밀 목표", last_step=1)
    records = read_execution_history()
    assert records[0]["event"] == "completed"
    assert records[0]["workflow"] == "이력 테스트"
    assert records[0]["last_step"] == 1
    assert records[0]["goal"] == "비밀 목표"


def test_player_pause_and_resume_state():
    player = WorkflowPlayer()
    assert player.paused is False
    player.running = True
    player.pause()
    assert player.paused is True
    player.resume()
    assert player.paused is False
    player.stop()
    assert player.paused is False


def test_error_report_is_written_locally(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(engine, "APP_DIR", tmp_path / "app")
    monkeypatch.setattr(engine, "WORKFLOW_DIR", tmp_path / "app" / "workflows")
    monkeypatch.setattr(engine, "CACHE_DIR", tmp_path / "app" / "cache")
    monkeypatch.setattr(engine, "ERROR_DIR", tmp_path / "app" / "errors")
    monkeypatch.setattr(engine, "DEBUG_DIR", tmp_path / "app" / "debug_snapshots")
    monkeypatch.setattr(engine, "LOG_PATH", tmp_path / "app" / "autowork.log")
    try:
        raise RuntimeError("test failure")
    except RuntimeError as exc:
        report_path = write_error_report({"component": "test"}, exc, capture_screen=False)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["context"]["component"] == "test"
    assert report["error_type"] == "RuntimeError"
    assert "test failure" in report["error"]
