from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import threading
import time
import traceback
from urllib.parse import urlparse
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError

try:
    import pyautogui
except Exception:  # pragma: no cover - Windows runtime dependency
    pyautogui = None

try:
    import pyperclip
except Exception:  # pragma: no cover - Windows runtime dependency
    pyperclip = None

try:
    from pynput import keyboard, mouse
except Exception:  # pragma: no cover - Windows runtime dependency
    keyboard = None
    mouse = None


APP_DIR = Path(os.getenv("LOCALAPPDATA", Path.home())) / "AutoWorkAgent"
WORKFLOW_DIR = APP_DIR / "workflows"
CACHE_DIR = APP_DIR / "cache"
ERROR_DIR = APP_DIR / "errors"
DEBUG_DIR = APP_DIR / "debug_snapshots"
LOG_PATH = APP_DIR / "autowork.log"
CONFIG_PATH = APP_DIR / "config.json"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
_LOG_LOCK = threading.Lock()


def append_log(message: str, level: str = "INFO") -> None:
    """Append a local diagnostic line without sending data anywhere."""
    ensure_app_dirs()
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} [{level.upper()}] {message}\n"
    with _LOG_LOCK:
        with LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(line)


def write_error_report(context: Dict[str, Any], exc: BaseException, capture_screen: bool = False, traceback_text: Optional[str] = None) -> Path:
    """Write a local JSON error report and optionally a screen snapshot."""
    ensure_app_dirs()
    stamp = time.strftime("%Y%m%d_%H%M%S") + f"_{int(time.time() * 1000) % 1000:03d}"
    report_path = ERROR_DIR / f"error_{stamp}.json"
    report: Dict[str, Any] = {
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "context": context,
        "error_type": type(exc).__name__,
        "error": str(exc)[:4000],
        "traceback": (traceback_text or traceback.format_exc())[-16000:],
    }
    if capture_screen and pyautogui is not None:
        try:
            image_path = DEBUG_DIR / f"error_{stamp}.png"
            pyautogui.screenshot().save(image_path)
            report["screen_snapshot"] = str(image_path)
        except Exception as snapshot_exc:
            report["screen_snapshot_error"] = str(snapshot_exc)[:1000]
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    append_log(f"오류 보고서 생성: {report_path} · {type(exc).__name__}: {str(exc)[:500]}", "ERROR")
    return report_path


def validate_local_endpoint(endpoint: str) -> str:
    """Allow only a local loopback OpenAI-compatible endpoint."""
    candidate = (endpoint or "").strip()
    if "://" not in candidate:
        candidate = "http://" + candidate
    parsed = urlparse(candidate)
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in LOCAL_HOSTS:
        raise ValueError("보안 설정상 로컬 LLM 주소만 허용됩니다. 예: http://127.0.0.1:11434/v1")
    if parsed.username or parsed.password:
        raise ValueError("로컬 LLM 주소에 사용자명·비밀번호를 넣을 수 없습니다.")
    return candidate.rstrip("/")


class ObservedElement(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    role: str = "unknown"
    name: str = ""
    value: str = ""
    bbox: list[int] = Field(default_factory=list)
    source: Literal["uia", "ocr", "cv"] = "uia"
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    enabled: bool = True
    visible: bool = True
    automation_id: Optional[str] = None
    control_type: Optional[str] = None


class UIAction(BaseModel):
    """Gemma가 반환하는 안전한 element_id 기반 단일 동작."""

    model_config = ConfigDict(extra="forbid")

    action: Literal["click", "double_click", "type", "hotkey", "scroll", "wait", "none"]
    element_id: Optional[str] = None
    text: Optional[str] = None
    keys: list[str] = Field(default_factory=list)
    amount: int = Field(default=0, ge=-5, le=5)
    seconds: float = Field(default=0.0, ge=0.0, le=60.0)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    risk: Literal["read", "write", "submit", "delete", "unknown"] = "unknown"
    requires_confirmation: bool = True
    reason: str = ""
    expected_texts: list[str] = Field(default_factory=list)


class AutomationPlan(BaseModel):
    model_config = ConfigDict(extra="ignore")

    summary: str = ""
    risk: Literal["low", "medium", "high"] = "high"
    steps: list[UIAction] = Field(default_factory=list, max_length=30)


class RetryableAutomationError(RuntimeError):
    pass


class UserInterventionRequired(RuntimeError):
    pass


@dataclass
class Step:
    """A replayable, user-observable input step."""

    type: str
    delay: float = 0.0
    x: Optional[int] = None
    y: Optional[int] = None
    button: Optional[str] = None
    event: Optional[str] = None
    kind: Optional[str] = None
    value: Optional[str] = None


@dataclass
class Workflow:
    name: str = "새 작업"
    description: str = ""
    steps: List[Step] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": 1,
            "name": self.name,
            "description": self.description,
            "steps": [asdict(step) for step in self.steps],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Workflow":
        steps = []
        for raw in data.get("steps", []):
            if not isinstance(raw, dict) or "type" not in raw:
                continue
            steps.append(Step(**{key: raw.get(key) for key in Step.__dataclass_fields__}))
        return cls(
            name=str(data.get("name", "불러온 작업")),
            description=str(data.get("description", "")),
            steps=steps,
        )


class InputRecorder:
    """Records mouse clicks and keyboard press/release events globally."""

    def __init__(self, on_step: Optional[Callable[[Step], None]] = None, on_error: Optional[Callable[[BaseException], None]] = None):
        self.on_step = on_step
        self.on_error = on_error
        self.steps: List[Step] = []
        self._mouse_listener = None
        self._keyboard_listener = None
        self._running = False
        self._started_at = 0.0
        self._last_event_at = 0.0
        self._lock = threading.Lock()

    @property
    def running(self) -> bool:
        return self._running

    @staticmethod
    def _button_name(button: Any) -> str:
        return str(button).split(".")[-1]

    @staticmethod
    def _key_payload(key: Any) -> tuple[str, str]:
        char = getattr(key, "char", None)
        if char:
            return "char", char
        name = str(key).replace("Key.", "")
        return "special", name

    def _append(self, step: Step) -> None:
        with self._lock:
            now = time.monotonic()
            step.delay = round(max(0.0, now - self._last_event_at), 3) if self._last_event_at else 0.0
            self._last_event_at = now
            self.steps.append(step)
        if self.on_step:
            try:
                self.on_step(step)
            except Exception as exc:
                self.stop()
                if self.on_error:
                    self.on_error(exc)

    def start(self, clear: bool = True) -> None:
        if keyboard is None or mouse is None:
            raise RuntimeError("pynput이 설치되어 있지 않거나 Windows 입력 장치에 접근할 수 없습니다.")
        self.stop()
        if clear:
            self.steps.clear()
        self._running = True
        self._started_at = time.monotonic()
        self._last_event_at = 0.0
        try:
            self._mouse_listener = mouse.Listener(on_click=self._on_click)
            self._keyboard_listener = keyboard.Listener(on_press=self._on_press, on_release=self._on_release)
            self._mouse_listener.start()
            self._keyboard_listener.start()
        except Exception:
            self.stop()
            raise

    def stop(self) -> None:
        self._running = False
        for listener in (self._mouse_listener, self._keyboard_listener):
            if listener is not None:
                try:
                    listener.stop()
                except Exception:
                    pass
        self._mouse_listener = None
        self._keyboard_listener = None

    def _on_click(self, x: int, y: int, button: Any, pressed: bool) -> None:
        if self._running and pressed:
            self._append(Step(type="click", x=int(x), y=int(y), button=self._button_name(button)))

    def _on_press(self, key: Any) -> None:
        if not self._running:
            return
        kind, value = self._key_payload(key)
        self._append(Step(type="key", event="down", kind=kind, value=value))

    def _on_release(self, key: Any) -> None:
        if not self._running:
            return
        kind, value = self._key_payload(key)
        self._append(Step(type="key", event="up", kind=kind, value=value))


class WorkflowPlayer:
    """Replays a workflow and can be interrupted by a stop event."""

    BUTTONS = {"left", "right", "middle", "x1", "x2"}
    KEY_ALIASES = {
        "ctrl_l": "ctrl", "ctrl_r": "ctrl", "shift": "shift", "shift_l": "shift", "shift_r": "shift",
        "alt_l": "alt", "alt_r": "alt", "cmd": "win", "cmd_l": "win", "cmd_r": "win",
        "space": "space", "enter": "enter", "tab": "tab", "backspace": "backspace",
        "delete": "delete", "esc": "esc", "up": "up", "down": "down", "left": "left", "right": "right",
        "home": "home", "end": "end", "page_up": "pageup", "page_down": "pagedown",
        "insert": "insert", "caps_lock": "capslock", "num_lock": "numlock", "print_screen": "printscreen",
    }

    def __init__(self, on_step: Optional[Callable[[int, Step], None]] = None, on_status: Optional[Callable[[str], None]] = None, on_error: Optional[Callable[[BaseException, int, Step], None]] = None):
        self.on_step = on_step
        self.on_status = on_status
        self.on_error = on_error
        self.stop_event = threading.Event()
        self.running = False
        self.current_index = 0
        self.current_step: Optional[Step] = None
        self._pressed_keys: set[str] = set()

    def stop(self) -> None:
        self.stop_event.set()

    def _sleep(self, seconds: float) -> bool:
        return not self.stop_event.wait(max(0.0, min(float(seconds), 60.0)))

    @staticmethod
    def _paste(text: str) -> None:
        if pyperclip is None or pyautogui is None:
            raise RuntimeError("pyperclip 또는 pyautogui가 설치되어 있지 않습니다.")
        previous = None
        try:
            previous = pyperclip.paste()
        except Exception:
            pass
        pyperclip.copy(text)
        pyautogui.hotkey("ctrl", "v")
        time.sleep(0.03)
        if previous is not None:
            try:
                pyperclip.copy(previous)
            except Exception:
                pass

    @classmethod
    def _key_name(cls, kind: str, value: str) -> str:
        if kind == "char":
            return value
        return cls.KEY_ALIASES.get(value, value)

    def _play_key(self, step: Step) -> None:
        if pyautogui is None:
            raise RuntimeError("pyautogui가 설치되어 있지 않습니다.")
        value = step.value or ""
        key_name = self._key_name(step.kind or "special", value)
        event = step.event or "down"
        if step.kind == "char" and len(value) == 1 and ord(value) > 127:
            if event == "down":
                self._paste(value)
            return
        if event == "down":
            pyautogui.keyDown(key_name)
            self._pressed_keys.add(key_name)
        else:
            pyautogui.keyUp(key_name)
            self._pressed_keys.discard(key_name)

    def _release_pressed_keys(self) -> None:
        if pyautogui is None:
            self._pressed_keys.clear()
            return
        for key_name in list(self._pressed_keys):
            try:
                pyautogui.keyUp(key_name)
            except Exception:
                pass
        self._pressed_keys.clear()

    def play(self, workflow: Workflow) -> None:
        if pyautogui is None:
            raise RuntimeError("pyautogui가 설치되어 있지 않습니다.")
        self.stop_event.clear()
        self.running = True
        try:
            pyautogui.PAUSE = 0.03
            pyautogui.FAILSAFE = True
            total = len(workflow.steps)
            for index, step in enumerate(workflow.steps, start=1):
                self.current_index = index
                self.current_step = step
                if not self._sleep(step.delay) or self.stop_event.is_set():
                    break
                if self.on_step:
                    self.on_step(index, step)
                if step.type == "click":
                    if step.x is None or step.y is None:
                        continue
                    button = step.button if step.button in self.BUTTONS else "left"
                    pyautogui.click(step.x, step.y, button=button)
                elif step.type == "key":
                    self._play_key(step)
                elif step.type == "wait":
                    self._sleep(float(step.value or 0.5))
                if self.on_status:
                    self.on_status(f"재생 중: {index}/{total}")
        except Exception as exc:
            if self.on_error and self.current_step is not None:
                self.on_error(exc, self.current_index, self.current_step)
            else:
                raise
        finally:
            self._release_pressed_keys()
            self.running = False
            if self.on_status:
                self.on_status("재생 종료")


def ensure_app_dirs() -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    WORKFLOW_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    ERROR_DIR.mkdir(parents=True, exist_ok=True)
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)


def save_workflow(workflow: Workflow, path: Path) -> None:
    ensure_app_dirs()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(workflow.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")


def load_workflow(path: Path) -> Workflow:
    return Workflow.from_dict(json.loads(path.read_text(encoding="utf-8")))


def capture_observation() -> Dict[str, Any]:
    """Capture the screen and best-effort active-window/OCR context."""
    ensure_app_dirs()
    if pyautogui is None:
        raise RuntimeError("pyautogui가 설치되어 있지 않습니다.")
    image_path = CACHE_DIR / f"observation_{int(time.time())}.png"
    image = pyautogui.screenshot()
    image.save(image_path)

    active_window = ""
    ui_controls: List[Dict[str, Any]] = []
    element_records: List[Dict[str, Any]] = []
    try:
        from pywinauto import Desktop
        active = Desktop(backend="uia").get_active()
        active_window = active.window_text()
        # Accessibility metadata is best-effort: some legacy applications expose little or none.
        for control in active.descendants()[:80]:
            try:
                rect = control.rectangle()
                name = control.window_text()
                control_type = control.element_info.control_type or ""
                if name or control_type:
                    bbox = [rect.left, rect.top, rect.right, rect.bottom]
                    automation_id = str(getattr(control.element_info, "automation_id", "") or "")
                    identity = f"{automation_id}|{control_type}|{name}|{bbox}"
                    element_id = "uia_" + hashlib.sha1(identity.encode("utf-8")).hexdigest()[:12]
                    role = {
                        "Button": "button", "Edit": "edit", "CheckBox": "checkbox", "RadioButton": "radio",
                        "Hyperlink": "link", "Menu": "menu", "List": "list", "TabItem": "tab", "Text": "text",
                    }.get(control_type, "unknown")
                    element = {
                        "id": element_id,
                        "name": name[:160],
                        "value": "",
                        "role": role,
                        "source": "uia",
                        "bbox": bbox,
                        "control_type": control_type,
                        "automation_id": automation_id or None,
                        "enabled": bool(control.is_enabled()) if hasattr(control, "is_enabled") else True,
                        "visible": bool(control.is_visible()) if hasattr(control, "is_visible") else True,
                        "confidence": 1.0,
                    }
                    element_records.append(element)
                    ui_controls.append({
                        "id": element_id,
                        "name": name[:160],
                        "control_type": control_type,
                        "rectangle": bbox,
                    })
            except Exception:
                continue
    except Exception:
        active_window = ""

    ocr_text = ""
    try:
        import pytesseract
        ocr_text = pytesseract.image_to_string(image, lang="kor+eng")
    except Exception:
        ocr_text = "OCR을 사용할 수 없습니다. Windows에 Tesseract와 kor+eng 언어 데이터를 설치하면 화면 문자를 읽을 수 있습니다."

    return {
        "image_path": str(image_path),
        "screen_size": list(image.size),
        "active_window": active_window,
        "ocr_text": ocr_text[:12000],
        "ui_controls": ui_controls,
        "elements": element_records,
        "frame_hash": hashlib.sha256(image_path.read_bytes()).hexdigest(),
        "captured_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


class LocalAIClient:
    """OpenAI-compatible local model client for Ollama, LM Studio, or similar servers."""

    def __init__(self, endpoint: str = "http://127.0.0.1:11434/v1", model: str = "gemma4:e2b", timeout: int = 120, vision: bool = False):
        self.endpoint = validate_local_endpoint(endpoint)
        self.model = model
        self.timeout = timeout
        self.vision = vision

    @staticmethod
    def _image_data_url(path: str) -> str:
        raw = Path(path).read_bytes()
        encoded = base64.b64encode(raw).decode("ascii")
        return f"data:image/png;base64,{encoded}"

    @staticmethod
    def _extract_content(response: Dict[str, Any]) -> str:
        choices = response.get("choices") or []
        if not choices:
            raise RuntimeError("로컬 AI 서버 응답에 choices가 없습니다.")
        message = choices[0].get("message", {})
        content = message.get("content", "")
        if isinstance(content, list):
            return "".join(part.get("text", "") for part in content if isinstance(part, dict))
        return str(content)

    @staticmethod
    def _parse_json(text: str) -> Dict[str, Any]:
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.I)
            cleaned = re.sub(r"\s*```$", "", cleaned)
        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", cleaned, flags=re.S)
            if not match:
                return {"summary": cleaned, "risk": "high", "steps": []}
            try:
                data = json.loads(match.group(0))
            except json.JSONDecodeError:
                return {"summary": cleaned, "risk": "high", "steps": []}
        if not isinstance(data, dict):
            return {"summary": str(data), "risk": "high", "steps": []}
        return data

    def make_plan(self, goal: str, observation: Dict[str, Any]) -> Dict[str, Any]:
        import requests

        system = (
            "당신은 Windows 데스크톱 업무 자동화의 계획기입니다. "
            "반드시 관찰 JSON의 elements에 실제로 존재하는 element_id만 사용하십시오. "
            "좌표를 생성하거나 추측하지 말고, element_id가 없거나 확신이 낮으면 action=none을 반환하십시오. "
            "허용 action은 click, double_click, type, hotkey, scroll, wait, none뿐입니다. "
            "파일 삭제, 명령 셸, 결제, 전송, 게시, 로그인 정보 입력, 결재는 계획하지 마십시오. "
            "제출·삭제·저장 덮어쓰기·권한 변경처럼 위험한 동작은 requires_confirmation=true로 설정하십시오. "
            "반드시 JSON Schema에 맞는 계획 하나만 반환하십시오."
        )
        user_text = {
            "goal": goal,
            "active_window": observation.get("active_window", ""),
            "screen_size": observation.get("screen_size", []),
            "ocr_text": observation.get("ocr_text", ""),
            "elements": observation.get("elements", [])[:120],
            "frame_hash": observation.get("frame_hash", ""),
        }
        content: Any = json.dumps(user_text, ensure_ascii=False)
        if self.vision and observation.get("image_path"):
            content = [
                {"type": "text", "text": json.dumps(user_text, ensure_ascii=False)},
                {"type": "image_url", "image_url": {"url": self._image_data_url(observation["image_path"])}},
            ]
        schema = AutomationPlan.model_json_schema()
        payload = {
            "model": self.model,
            "temperature": 0.0,
            "max_tokens": 1600,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "automation_plan", "strict": True, "schema": schema},
            },
        }
        response = requests.post(f"{self.endpoint}/chat/completions", json=payload, timeout=self.timeout)
        if response.status_code == 400:
            # 일부 로컬 서버는 response_format을 아직 지원하지 않으므로 한 번만 호환 모드로 재시도합니다.
            payload.pop("response_format", None)
            response = requests.post(f"{self.endpoint}/chat/completions", json=payload, timeout=self.timeout)
        response.raise_for_status()
        raw = self._parse_json(self._extract_content(response.json()))
        try:
            return AutomationPlan.model_validate(raw).model_dump(mode="json")
        except ValidationError as exc:
            raise RetryableAutomationError(f"Gemma 구조화 계획 검증 실패: {exc}") from exc


def validate_ai_steps(
    plan: Dict[str, Any],
    screen_size: tuple[int, int] | None = None,
    observation: Dict[str, Any] | None = None,
) -> tuple[bool, str]:
    """Validate a plan against the current screen elements; direct coordinates are rejected."""
    try:
        parsed = AutomationPlan.model_validate(plan)
    except ValidationError as exc:
        return False, f"구조화 계획 형식 오류: {exc.errors()[0].get('msg', str(exc))}"
    if observation is None:
        return False, "현재 화면의 element_id 정보가 없어 실행할 수 없습니다. 화면을 다시 관찰하세요."
    try:
        elements = [ObservedElement.model_validate(item) for item in observation.get("elements", [])]
    except ValidationError as exc:
        return False, f"화면 요소 형식 오류: {exc.errors()[0].get('msg', str(exc))}"
    element_map = {element.id: element for element in elements}
    width, height = screen_size or tuple(observation.get("screen_size", [100000, 100000]))
    safe_hotkeys = {"ctrl", "shift", "alt", "win", "enter", "esc", "tab", "space", "backspace", "delete", "home", "end", "left", "right", "up", "down"}
    dangerous = {("alt", "f4"), ("ctrl", "shift", "delete")}
    for index, action in enumerate(parsed.steps, start=1):
        if action.action == "none":
            return False, f"{index}번째 단계: AI가 확신하지 못해 실행하지 않습니다."
        if action.action not in {"wait", "scroll"} and action.confidence < 0.80:
            return False, f"{index}번째 단계의 신뢰도가 낮습니다({action.confidence:.2f})."
        if action.action in {"click", "double_click", "type"}:
            if not action.element_id:
                return False, f"{index}번째 단계에 element_id가 없습니다. 좌표 직접 지정은 허용하지 않습니다."
            element = element_map.get(action.element_id)
            if element is None:
                return False, f"{index}번째 단계의 element_id가 현재 화면에 없습니다."
            if not element.visible or not element.enabled:
                return False, f"{index}번째 단계의 UI 요소가 보이지 않거나 비활성 상태입니다."
            if len(element.bbox) != 4 or element.bbox[2] <= element.bbox[0] or element.bbox[3] <= element.bbox[1]:
                return False, f"{index}번째 단계의 UI 요소 영역이 유효하지 않습니다."
            if not (0 <= element.bbox[0] < width and 0 < element.bbox[2] <= width and 0 <= element.bbox[1] < height and 0 < element.bbox[3] <= height):
                return False, f"{index}번째 UI 요소가 화면 밖입니다."
        if action.action == "type" and not action.text:
            return False, f"{index}번째 입력값이 없습니다."
        if action.action == "hotkey":
            keys = tuple(key.lower() for key in action.keys)
            if not keys or any(key not in safe_hotkeys and not re.fullmatch(r"f([1-9]|1[0-2])", key) for key in keys):
                return False, f"{index}번째 단축키가 허용되지 않습니다."
            if keys in dangerous:
                return False, f"{index}번째 위험 단축키는 자동 실행하지 않습니다."
        if action.action == "wait" and not 0 <= action.seconds <= 60:
            return False, f"{index}번째 대기 시간이 허용 범위를 벗어났습니다."
        if action.risk in {"submit", "delete", "unknown"} and not action.requires_confirmation:
            return False, f"{index}번째 위험 동작은 사용자 확인이 필요합니다."
    return True, "검증 완료"
