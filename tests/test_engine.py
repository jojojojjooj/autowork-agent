import json
from pathlib import Path

import pytest

from app import engine
from app.engine import (
    LocalAIClient,
    Step,
    Workflow,
    WorkflowPlayer,
    load_workflow,
    save_workflow,
    validate_ai_steps,
    validate_local_endpoint,
    validate_timeout,
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


def test_workflow_remove_step():
    workflow = Workflow(steps=[Step(type="wait", value="1"), Step(type="wait", value="2")])
    removed = workflow.remove_step(0)
    assert removed.value == "1"
    assert len(workflow.steps) == 1
    with pytest.raises(IndexError):
        workflow.remove_step(3)


def test_workflow_rejects_invalid_screen_metadata():
    with pytest.raises(ValueError, match="화면 크기"):
        Workflow.from_dict({"version": 2, "recorded_screen_size": [0, 1080], "steps": []})
    with pytest.raises(ValueError, match="화면 크기"):
        Workflow.from_dict({"version": 2, "recorded_screen_size": [1920], "steps": []})


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


def test_workflow_is_saved_as_version_2_and_rejects_unsafe_steps(tmp_path: Path):
    path = tmp_path / "workflow.json"
    save_workflow(Workflow(steps=[Step(type="wait", value="1")]), path)
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["version"] == 2
    assert load_workflow(path).steps[0].value == "1"

    with pytest.raises(ValueError, match="동작 유형"):
        Workflow.from_dict({"version": 2, "steps": [{"type": "shell"}]})


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
