from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import shutil
import threading
import time
import traceback
from urllib.parse import urlparse
from collections import Counter
from dataclasses import asdict, dataclass, field, replace
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
BACKUP_DIR = APP_DIR / "workflow_backups"
CACHE_DIR = APP_DIR / "cache"
ERROR_DIR = APP_DIR / "errors"
DEBUG_DIR = APP_DIR / "debug_snapshots"
TEMPLATE_DIR = APP_DIR / "templates"
HISTORY_PATH = APP_DIR / "execution_history.jsonl"
WORKFLOW_KEY_PATH = APP_DIR / "workflow.key"
LOG_PATH = APP_DIR / "autowork.log"
CONFIG_PATH = APP_DIR / "config.json"
CHECKPOINT_PATH = APP_DIR / "execution_checkpoint.json"
MONITOR_STATE_PATH = APP_DIR / "monitor_state.json"
RUN_REPORT_DIR = APP_DIR / "run_reports"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
WORKFLOW_VERSION = 2
MAX_WORKFLOW_FILE_BYTES = 10 * 1024 * 1024
MAX_WORKFLOW_STEPS = 10_000
MAX_TEXT_INPUT_LENGTH = 100_000
MAX_HISTORY_FILE_BYTES = 5 * 1024 * 1024
MAX_HISTORY_RECORDS_ON_ROTATE = 1_000
MAX_RUN_REPORTS = 100
MAX_STEP_DELAY = 60.0
MAX_CAPTURE_AGE_DAYS = 30
MAX_HISTORY_FILE_BYTES = 5 * 1024 * 1024
MAX_SCHEDULE_INTERVAL_SECONDS = 24 * 60 * 60
MAX_CONFIG_FILE_BYTES = 256 * 1024
AUDIT_HASH_VERSION = 1
AUDIT_GENESIS = "0" * 64
MAX_WORKFLOW_BACKUPS = 20
CHECKPOINT_VERSION = 1
SENSITIVE_FIELD_NAMES = {"text", "value", "password", "token", "secret", "authorization", "api_key"}
PII_PATTERNS = (
    (re.compile(r"(?<!\d)\d{6}[- ]\d{7}(?!\d)"), "<주민번호 마스킹>"),
    (re.compile(r"(?<!\d)01[016789][- ]?\d{3,4}[- ]?\d{4}(?!\d)"), "<전화번호 마스킹>"),
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "<이메일 마스킹>"),
)
_LOG_LOCK = threading.Lock()
_HISTORY_LOCK = threading.Lock()
_MONITOR_LOCK = threading.Lock()


def redact_sensitive(value: Any) -> Any:
    """Redact values that may contain typed text or credentials in diagnostics."""
    if isinstance(value, dict):
        return {
            key: (f"<redacted:{len(str(item))} chars>" if str(key).lower() in SENSITIVE_FIELD_NAMES else redact_sensitive(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_sensitive(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_sensitive(item) for item in value)
    return value


def mask_sensitive_text(text: str) -> str:
    """Mask common personal identifiers before OCR text is persisted or sent to AI."""
    masked = str(text or "")
    for pattern, replacement in PII_PATTERNS:
        masked = pattern.sub(replacement, masked)
    return masked


def mask_sensitive_image(image: Any) -> Any:
    """Best-effort blackout of OCR tokens matching common personal identifiers."""
    try:
        import pytesseract
        from PIL import ImageDraw
        data = pytesseract.image_to_data(image, output_type=pytesseract.Output.DICT, lang="kor+eng")
        draw = ImageDraw.Draw(image)
        for index, token in enumerate(data.get("text", [])):
            if not token or token == mask_sensitive_text(token):
                continue
            left = int(data["left"][index])
            top = int(data["top"][index])
            width = int(data["width"][index])
            height = int(data["height"][index])
            draw.rectangle((left, top, left + width, top + height), fill="black")
    except Exception:
        pass
    return image


def append_log(message: str, level: str = "INFO") -> None:
    """Append a local diagnostic line without sending data anywhere."""
    ensure_app_dirs()
    safe_message = mask_sensitive_text(str(redact_sensitive(message)))
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} [{level.upper()}] {safe_message}\n"
    with _LOG_LOCK:
        with LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(line)


def _audit_hash(record: Dict[str, Any]) -> str:
    payload = {key: value for key, value in record.items() if key != "record_hash"}
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _last_audit_hash() -> str:
    if not HISTORY_PATH.exists():
        return AUDIT_GENESIS
    try:
        for line in reversed(HISTORY_PATH.read_text(encoding="utf-8", errors="replace").splitlines()):
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict) and isinstance(data.get("record_hash"), str) and len(data["record_hash"]) == 64:
                return data["record_hash"]
    except OSError:
        pass
    return AUDIT_GENESIS


def append_execution_history(event: str, workflow: Optional["Workflow"] = None, **details: Any) -> None:
    """Append a privacy-conscious, hash-chained execution event to local JSONL."""
    ensure_app_dirs()
    try:
        with _HISTORY_LOCK:
            if HISTORY_PATH.exists() and HISTORY_PATH.stat().st_size >= MAX_HISTORY_FILE_BYTES:
                previous = HISTORY_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
                temporary = HISTORY_PATH.with_name(f".{HISTORY_PATH.name}.tmp")
                kept = previous[-MAX_HISTORY_RECORDS_ON_ROTATE:]
                temporary.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
                os.replace(temporary, HISTORY_PATH)
            record: Dict[str, Any] = {
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                "event": str(event)[:80],
                "workflow": workflow.name[:200] if workflow else "",
                "step_count": len(workflow.steps) if workflow else 0,
                "audit_hash_version": AUDIT_HASH_VERSION,
                "previous_hash": _last_audit_hash(),
            }
            for key, value in details.items():
                if isinstance(value, (str, int, float, bool)) or value is None:
                    record[str(key)[:80]] = value
            record["record_hash"] = _audit_hash(record)
            with HISTORY_PATH.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        trim_execution_history()
    except OSError as exc:
        # Audit history is useful but must never block the requested automation.
        append_log(f"실행 이력 저장 실패: {exc}", "WARNING")


def verify_execution_history(records: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Verify hash and previous-hash links for retained audit records."""
    items = records if records is not None else read_execution_history(2_000)
    issues: List[str] = []
    checked = 0
    legacy = 0
    previous_hash: Optional[str] = None
    first_hashed_record = True
    anchored = False
    for index, record in enumerate(items):
        stored = record.get("record_hash")
        if not isinstance(stored, str):
            legacy += 1
            previous_hash = None
            continue
        expected = _audit_hash(record)
        if stored != expected:
            issues.append(f"record_{index}_hash_mismatch")
        link = record.get("previous_hash")
        if previous_hash is not None and link != previous_hash:
            issues.append(f"record_{index}_link_mismatch")
        if first_hashed_record:
            anchored = record.get("previous_hash") == AUDIT_GENESIS
            first_hashed_record = False
        checked += 1
        previous_hash = stored
    return {"valid": not issues, "anchored": anchored, "checked_records": checked, "legacy_records": legacy, "issues": issues[:20]}


def validate_schedule_interval(value: Any) -> int:
    """Validate a low-frequency local schedule interval in seconds."""
    try:
        interval = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("예약 간격은 정수 초 단위여야 합니다.") from exc
    if not 60 <= interval <= MAX_SCHEDULE_INTERVAL_SECONDS:
        raise ValueError("예약 간격은 60초~24시간 범위여야 합니다.")
    return interval


class LocalScheduler:
    """Run a callback at a low frequency while the desktop app remains open."""

    def __init__(self, callback: Callable[[], None]) -> None:
        self.callback = callback
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self.interval_seconds = 0

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self, interval_seconds: int, run_immediately: bool = False) -> None:
        interval = validate_schedule_interval(interval_seconds)
        with self._lock:
            if self.running:
                raise RuntimeError("예약 실행이 이미 동작 중입니다.")
            self.interval_seconds = interval
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._run, args=(interval, run_immediately), daemon=True, name="autowork-scheduler")
            self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self._thread = None

    def _run(self, interval: int, run_immediately: bool) -> None:
        if run_immediately:
            try:
                self.callback()
            except Exception as exc:
                append_log(f"예약 실행 콜백 실패: {exc}", "ERROR")
        while not self._stop_event.wait(interval):
            try:
                self.callback()
            except Exception as exc:
                append_log(f"예약 실행 콜백 실패: {exc}", "ERROR")


def trim_execution_history(max_bytes: int = MAX_HISTORY_FILE_BYTES) -> None:
    """Keep only the newest complete JSONL records within a bounded file size."""
    ensure_app_dirs()
    try:
        limit = max(1024, int(max_bytes))
        with _HISTORY_LOCK:
            if not HISTORY_PATH.exists() or HISTORY_PATH.stat().st_size <= limit:
                return
            lines = HISTORY_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
            kept: List[str] = []
            total = 0
            for line in reversed(lines):
                encoded_size = len((line + "\n").encode("utf-8"))
                if kept and total + encoded_size > limit:
                    break
                kept.append(line)
                total += encoded_size
            kept.reverse()
            temporary = HISTORY_PATH.with_suffix(".tmp")
            temporary.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
            os.replace(temporary, HISTORY_PATH)
    except (OSError, ValueError) as exc:
        append_log(f"실행 이력 정리 실패: {exc}", "WARNING")


def read_execution_history(limit: int = 200) -> List[Dict[str, Any]]:
    """Read the newest local execution events without exposing input contents."""
    ensure_app_dirs()
    try:
        count = max(1, min(int(limit), 2_000))
    except (TypeError, ValueError):
        count = 200
    if not HISTORY_PATH.exists():
        return []
    records: List[Dict[str, Any]] = []
    try:
        with _HISTORY_LOCK:
            lines = HISTORY_PATH.read_text(encoding="utf-8", errors="replace").splitlines()[-count:]
    except OSError:
        return records
    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def summarize_execution_history(records: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Summarize bounded audit records without exposing input contents."""
    items = records if records is not None else read_execution_history(2_000)
    events = Counter(str(item.get("event", "unknown")) for item in items)
    completed = sum(events[event] for event in ("playback_completed", "ai_plan_completed"))
    failed = sum(count for event, count in events.items() if event.endswith("_failed"))
    runs = completed + failed
    return {
        "total_events": len(items),
        "completed_runs": completed,
        "failed_runs": failed,
        "success_rate": round((completed / runs) * 100, 1) if runs else None,
        "events": dict(events),
        "last_event": items[-1].get("event") if items else None,
    }


MONITOR_SCHEMA_VERSION = 1


def evaluate_monitor_alerts(summary: Dict[str, Any], min_runs: int = 3, failure_threshold_percent: float = 50.0) -> List[Dict[str, Any]]:
    """Return deterministic local alerts from bounded execution statistics."""
    alerts: List[Dict[str, Any]] = []
    failed = int(summary.get("failed_runs", 0) or 0)
    completed = int(summary.get("completed_runs", 0) or 0)
    runs = failed + completed
    if failed >= 3:
        alerts.append({"severity": "high", "code": "repeated_failures", "message": f"최근 실행에서 {failed}건의 실패가 누적되었습니다."})
    success_rate = summary.get("success_rate")
    if runs >= max(1, int(min_runs)) and success_rate is not None and float(success_rate) < max(0.0, float(100.0 - failure_threshold_percent)):
        alerts.append({"severity": "medium", "code": "low_success_rate", "message": f"성공률이 {float(success_rate):.1f}%로 기준보다 낮습니다."})
    if not runs:
        alerts.append({"severity": "info", "code": "no_runs", "message": "아직 완료 또는 실패한 실행 기록이 없습니다."})
    return alerts


def evaluate_scheduler_health(records: Optional[List[Dict[str, Any]]] = None, window: int = 5, failure_threshold: int = 3) -> Dict[str, Any]:
    """Return a deterministic circuit-breaker state for scheduled playback."""
    items = records if records is not None else read_execution_history(2_000)
    scheduled = [
        item for item in items
        if bool(item.get("scheduled")) and str(item.get("event", "")) in {
            "playback_completed", "playback_failed", "playback_stopped"
        }
    ][-max(1, int(window)):]
    consecutive_failures = 0
    for item in reversed(scheduled):
        if item.get("event") != "playback_failed":
            break
        consecutive_failures += 1
    threshold = max(1, int(failure_threshold))
    return {
        "window": len(scheduled),
        "failures": sum(item.get("event") == "playback_failed" for item in scheduled),
        "consecutive_failures": consecutive_failures,
        "circuit_open": consecutive_failures >= threshold,
        "failure_threshold": threshold,
    }


def build_monitor_snapshot(status: str = "idle", workflow: Optional["Workflow"] = None, scheduler_running: bool = False, current_step: Optional[int] = None) -> Dict[str, Any]:
    """Build a privacy-conscious operational snapshot without steps or input values."""
    records = read_execution_history(2_000)
    summary = summarize_execution_history(records)
    audit_integrity = verify_execution_history(records)
    alerts = evaluate_monitor_alerts(summary)
    if not audit_integrity["valid"]:
        alerts.insert(0, {"severity": "critical", "code": "audit_integrity_failure", "message": "실행 감사 이력의 무결성 검증에 실패했습니다."})
    checkpoint = load_execution_checkpoint()
    scheduler_health = evaluate_scheduler_health(records)
    if scheduler_health["circuit_open"]:
        alerts.insert(0, {"severity": "critical", "code": "scheduler_circuit_open", "message": "예약 실행이 반복 실패로 자동 중지 대기 상태입니다."})
    return redact_sensitive({
        "schema_version": MONITOR_SCHEMA_VERSION,
        "heartbeat_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "status": str(status)[:160],
        "workflow_name": workflow.name[:200] if workflow else "",
        "workflow_step_count": len(workflow.steps) if workflow else 0,
        "workflow_signed": bool(workflow and workflow.signature),
        "scheduler_running": bool(scheduler_running),
        "scheduler_health": scheduler_health,
        "current_step": current_step if current_step is None else max(0, int(current_step)),
        "checkpoint_pending": bool(checkpoint),
        "summary": summary,
        "audit_integrity": audit_integrity,
        "alerts": alerts,
    })


def write_monitor_snapshot(snapshot: Dict[str, Any]) -> Path:
    """Atomically persist the latest local monitor snapshot."""
    ensure_app_dirs()
    payload = redact_sensitive(dict(snapshot))
    payload.setdefault("schema_version", MONITOR_SCHEMA_VERSION)
    temporary = MONITOR_STATE_PATH.with_suffix(".tmp")
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        with _MONITOR_LOCK:
            os.replace(temporary, MONITOR_STATE_PATH)
    finally:
        if temporary.exists():
            temporary.unlink()
    return MONITOR_STATE_PATH


def read_monitor_snapshot() -> Optional[Dict[str, Any]]:
    """Read a local monitor snapshot, rejecting malformed or unsupported state."""
    try:
        with _MONITOR_LOCK:
            data = json.loads(MONITOR_STATE_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("schema_version") != MONITOR_SCHEMA_VERSION:
            return None
        return redact_sensitive(data)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError):
        return None


def export_support_bundle(target: Path) -> Path:
    """Export a bounded, local-only support bundle without workflow steps or screenshots."""
    ensure_app_dirs()
    logs = LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()[-300:] if LOG_PATH.exists() else []
    errors = []
    for path in sorted(ERROR_DIR.glob("error_*.json"), key=lambda item: item.stat().st_mtime, reverse=True)[:50]:
        try:
            errors.append({"name": path.name, "size_bytes": path.stat().st_size})
        except OSError:
            continue
    snapshot = read_monitor_snapshot() or build_monitor_snapshot()
    bundle = redact_sensitive({
        "schema_version": MONITOR_SCHEMA_VERSION,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "monitor": snapshot,
        "recent_events": read_execution_history(200),
        "recent_logs": logs,
        "error_reports": errors,
        "note": "이 패키지에는 Workflow 단계, 키 입력, 화면 캡처 원문을 포함하지 않습니다.",
    })
    destination = Path(target)
    destination.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(bundle, ensure_ascii=False, indent=2).encode("utf-8")
    if len(encoded) > 5 * 1024 * 1024:
        raise ValueError("지원 진단 패키지가 5MB 제한을 초과했습니다.")
    temporary = destination.with_suffix(".tmp")
    try:
        temporary.write_bytes(encoded)
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()
    return destination


def write_execution_report(
    run_id: str,
    event: str,
    workflow: Optional["Workflow"] = None,
    *,
    mode: str = "playback",
    start_step: int = 0,
    last_step: int = 0,
    duration_seconds: float = 0.0,
    error_type: str = "",
    policy_profile: str = "",
) -> Optional[Path]:
    """Write a bounded, privacy-conscious per-run report without workflow inputs."""
    ensure_app_dirs()
    safe_run_id = re.sub(r"[^A-Za-z0-9_-]", "", str(run_id))[:40] or "unknown"
    try:
        duration = round(max(0.0, float(duration_seconds)), 3)
    except (TypeError, ValueError):
        duration = 0.0
    payload = {
        "version": 1,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "run_id": safe_run_id,
        "event": str(event)[:80],
        "mode": str(mode)[:40],
        "workflow": workflow.name[:200] if workflow else "",
        "step_count": len(workflow.steps) if workflow else 0,
        "start_step": max(0, int(start_step or 0)),
        "last_step": max(0, int(last_step or 0)),
        "duration_seconds": duration,
        "error_type": str(error_type)[:120],
        "policy_profile": str(policy_profile)[:80],
    }
    report_path = RUN_REPORT_DIR / f"run_{safe_run_id}_{int(time.time() * 1000)}.json"
    temporary = report_path.with_suffix(".tmp")
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, report_path)
        reports = sorted(RUN_REPORT_DIR.glob("run_*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
        for stale in reports[MAX_RUN_REPORTS:]:
            try:
                stale.unlink()
            except OSError:
                pass
        return report_path
    except (OSError, TypeError, ValueError) as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        append_log(f"실행 리포트 저장 실패: {exc}", "WARNING")
        return None


def read_execution_reports(limit: int = 100) -> List[Dict[str, Any]]:
    """Read bounded local execution summaries using a strict non-sensitive field allowlist."""
    ensure_app_dirs()
    try:
        count = max(1, min(int(limit), MAX_RUN_REPORTS))
    except (TypeError, ValueError):
        count = MAX_RUN_REPORTS
    reports: List[Dict[str, Any]] = []
    fields = (
        "version", "created_at", "run_id", "event", "mode", "workflow", "step_count",
        "start_step", "last_step", "duration_seconds", "error_type", "policy_profile",
    )
    for path in sorted(RUN_REPORT_DIR.glob("run_*.json"), key=lambda item: item.stat().st_mtime, reverse=True)[:count]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("version") != 1:
                continue
            record = {field_name: data.get(field_name) for field_name in fields}
            record["report_file"] = path.name
            reports.append(redact_sensitive(record))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError):
            continue
    return reports


def _summarize_report_group(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    completed_events = {"playback_completed", "ai_plan_completed", "ai_plan_dry_run_completed"}
    failed_events = [item for item in items if str(item.get("event", "")).endswith("_failed")]
    completed = sum(1 for item in items if item.get("event") in completed_events)
    stopped = sum(1 for item in items if str(item.get("event", "")).endswith("_stopped"))
    terminal_runs = completed + len(failed_events)
    return {
        "total_reports": len(items),
        "completed_runs": completed,
        "failed_runs": len(failed_events),
        "stopped_runs": stopped,
        "terminal_runs": terminal_runs,
        "success_rate": round((completed / terminal_runs) * 100, 1) if terminal_runs else None,
    }


def summarize_execution_reports(reports: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Summarize terminal reports, policy/date trends, and bounded failure patterns."""
    items = reports if reports is not None else read_execution_reports(MAX_RUN_REPORTS)
    base = _summarize_report_group(items)
    failed_events = [item for item in items if str(item.get("event", "")).endswith("_failed")]
    error_counts = Counter(str(item.get("error_type", "")).strip() for item in failed_events if str(item.get("error_type", "")).strip())
    workflow_counts = Counter(str(item.get("workflow", "")).strip() for item in failed_events if str(item.get("workflow", "")).strip())
    policy_groups: Dict[str, List[Dict[str, Any]]] = {}
    date_groups: Dict[str, List[Dict[str, Any]]] = {}
    for item in items:
        policy = str(item.get("policy_profile", "")).strip() or "unknown"
        policy_groups.setdefault(policy, []).append(item)
        created_at = str(item.get("created_at", ""))
        date_key = created_at[:10] if re.fullmatch(r"\d{4}-\d{2}-\d{2}", created_at[:10]) else "unknown"
        date_groups.setdefault(date_key, []).append(item)
    policy_stats = []
    for policy in sorted(policy_groups):
        policy_stats.append({"policy_profile": policy, **_summarize_report_group(policy_groups[policy])})
    daily_trend = []
    for date_key in sorted(date_groups):
        daily_trend.append({"date": date_key, **_summarize_report_group(date_groups[date_key])})
    return {
        **base,
        "event_counts": dict(Counter(str(item.get("event", "unknown")) for item in items)),
        "mode_counts": dict(Counter(str(item.get("mode", "unknown")) for item in items)),
        "policy_stats": policy_stats,
        "daily_trend": daily_trend,
        "failure_patterns": [{"error_type": name, "count": count} for name, count in error_counts.most_common(10)],
        "workflow_failure_patterns": [{"workflow": name, "count": count} for name, count in workflow_counts.most_common(10)],
        "last_report_at": items[0].get("created_at") if items else None,
    }


def build_execution_report_dashboard(limit: int = 20) -> Dict[str, Any]:
    """Build a local dashboard payload containing only report summaries and recent safe fields."""
    reports = read_execution_reports(MAX_RUN_REPORTS)
    try:
        recent_limit = max(1, min(int(limit), MAX_RUN_REPORTS))
    except (TypeError, ValueError):
        recent_limit = 20
    return {
        "summary": summarize_execution_reports(reports),
        "recent_reports": reports[:recent_limit],
    }


def export_execution_report_summary(target: Path, period_days: int = 30) -> Path:
    """Export a bounded, user-initiated local report summary without inputs or workflow steps."""
    try:
        days = int(period_days)
    except (TypeError, ValueError) as exc:
        raise ValueError("리포트 기간은 1~3650일이어야 합니다.") from exc
    if not 1 <= days <= 3_650:
        raise ValueError("리포트 기간은 1~3650일이어야 합니다.")
    cutoff = time.time() - days * 86_400
    reports = []
    for report in read_execution_reports(MAX_RUN_REPORTS):
        try:
            created = time.mktime(time.strptime(str(report.get("created_at", "")), "%Y-%m-%d %H:%M:%S"))
        except (TypeError, ValueError, OverflowError):
            created = None
        if created is None or created >= cutoff:
            reports.append(report)
    payload = redact_sensitive({
        "schema_version": 1,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "period_days": days,
        "summary": summarize_execution_reports(reports),
        "reports": reports[:MAX_RUN_REPORTS],
        "note": "이 export에는 입력값, 키 입력, 좌표, 화면 캡처, 전체 Workflow 단계 원문을 포함하지 않습니다.",
    })
    encoded = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    if len(encoded) > 2 * 1024 * 1024:
        raise ValueError("실행 리포트 요약 export가 2MB 제한을 초과했습니다.")
    destination = Path(target)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".tmp")
    try:
        temporary.write_bytes(encoded)
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()
    return destination


def save_execution_checkpoint(workflow_path: Path | None, next_index: int, workflow_signature: str = "", mode: str = "playback", error_type: str = "") -> None:
    """Persist only resumable metadata; never persist workflow steps or typed input."""
    if workflow_path is None:
        return
    try:
        index = max(0, int(next_index))
    except (TypeError, ValueError):
        return
    ensure_app_dirs()
    payload = {
        "version": CHECKPOINT_VERSION,
        "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "workflow_path": str(Path(workflow_path).resolve())[:1_000],
        "workflow_signature": str(workflow_signature or "")[:64],
        "next_index": index,
        "mode": str(mode)[:40],
        "error_type": str(error_type)[:120],
    }
    temporary = CHECKPOINT_PATH.with_suffix(".tmp")
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, CHECKPOINT_PATH)
    except OSError as exc:
        append_log(f"실행 체크포인트 저장 실패: {exc}", "WARNING")
        if temporary.exists():
            temporary.unlink()


def load_execution_checkpoint() -> Optional[Dict[str, Any]]:
    """Load and validate resumable metadata, returning None for stale/corrupt state."""
    if not CHECKPOINT_PATH.exists():
        return None
    try:
        data = json.loads(CHECKPOINT_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("version") != CHECKPOINT_VERSION:
            return None
        path = Path(str(data.get("workflow_path", "")))
        next_index = int(data.get("next_index", 0))
        if not path.is_file() or not 0 <= next_index <= MAX_WORKFLOW_STEPS:
            return None
        return {
            "version": CHECKPOINT_VERSION,
            "saved_at": str(data.get("saved_at", ""))[:40],
            "workflow_path": str(path),
            "workflow_signature": str(data.get("workflow_signature", ""))[:64],
            "next_index": next_index,
            "mode": str(data.get("mode", "playback"))[:40],
            "error_type": str(data.get("error_type", ""))[:120],
        }
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        return None


def clear_execution_checkpoint() -> None:
    try:
        CHECKPOINT_PATH.unlink(missing_ok=True)
    except OSError as exc:
        append_log(f"실행 체크포인트 삭제 실패: {exc}", "WARNING")


def write_error_report(context: Dict[str, Any], exc: BaseException, capture_screen: bool = False, traceback_text: Optional[str] = None, mask_sensitive: bool = True) -> Path:
    """Write a local JSON error report and optionally a screen snapshot."""
    ensure_app_dirs()
    stamp = time.strftime("%Y%m%d_%H%M%S") + f"_{int(time.time() * 1000) % 1000:03d}"
    report_path = ERROR_DIR / f"error_{stamp}.json"
    report: Dict[str, Any] = {
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "context": redact_sensitive(context),
        "error_type": type(exc).__name__,
        "error": mask_sensitive_text(str(exc)[:4000]),
        "traceback": mask_sensitive_text((traceback_text or traceback.format_exc())[-16000:]),
    }
    if capture_screen and pyautogui is not None:
        try:
            image_path = DEBUG_DIR / f"error_{stamp}.png"
            image = pyautogui.screenshot()
            if mask_sensitive:
                image = mask_sensitive_image(image)
            image.save(image_path)
            report["screen_snapshot"] = str(image_path)
        except Exception as snapshot_exc:
            report["screen_snapshot_error"] = mask_sensitive_text(str(snapshot_exc)[:1000])
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    append_log(f"오류 보고서 생성: {report_path} · {type(exc).__name__}: {str(exc)[:500]}", "ERROR")
    return report_path


def validate_local_endpoint(endpoint: str) -> str:
    """Allow only a loopback OpenAI-compatible endpoint rooted at ``/v1``."""
    candidate = (endpoint or "").strip()
    if not candidate:
        raise ValueError("로컬 LLM 주소를 입력하세요.")
    if "://" not in candidate:
        candidate = "http://" + candidate
    parsed = urlparse(candidate)
    hostname = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or hostname not in LOCAL_HOSTS:
        raise ValueError("보안 설정상 로컬 LLM 주소만 허용됩니다. 예: http://127.0.0.1:11434/v1")
    if parsed.username or parsed.password:
        raise ValueError("로컬 LLM 주소에 사용자명·비밀번호를 넣을 수 없습니다.")
    if parsed.query or parsed.fragment:
        raise ValueError("로컬 LLM 주소에는 query string이나 fragment를 넣을 수 없습니다.")
    try:
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            raise ValueError("포트 번호는 1~65535 범위여야 합니다.")
    except ValueError as exc:
        raise ValueError("로컬 LLM 주소의 포트가 올바르지 않습니다.") from exc
    path = parsed.path.rstrip("/")
    if path not in {"", "/v1"}:
        raise ValueError("로컬 LLM 주소의 경로는 /v1만 허용됩니다.")
    netloc = parsed.netloc
    return f"{parsed.scheme}://{netloc}/v1"


def validate_timeout(value: Any) -> int:
    """Validate a bounded request timeout used for the local model."""
    try:
        timeout = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("응답 제한 시간은 정수여야 합니다.") from exc
    if not 5 <= timeout <= 600:
        raise ValueError("응답 제한 시간은 5~600초 범위여야 합니다.")
    return timeout


def validate_runtime_config(data: Any, defaults: Optional[Dict[str, Any]] = None) -> tuple[Dict[str, Any], List[str]]:
    """Return a bounded local configuration and fields that were reset after validation."""
    safe_defaults: Dict[str, Any] = {
        "endpoint": "http://127.0.0.1:11434/v1",
        "model": "gemma4:e2b",
        "timeout": 120,
        "vision": False,
        "capture_on_error": True,
        "dry_run": True,
        "schedule_interval": 3600,
        "document_roots": [],
    }
    if isinstance(defaults, dict):
        safe_defaults.update(defaults)
    raw = data if isinstance(data, dict) else {}
    result = dict(safe_defaults)
    reset_fields: List[str] = []

    try:
        result["endpoint"] = validate_local_endpoint(raw.get("endpoint", safe_defaults["endpoint"]))
    except (TypeError, ValueError):
        reset_fields.append("endpoint")
    model = raw.get("model", safe_defaults["model"])
    if isinstance(model, str) and 1 <= len(model.strip()) <= 200:
        result["model"] = model.strip()
    else:
        reset_fields.append("model")
    profile = raw.get("policy_profile", safe_defaults.get("policy_profile", "standard"))
    if isinstance(profile, str) and 1 <= len(profile.strip()) <= 80:
        result["policy_profile"] = profile.strip()
    else:
        reset_fields.append("policy_profile")
    try:
        result["timeout"] = validate_timeout(raw.get("timeout", safe_defaults["timeout"]))
    except (TypeError, ValueError):
        reset_fields.append("timeout")
    for field_name in ("vision", "capture_on_error", "dry_run"):
        value = raw.get(field_name, safe_defaults[field_name])
        if isinstance(value, bool):
            result[field_name] = value
        else:
            reset_fields.append(field_name)
    try:
        result["schedule_interval"] = validate_schedule_interval(raw.get("schedule_interval", safe_defaults["schedule_interval"]))
    except (TypeError, ValueError):
        reset_fields.append("schedule_interval")
    roots = raw.get("document_roots", safe_defaults["document_roots"])
    if isinstance(roots, list) and all(isinstance(item, str) for item in roots):
        result["document_roots"] = roots[:5]
    else:
        reset_fields.append("document_roots")
    return result, reset_fields


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
    element_role: Optional[str] = None
    element_name: Optional[str] = None
    element_control_type: Optional[str] = None


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
    uia_automation_id: Optional[str] = None
    uia_name: Optional[str] = None
    uia_control_type: Optional[str] = None
    window_title: Optional[str] = None


@dataclass
class Workflow:
    name: str = "새 작업"
    description: str = ""
    steps: List[Step] = field(default_factory=list)
    recorded_screen_size: Optional[List[int]] = None
    signature: Optional[str] = None

    def to_dict(self, include_signature: bool = True) -> Dict[str, Any]:
        payload = {
            "version": WORKFLOW_VERSION,
            "name": self.name[:200],
            "description": self.description[:2000],
            "recorded_screen_size": self.recorded_screen_size,
            "steps": [asdict(step) for step in self.steps],
        }
        if include_signature and self.signature:
            payload["signature"] = self.signature
        return payload

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Workflow":
        if not isinstance(data, dict):
            raise ValueError("작업 파일의 최상위 형식은 JSON 객체여야 합니다.")
        version = data.get("version", 1)
        if version not in {1, WORKFLOW_VERSION}:
            raise ValueError(f"지원하지 않는 작업 파일 버전입니다: {version}")
        raw_steps = data.get("steps", [])
        if not isinstance(raw_steps, list):
            raise ValueError("작업 단계는 배열이어야 합니다.")
        if len(raw_steps) > MAX_WORKFLOW_STEPS:
            raise ValueError(f"작업 단계는 최대 {MAX_WORKFLOW_STEPS:,}개까지 허용됩니다.")
        steps: List[Step] = []
        allowed_types = {"click", "key", "wait"}
        allowed_buttons = {"left", "right", "middle", "x1", "x2"}
        allowed_fields = set(Step.__dataclass_fields__)
        for index, raw in enumerate(raw_steps, start=1):
            if not isinstance(raw, dict) or raw.get("type") not in allowed_types:
                raise ValueError(f"{index}번째 단계의 동작 유형이 올바르지 않습니다.")
            unknown = set(raw) - allowed_fields
            if unknown:
                raise ValueError(f"{index}번째 단계에 허용되지 않은 필드가 있습니다: {sorted(unknown)}")
            try:
                step = Step(**{key: raw[key] for key in raw})
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{index}번째 단계의 형식이 올바르지 않습니다.") from exc
            try:
                delay = float(step.delay)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{index}번째 단계의 대기 시간이 올바르지 않습니다.") from exc
            if not math.isfinite(delay) or not 0 <= delay <= MAX_STEP_DELAY:
                raise ValueError(f"{index}번째 단계의 대기 시간은 0~60초여야 합니다.")
            if step.type == "click":
                if step.x is None or step.y is None or not (-100_000 <= int(step.x) <= 100_000) or not (-100_000 <= int(step.y) <= 100_000):
                    raise ValueError(f"{index}번째 클릭 좌표가 올바르지 않습니다.")
                if step.button not in allowed_buttons:
                    raise ValueError(f"{index}번째 마우스 버튼이 올바르지 않습니다.")
            elif step.type == "key":
                if step.event not in {"down", "up"} or step.kind not in {"char", "special"} or not step.value or len(step.value) > 32:
                    raise ValueError(f"{index}번째 키 입력 형식이 올바르지 않습니다.")
                if step.kind == "special":
                    allowed_specials = set(WorkflowPlayer.KEY_ALIASES) | {f"f{i}" for i in range(1, 13)}
                    if step.value not in allowed_specials:
                        raise ValueError(f"{index}번째 특수 키 이름이 허용되지 않습니다.")
            elif step.type == "wait":
                try:
                    if not 0 <= float(step.value or 0) <= 60:
                        raise ValueError
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"{index}번째 대기 단계의 시간이 올바르지 않습니다.") from exc
            for field_name in ("uia_automation_id", "uia_name", "uia_control_type", "window_title"):
                value = getattr(step, field_name)
                if value is not None and (not isinstance(value, str) or len(value) > 256):
                    raise ValueError(f"{index}번째 UI Automation 식별자가 올바르지 않습니다.")
            steps.append(step)
        name = data.get("name", "불러온 작업")
        description = data.get("description", "")
        recorded_screen_size = data.get("recorded_screen_size")
        signature = data.get("signature")
        if signature is not None and (not isinstance(signature, str) or not re.fullmatch(r"[0-9a-f]{64}", signature)):
            raise ValueError("작업 파일 서명이 올바르지 않습니다.")
        if not isinstance(name, str) or not isinstance(description, str):
            raise ValueError("작업 이름과 설명은 문자열이어야 합니다.")
        if recorded_screen_size is not None:
            if not isinstance(recorded_screen_size, (list, tuple)) or len(recorded_screen_size) != 2:
                raise ValueError("기록 당시 화면 크기 정보가 올바르지 않습니다.")
            try:
                recorded_screen_size = [int(recorded_screen_size[0]), int(recorded_screen_size[1])]
            except (TypeError, ValueError) as exc:
                raise ValueError("기록 당시 화면 크기 정보가 올바르지 않습니다.") from exc
            if any(value <= 0 for value in recorded_screen_size):
                raise ValueError("기록 당시 화면 크기는 양수여야 합니다.")
        return cls(
            name=name[:200] or "불러온 작업",
            description=description[:2000],
            steps=steps,
            recorded_screen_size=recorded_screen_size,
            signature=signature,
        )

    def remove_step(self, index: int) -> Step:
        """Remove and return one step using a zero-based index."""
        if not 0 <= index < len(self.steps):
            raise IndexError("삭제할 작업 단계가 없습니다.")
        self.signature = None
        return self.steps.pop(index)

    def move_step(self, index: int, offset: int) -> None:
        """Move one step by an offset, keeping the workflow order valid."""
        if not 0 <= index < len(self.steps):
            raise IndexError("이동할 작업 단계가 없습니다.")
        target = index + offset
        if not 0 <= target < len(self.steps):
            raise IndexError("작업 단계를 더 이동할 수 없습니다.")
        self.steps[index], self.steps[target] = self.steps[target], self.steps[index]
        self.signature = None

    def duplicate_step(self, index: int) -> Step:
        """Insert a copy immediately after one step and return the copy."""
        if not 0 <= index < len(self.steps):
            raise IndexError("복제할 작업 단계가 없습니다.")
        copied = replace(self.steps[index])
        self.steps.insert(index + 1, copied)
        self.signature = None
        return copied

    def update_step_delay(self, index: int, delay: float) -> None:
        """Update one step delay within the same bounds used by file validation."""
        if not 0 <= index < len(self.steps):
            raise IndexError("수정할 작업 단계가 없습니다.")
        try:
            value = float(delay)
        except (TypeError, ValueError) as exc:
            raise ValueError("대기 시간은 숫자여야 합니다.") from exc
        if not 0 <= value <= 60:
            raise ValueError("대기 시간은 0~60초 범위여야 합니다.")
        self.steps[index].delay = round(value, 3)
        self.signature = None


def get_active_window_title() -> str:
    """Return the foreground window title when Windows UI Automation is available."""
    try:
        from pywinauto import Desktop
        return str(Desktop(backend="uia").get_active().window_text() or "")[:512]
    except Exception:
        return ""


def get_uia_metadata_at_point(x: int, y: int) -> Dict[str, str]:
    """Best-effort metadata lookup for a recorded click point."""
    try:
        from pywinauto import Desktop
        active = Desktop(backend="uia").get_active()
        candidates: List[tuple[int, Dict[str, str]]] = []
        for control in active.descendants()[:200]:
            try:
                rect = control.rectangle()
                if not (rect.left <= x <= rect.right and rect.top <= y <= rect.bottom):
                    continue
                info = control.element_info
                name = str(control.window_text() or "")[:256]
                control_type = str(getattr(info, "control_type", "") or "")[:128]
                automation_id = str(getattr(info, "automation_id", "") or "")[:256]
                if automation_id or name or control_type:
                    area = max(1, int(rect.right - rect.left) * int(rect.bottom - rect.top))
                    candidates.append((area, {
                        "uia_automation_id": automation_id or None,
                        "uia_name": name or None,
                        "uia_control_type": control_type or None,
                    }))
            except Exception:
                continue
        if candidates:
            candidates.sort(key=lambda item: item[0])
            return candidates[0][1]
    except Exception:
        pass
    return {}


def _workflow_canonical_bytes(workflow: Workflow) -> bytes:
    return json.dumps(workflow.to_dict(include_signature=False), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _get_workflow_signing_key() -> bytes:
    ensure_app_dirs()
    try:
        if WORKFLOW_KEY_PATH.exists():
            key = WORKFLOW_KEY_PATH.read_bytes()
            if len(key) >= 32:
                return key
        key = secrets.token_bytes(32)
        temporary = WORKFLOW_KEY_PATH.with_suffix(".tmp")
        temporary.write_bytes(key)
        os.replace(temporary, WORKFLOW_KEY_PATH)
        try:
            os.chmod(WORKFLOW_KEY_PATH, 0o600)
        except OSError:
            pass
        return key
    except OSError as exc:
        raise RuntimeError(f"작업 서명 키를 준비할 수 없습니다: {exc}") from exc


def sign_workflow(workflow: Workflow) -> str:
    """Sign a workflow using a local key; the key never leaves this machine."""
    if not isinstance(workflow, Workflow):
        raise TypeError("Workflow 객체만 서명할 수 있습니다.")
    signature = hmac.new(_get_workflow_signing_key(), _workflow_canonical_bytes(workflow), hashlib.sha256).hexdigest()
    workflow.signature = signature
    return signature


def verify_workflow_signature(workflow: Workflow) -> bool:
    """Return whether a workflow has a valid local signature."""
    if not isinstance(workflow, Workflow) or not workflow.signature:
        return False
    try:
        expected = hmac.new(_get_workflow_signing_key(), _workflow_canonical_bytes(workflow), hashlib.sha256).hexdigest()
        return hmac.compare_digest(workflow.signature, expected)
    except (OSError, RuntimeError, TypeError):
        return False


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
            metadata = get_uia_metadata_at_point(int(x), int(y))
            window_title = get_active_window_title()
            self._append(Step(type="click", x=int(x), y=int(y), button=self._button_name(button), window_title=window_title or None, **metadata))

    def _on_press(self, key: Any) -> None:
        if not self._running:
            return
        kind, value = self._key_payload(key)
        self._append(Step(type="key", event="down", kind=kind, value=value, window_title=get_active_window_title() or None))

    def _on_release(self, key: Any) -> None:
        if not self._running:
            return
        kind, value = self._key_payload(key)
        self._append(Step(type="key", event="up", kind=kind, value=value, window_title=get_active_window_title() or None))


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
        self.pause_event = threading.Event()
        self.running = False
        self.current_index = 0
        self.current_step: Optional[Step] = None
        self.last_error: Optional[BaseException] = None
        self._pressed_keys: set[str] = set()
        self.failed: Optional[BaseException] = None
        self.failure_index = 0
        self._run_lock = threading.Lock()

    @property
    def paused(self) -> bool:
        return self.pause_event.is_set()

    def stop(self) -> None:
        self.stop_event.set()
        self.pause_event.clear()

    def pause(self) -> None:
        if self.running:
            self.pause_event.set()
            if self.on_status:
                self.on_status("재생 일시정지")

    def resume(self) -> None:
        self.pause_event.clear()
        if self.running and self.on_status:
            self.on_status("재생 재개")

    def _sleep(self, seconds: float) -> bool:
        remaining = max(0.0, min(float(seconds), 60.0))
        while remaining > 0:
            if self.stop_event.is_set():
                return False
            if self.pause_event.is_set():
                if self.stop_event.wait(0.1):
                    return False
                continue
            chunk = min(remaining, 0.1)
            started = time.monotonic()
            if self.stop_event.wait(chunk):
                return False
            if not self.pause_event.is_set():
                remaining -= min(chunk, max(0.0, time.monotonic() - started))
        return not self.stop_event.is_set()

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

    @staticmethod
    def _validate_window_context(step: Step) -> None:
        expected = (step.window_title or "").strip()
        if not expected:
            return
        current = get_active_window_title().strip()
        if not current:
            raise UserInterventionRequired("현재 활성 창을 확인할 수 없습니다. 대상 창을 확인한 뒤 다시 시도하세요.")
        expected_folded, current_folded = expected.casefold(), current.casefold()
        if expected_folded not in current_folded and current_folded not in expected_folded:
            raise UserInterventionRequired(
                f"활성 창이 기록 당시와 다릅니다. 기록: {expected[:160]} · 현재: {current[:160]}"
            )

    @staticmethod
    def _uia_click_position(step: Step) -> tuple[int, int]:
        if not any((step.uia_automation_id, step.uia_name, step.uia_control_type)):
            if step.x is None or step.y is None:
                raise RetryableAutomationError("클릭 단계에 좌표가 없습니다.")
            return int(step.x), int(step.y)
        try:
            from pywinauto import Desktop
            active = Desktop(backend="uia").get_active()
            candidates = []
            for control in active.descendants()[:300]:
                try:
                    info = control.element_info
                    automation_id = str(getattr(info, "automation_id", "") or "")
                    control_type = str(getattr(info, "control_type", "") or "")
                    name = str(control.window_text() or "")
                    if step.uia_automation_id and automation_id != step.uia_automation_id:
                        continue
                    if step.uia_control_type and control_type != step.uia_control_type:
                        continue
                    if step.uia_name and name != step.uia_name:
                        continue
                    if hasattr(control, "is_visible") and not control.is_visible():
                        continue
                    if hasattr(control, "is_enabled") and not control.is_enabled():
                        continue
                    rect = control.rectangle()
                    if rect.right <= rect.left or rect.bottom <= rect.top:
                        continue
                    candidates.append((rect, control_type, name))
                except Exception:
                    continue
            if len(candidates) == 1:
                rect = candidates[0][0]
                return int((rect.left + rect.right) / 2), int((rect.top + rect.bottom) / 2)
            if len(candidates) > 1:
                # Ambiguous matches are safer to reject than to guess.
                raise RetryableAutomationError("UI Automation 요소가 여러 개라 대상을 특정할 수 없습니다.")
        except RetryableAutomationError:
            raise
        except Exception as exc:
            raise RetryableAutomationError(f"UI Automation 요소를 찾을 수 없습니다: {exc}") from exc
        raise RetryableAutomationError("기록 당시 UI Automation 요소를 현재 화면에서 찾을 수 없습니다.")

    def play(self, workflow: Workflow, start_index: int = 0) -> None:
        if pyautogui is None:
            raise RuntimeError("pyautogui가 설치되어 있지 않습니다.")
        if not isinstance(start_index, int) or start_index < 0 or start_index > len(workflow.steps):
            raise ValueError("재생 시작 단계가 올바르지 않습니다.")
        current_screen = get_screen_size()
        valid, reason = validate_workflow(workflow, current_screen)
        if not valid:
            raise ValueError(reason)
        if not self._run_lock.acquire(blocking=False):
            raise RuntimeError("이미 다른 작업을 재생 중입니다.")
        self.stop_event.clear()
        self.pause_event.clear()
        self.failed = None
        self.failure_index = 0
        self.last_error = None
        self.running = True
        try:
            pyautogui.PAUSE = 0.03
            pyautogui.FAILSAFE = True
            total = len(workflow.steps)
            for index, step in enumerate(workflow.steps[start_index:], start=start_index + 1):
                self.current_index = index
                self.current_step = step
                if not self._sleep(step.delay) or self.stop_event.is_set():
                    break
                self._validate_window_context(step)
                if self.on_step:
                    self.on_step(index, step)
                if step.type == "click":
                    x, y = self._uia_click_position(step)
                    button = step.button if step.button in self.BUTTONS else "left"
                    pyautogui.click(x, y, button=button)
                elif step.type == "key":
                    self._play_key(step)
                elif step.type == "wait":
                    self._sleep(float(step.value or 0.5))
                if self.on_status:
                    self.on_status(f"재생 중: {index}/{total}")
        except Exception as exc:
            self.failed = exc
            self.failure_index = self.current_index
            self.last_error = exc
            if self.on_error and self.current_step is not None:
                self.on_error(exc, self.current_index, self.current_step)
            else:
                raise
        finally:
            self._release_pressed_keys()
            self.running = False
            if self.on_status:
                self.on_status("재생 종료")
            self._run_lock.release()


def validate_workflow(workflow: Workflow, screen_size: tuple[int, int] | None = None) -> tuple[bool, str]:
    """Validate persisted or recorded workflows before OS-level input is sent."""
    if not isinstance(workflow, Workflow):
        return False, "유효하지 않은 작업 객체입니다."
    if len(workflow.steps) > MAX_WORKFLOW_STEPS:
        return False, f"작업 단계는 {MAX_WORKFLOW_STEPS:,}개를 초과할 수 없습니다."
    width, height = 100000, 100000
    if screen_size is not None:
        try:
            width, height = int(screen_size[0]), int(screen_size[1])
        except (TypeError, ValueError, IndexError):
            return False, "화면 크기 형식이 유효하지 않습니다."
        if width <= 0 or height <= 0:
            return False, "화면 크기는 양수여야 합니다."
    for index, step in enumerate(workflow.steps, start=1):
        if step.type not in {"click", "key", "wait"}:
            return False, f"{index}번째 단계의 동작이 허용되지 않습니다: {step.type!r}"
        try:
            delay = float(step.delay)
        except (TypeError, ValueError):
            return False, f"{index}번째 단계의 지연시간이 숫자가 아닙니다."
        if not math.isfinite(delay) or not 0 <= delay <= MAX_STEP_DELAY:
            return False, f"{index}번째 단계의 지연시간이 허용 범위를 벗어났습니다."
        if step.type == "click":
            if step.x is None or step.y is None:
                return False, f"{index}번째 클릭 단계에 좌표가 없습니다."
            try:
                x, y = int(step.x), int(step.y)
            except (TypeError, ValueError):
                return False, f"{index}번째 클릭 단계의 좌표가 숫자가 아닙니다."
            if not (0 <= x < width and 0 <= y < height):
                return False, f"{index}번째 클릭 좌표가 화면 밖입니다."
            if step.button not in WorkflowPlayer.BUTTONS:
                return False, f"{index}번째 클릭의 마우스 버튼이 허용되지 않습니다."
        elif step.type == "key":
            if step.event not in {"down", "up"} or step.kind not in {"char", "special"}:
                return False, f"{index}번째 키 단계의 event/kind가 유효하지 않습니다."
            value = str(step.value or "")
            if step.kind == "char" and len(value) != 1:
                return False, f"{index}번째 문자 키의 길이가 유효하지 않습니다."
            if step.kind == "special":
                allowed_specials = set(WorkflowPlayer.KEY_ALIASES) | {f"f{i}" for i in range(1, 13)}
                if value not in allowed_specials:
                    return False, f"{index}번째 특수 키 이름이 허용되지 않습니다: {value!r}"
        elif step.type == "wait":
            try:
                seconds = float(step.value or 0.5)
            except (TypeError, ValueError):
                return False, f"{index}번째 대기 시간이 숫자가 아닙니다."
            if not math.isfinite(seconds) or not 0 <= seconds <= 60:
                return False, f"{index}번째 대기 시간이 허용 범위를 벗어났습니다."
    return True, "검증 완료"


def resolve_observed_element(action: UIAction, observation: Dict[str, Any]) -> Optional[ObservedElement]:
    """Resolve an AI action to one current element, allowing safe semantic re-binding."""
    try:
        elements = [ObservedElement.model_validate(item) for item in observation.get("elements", [])]
    except ValidationError:
        return None
    exact = [element for element in elements if element.id == action.element_id]
    if len(exact) == 1:
        return exact[0]
    candidates = elements
    if action.element_role:
        candidates = [element for element in candidates if element.role.casefold() == action.element_role.strip().casefold()]
    if action.element_control_type:
        candidates = [element for element in candidates if (element.control_type or "").casefold() == action.element_control_type.strip().casefold()]
    if action.element_name:
        target = action.element_name.strip().casefold()
        candidates = [element for element in candidates if element.name.strip().casefold() == target]
    candidates = [element for element in candidates if element.visible and element.enabled]
    return candidates[0] if len(candidates) == 1 else None


def inspect_workflow(workflow: Workflow, screen_size: tuple[int, int] | None = None) -> Dict[str, Any]:
    """Return a non-destructive safety and readiness report for a workflow."""
    valid, reason = validate_workflow(workflow, screen_size)
    steps = list(workflow.steps) if isinstance(workflow, Workflow) else []
    current_size = tuple(int(value) for value in screen_size) if screen_size else None
    recorded_size = tuple(workflow.recorded_screen_size) if isinstance(workflow, Workflow) and workflow.recorded_screen_size else None
    warnings: List[str] = []
    if recorded_size and current_size and recorded_size != current_size:
        warnings.append(f"기록 화면 {recorded_size[0]}×{recorded_size[1]}와 현재 화면 {current_size[0]}×{current_size[1]}이 다릅니다.")
    window_titles = list(dict.fromkeys(step.window_title.strip() for step in steps if (step.window_title or "").strip()))
    return {
        "valid": valid,
        "reason": reason,
        "step_count": len(steps),
        "click_count": sum(step.type == "click" for step in steps),
        "key_count": sum(step.type == "key" for step in steps),
        "wait_count": sum(step.type == "wait" for step in steps),
        "window_titles": window_titles,
        "warnings": warnings,
    }


def cleanup_expired_artifacts(max_age_days: int = MAX_CAPTURE_AGE_DAYS) -> None:
    """Remove old screenshots and error reports while keeping workflow files intact."""
    ensure_app_dirs()
    cutoff = time.time() - max(1, int(max_age_days)) * 86400
    for directory in (CACHE_DIR, ERROR_DIR, DEBUG_DIR):
        for path in directory.glob("*"):
            try:
                if path.is_file() and path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError:
                continue
    cleanup_workflow_backups()


def ensure_app_dirs() -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    WORKFLOW_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    ERROR_DIR.mkdir(parents=True, exist_ok=True)
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    TEMPLATE_DIR.mkdir(parents=True, exist_ok=True)
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    RUN_REPORT_DIR.mkdir(parents=True, exist_ok=True)


def get_screen_size() -> Optional[tuple[int, int]]:
    """Return the current primary screen size when the GUI backend is available."""
    if pyautogui is None:
        return None
    try:
        width, height = pyautogui.size()
        width, height = int(width), int(height)
    except (AttributeError, TypeError, ValueError, OSError):
        return None
    return (width, height) if width > 0 and height > 0 else None


def capture_observation() -> Dict[str, Any]:
    """Capture the screen and best-effort active-window/OCR context."""
    ensure_app_dirs()
    if pyautogui is None:
        raise RuntimeError("pyautogui가 설치되어 있지 않습니다.")
    image_path = CACHE_DIR / f"observation_{time.time_ns()}.png"
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
        ocr_text = mask_sensitive_text(pytesseract.image_to_string(image, lang="kor+eng"))
    except Exception:
        ocr_text = "OCR을 사용할 수 없습니다. Windows에 Tesseract와 kor+eng 언어 데이터를 설치하면 화면 문자를 읽을 수 있습니다."

    try:
        from adapters import build_adapter_context, build_read_only_adapter_validation
    except ImportError:  # package import path for tests and embedded use
        from app.adapters import build_adapter_context, build_read_only_adapter_validation
    application_context = build_adapter_context(active_window, ocr_text)
    adapter_validation = build_read_only_adapter_validation(active_window, ocr_text)

    return {
        "image_path": str(image_path),
        "screen_size": list(image.size),
        "active_window": active_window,
        "application_context": application_context,
        "adapter_validation": adapter_validation,
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
        self.model = (model or "").strip()
        if not self.model or len(self.model) > 200:
            raise ValueError("로컬 AI 모델 이름을 입력하세요(최대 200자).")
        self.timeout = validate_timeout(timeout)
        self.vision = vision

    def health_check(self) -> Dict[str, Any]:
        """Check the local OpenAI-compatible server without sending user data."""
        import requests

        try:
            response = requests.get(f"{self.endpoint}/models", timeout=min(self.timeout, 5))
            response.raise_for_status()
            payload = response.json()
            models = payload.get("data", []) if isinstance(payload, dict) else []
            model_names = [str(item.get("id", "")) for item in models if isinstance(item, dict) and item.get("id")]
            return {"reachable": True, "status_code": response.status_code, "models": model_names[:50]}
        except requests.RequestException as exc:
            raise RuntimeError(f"로컬 AI 서버에 연결할 수 없습니다: {exc}") from exc
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"로컬 AI 서버 응답 형식이 올바르지 않습니다: {exc}") from exc

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

    def make_recovery_plan(self, failure_context: Dict[str, Any], observation: Dict[str, Any]) -> Dict[str, Any]:
        """Generate a conservative recovery proposal; never execute it automatically."""
        enriched = dict(observation)
        enriched["recovery_context"] = redact_sensitive(failure_context)
        return self.make_plan("실패 원인을 분석하고, 위험 동작 없이 사용자가 검토할 복구 계획을 제안해 줘", enriched)

    def make_plan(self, goal: str, observation: Dict[str, Any]) -> Dict[str, Any]:
        import requests

        goal = (goal or "").strip()
        if not goal:
            raise ValueError("업무 목표를 입력하세요.")
        if len(goal) > 4000:
            raise ValueError("업무 목표는 최대 4,000자까지 입력할 수 있습니다.")
        try:
            from policies import profile_prompt_rules
        except ImportError:
            from app.policies import profile_prompt_rules
        policy_profile = str(observation.get("policy_profile", "standard"))
        system = (
            "당신은 Windows 데스크톱 업무 자동화의 계획기입니다. "
            + profile_prompt_rules(policy_profile) + " "
            "반드시 관찰 JSON의 elements에 실제로 존재하는 element_id를 우선 사용하십시오. "
            "각 UI 동작에는 가능하면 element_role, element_name, element_control_type도 함께 복사하십시오. "
            "실행 직전 화면이 갱신될 수 있으므로 element_id가 바뀌더라도 세 의미 단서로 하나의 요소만 특정될 때만 안전하게 재바인딩합니다. "
            "좌표를 생성하거나 추측하지 말고, 식별 단서가 없거나 확신이 낮으면 action=none을 반환하십시오. "
            "허용 action은 click, double_click, type, hotkey, scroll, wait, none뿐입니다. "
            "파일 삭제, 명령 셸, 결제, 전송, 게시, 로그인 정보 입력, 결재는 계획하지 마십시오. "
            "제출·삭제·저장 덮어쓰기·권한 변경처럼 위험한 동작은 requires_confirmation=true로 설정하십시오. "
            "복구 계획 요청에서는 원인 분석과 재관찰·대기·읽기 동작을 우선하고, 파일 삭제·전송·게시·민감정보 입력은 절대 제안하지 마십시오. "
            "반드시 JSON Schema에 맞는 계획 하나만 반환하십시오."
        )
        user_text = {
            "goal": goal,
            "active_window": observation.get("active_window", ""),
            "screen_size": observation.get("screen_size", []),
            "ocr_text": observation.get("ocr_text", ""),
            "application_context": observation.get("application_context", {}),
            "policy_profile": policy_profile,
            "document_context": observation.get("document_context", {}),
            "recovery_context": observation.get("recovery_context", {}),
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
        try:
            response = requests.post(f"{self.endpoint}/chat/completions", json=payload, timeout=self.timeout)
            if response.status_code == 400:
                # 일부 로컬 서버는 response_format을 아직 지원하지 않으므로 한 번만 호환 모드로 재시도합니다.
                payload.pop("response_format", None)
                response = requests.post(f"{self.endpoint}/chat/completions", json=payload, timeout=self.timeout)
            response.raise_for_status()
        except requests.RequestException as exc:
            detail = getattr(getattr(exc, "response", None), "text", "")[:500]
            raise RuntimeError(f"로컬 AI 요청에 실패했습니다: {exc}{(' · ' + detail) if detail else ''}") from exc
        raw = self._parse_json(self._extract_content(response.json()))
        try:
            return AutomationPlan.model_validate(raw).model_dump(mode="json")
        except ValidationError as exc:
            raise RetryableAutomationError(f"Gemma 구조화 계획 검증 실패: {exc}") from exc


def cleanup_workflow_backups(max_backups: int = MAX_WORKFLOW_BACKUPS) -> None:
    """Keep the newest workflow backups while preserving the active workflow files."""
    ensure_app_dirs()
    try:
        limit = max(1, int(max_backups))
        backups = sorted((path for path in BACKUP_DIR.glob("*.json") if path.is_file()), key=lambda path: path.stat().st_mtime, reverse=True)
        for path in backups[limit:]:
            try:
                path.unlink()
            except OSError:
                continue
    except (OSError, ValueError):
        return


def backup_workflow(path: Path) -> Optional[Path]:
    """Create a timestamped local backup before an existing workflow is overwritten."""
    source = Path(path)
    if not source.exists() or not source.is_file():
        return None
    ensure_app_dirs()
    stamp = time.strftime("%Y%m%d_%H%M%S") + f"_{time.time_ns() % 1_000_000_000:09d}"
    digest = hashlib.sha256(str(source.resolve()).encode("utf-8")).hexdigest()[:10]
    target = BACKUP_DIR / f"{source.stem}_{stamp}_{digest}.json"
    shutil.copy2(source, target)
    cleanup_workflow_backups()
    return target


def list_workflow_backups(workflow_path: Path | None = None) -> List[Path]:
    """List newest local workflow backups, optionally filtered by original stem."""
    ensure_app_dirs()
    stem = Path(workflow_path).stem if workflow_path else None
    paths = [path for path in BACKUP_DIR.glob("*.json") if path.is_file() and (stem is None or path.name.startswith(f"{stem}_"))]
    return sorted(paths, key=lambda path: path.stat().st_mtime, reverse=True)


def save_workflow(workflow: Workflow, path: Path) -> Optional[Path]:
    ensure_app_dirs()
    if not isinstance(workflow, Workflow):
        raise TypeError("Workflow 객체만 저장할 수 있습니다.")
    valid, reason = validate_workflow(workflow)
    if not valid:
        raise ValueError(reason)
    sign_workflow(workflow)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    backup_path = backup_workflow(path)
    payload = json.dumps(workflow.to_dict(), ensure_ascii=False, indent=2)
    if len(payload.encode("utf-8")) > MAX_WORKFLOW_FILE_BYTES:
        raise ValueError("작업 파일이 너무 큽니다.")
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        temporary.write_text(payload, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return backup_path


PROMPT_TEMPLATE_VERSION = 1
MAX_PROMPT_TEMPLATE_LENGTH = 4_000
MAX_PROMPT_TEMPLATE_VERSIONS = 20


def _prompt_template_fingerprint(data: Dict[str, Any]) -> str:
    """Hash only normalized template semantics, excluding timestamps and revision metadata."""
    normalized = {
        "version": PROMPT_TEMPLATE_VERSION,
        "name": str(data.get("name", ""))[:200],
        "goal_template": str(data.get("goal_template", ""))[:MAX_PROMPT_TEMPLATE_LENGTH],
        "placeholders": list(data.get("placeholders", []))[:30],
        "policy_profile": str(data.get("policy_profile", "standard"))[:80],
        "document_roots": list(data.get("document_roots", []))[:5],
    }
    return hashlib.sha256(json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _prompt_template_history_dir(path: Path) -> Path:
    target = Path(path)
    return target.parent / f".{target.stem}.versions"


def _prompt_template_revision(data: Dict[str, Any]) -> int:
    try:
        return max(1, int(data.get("revision", 1) or 1))
    except (TypeError, ValueError):
        return 1


def save_prompt_template(path: Path, name: str, goal_template: str, policy_profile: str = "standard", document_roots: list[str] | None = None) -> Dict[str, Any]:
    """Persist a versioned natural-language task template locally and atomically."""
    title = (name or "자연어 작업 템플릿").strip()[:200]
    template = (goal_template or "").strip()
    if not template or len(template) > MAX_PROMPT_TEMPLATE_LENGTH:
        raise ValueError(f"자연어 템플릿은 1~{MAX_PROMPT_TEMPLATE_LENGTH:,}자여야 합니다.")
    placeholders = sorted(set(re.findall(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}", template)))[:30]
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload: Dict[str, Any] = {
        "version": PROMPT_TEMPLATE_VERSION,
        "name": title,
        "goal_template": template,
        "placeholders": placeholders,
        "policy_profile": policy_profile or "standard",
        "document_roots": [str(item)[:500] for item in (document_roots or [])[:5]],
    }
    payload["fingerprint"] = _prompt_template_fingerprint(payload)
    previous: Optional[Dict[str, Any]] = None
    if target.exists():
        try:
            previous = load_prompt_template(target)
        except (OSError, ValueError):
            previous = None
    if previous and previous.get("fingerprint") == payload["fingerprint"]:
        payload["revision"] = _prompt_template_revision(previous)
    else:
        previous_revision = _prompt_template_revision(previous or {})
        payload["revision"] = previous_revision + 1 if previous else 1
        if previous:
            history_dir = _prompt_template_history_dir(target)
            history_dir.mkdir(parents=True, exist_ok=True)
            archive = history_dir / f"v{previous_revision:04d}.json"
            if not archive.exists():
                archive.write_text(json.dumps(previous, ensure_ascii=False, indent=2), encoding="utf-8")
            archives = sorted(history_dir.glob("v[0-9][0-9][0-9][0-9].json"), key=lambda item: item.name, reverse=True)
            for stale in archives[MAX_PROMPT_TEMPLATE_VERSIONS:]:
                try:
                    stale.unlink()
                except OSError:
                    pass
    payload["saved_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    temporary = target.with_name(f".{target.name}.tmp")
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()
    return payload


def load_prompt_template(path: Path) -> Dict[str, Any]:
    """Load and validate a natural-language task template."""
    target = Path(path)
    if target.stat().st_size > 256 * 1024:
        raise ValueError("자연어 템플릿 파일이 너무 큽니다.")
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"자연어 템플릿을 읽을 수 없습니다: {exc}") from exc
    if not isinstance(data, dict) or data.get("version") != PROMPT_TEMPLATE_VERSION:
        raise ValueError("지원하지 않는 자연어 템플릿 버전입니다.")
    template = str(data.get("goal_template", "")).strip()
    if not template or len(template) > MAX_PROMPT_TEMPLATE_LENGTH:
        raise ValueError("자연어 템플릿 내용이 올바르지 않습니다.")
    data["name"] = str(data.get("name", "자연어 작업 템플릿"))[:200]
    data["goal_template"] = template
    data["placeholders"] = sorted(set(re.findall(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}", template)))[:30]
    data["policy_profile"] = str(data.get("policy_profile", "standard"))[:80]
    roots = data.get("document_roots", [])
    data["document_roots"] = [str(item)[:500] for item in roots[:5]] if isinstance(roots, list) else []
    computed_fingerprint = _prompt_template_fingerprint(data)
    stored_fingerprint = str(data.get("fingerprint", ""))
    if stored_fingerprint and not hmac.compare_digest(stored_fingerprint, computed_fingerprint):
        raise ValueError("자연어 템플릿 fingerprint가 일치하지 않습니다. 파일이 변경되었을 수 있습니다.")
    data["fingerprint"] = computed_fingerprint
    data["revision"] = _prompt_template_revision(data)
    return data


def _prompt_template_summary(data: Dict[str, Any], is_current: bool = False) -> Dict[str, Any]:
    return {
        "revision": _prompt_template_revision(data),
        "name": str(data.get("name", ""))[:200],
        "policy_profile": str(data.get("policy_profile", "standard"))[:80],
        "placeholder_count": len(data.get("placeholders", [])),
        "fingerprint": str(data.get("fingerprint", ""))[:64],
        "is_current": bool(is_current),
    }


def list_prompt_template_versions(path: Path) -> list[Dict[str, Any]]:
    """List local template revision metadata without returning goal text or document roots."""
    target = Path(path)
    current = load_prompt_template(target)
    versions = [_prompt_template_summary(current, is_current=True)]
    history_dir = _prompt_template_history_dir(target)
    if history_dir.exists():
        for archive in sorted(history_dir.glob("v[0-9][0-9][0-9][0-9].json"), reverse=True):
            try:
                versions.append(_prompt_template_summary(load_prompt_template(archive)))
            except (OSError, ValueError):
                continue
    return versions[:MAX_PROMPT_TEMPLATE_VERSIONS + 1]


def load_prompt_template_version(path: Path, revision: int) -> Dict[str, Any]:
    """Load one local template revision, failing closed when the revision is absent."""
    target = Path(path)
    current = load_prompt_template(target)
    requested = int(revision)
    if requested == _prompt_template_revision(current):
        return current
    archive = _prompt_template_history_dir(target) / f"v{requested:04d}.json"
    if not archive.is_file():
        raise ValueError(f"템플릿 버전 v{requested}을 찾을 수 없습니다.")
    return load_prompt_template(archive)


def _compare_prompt_template_data(left: Dict[str, Any], right: Dict[str, Any]) -> Dict[str, Any]:
    changed = []
    for field_name in ("name", "goal_template", "placeholders", "policy_profile", "document_roots"):
        if left.get(field_name) != right.get(field_name):
            changed.append(field_name)
    return {
        "same": not changed,
        "changed_fields": changed,
        "left": _prompt_template_summary(left),
        "right": _prompt_template_summary(right),
    }


def compare_prompt_templates(left_path: Path, right_path: Path) -> Dict[str, Any]:
    """Compare two local templates by metadata and semantic fields without emitting their text."""
    return _compare_prompt_template_data(load_prompt_template(Path(left_path)), load_prompt_template(Path(right_path)))


def compare_prompt_template_versions(path: Path, left_revision: int, right_revision: int) -> Dict[str, Any]:
    """Compare two revisions of one local template without returning goal text."""
    left = load_prompt_template_version(path, left_revision)
    right = load_prompt_template_version(path, right_revision)
    return _compare_prompt_template_data(left, right)


def render_prompt_template(template: str, values: Dict[str, Any]) -> str:
    """Render explicit double-brace variables and fail closed on missing values."""
    text = str(template or "")
    names = sorted(set(re.findall(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}", text)))
    for name in names:
        if name not in values or not str(values[name]).strip():
            raise ValueError(f"템플릿 변수 {name} 값이 없습니다.")
        text = re.sub(r"\{\{\s*" + re.escape(name) + r"\s*\}\}", str(values[name]).strip(), text)
    if len(text) > MAX_PROMPT_TEMPLATE_LENGTH:
        raise ValueError("렌더링된 자연어 템플릿이 너무 깁니다.")
    return text


def load_workflow(path: Path, require_signature: bool = False) -> Workflow:
    path = Path(path)
    if path.stat().st_size > MAX_WORKFLOW_FILE_BYTES:
        raise ValueError("작업 파일이 너무 큽니다.")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"작업 파일을 읽을 수 없습니다: {exc}") from exc
    workflow = Workflow.from_dict(data)
    if workflow.signature:
        if not verify_workflow_signature(workflow):
            raise ValueError("작업 파일 서명이 일치하지 않습니다. 파일이 변조되었을 수 있습니다.")
    elif require_signature:
        raise ValueError("서명되지 않은 작업 파일은 현재 보안 설정에서 허용되지 않습니다.")
    return workflow


def validate_ai_steps(
    plan: Dict[str, Any],
    screen_size: tuple[int, int] | None = None,
    observation: Dict[str, Any] | None = None,
    policy_profile: str = "standard",
) -> tuple[bool, str]:
    """Validate a plan against the current screen elements; direct coordinates are rejected."""
    try:
        parsed = AutomationPlan.model_validate(plan)
    except ValidationError as exc:
        return False, f"구조화 계획 형식 오류: {exc.errors()[0].get('msg', str(exc))}"
    try:
        from policies import validate_plan_policy
    except ImportError:
        from app.policies import validate_plan_policy
    policy_valid, policy_reason = validate_plan_policy(plan, policy_profile)
    if not policy_valid:
        return False, policy_reason
    if observation is None:
        return False, "현재 화면의 element_id 정보가 없어 실행할 수 없습니다. 화면을 다시 관찰하세요."
    try:
        elements = [ObservedElement.model_validate(item) for item in observation.get("elements", [])]
    except ValidationError as exc:
        return False, f"화면 요소 형식 오류: {exc.errors()[0].get('msg', str(exc))}"
    element_map = {element.id: element for element in elements}
    if len(element_map) != len(elements):
        return False, "현재 화면 요소에 중복된 element_id가 있습니다."
    raw_size = screen_size or observation.get("screen_size", [])
    if not isinstance(raw_size, (list, tuple)) or len(raw_size) != 2:
        return False, "화면 크기 정보가 올바르지 않습니다. 화면을 다시 관찰하세요."
    try:
        width, height = int(raw_size[0]), int(raw_size[1])
    except (TypeError, ValueError):
        return False, "화면 크기 정보가 올바르지 않습니다. 화면을 다시 관찰하세요."
    if width <= 0 or height <= 0:
        return False, "화면 크기가 올바르지 않습니다. 화면을 다시 관찰하세요."
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
            element = resolve_observed_element(action, observation)
            if element is None:
                return False, f"{index}번째 단계의 UI 요소가 현재 화면에 없습니다 또는 하나로 특정할 수 없습니다."
            if element.id != action.element_id:
                append_log(f"AI 요소 의미 기반 재바인딩: {action.element_id} -> {element.id}", "WARNING")
            if not element.visible or not element.enabled:
                return False, f"{index}번째 단계의 UI 요소가 보이지 않거나 비활성 상태입니다."
            if len(element.bbox) != 4 or element.bbox[2] <= element.bbox[0] or element.bbox[3] <= element.bbox[1]:
                return False, f"{index}번째 단계의 UI 요소 영역이 유효하지 않습니다."
            if not (0 <= element.bbox[0] < width and 0 < element.bbox[2] <= width and 0 <= element.bbox[1] < height and 0 < element.bbox[3] <= height):
                return False, f"{index}번째 UI 요소가 화면 밖입니다."
        if action.action == "type":
            if not action.text:
                return False, f"{index}번째 입력값이 없습니다."
            if len(action.text) > MAX_TEXT_INPUT_LENGTH:
                return False, f"{index}번째 입력값이 너무 깁니다(최대 {MAX_TEXT_INPUT_LENGTH:,}자)."
            if action.risk == "read" or not action.requires_confirmation:
                return False, f"{index}번째 입력은 쓰기 동작이므로 사용자 확인이 필요합니다."
            element_label = " ".join([element.name, element.control_type or ""]).lower()
            sensitive_terms = ("password", "passwd", "비밀번호", "주민번호", "주민등록", "ssn", "secret", "token")
            if any(term in element_label for term in sensitive_terms):
                return False, f"{index}번째 입력 대상이 민감정보 필드로 보입니다. 자동 입력하지 않습니다."
        if action.action == "hotkey":
            keys = tuple(key.lower() for key in action.keys)
            if not 1 <= len(keys) <= 4 or any(key not in safe_hotkeys and not re.fullmatch(r"f([1-9]|1[0-2])", key) for key in keys):
                return False, f"{index}번째 단축키가 허용되지 않습니다."
            if set(keys) in ({"alt", "f4"}, {"ctrl", "shift", "delete"}, {"ctrl", "alt", "delete"}, {"ctrl", "shift", "esc"}):
                return False, f"{index}번째 위험 단축키는 자동 실행하지 않습니다."
            if any(key in {"delete", "enter"} for key in keys) and (action.risk == "read" or not action.requires_confirmation):
                return False, f"{index}번째 단축키는 쓰기·제출 동작일 수 있어 사용자 확인이 필요합니다."
        if action.action == "scroll" and action.amount == 0:
            return False, f"{index}번째 스크롤 양이 없습니다."
        if action.action == "wait" and not 0 <= action.seconds <= 60:
            return False, f"{index}번째 대기 시간이 허용 범위를 벗어났습니다."
        if action.risk in {"submit", "delete", "unknown"} and not action.requires_confirmation:
            return False, f"{index}번째 위험 동작은 사용자 확인이 필요합니다."
    return True, "검증 완료"
