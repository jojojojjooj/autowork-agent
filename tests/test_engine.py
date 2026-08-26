import json
import threading
import time
from pathlib import Path

import pytest

from app import engine
from app.adapters import (
    build_adapter_context,
    build_document_context,
    build_read_only_adapter_validation,
    detect_application,
    normalize_document_roots,
    normalize_excel_cell_reference,
    search_approved_documents,
    validate_pdf_page_number,
)
from app.engine import (
    LocalAIClient,
    LocalScheduler,
    Step,
    Workflow,
    WorkflowPlayer,
    append_execution_history,
    atomic_write_text,
    backup_workflow,
    build_execution_report_dashboard,
    build_monitor_snapshot,
    cleanup_workflow_backups,
    clear_execution_checkpoint,
    compare_prompt_template_versions,
    compare_prompt_templates,
    evaluate_monitor_alerts,
    evaluate_scheduler_health,
    export_execution_report_summary,
    export_support_bundle,
    inspect_workflow,
    list_prompt_template_versions,
    list_workflow_backups,
    load_execution_checkpoint,
    load_prompt_template,
    load_runtime_config,
    load_workflow,
    mask_sensitive_text,
    read_execution_history,
    read_execution_reports,
    read_monitor_snapshot,
    render_prompt_template,
    resolve_observed_element,
    save_execution_checkpoint,
    save_prompt_template,
    save_workflow,
    sign_workflow,
    summarize_execution_history,
    summarize_execution_reports,
    trim_execution_history,
    validate_ai_steps,
    validate_local_endpoint,
    validate_runtime_config,
    validate_schedule_interval,
    validate_timeout,
    validate_workflow,
    verify_execution_history,
    verify_workflow_signature,
    write_error_report,
    write_execution_report,
    write_monitor_snapshot,
)
from app.policies import review_plan, validate_plan_policy
from app.release import validate_release_layout


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


def test_execution_history_hash_chain_detects_tampering(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(engine, "APP_DIR", tmp_path / "app")
    monkeypatch.setattr(engine, "HISTORY_PATH", tmp_path / "app" / "history.jsonl")
    append_execution_history("first")
    append_execution_history("second")
    records = read_execution_history()
    integrity = verify_execution_history(records)
    assert integrity["valid"] is True
    assert integrity["checked_records"] == 2
    assert integrity["anchored"] is True
    records[0]["event"] = "tampered"
    assert verify_execution_history(records)["valid"] is False


def test_monitor_alerts_are_deterministic():
    alerts = evaluate_monitor_alerts({"completed_runs": 1, "failed_runs": 3, "success_rate": 25.0})
    assert [item["code"] for item in alerts] == ["repeated_failures", "low_success_rate"]
    assert evaluate_monitor_alerts({"completed_runs": 0, "failed_runs": 0, "success_rate": None})[0]["code"] == "no_runs"


def test_monitor_snapshot_and_support_bundle_are_redacted(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(engine, "APP_DIR", tmp_path / "app")
    monkeypatch.setattr(engine, "MONITOR_STATE_PATH", tmp_path / "app" / "monitor.json")
    monkeypatch.setattr(engine, "HISTORY_PATH", tmp_path / "app" / "history.jsonl")
    monkeypatch.setattr(engine, "LOG_PATH", tmp_path / "app" / "autowork.log")
    monkeypatch.setattr(engine, "ERROR_DIR", tmp_path / "app" / "errors")
    snapshot = build_monitor_snapshot(status="idle")
    snapshot["secret"] = "password-value"
    write_monitor_snapshot(snapshot)
    loaded = read_monitor_snapshot()
    assert loaded is not None and loaded["secret"].startswith("<redacted:")
    bundle_path = export_support_bundle(tmp_path / "bundle.json")
    bundle_text = bundle_path.read_text(encoding="utf-8")
    assert "password-value" not in bundle_text
    assert "Workflow 단계" in bundle_text


def test_execution_checkpoint_roundtrip(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(engine, "APP_DIR", tmp_path / "app")
    monkeypatch.setattr(engine, "CHECKPOINT_PATH", tmp_path / "app" / "checkpoint.json")
    workflow_path = tmp_path / "job.json"
    workflow_path.write_text("{}", encoding="utf-8")
    save_execution_checkpoint(workflow_path, 2, "a" * 64, mode="playback", error_type="RuntimeError")
    checkpoint = load_execution_checkpoint()
    assert checkpoint is not None
    assert checkpoint["next_index"] == 2
    assert checkpoint["workflow_signature"] == "a" * 64
    assert "steps" not in checkpoint
    clear_execution_checkpoint()
    assert load_execution_checkpoint() is None


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


def test_read_only_adapter_validation_is_bounded():
    excel = build_read_only_adapter_validation("Book1 - Excel", "선택 영역 B2:C4, 현재 셀 $D$5")
    assert excel["application"] == "excel"
    assert excel["read_only"] is True
    assert {item["value"] for item in excel["targets"]} >= {"B2:C4", "D5"}
    assert any("수정하지 않습니다" in item for item in excel["warnings"])
    pdf = build_read_only_adapter_validation("report.pdf - Acrobat", "페이지 3, page: 4")
    assert [item["value"] for item in pdf["targets"]] == [3, 4]
    browser = build_read_only_adapter_validation("Chrome", "로그인 화면")
    assert browser["targets"] == []
    assert browser["read_only"] is True


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


def test_prompt_template_versions_and_metadata_comparison(tmp_path: Path, monkeypatch):
    path = tmp_path / "prompt.json"
    first = save_prompt_template(path, "업무 템플릿", "{{month}} 문서를 확인", "standard", [])
    assert first["revision"] == 1
    unchanged = save_prompt_template(path, "업무 템플릿", "{{month}} 문서를 확인", "standard", [])
    assert unchanged["revision"] == 1
    second = save_prompt_template(path, "업무 템플릿", "{{month}} 문서를 검토", "browser", [])
    assert second["revision"] == 2
    assert (tmp_path / ".prompt.versions" / "v0001.json").exists()
    versions = list_prompt_template_versions(path)
    assert [item["revision"] for item in versions] == [2, 1]
    comparison = compare_prompt_template_versions(path, 1, 2)
    assert comparison["same"] is False
    assert "goal_template" in comparison["changed_fields"]
    assert "policy_profile" in comparison["changed_fields"]
    serialized = json.dumps(comparison, ensure_ascii=False)
    assert "{{month}} 문서를 확인" not in serialized
    assert "{{month}} 문서를 검토" not in serialized
    tampered = json.loads(path.read_text(encoding="utf-8"))
    tampered["goal_template"] = "변조된 목표"
    path.write_text(json.dumps(tampered, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="fingerprint"):
        load_prompt_template(path)


def test_prompt_template_file_comparison_omits_bodies(tmp_path: Path):
    left_path = tmp_path / "left.json"
    right_path = tmp_path / "right.json"
    save_prompt_template(left_path, "왼쪽", "왼쪽 목표 {{month}}", "standard", [])
    save_prompt_template(right_path, "오른쪽", "오른쪽 목표 {{month}}", "browser", [])
    comparison = compare_prompt_templates(left_path, right_path)
    assert comparison["same"] is False
    assert set(comparison["changed_fields"]) == {"name", "goal_template", "policy_profile"}
    serialized = json.dumps(comparison, ensure_ascii=False)
    assert "왼쪽 목표" not in serialized
    assert "오른쪽 목표" not in serialized


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


def test_policy_validation_rejects_malformed_plan_shapes():
    valid, message = validate_plan_policy(None, "standard")
    assert valid is False
    assert "객체" in message
    valid, message = validate_plan_policy({"steps": "not-a-list"}, "standard")
    assert valid is False
    assert "배열" in message


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


def test_runtime_config_validation_resets_unsafe_values():
    config, reset_fields = validate_runtime_config({
        "endpoint": "https://example.com/v1",
        "model": "   ",
        "timeout": 1,
        "vision": "true",
        "capture_on_error": True,
        "dry_run": False,
        "schedule_interval": 10,
        "document_roots": "C:/untrusted",
    })
    assert config["endpoint"] == "http://127.0.0.1:11434/v1"
    assert config["model"] == "gemma4:e2b"
    assert config["timeout"] == 120
    assert config["vision"] is False
    assert config["dry_run"] is False
    assert config["schedule_interval"] == 3600
    assert config["document_roots"] == []
    assert {"endpoint", "model", "timeout", "vision", "schedule_interval", "document_roots"}.issubset(reset_fields)


def test_atomic_write_text_replaces_without_leaving_deterministic_temp(tmp_path: Path):
    target = tmp_path / "state.json"
    atomic_write_text(target, "first")
    atomic_write_text(target, "second")
    assert target.read_text(encoding="utf-8") == "second"
    assert list(tmp_path.glob("state.json.tmp")) == []
    assert list(tmp_path.glob(".state.json.tmp")) == []


def test_runtime_config_loader_rejects_oversized_and_malformed_files(tmp_path: Path):
    oversized = tmp_path / "oversized.json"
    oversized.write_text("x" * (engine.MAX_CONFIG_FILE_BYTES + 1), encoding="utf-8")
    config, reasons = load_runtime_config(oversized)
    assert config["endpoint"] == "http://127.0.0.1:11434/v1"
    assert "file_size" in reasons

    malformed = tmp_path / "malformed.json"
    malformed.write_text("{not-json", encoding="utf-8")
    config, reasons = load_runtime_config(malformed)
    assert config["dry_run"] is True
    assert "file" in reasons


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


def test_scheduler_health_opens_after_consecutive_scheduled_failures():
    records = [
        {"event": "playback_failed", "scheduled": True},
        {"event": "playback_failed", "scheduled": True},
        {"event": "playback_completed", "scheduled": True},
        {"event": "playback_failed", "scheduled": True},
        {"event": "playback_failed", "scheduled": True},
        {"event": "playback_failed", "scheduled": True},
    ]
    health = evaluate_scheduler_health(records, window=5, failure_threshold=3)
    assert health["failures"] == 4
    assert health["consecutive_failures"] == 3
    assert health["circuit_open"] is True
    reset = evaluate_scheduler_health(records + [{"event": "playback_completed", "scheduled": True}])
    assert reset["consecutive_failures"] == 0
    assert reset["circuit_open"] is False


def test_scheduler_health_ignores_manual_failures_and_honors_threshold():
    records = [
        {"event": "playback_failed", "scheduled": False},
        {"event": "playback_failed", "scheduled": False},
        {"event": "playback_failed", "scheduled": True},
        {"event": "playback_failed", "scheduled": True},
    ]
    assert evaluate_scheduler_health(records)["circuit_open"] is False
    assert evaluate_scheduler_health(records, failure_threshold=2)["circuit_open"] is True


def test_execution_report_is_privacy_conscious_and_bounded(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(engine, "APP_DIR", tmp_path / "app")
    monkeypatch.setattr(engine, "RUN_REPORT_DIR", tmp_path / "app" / "run_reports")
    workflow = Workflow(name="보고서 작업", steps=[Step(type="key", value="secret")])
    report_path = write_execution_report(
        "run/../unsafe", "playback_completed", workflow, start_step=1,
        last_step=1, duration_seconds=1.23456, policy_profile="standard",
    )
    assert report_path is not None and report_path.exists()
    data = json.loads(report_path.read_text(encoding="utf-8"))
    assert data["run_id"] == "rununsafe"
    assert data["duration_seconds"] == 1.235
    assert data["workflow"] == "보고서 작업"
    assert "secret" not in json.dumps(data, ensure_ascii=False)
    assert "steps" not in data
    negative_path = write_execution_report("negative", "playback_failed", workflow, duration_seconds=-5)
    invalid_path = write_execution_report("invalid", "playback_failed", workflow, duration_seconds="not-a-number")
    assert json.loads(negative_path.read_text(encoding="utf-8"))["duration_seconds"] == 0.0
    assert json.loads(invalid_path.read_text(encoding="utf-8"))["duration_seconds"] == 0.0


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


def test_execution_report_rotation_and_directory_creation(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(engine, "APP_DIR", tmp_path / "app")
    monkeypatch.setattr(engine, "RUN_REPORT_DIR", tmp_path / "app" / "run_reports")
    monkeypatch.setattr(engine, "MAX_RUN_REPORTS", 2)
    engine.ensure_app_dirs()
    assert engine.RUN_REPORT_DIR.is_dir()
    workflow = Workflow(name="회전 작업")
    for index in range(3):
        assert write_execution_report(f"rotation-{index}", "playback_completed", workflow)
        time.sleep(0.002)
    assert len(list(engine.RUN_REPORT_DIR.glob("run_*.json"))) == 2


def test_execution_report_dashboard_summarizes_failures_without_sensitive_fields(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(engine, "APP_DIR", tmp_path / "app")
    monkeypatch.setattr(engine, "RUN_REPORT_DIR", tmp_path / "app" / "run_reports")
    workflow = Workflow(name="대시보드 작업", steps=[Step(type="key", value="입력 비밀")])
    write_execution_report("completed", "playback_completed", workflow, mode="playback")
    write_execution_report("failed-a", "playback_failed", workflow, mode="playback", error_type="WindowGuardError")
    write_execution_report("failed-b", "ai_plan_failed", workflow, mode="ai_plan", error_type="WindowGuardError")
    write_execution_report("stopped", "playback_stopped", workflow, mode="playback")
    reports = read_execution_reports()
    assert len(reports) == 4
    assert all("steps" not in item and "value" not in item for item in reports)
    summary = summarize_execution_reports(reports)
    assert summary["completed_runs"] == 1
    assert summary["failed_runs"] == 2
    assert summary["stopped_runs"] == 1
    assert summary["success_rate"] == 33.3
    assert summary["failure_patterns"] == [{"error_type": "WindowGuardError", "count": 2}]
    dashboard = build_execution_report_dashboard(limit=2)
    assert len(dashboard["recent_reports"]) == 2
    assert dashboard["summary"]["total_reports"] == 4


def test_execution_report_trends_and_user_initiated_export(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(engine, "APP_DIR", tmp_path / "app")
    monkeypatch.setattr(engine, "RUN_REPORT_DIR", tmp_path / "app" / "run_reports")
    workflow = Workflow(name="추이 작업", steps=[Step(type="key", value="민감 입력")])
    write_execution_report("policy-standard", "playback_completed", workflow, policy_profile="standard")
    write_execution_report("policy-browser", "playback_failed", workflow, mode="ai_plan", error_type="PolicyError", policy_profile="browser")
    reports = read_execution_reports()
    summary = summarize_execution_reports(reports)
    policies = {item["policy_profile"]: item for item in summary["policy_stats"]}
    assert policies["standard"]["completed_runs"] == 1
    assert policies["browser"]["failed_runs"] == 1
    assert len(summary["daily_trend"]) == 1
    output = tmp_path / "export" / "summary.json"
    export_execution_report_summary(output, period_days=30)
    exported = json.loads(output.read_text(encoding="utf-8"))
    assert exported["period_days"] == 30
    assert exported["summary"]["total_reports"] == 2
    assert "민감 입력" not in json.dumps(exported, ensure_ascii=False)
    assert "steps" not in json.dumps(exported, ensure_ascii=False)
    with pytest.raises(ValueError):
        export_execution_report_summary(tmp_path / "bad.json", period_days=0)


def test_windows_release_layout_check():
    result = validate_release_layout(Path(__file__).resolve().parents[1])
    assert result["valid"] is True
    assert result["failed_checks"] == []
    assert "Windows 실행·설치·빌드를 직접 수행하지 않았습니다" in result["notice"]


def test_windows_release_layout_rejects_missing_files(tmp_path: Path):
    result = validate_release_layout(tmp_path)
    assert result["valid"] is False
    assert "file:README.md" in result["failed_checks"]
    assert "requirements:fully_pinned" in result["failed_checks"]


def test_atomic_write_text_concurrent_writers_leave_one_complete_payload(tmp_path: Path):
    target = tmp_path / "concurrent-state.json"
    payloads = [f"writer-{index}-" + ("가" * 4096) for index in range(8)]
    barrier = threading.Barrier(len(payloads))
    failures: list[Exception] = []

    def write_payload(payload: str) -> None:
        try:
            barrier.wait(timeout=5)
            atomic_write_text(target, payload)
        except Exception as exc:  # noqa: BLE001
            failures.append(exc)

    threads = [threading.Thread(target=write_payload, args=(payload,)) for payload in payloads]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert failures == []
    assert target.read_text(encoding="utf-8") in payloads
    assert list(tmp_path.glob(".*.tmp")) == []


def test_workflow_from_dict_reports_type_errors_for_wrong_container_types():
    with pytest.raises(TypeError, match="최상위"):
        Workflow.from_dict([])
    with pytest.raises(TypeError, match="단계"):
        Workflow.from_dict({"version": 2, "steps": {}})
    with pytest.raises(TypeError, match="이름"):
        Workflow.from_dict({"version": 2, "name": 123, "steps": []})
