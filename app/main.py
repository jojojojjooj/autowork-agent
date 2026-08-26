from __future__ import annotations

import json
import os
import secrets
import threading
import time
import tkinter as tk
import traceback
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import Any, Dict, Optional

from engine import (
    APP_DIR,
    BACKUP_DIR,
    CHECKPOINT_PATH,
    CONFIG_PATH,
    MAX_CONFIG_FILE_BYTES,
    HISTORY_PATH,
    DEBUG_DIR,
    ERROR_DIR,
    TEMPLATE_DIR,
    LOG_PATH,
    WORKFLOW_DIR,
    InputRecorder,
    LocalAIClient,
    LocalScheduler,
    RetryableAutomationError,
    UserInterventionRequired,
    Step,
    Workflow,
    get_screen_size,
    WorkflowPlayer,
    append_log,
    append_execution_history,
    build_execution_report_dashboard,
    export_execution_report_summary,
    compare_prompt_templates,
    list_prompt_template_versions,
    write_execution_report,
    read_execution_history,
    summarize_execution_history,
    build_monitor_snapshot,
    write_monitor_snapshot,
    read_monitor_snapshot,
    export_support_bundle,
    capture_observation,
    cleanup_expired_artifacts,
    ensure_app_dirs,
    redact_sensitive,
    inspect_workflow,
    validate_workflow,
    verify_workflow_signature,
    verify_execution_history,
    evaluate_scheduler_health,
    write_error_report,
    load_workflow,
    save_workflow,
    save_execution_checkpoint,
    load_execution_checkpoint,
    clear_execution_checkpoint,
    validate_ai_steps,
    validate_local_endpoint,
    validate_timeout,
    validate_runtime_config,
    validate_schedule_interval,
    save_prompt_template,
    load_prompt_template,
    render_prompt_template,
)
try:
    from adapters import build_document_context, normalize_document_roots
except ImportError:
    from app.adapters import build_document_context, normalize_document_roots
try:
    from policies import POLICY_PROFILES, DEFAULT_POLICY_PROFILE, get_policy_profile, review_plan
except ImportError:
    from app.policies import POLICY_PROFILES, DEFAULT_POLICY_PROFILE, get_policy_profile, review_plan

try:
    import pyautogui
except Exception:
    pyautogui = None

try:
    import pyperclip
except Exception:
    pyperclip = None

try:
    from pynput import keyboard as pynput_keyboard
except Exception:
    pynput_keyboard = None


class AutoWorkAgent(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        ensure_app_dirs()
        cleanup_expired_artifacts()
        self._pending_checkpoint = load_execution_checkpoint()
        self.title("AutoWork Agent · 로컬 데스크톱 자동화")
        self.geometry("1060x760")
        self.minsize(900, 620)
        self.configure(bg="#f4f6f8")

        self.workflow = Workflow()
        self.current_path: Optional[Path] = None
        self.last_observation: Optional[Dict[str, Any]] = None
        self.last_plan: Optional[Dict[str, Any]] = None
        self.recording = False
        self.hotkey_listener = None
        self.ai_stop_event = threading.Event()
        self.ai_running = False
        self._ai_job_lock = threading.Lock()
        self._playback_launch_lock = threading.Lock()
        self._step_gate = threading.Event()
        self.step_mode_enabled = False
        self.last_failed_step_index: Optional[int] = None
        self.last_ai_index = 0
        self.last_ai_run_id = ""
        self.dry_run_enabled = True
        self.capture_on_error_enabled = True

        self.recorder = InputRecorder(on_step=self._on_recorded_step, on_error=self._on_recorder_error)
        self.player = WorkflowPlayer(on_step=self._on_play_step, on_status=self._set_status, on_error=self._on_player_error)
        self.scheduler = LocalScheduler(self._scheduled_run)
        self._scheduler_circuit_override = False

        self._load_config()
        self._build_style()
        self._build_header()
        self._build_notebook()
        self._start_global_hotkey()
        self._set_status("대기 중 · 기록 또는 AI 보조 작업을 선택하세요.")
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_style(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure("Title.TLabel", font=("Segoe UI", 18, "bold"), foreground="#14213d")
        style.configure("Sub.TLabel", font=("Segoe UI", 9), foreground="#5c677d")
        style.configure("Primary.TButton", font=("Segoe UI", 10, "bold"))
        style.configure("Danger.TButton", foreground="#a61b1b")
        style.configure("Card.TLabelframe", padding=12)
        style.configure("Card.TLabelframe.Label", font=("Segoe UI", 10, "bold"), foreground="#14213d")

    def _build_header(self) -> None:
        header = ttk.Frame(self, padding=(18, 14, 18, 8))
        header.pack(fill="x")
        ttk.Label(header, text="AutoWork Agent", style="Title.TLabel").pack(side="left")
        ttk.Label(header, text="기록·재생 + 화면 기반 로컬 AI 업무 보조", style="Sub.TLabel").pack(side="left", padx=(14, 0), pady=(7, 0))
        self.status_var = tk.StringVar(value="대기 중")
        ttk.Label(header, textvariable=self.status_var, style="Sub.TLabel").pack(side="right", pady=(7, 0))

    def _build_notebook(self) -> None:
        notebook = ttk.Notebook(self)
        notebook.pack(fill="both", expand=True, padx=16, pady=(0, 16))
        self.record_tab = ttk.Frame(notebook, padding=12)
        self.ai_tab = ttk.Frame(notebook, padding=12)
        self.settings_tab = ttk.Frame(notebook, padding=12)
        self.debug_tab = ttk.Frame(notebook, padding=12)
        notebook.add(self.record_tab, text="기록·재생")
        notebook.add(self.ai_tab, text="AI 업무 보조")
        notebook.add(self.settings_tab, text="설정")
        notebook.add(self.debug_tab, text="진단·로그")
        self._build_record_tab()
        self._build_ai_tab()
        self._build_settings_tab()
        self._build_debug_tab()

    def _build_record_tab(self) -> None:
        top = ttk.Frame(self.record_tab)
        top.pack(fill="x", pady=(0, 10))
        self.record_button = ttk.Button(top, text="● 기록 시작", command=self._start_recording, style="Primary.TButton")
        self.record_button.pack(side="left")
        self.stop_record_button = ttk.Button(top, text="■ 기록 중지", command=self._stop_recording, state="disabled")
        self.stop_record_button.pack(side="left", padx=6)
        ttk.Button(top, text="▶ 재생", command=self._play_workflow, style="Primary.TButton").pack(side="left", padx=(18, 6))
        self.pause_play_button = ttk.Button(top, text="재생 일시정지", command=self._toggle_pause_playback, state="disabled")
        self.pause_play_button.pack(side="left", padx=6)
        ttk.Button(top, text="재생 중지", command=self._stop_playback).pack(side="left")
        ttk.Button(top, text="안전 점검", command=self._inspect_current_workflow).pack(side="left", padx=(12, 0))
        self.resume_play_button = ttk.Button(top, text="실패 단계부터 재개", command=self._resume_failed_workflow, state="disabled")
        self.resume_play_button.pack(side="left", padx=6)
        checkpoint_state = "normal" if self._pending_checkpoint else "disabled"
        self.recover_checkpoint_button = ttk.Button(top, text="재시작 복구", command=self._recover_checkpoint, state=checkpoint_state)
        self.recover_checkpoint_button.pack(side="left", padx=6)
        ttk.Button(top, text="템플릿 저장", command=self._save_template).pack(side="right", padx=(6, 0))
        ttk.Button(top, text="템플릿 불러오기", command=self._load_template).pack(side="right")
        ttk.Button(top, text="백업 복원", command=self._restore_workflow_backup).pack(side="right", padx=(6, 0))
        ttk.Button(top, text="선택 단계 삭제", command=self._delete_selected_step).pack(side="right", padx=(6, 0))
        ttk.Button(top, text="저장", command=self._save_workflow).pack(side="right", padx=(6, 0))
        ttk.Button(top, text="불러오기", command=self._load_workflow).pack(side="right")

        info = ttk.LabelFrame(self.record_tab, text="사용 방법", style="Card.TLabelframe")
        info.pack(fill="x", pady=(0, 10))
        ttk.Label(
            info,
            text="기록 시작을 누른 뒤 다른 프로그램으로 전환하여 작업하세요. 기록 중지 후 저장하면 동일한 순서로 재생할 수 있습니다. "
            "재생 중에는 마우스를 화면 왼쪽 위 모서리로 이동하면 즉시 중단됩니다.",
            wraplength=950,
            justify="left",
        ).pack(anchor="w")

        edit_bar = ttk.Frame(self.record_tab)
        edit_bar.pack(fill="x", pady=(0, 8))
        ttk.Label(edit_bar, text="선택 단계 편집:").pack(side="left")
        ttk.Button(edit_bar, text="위로", command=lambda: self._move_selected_step(-1)).pack(side="left", padx=(8, 3))
        ttk.Button(edit_bar, text="아래로", command=lambda: self._move_selected_step(1)).pack(side="left", padx=3)
        ttk.Button(edit_bar, text="복제", command=self._duplicate_selected_step).pack(side="left", padx=3)
        ttk.Button(edit_bar, text="대기시간 수정", command=self._edit_selected_delay).pack(side="left", padx=3)
        self.step_mode_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(edit_bar, text="단계별 승인", variable=self.step_mode_var).pack(side="left", padx=(12, 0))
        ttk.Label(edit_bar, text="단계를 선택하고 편집하세요. 실행 중에는 편집할 수 없습니다.", style="Sub.TLabel").pack(side="left", padx=(12, 0))

        name_row = ttk.Frame(self.record_tab)
        name_row.pack(fill="x", pady=(0, 8))
        ttk.Label(name_row, text="작업 이름").pack(side="left")
        self.workflow_name_var = tk.StringVar(value=self.workflow.name)
        ttk.Entry(name_row, textvariable=self.workflow_name_var, width=36).pack(side="left", padx=(8, 0))
        ttk.Label(name_row, text="단계 수:").pack(side="left", padx=(24, 4))
        self.step_count_var = tk.StringVar(value="0")
        ttk.Label(name_row, textvariable=self.step_count_var).pack(side="left")
        ttk.Label(name_row, text="기록 화면:").pack(side="left", padx=(24, 4))
        self.recorded_screen_var = tk.StringVar(value="미기록")
        ttk.Label(name_row, textvariable=self.recorded_screen_var, style="Sub.TLabel").pack(side="left")

        table_frame = ttk.Frame(self.record_tab)
        table_frame.pack(fill="both", expand=True)
        columns = ("no", "type", "delay", "position", "detail")
        self.step_tree = ttk.Treeview(table_frame, columns=columns, show="headings", height=22)
        headings = {"no": "번호", "type": "동작", "delay": "대기(초)", "position": "위치", "detail": "세부 정보"}
        widths = {"no": 60, "type": 100, "delay": 100, "position": 150, "detail": 500}
        for column in columns:
            self.step_tree.heading(column, text=headings[column])
            self.step_tree.column(column, width=widths[column], anchor="w")
        scroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.step_tree.yview)
        self.step_tree.configure(yscrollcommand=scroll.set)
        self.step_tree.pack(side="left", fill="both", expand=True)
        self.step_tree.bind("<Delete>", lambda _event: self._delete_selected_step())
        scroll.pack(side="right", fill="y")

        footer = ttk.Frame(self.record_tab)
        footer.pack(fill="x", pady=(10, 0))
        ttk.Label(footer, text="단축키: 기록 중지 Ctrl+Alt+F12 · 재생 긴급 중지: 마우스를 화면 왼쪽 위 모서리", style="Sub.TLabel").pack(side="left")

    def _build_ai_tab(self) -> None:
        goal_frame = ttk.LabelFrame(self.ai_tab, text="업무 목표", style="Card.TLabelframe")
        goal_frame.pack(fill="x", pady=(0, 10))
        ttk.Label(goal_frame, text="예: 현재 문서에서 표의 합계를 계산하고 결과를 지정된 셀에 입력해 줘").pack(anchor="w")
        self.goal_text = tk.Text(goal_frame, height=3, wrap="word", font=("Segoe UI", 10))
        self.goal_text.pack(fill="x", pady=(7, 0))
        goal_actions = ttk.Frame(goal_frame)
        goal_actions.pack(fill="x", pady=(8, 0))
        ttk.Button(goal_actions, text="자연어 템플릿 불러오기", command=self._load_prompt_template).pack(side="left")
        ttk.Button(goal_actions, text="자연어 템플릿 저장", command=self._save_prompt_template).pack(side="left", padx=6)
        ttk.Button(goal_actions, text="템플릿 버전·비교", command=self._compare_prompt_templates).pack(side="left", padx=6)
        ttk.Button(goal_actions, text="화면 관찰 후 계획 생성", command=self._make_ai_plan, style="Primary.TButton").pack(side="right")

        body = ttk.Panedwindow(self.ai_tab, orient="horizontal")
        body.pack(fill="both", expand=True)
        left = ttk.LabelFrame(body, text="관찰 정보", style="Card.TLabelframe")
        right = ttk.LabelFrame(body, text="AI 계획 · 승인 후 실행", style="Card.TLabelframe")
        body.add(left, weight=1)
        body.add(right, weight=1)

        self.observation_text = tk.Text(left, wrap="word", state="disabled", font=("Consolas", 9))
        self.observation_text.pack(fill="both", expand=True)
        self.plan_text = tk.Text(right, wrap="word", state="disabled", font=("Consolas", 9))
        self.plan_text.pack(fill="both", expand=True)
        plan_buttons = ttk.Frame(right)
        plan_buttons.pack(fill="x", pady=(8, 0))
        self.stop_ai_button = ttk.Button(plan_buttons, text="AI 실행 중지", command=self._stop_ai_plan, state="disabled")
        self.stop_ai_button.pack(side="right", padx=(0, 6))
        self.execute_plan_button = ttk.Button(plan_buttons, text="검토 후 계획 실행", command=self._execute_ai_plan, state="disabled", style="Primary.TButton")
        self.execute_plan_button.pack(side="right")
        ttk.Label(
            right,
            text="AI는 허용된 클릭·입력·대기 동작만 제안합니다. 결제·전송·게시·삭제·로그인 정보 입력은 자동 실행하지 않습니다.",
            style="Sub.TLabel",
            wraplength=430,
            justify="left",
        ).pack(anchor="w", pady=(8, 0))

    def _build_settings_tab(self) -> None:
        card = ttk.LabelFrame(self.settings_tab, text="로컬 AI 연결 설정", style="Card.TLabelframe")
        card.pack(fill="x", anchor="n")
        rows = [
            ("OpenAI 호환 주소", "endpoint_var", str(self._config_data.get("endpoint", "http://127.0.0.1:11434/v1"))),
            ("모델 이름", "model_var", str(self._config_data.get("model", "gemma4:e2b"))),
            ("응답 제한 시간(초)", "timeout_var", str(self._config_data.get("timeout", "120"))),
        ]
        for row, (label, attr, default) in enumerate(rows):
            ttk.Label(card, text=label, width=22).grid(row=row, column=0, sticky="w", pady=6)
            var = tk.StringVar(value=default)
            setattr(self, attr, var)
            ttk.Entry(card, textvariable=var, width=58).grid(row=row, column=1, sticky="w", pady=6)
        self.vision_var = tk.BooleanVar(value=bool(self._config_data.get("vision", False)))
        ttk.Checkbutton(card, text="가능한 경우 화면 이미지도 로컬 모델에 전달", variable=self.vision_var).grid(row=3, column=1, sticky="w", pady=6)
        self.dry_run_enabled = bool(self._config_data.get("dry_run", True))
        self.dry_run_var = tk.BooleanVar(value=self.dry_run_enabled)
        ttk.Checkbutton(card, text="AI 계획은 실제 입력 없이 검증만 수행(dry-run)", variable=self.dry_run_var, command=self._sync_safety_settings).grid(row=4, column=1, sticky="w", pady=6)
        ttk.Label(card, text="업무별 안전 프로필", width=22).grid(row=5, column=0, sticky="w", pady=6)
        profile_values = list(POLICY_PROFILES.keys())
        saved_profile = str(self._config_data.get("policy_profile", DEFAULT_POLICY_PROFILE))
        if saved_profile not in profile_values:
            saved_profile = DEFAULT_POLICY_PROFILE
        self.policy_profile_var = tk.StringVar(value=saved_profile)
        ttk.Combobox(card, textvariable=self.policy_profile_var, values=profile_values, state="readonly", width=30).grid(row=5, column=1, sticky="w", pady=6)
        ttk.Label(card, text="승인 문서 루트(;로 구분)", width=22).grid(row=6, column=0, sticky="w", pady=6)
        self.document_roots_var = tk.StringVar(value=";".join(str(item) for item in self._config_data.get("document_roots", []) if isinstance(item, str)))
        ttk.Entry(card, textvariable=self.document_roots_var, width=58).grid(row=6, column=1, sticky="w", pady=6)
        settings_buttons = ttk.Frame(card)
        settings_buttons.grid(row=7, column=1, sticky="w", pady=(10, 0))
        ttk.Button(settings_buttons, text="설정 저장", command=self._save_config).pack(side="left")
        ttk.Button(settings_buttons, text="연결 점검", command=self._check_ai_connection).pack(side="left", padx=6)
        self.connection_status_var = tk.StringVar(value="연결 상태 확인 전")
        ttk.Label(settings_buttons, textvariable=self.connection_status_var, style="Sub.TLabel").pack(side="left", padx=(8, 0))
        ttk.Label(card, text="예약 실행 간격(초)", width=22).grid(row=8, column=0, sticky="w", pady=(18, 6))
        self.schedule_interval_var = tk.StringVar(value=str(self._config_data.get("schedule_interval", 3600)))
        ttk.Entry(card, textvariable=self.schedule_interval_var, width=18).grid(row=8, column=1, sticky="w", pady=(18, 6))
        self.schedule_status_var = tk.StringVar(value="예약 실행 중지")
        ttk.Label(card, textvariable=self.schedule_status_var, style="Sub.TLabel").grid(row=9, column=1, sticky="w")
        schedule_buttons = ttk.Frame(card)
        schedule_buttons.grid(row=10, column=1, sticky="w", pady=(4, 0))
        ttk.Button(schedule_buttons, text="예약 시작", command=self._start_scheduler).pack(side="left")
        ttk.Button(schedule_buttons, text="예약 중지", command=self._stop_scheduler).pack(side="left", padx=6)
        ttk.Label(
            self.settings_tab,
            text="기본값은 Ollama의 OpenAI 호환 주소입니다. LM Studio 등 다른 로컬 서버를 쓰면 주소와 모델 이름만 바꾸면 됩니다. "
            "서버가 꺼져 있어도 기록·재생 기능은 독립적으로 사용할 수 있습니다.",
            style="Sub.TLabel",
            wraplength=900,
            justify="left",
        ).pack(anchor="w", pady=(14, 0))

    def _build_debug_tab(self) -> None:
        top = ttk.Frame(self.debug_tab)
        top.pack(fill="x", pady=(0, 10))
        ttk.Button(top, text="로그 새로고침", command=self._refresh_log_view).pack(side="left")
        ttk.Button(top, text="실행 이력 새로고침", command=self._refresh_history_view).pack(side="left", padx=6)
        ttk.Button(top, text="실행 리포트 새로고침", command=self._refresh_report_view).pack(side="left", padx=6)
        ttk.Button(top, text="리포트 요약 export", command=self._export_execution_report_summary).pack(side="left", padx=6)
        ttk.Button(top, text="운영 상태 새로고침", command=self._refresh_monitor_view).pack(side="left", padx=6)
        ttk.Button(top, text="지원 패키지 저장", command=self._export_support_bundle).pack(side="left", padx=6)
        ttk.Button(top, text="로그 폴더 열기", command=self._open_log_folder).pack(side="left", padx=6)
        self.capture_on_error_enabled = bool(self._config_data.get("capture_on_error", True))
        self.capture_on_error_var = tk.BooleanVar(value=self.capture_on_error_enabled)
        ttk.Checkbutton(top, text="예외 발생 시 화면 캡처 저장", variable=self.capture_on_error_var, command=self._sync_safety_settings).pack(side="left", padx=(18, 0))
        ttk.Label(
            self.debug_tab,
            text=f"오류 보고서: {ERROR_DIR} · 진단 로그: {LOG_PATH} · 자동화 중 문제가 생기면 현재 단계와 오류가 이 위치에 저장됩니다.",
            style="Sub.TLabel",
            wraplength=950,
            justify="left",
        ).pack(anchor="w", pady=(0, 8))
        self.last_action_var = tk.StringVar(value="최근 상태: 대기 중")
        ttk.Label(self.debug_tab, textvariable=self.last_action_var, style="Sub.TLabel").pack(anchor="w", pady=(0, 4))
        self.monitor_status_var = tk.StringVar(value="운영 모니터: 확인 전")
        ttk.Label(self.debug_tab, textvariable=self.monitor_status_var, style="Sub.TLabel", wraplength=950, justify="left").pack(anchor="w", pady=(0, 8))
        body = ttk.Panedwindow(self.debug_tab, orient="vertical")
        body.pack(fill="both", expand=True)
        log_frame = ttk.LabelFrame(body, text="최근 로그", style="Card.TLabelframe")
        body.add(log_frame, weight=3)
        self.log_text = tk.Text(log_frame, wrap="word", state="disabled", font=("Consolas", 9))
        self.log_text.pack(side="left", fill="both", expand=True)
        log_scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scroll.set)
        log_scroll.pack(side="right", fill="y")
        history_frame = ttk.LabelFrame(body, text="실행 이력(입력 내용 제외)", style="Card.TLabelframe")
        body.add(history_frame, weight=2)
        self.history_text = tk.Text(history_frame, wrap="word", state="disabled", font=("Consolas", 9))
        self.history_text.pack(side="left", fill="both", expand=True)
        history_scroll = ttk.Scrollbar(history_frame, orient="vertical", command=self.history_text.yview)
        self.history_text.configure(yscrollcommand=history_scroll.set)
        history_scroll.pack(side="right", fill="y")
        report_frame = ttk.LabelFrame(body, text="실행 리포트 대시보드(입력·단계 원문 제외)", style="Card.TLabelframe")
        body.add(report_frame, weight=2)
        self.report_text = tk.Text(report_frame, wrap="word", state="disabled", font=("Consolas", 9))
        self.report_text.pack(side="left", fill="both", expand=True)
        report_scroll = ttk.Scrollbar(report_frame, orient="vertical", command=self.report_text.yview)
        self.report_text.configure(yscrollcommand=report_scroll.set)
        report_scroll.pack(side="right", fill="y")
        self._refresh_log_view()
        self._refresh_history_view()
        self._refresh_report_view()
        self._refresh_monitor_view()
        self.after(30_000, self._monitor_heartbeat)

    def _refresh_log_view(self) -> None:
        if not hasattr(self, "log_text"):
            return
        try:
            if LOG_PATH.exists():
                lines = LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()[-300:]
                text = "\n".join(lines) or "로그가 아직 없습니다."
            else:
                text = "로그가 아직 없습니다."
        except Exception as exc:
            text = f"로그를 읽을 수 없습니다: {exc}"
        self._set_text(self.log_text, text)

    def _refresh_history_view(self) -> None:
        if not hasattr(self, "history_text"):
            return
        try:
            records = read_execution_history(200)
            summary = summarize_execution_history(records)
            success_rate = "-" if summary["success_rate"] is None else f"{summary['success_rate']:.1f}%"
            header = f"통계 · 이벤트 {summary['total_events']} · 완료 {summary['completed_runs']} · 실패 {summary['failed_runs']} · 성공률 {success_rate} · 마지막 이벤트 {summary['last_event'] or '-'}"
            text = header + "\n\n" + ("\n".join(json.dumps(record, ensure_ascii=False) for record in records) or "실행 이력이 아직 없습니다.")
        except Exception as exc:
            text = f"실행 이력을 읽을 수 없습니다: {exc}"
        self._set_text(self.history_text, text)

    def _refresh_report_view(self) -> None:
        if not hasattr(self, "report_text"):
            return
        try:
            dashboard = build_execution_report_dashboard(limit=20)
            summary = dashboard.get("summary", {})
            success_rate = "-" if summary.get("success_rate") is None else f"{summary['success_rate']:.1f}%"
            failure_patterns = summary.get("failure_patterns", [])
            workflow_patterns = summary.get("workflow_failure_patterns", [])
            policy_stats = summary.get("policy_stats", [])
            daily_trend = summary.get("daily_trend", [])[-7:]
            failure_text = ", ".join(f"{item.get('error_type')}({item.get('count')})" for item in failure_patterns) or "없음"
            workflow_text = ", ".join(f"{item.get('workflow')}({item.get('count')})" for item in workflow_patterns) or "없음"
            policy_text = ", ".join(f"{item.get('policy_profile')}: {item.get('success_rate', '-')}%" for item in policy_stats) or "없음"
            trend_text = ", ".join(f"{item.get('date')}: {item.get('success_rate', '-')}%" for item in daily_trend) or "없음"
            lines = [
                f"요약 · 리포트 {summary.get('total_reports', 0)} · 완료 {summary.get('completed_runs', 0)} · "
                f"실패 {summary.get('failed_runs', 0)} · 중지 {summary.get('stopped_runs', 0)} · 성공률 {success_rate}",
                f"주요 오류 유형: {failure_text}",
                f"실패 작업: {workflow_text}",
                f"정책별 성공률: {policy_text}",
                f"최근 날짜별 성공률: {trend_text}",
                "",
                "최근 리포트(입력값·단계 원문 제외):",
            ]
            safe_fields = ("created_at", "run_id", "event", "mode", "workflow", "last_step", "duration_seconds", "error_type", "policy_profile")
            for report in dashboard.get("recent_reports", []):
                lines.append(json.dumps({field_name: report.get(field_name) for field_name in safe_fields}, ensure_ascii=False))
            self._set_text(self.report_text, "\n".join(lines))
        except Exception as exc:
            self._set_text(self.report_text, f"실행 리포트를 읽을 수 없습니다: {exc}")

    def _refresh_monitor_view(self, status_override: Optional[str] = None) -> None:
        if not hasattr(self, "monitor_status_var"):
            return
        try:
            snapshot = build_monitor_snapshot(
                status=status_override or self.status_var.get(),
                workflow=self.workflow,
                scheduler_running=self.scheduler.running,
                current_step=self.player.current_index if self.player.running else None,
            )
            write_monitor_snapshot(snapshot)
            alerts = snapshot.get("alerts", [])
            alert_text = " / ".join(str(item.get("message", "")) for item in alerts[:3]) or "현재 경보 없음"
            summary = snapshot.get("summary", {})
            integrity = snapshot.get("audit_integrity", {})
            integrity_text = "정상" if integrity.get("valid") else "검증 실패"
            if integrity.get("legacy_records"):
                integrity_text += f" · 레거시 {integrity['legacy_records']}건"
            self.monitor_status_var.set(
                f"운영 모니터: {snapshot.get('heartbeat_at', '-')} · 상태 {snapshot.get('status', '-')} · "
                f"성공률 {summary.get('success_rate', '-')} · 감사 {integrity_text} · "
                f"경보 {len(alerts)}개 · {alert_text}"
            )
        except Exception as exc:
            self.monitor_status_var.set(f"운영 모니터 오류: {exc}")

    def _monitor_heartbeat(self) -> None:
        try:
            self._refresh_monitor_view()
            self.after(30_000, self._monitor_heartbeat)
        except tk.TclError:
            return

    def _export_execution_report_summary(self) -> None:
        period_days = simpledialog.askinteger(
            "리포트 기간", "최근 며칠의 리포트를 export할까요?", parent=self, initialvalue=30, minvalue=1, maxvalue=3650,
        )
        if period_days is None:
            return
        path = filedialog.asksaveasfilename(
            title="실행 리포트 요약 export",
            initialdir=str(APP_DIR),
            initialfile=f"execution_report_summary_{period_days}d.json",
            defaultextension=".json",
            filetypes=[("AutoWork 실행 리포트 요약", "*.json")],
        )
        if not path:
            return
        try:
            summary_path = export_execution_report_summary(Path(path), period_days)
            append_execution_history("execution_report_summary_exported", self.workflow, period_days=period_days, size_bytes=summary_path.stat().st_size)
            self._set_status(f"실행 리포트 요약 export 완료 · {summary_path.name}")
            messagebox.showinfo("리포트 export 완료", f"입력값과 Workflow 단계 원문을 제외한 요약을 저장했습니다.\n\n{summary_path}")
        except Exception as exc:
            messagebox.showerror("실행 리포트 export 실패", str(exc))

    def _export_support_bundle(self) -> None:
        path = filedialog.asksaveasfilename(
            title="지원 진단 패키지 저장",
            initialdir=str(APP_DIR),
            initialfile="autowork_support_bundle.json",
            defaultextension=".json",
            filetypes=[("AutoWork 진단 패키지", "*.json")],
        )
        if not path:
            return
        try:
            bundle_path = export_support_bundle(Path(path))
            append_execution_history("support_bundle_exported", self.workflow, size_bytes=bundle_path.stat().st_size)
            self._set_status(f"지원 진단 패키지 저장 완료 · {bundle_path.name}")
            messagebox.showinfo("지원 패키지 저장 완료", f"민감한 Workflow 단계와 화면 원문을 제외한 진단 패키지를 저장했습니다.\n\n{bundle_path}")
        except Exception as exc:
            messagebox.showerror("지원 패키지 저장 실패", str(exc))

    def _open_log_folder(self) -> None:
        try:
            if hasattr(os, "startfile"):
                os.startfile(str(APP_DIR))
            else:
                messagebox.showinfo("로그 위치", str(APP_DIR))
        except Exception as exc:
            messagebox.showerror("로그 폴더 열기 실패", str(exc))

    def _handle_exception(self, component: str, exc: BaseException, extra: Optional[Dict[str, Any]] = None) -> Optional[Path]:
        context: Dict[str, Any] = {
            "component": component,
            "workflow": self.workflow.name,
            "workflow_path": str(self.current_path) if self.current_path else "",
            "step_count": len(self.workflow.steps),
            "player_step_index": getattr(self.player, "current_index", 0),
            "ai_step_index": self.last_ai_index,
        }
        if extra:
            context.update(extra)
        report_path: Optional[Path] = None
        capture_screen = bool(self.capture_on_error_enabled)
        try:
            report_path = write_error_report(context, exc, capture_screen=capture_screen, traceback_text=str(context.get("traceback", "")) or None)
        except Exception as report_exc:
            append_log(f"오류 보고서 작성 실패: {report_exc}", "ERROR")
        append_log(f"{component} 중지: {type(exc).__name__}: {str(exc)[:500]}", "ERROR")
        status = f"{component} 안전 중지"
        if report_path:
            status += f" · {report_path.name}"
        self._set_status(status)
        self.after(0, self._refresh_log_view)
        return report_path

    def _show_exception_dialog(self, component: str, exc: BaseException, report_path: Optional[Path]) -> None:
        location = f"\n\n오류 보고서: {report_path}" if report_path else ""
        messagebox.showerror(f"{component} 안전 중지", f"{type(exc).__name__}: {str(exc)[:1200]}{location}\n\n진단·로그 탭에서 상세 기록을 확인하세요.")

    def _on_recorder_error(self, exc: BaseException) -> None:
        report_path = self._handle_exception("입력 기록", exc)
        self.after(0, lambda: self._show_exception_dialog("입력 기록", exc, report_path))
        self.after(0, self._stop_recording)

    def _on_player_error(self, exc: BaseException, index: int, step: Step) -> None:
        self.last_failed_step_index = max(0, index - 1)
        if hasattr(self, "resume_play_button"):
            self.after(0, lambda: self.resume_play_button.configure(state="normal"))
        failure_context = {"failed_step": index, "step": redact_sensitive(step.__dict__), "error_type": type(exc).__name__, "error": str(exc)[:1200]}
        save_execution_checkpoint(self.current_path, index - 1, self.workflow.signature or "", error_type=type(exc).__name__)
        self._pending_checkpoint = load_execution_checkpoint()
        if hasattr(self, "recover_checkpoint_button"):
            self.after(0, lambda: self.recover_checkpoint_button.configure(state="normal"))
        report_path = self._handle_exception("작업 재생", exc, failure_context)
        self.after(0, lambda: self._show_exception_dialog("작업 재생", exc, report_path))
        self.after(0, lambda: self._offer_recovery_plan(failure_context))

    def _offer_recovery_plan(self, failure_context: Dict[str, Any]) -> None:
        if not messagebox.askyesno("AI 복구 분석", "실패한 화면을 다시 관찰해 복구 계획을 제안할까요?\n\n복구안은 자동 실행되지 않으며 사용자가 검토해야 합니다."):
            return
        if self.ai_running or not self._ai_job_lock.acquire(blocking=False):
            messagebox.showwarning("AI 실행 중", "다른 AI 작업이 진행 중입니다.")
            return
        settings = {
            "endpoint": self.endpoint_var.get().strip(),
            "model": self.model_var.get().strip(),
            "timeout": self.timeout_var.get().strip(),
            "vision": bool(self.vision_var.get()),
            "policy_profile": self._current_policy_profile(),
            "document_roots": [str(path) for path in normalize_document_roots(self.document_roots_var.get())],
        }
        self.ai_running = True
        self._set_status("실패 원인 분석 및 복구 계획 생성 중…")
        threading.Thread(target=self._recovery_worker, args=(failure_context, settings), daemon=True, name="recovery-planner").start()

    def _recovery_worker(self, failure_context: Dict[str, Any], settings: Dict[str, Any]) -> None:
        try:
            observation = capture_observation()
            observation["policy_profile"] = str(settings.get("policy_profile", DEFAULT_POLICY_PROFILE))
            observation["document_context"] = build_document_context(settings.get("document_roots", []), "복구에 필요한 현재 문서 상태")
            settings = {
                **settings,
                "endpoint": validate_local_endpoint(str(settings["endpoint"])),
                "model": str(settings["model"]),
                "timeout": validate_timeout(settings["timeout"]),
                "vision": bool(settings["vision"]),
            }
            if not settings["model"]:
                raise ValueError("모델 이름을 입력하세요.")
            client = LocalAIClient(settings["endpoint"], settings["model"], settings["timeout"], settings["vision"])
            plan = client.make_recovery_plan(failure_context, observation)
            self.after(0, lambda: self._show_ai_result(observation, plan))
            self.after(0, lambda: self._set_status("복구 계획 생성 완료 · 검토 후 실행 가능"))
        except Exception as exc:
            report_path = self._handle_exception("복구 계획 생성", exc, {"failure": failure_context})
            self.after(0, lambda: self._show_exception_dialog("복구 계획 생성", exc, report_path))
        finally:
            self.ai_running = False
            self._ai_job_lock.release()

    def report_callback_exception(self, exc: type[BaseException], value: BaseException, tb: Any) -> None:
        report_path = self._handle_exception("GUI 이벤트", value, {"traceback": "".join(traceback.format_exception(exc, value, tb))[-12000:]})
        self._show_exception_dialog("GUI 이벤트", value, report_path)

    def _start_global_hotkey(self) -> None:
        if pynput_keyboard is None:
            return
        try:
            self.hotkey_listener = pynput_keyboard.GlobalHotKeys({
                "<ctrl>+<alt>+<f12>": self._emergency_stop,
            })
            self.hotkey_listener.start()
        except Exception:
            self.hotkey_listener = None

    def _emergency_stop(self) -> None:
        self.player.stop()
        self.ai_stop_event.set()
        self.after(0, self._stop_recording)
        if hasattr(self, "stop_ai_button"):
            self.after(0, lambda: self.stop_ai_button.configure(state="disabled"))
        self._set_status("긴급 중지 요청 · 재생 및 AI 실행 중단")

    def _current_policy_profile(self) -> str:
        selected = self.policy_profile_var.get().strip() if hasattr(self, "policy_profile_var") else DEFAULT_POLICY_PROFILE
        return selected if selected in POLICY_PROFILES else DEFAULT_POLICY_PROFILE

    def _sync_safety_settings(self) -> None:
        self.dry_run_enabled = bool(self.dry_run_var.get()) if hasattr(self, "dry_run_var") else self.dry_run_enabled
        self.capture_on_error_enabled = bool(self.capture_on_error_var.get()) if hasattr(self, "capture_on_error_var") else self.capture_on_error_enabled
        self._save_config()

    def _set_status(self, text: str) -> None:
        def update() -> None:
            self.status_var.set(text)
            if hasattr(self, "last_action_var"):
                self.last_action_var.set(f"최근 상태: {text}")
        self.after(0, update)

    def _on_recorded_step(self, step: Step) -> None:
        self.after(0, lambda: self._append_step_row(step))

    def _append_step_row(self, step: Step) -> None:
        index = len(self.workflow.steps) + 1
        self.workflow.steps.append(step)
        if step.type == "click":
            position = f"({step.x}, {step.y})"
            detail = f"마우스 {step.button or 'left'} 클릭"
        else:
            position = "-"
            detail = f"키 {step.event or ''}: {step.value or ''}"
        self.step_tree.insert("", "end", values=(index, step.type, f"{step.delay:.3f}", position, detail))
        self.step_count_var.set(str(len(self.workflow.steps)))

    def _refresh_steps(self) -> None:
        for item in self.step_tree.get_children():
            self.step_tree.delete(item)
        for step in self.workflow.steps:
            self._append_step_row_without_mutation(step)
        self.step_count_var.set(str(len(self.workflow.steps)))
        self.workflow_name_var.set(self.workflow.name)
        recorded_size = self.workflow.recorded_screen_size
        self.recorded_screen_var.set(f"{recorded_size[0]}×{recorded_size[1]}" if recorded_size else "미기록")

    def _append_step_row_without_mutation(self, step: Step) -> None:
        index = len(self.step_tree.get_children()) + 1
        if step.type == "click":
            position = f"({step.x}, {step.y})"
            detail = f"마우스 {step.button or 'left'} 클릭"
        else:
            position = "-"
            detail = f"키 {step.event or ''}: {step.value or ''}"
        self.step_tree.insert("", "end", values=(index, step.type, f"{step.delay:.3f}", position, detail))

    def _selected_step_index(self, show_message: bool = True) -> Optional[int]:
        selection = self.step_tree.selection()
        if not selection:
            if show_message:
                messagebox.showinfo("단계 선택 필요", "편집할 단계를 먼저 선택하세요.")
            return None
        try:
            return int(self.step_tree.item(selection[0], "values")[0]) - 1
        except (TypeError, ValueError, IndexError):
            if show_message:
                messagebox.showerror("단계 선택 오류", "선택한 단계 정보를 읽을 수 없습니다.")
            return None

    def _can_edit_steps(self) -> bool:
        if self.recording or self.player.running:
            messagebox.showwarning("작업 실행 중", "기록 또는 재생 중에는 단계를 편집할 수 없습니다.")
            return False
        return True

    def _select_step(self, index: int) -> None:
        items = self.step_tree.get_children()
        if 0 <= index < len(items):
            self.step_tree.selection_set(items[index])
            self.step_tree.focus(items[index])

    def _inspect_current_workflow(self) -> None:
        report = inspect_workflow(self.workflow, get_screen_size())
        status = "통과" if report["valid"] else "실패"
        lines = [
            f"안전 점검: {status}",
            f"단계: {report['step_count']}개 · 클릭 {report['click_count']} · 키 {report['key_count']} · 대기 {report['wait_count']}",
            f"결과: {report['reason']}",
        ]
        if report["window_titles"]:
            lines.append("대상 창: " + ", ".join(report["window_titles"][:5]))
        if report["warnings"]:
            lines.append("경고: " + " ".join(report["warnings"]))
        if report["valid"] and not report["warnings"]:
            messagebox.showinfo("안전 점검 완료", "\n".join(lines))
        else:
            messagebox.showwarning("안전 점검 결과", "\n".join(lines))
        self._set_status(f"안전 점검 {status}")

    def _delete_selected_step(self) -> None:
        if not self._can_edit_steps():
            return
        index = self._selected_step_index()
        if index is None:
            return
        try:
            removed = self.workflow.remove_step(index)
        except IndexError as exc:
            messagebox.showerror("단계 삭제 실패", str(exc))
            return
        self._refresh_steps()
        self._set_status(f"단계 삭제 완료 · {removed.type} · 남은 단계 {len(self.workflow.steps)}개")

    def _move_selected_step(self, offset: int) -> None:
        if not self._can_edit_steps():
            return
        index = self._selected_step_index()
        if index is None:
            return
        try:
            self.workflow.move_step(index, offset)
        except IndexError as exc:
            messagebox.showinfo("단계 이동", str(exc))
            return
        self._refresh_steps()
        self._select_step(index + offset)
        self._set_status("단계 순서 변경 완료")

    def _duplicate_selected_step(self) -> None:
        if not self._can_edit_steps():
            return
        index = self._selected_step_index()
        if index is None:
            return
        try:
            self.workflow.duplicate_step(index)
        except IndexError as exc:
            messagebox.showerror("단계 복제 실패", str(exc))
            return
        self._refresh_steps()
        self._select_step(index + 1)
        self._set_status("단계 복제 완료")

    def _edit_selected_delay(self) -> None:
        if not self._can_edit_steps():
            return
        index = self._selected_step_index()
        if index is None:
            return
        current = self.workflow.steps[index].delay
        value = simpledialog.askstring("대기시간 수정", "단계 시작 전 대기시간(초)을 입력하세요.\n허용 범위: 0~60", initialvalue=f"{current:.3f}", parent=self)
        if value is None:
            return
        try:
            self.workflow.update_step_delay(index, value)
        except (IndexError, ValueError) as exc:
            messagebox.showerror("대기시간 수정 실패", str(exc))
            return
        self._refresh_steps()
        self._select_step(index)
        self._set_status("대기시간 수정 완료")

    def _save_template(self) -> None:
        self.workflow.name = self.workflow_name_var.get().strip() or "새 템플릿"
        path = filedialog.asksaveasfilename(
            title="템플릿 저장",
            initialdir=str(TEMPLATE_DIR),
            initialfile=f"{self.workflow.name}.json",
            defaultextension=".json",
            filetypes=[("AutoWork 템플릿", "*.json"), ("모든 파일", "*.*")],
        )
        if not path:
            return
        try:
            save_workflow(self.workflow, Path(path))
            self._set_status(f"템플릿 저장 완료 · {Path(path).name}")
        except Exception as exc:
            messagebox.showerror("템플릿 저장 실패", str(exc))

    def _load_template(self) -> None:
        if not self._can_edit_steps():
            return
        path = filedialog.askopenfilename(
            title="템플릿 불러오기",
            initialdir=str(TEMPLATE_DIR),
            filetypes=[("AutoWork 템플릿", "*.json"), ("모든 파일", "*.*")],
        )
        if not path:
            return
        try:
            self.workflow = load_workflow(Path(path))
            self.current_path = None
            self._refresh_steps()
            self._set_status(f"템플릿 불러오기 완료 · {Path(path).name}")
        except Exception as exc:
            messagebox.showerror("템플릿 불러오기 실패", str(exc))

    def _start_recording(self) -> None:
        if self.player.running:
            messagebox.showwarning("재생 중", "재생을 먼저 중지하세요.")
            return
        try:
            recorded_size = get_screen_size()
            self.workflow = Workflow(
                name=self.workflow_name_var.get().strip() or "새 작업",
                recorded_screen_size=list(recorded_size) if recorded_size else None,
            )
            self._refresh_steps()
            self.recorder.start(clear=True)
        except Exception as exc:
            messagebox.showerror("기록 시작 실패", str(exc))
            return
        self.recording = True
        self.record_button.configure(state="disabled")
        self.stop_record_button.configure(state="normal")
        self._set_status("기록 중 · Ctrl+Alt+F12로 중지할 수 있습니다.")

    def _stop_recording(self) -> None:
        self.recorder.stop()
        self.recording = False
        self.record_button.configure(state="normal")
        self.stop_record_button.configure(state="disabled")
        self._set_status(f"기록 중지 · {len(self.workflow.steps)}개 단계")

    def _show_step_confirmation(self, index: int, step: Step) -> None:
        if not self.step_mode_enabled or not self.player.running:
            self._step_gate.set()
            return
        detail = f"{index}. {step.type}"
        if step.type == "click":
            detail += f" · ({step.x}, {step.y}) · {step.button or 'left'}"
        elif step.type == "key":
            detail += f" · {step.event or ''} {step.value or ''}"
        elif step.type == "wait":
            detail += f" · {step.value or 0.5}초"
        approved = messagebox.askyesno("단계별 승인", f"다음 동작을 실행할까요?\n\n{detail}")
        if not approved:
            self.player.stop()
        self._step_gate.set()

    def _toggle_pause_playback(self) -> None:
        if not self.player.running:
            return
        if self.player.paused:
            self.player.resume()
            self.pause_play_button.configure(text="재생 일시정지")
        else:
            self.player.pause()
            self.pause_play_button.configure(text="재생 재개")

    def _stop_playback(self) -> None:
        self.player.stop()
        self._step_gate.set()
        if hasattr(self, "pause_play_button"):
            self.pause_play_button.configure(state="disabled", text="재생 일시정지")
        self._set_status("재생 중지 요청")

    def _stop_ai_plan(self) -> None:
        self.ai_stop_event.set()
        self._set_status("AI 실행 중지 요청")

    def _on_play_step(self, index: int, step: Step) -> None:
        self.after(0, lambda: self.step_tree.selection_set(self.step_tree.get_children()[index - 1]))
        if self.step_mode_enabled:
            self._step_gate.clear()
            self.after(0, lambda: self._show_step_confirmation(index, step))
            if not self._step_gate.wait(timeout=300):
                self.player.stop()

    def _recover_checkpoint(self) -> None:
        checkpoint = load_execution_checkpoint()
        if not checkpoint:
            self._pending_checkpoint = None
            self.recover_checkpoint_button.configure(state="disabled")
            messagebox.showinfo("복구할 체크포인트 없음", "유효한 실행 체크포인트가 없습니다.")
            return
        path = Path(checkpoint["workflow_path"])
        try:
            require_signature = bool(checkpoint.get("workflow_signature"))
            workflow = load_workflow(path, require_signature=require_signature)
            saved_signature = str(checkpoint.get("workflow_signature", ""))
            if saved_signature and workflow.signature != saved_signature:
                raise ValueError("체크포인트의 Workflow 서명이 현재 파일과 다릅니다.")
            start_index = int(checkpoint["next_index"])
            if not 0 <= start_index < len(workflow.steps):
                raise ValueError("체크포인트의 재개 단계가 현재 Workflow 범위를 벗어났습니다.")
            self.workflow = workflow
            self.current_path = path
            self.workflow_name_var.set(workflow.name)
            self._refresh_steps()
            self.last_failed_step_index = start_index
            self._resume_failed_workflow()
        except Exception as exc:
            clear_execution_checkpoint()
            self._pending_checkpoint = None
            self.recover_checkpoint_button.configure(state="disabled")
            messagebox.showerror("체크포인트 복구 실패", str(exc))

    def _resume_failed_workflow(self) -> None:
        start_index = self.last_failed_step_index
        if start_index is None:
            messagebox.showinfo("재개할 작업 없음", "최근 실패한 작업이 없습니다.")
            return
        if self.recording or self.player.running:
            messagebox.showwarning("작업 실행 중", "기록 또는 재생 중에는 재개할 수 없습니다.")
            return
        report = inspect_workflow(self.workflow, get_screen_size())
        if not report["valid"]:
            messagebox.showerror("재개할 수 없는 작업", report["reason"])
            return
        step_number = start_index + 1
        warning = "\n".join(report["warnings"])
        prompt = f"{step_number}번째 실패 단계부터 다시 실행합니다.\n\n재개 시 실패한 입력 동작이 다시 실행될 수 있습니다."
        if warning:
            prompt += f"\n\n경고: {warning}"
        if not messagebox.askyesno("실패 단계부터 재개", prompt):
            return
        if not self._playback_launch_lock.acquire(blocking=False):
            messagebox.showwarning("재생 중", "이미 다른 작업을 재생 중입니다.")
            return
        self.step_mode_enabled = bool(self.step_mode_var.get())
        self._step_gate.clear()
        self.pause_play_button.configure(state="normal", text="재생 일시정지")
        threading.Thread(target=self._play_workflow_worker, args=(self.workflow, start_index), daemon=True).start()

    def _play_workflow(self) -> None:
        if self.recording:
            messagebox.showwarning("기록 중", "기록을 먼저 중지하세요.")
            return
        if self.player.running or not self._playback_launch_lock.acquire(blocking=False):
            messagebox.showwarning("재생 중", "이미 다른 작업을 재생 중입니다.")
            return
        audit = verify_execution_history()
        if not audit["valid"]:
            self._playback_launch_lock.release()
            messagebox.showerror("감사 무결성 오류", "실행 감사 이력의 무결성 검증에 실패했습니다. 원인 확인 전 재생을 중지합니다.")
            return
        valid, reason = validate_workflow(self.workflow, get_screen_size())
        if not valid:
            self._playback_launch_lock.release()
            messagebox.showerror("재생할 수 없는 작업", reason)
            return
        if not self.workflow.steps:
            self._playback_launch_lock.release()
            messagebox.showinfo("재생할 작업 없음", "먼저 작업을 기록하거나 파일을 불러오세요.")
            return
        recorded_size = self.workflow.recorded_screen_size
        current_size = get_screen_size()
        if recorded_size and current_size and tuple(recorded_size) != current_size:
            if not messagebox.askyesno(
                "화면 크기 불일치",
                f"기록 당시 화면: {recorded_size[0]}×{recorded_size[1]}\n"
                f"현재 화면: {current_size[0]}×{current_size[1]}\n\n"
                "좌표 기반 재생의 위치가 달라질 수 있습니다. 그래도 계속하시겠습니까?",
            ):
                self._playback_launch_lock.release()
                return
        ok = messagebox.askyesno(
            "재생 확인",
            "저장된 모든 마우스·키보드 동작을 현재 화면에서 실행합니다.\n\n"            "민감한 정보 입력이나 문서 전송이 포함되어 있지 않은지 확인했습니까?",
        )
        if not ok:
            self._playback_launch_lock.release()
            return
        self.last_failed_step_index = None
        self.resume_play_button.configure(state="disabled")
        self.step_mode_enabled = bool(self.step_mode_var.get())
        self._step_gate.clear()
        self.pause_play_button.configure(state="normal", text="재생 일시정지")
        threading.Thread(target=self._play_workflow_worker, args=(self.workflow,), daemon=True).start()

    def _play_workflow_worker(self, workflow: Workflow, start_index: int = 0, scheduled: bool = False) -> None:
        run_id = secrets.token_hex(8)
        started_at = time.monotonic()
        recorded_size = workflow.recorded_screen_size
        size_text = f"{recorded_size[0]}x{recorded_size[1]}" if recorded_size else ""
        append_execution_history("playback_started", workflow, screen_size=size_text, start_step=start_index + 1, run_id=run_id, scheduled=bool(scheduled))
        try:
            self.player.play(workflow, start_index=start_index)
            failure = self.player.failed or self.player.last_error
            duration = round(time.monotonic() - started_at, 3)
            if failure is not None:
                last_step = self.player.failure_index or self.player.current_index
                report_path = write_execution_report(
                    run_id, "playback_failed", workflow, mode="playback", start_step=start_index + 1,
                    last_step=last_step, duration_seconds=duration, error_type=type(failure).__name__,
                )
                append_execution_history(
                    "playback_failed", workflow, last_step=last_step, error=type(failure).__name__,
                    run_id=run_id, duration_seconds=duration, report_path=str(report_path) if report_path else "", scheduled=bool(scheduled),
                )
            else:
                event = "playback_stopped" if self.player.stop_event.is_set() else "playback_completed"
                report_path = write_execution_report(
                    run_id, event, workflow, mode="playback", start_step=start_index + 1,
                    last_step=self.player.current_index, duration_seconds=duration,
                )
                append_execution_history(
                    event, workflow, last_step=self.player.current_index, run_id=run_id,
                    duration_seconds=duration, report_path=str(report_path) if report_path else "", scheduled=bool(scheduled),
                )
                clear_execution_checkpoint()
                self._pending_checkpoint = None
                self.last_failed_step_index = None
                self.after(0, lambda: self.resume_play_button.configure(state="disabled"))
                self.after(0, lambda: self.recover_checkpoint_button.configure(state="disabled"))
        except Exception as exc:
            duration = round(time.monotonic() - started_at, 3)
            report_path = write_execution_report(
                run_id, "playback_failed", workflow, mode="playback", start_step=start_index + 1,
                last_step=self.player.current_index, duration_seconds=duration, error_type=type(exc).__name__,
            )
            append_execution_history(
                "playback_failed", workflow, last_step=self.player.current_index, error=type(exc).__name__,
                run_id=run_id, duration_seconds=duration, report_path=str(report_path) if report_path else "", scheduled=bool(scheduled),
            )
            save_execution_checkpoint(self.current_path, max(0, self.player.current_index - 1), workflow.signature or "", error_type=type(exc).__name__)
            self._pending_checkpoint = load_execution_checkpoint()
            report_path = self._handle_exception("작업 재생", exc)
            self.after(0, lambda: self._show_exception_dialog("작업 재생", exc, report_path))
        finally:
            self._step_gate.set()
            self.step_mode_enabled = False
            self._playback_launch_lock.release()
            self.after(0, lambda: self.pause_play_button.configure(state="disabled", text="재생 일시정지"))
            self.after(0, self._refresh_history_view)

    def _save_workflow(self) -> None:
        self.workflow.name = self.workflow_name_var.get().strip() or "새 작업"
        path = filedialog.asksaveasfilename(
            title="작업 저장",
            initialdir=str(WORKFLOW_DIR),
            initialfile=f"{self.workflow.name}.json",
            defaultextension=".json",
            filetypes=[("AutoWork 작업", "*.json"), ("모든 파일", "*.*")],
        )
        if not path:
            return
        try:
            backup_path = save_workflow(self.workflow, Path(path))
            self.current_path = Path(path)
            backup_note = f" · 백업 {backup_path.name}" if backup_path else ""
            self._set_status(f"저장 완료 · {self.current_path.name}{backup_note}")
        except Exception as exc:
            messagebox.showerror("저장 실패", str(exc))

    def _restore_workflow_backup(self) -> None:
        backup_path = filedialog.askopenfilename(
            title="Workflow 백업 복원",
            initialdir=str(BACKUP_DIR),
            filetypes=[("AutoWork 백업", "*.json"), ("모든 파일", "*.*")],
        )
        if not backup_path:
            return
        target = self.current_path
        if target is None:
            target_name = filedialog.asksaveasfilename(
                title="복원할 작업 파일 지정",
                initialdir=str(WORKFLOW_DIR),
                defaultextension=".json",
                filetypes=[("AutoWork 작업", "*.json")],
            )
            if not target_name:
                return
            target = Path(target_name)
        if not messagebox.askyesno("백업 복원 확인", f"다음 백업으로 현재 작업을 덮어쓸까요?\n\n{Path(backup_path).name}"):
            return
        try:
            restored = load_workflow(Path(backup_path), require_signature=False)
            save_workflow(restored, target)
            self.workflow = restored
            self.current_path = target
            self.workflow_name_var.set(restored.name)
            self._refresh_steps()
            self._set_status(f"백업 복원 완료 · {target.name}")
            append_execution_history("workflow_backup_restored", restored, source=Path(backup_path).name)
        except Exception as exc:
            messagebox.showerror("백업 복원 실패", str(exc))

    def _load_workflow(self) -> None:
        path = filedialog.askopenfilename(
            title="작업 불러오기",
            initialdir=str(WORKFLOW_DIR),
            filetypes=[("AutoWork 작업", "*.json"), ("모든 파일", "*.*")],
        )
        if not path:
            return
        try:
            self.workflow = load_workflow(Path(path))
            self.current_path = Path(path)
            self._refresh_steps()
            signature_status = "서명 확인" if verify_workflow_signature(self.workflow) else "레거시 작업(서명 없음)"
            checkpoint = load_execution_checkpoint()
            if checkpoint and Path(checkpoint.get("workflow_path", "")) != self.current_path:
                self._pending_checkpoint = None
                self.recover_checkpoint_button.configure(state="disabled")
            self._set_status(f"불러오기 완료 · {self.current_path.name} · {signature_status}")
        except Exception as exc:
            messagebox.showerror("불러오기 실패", str(exc))

    def _save_prompt_template(self) -> None:
        goal_template = self.goal_text.get("1.0", "end").strip()
        if not goal_template:
            messagebox.showinfo("템플릿 내용 필요", "저장할 자연어 업무 목표를 입력하세요.")
            return
        path = filedialog.asksaveasfilename(
            title="자연어 템플릿 저장",
            initialdir=str(TEMPLATE_DIR),
            initialfile="prompt_template.json",
            defaultextension=".json",
            filetypes=[("자연어 템플릿", "*.json"), ("모든 파일", "*.*")],
        )
        if not path:
            return
        try:
            saved = save_prompt_template(Path(path), Path(path).stem, goal_template, self._current_policy_profile(), [str(item) for item in normalize_document_roots(self.document_roots_var.get())])
            self._set_status(f"자연어 템플릿 저장 완료 · {Path(path).name} · v{saved.get('revision', 1)}")
        except Exception as exc:
            messagebox.showerror("자연어 템플릿 저장 실패", str(exc))

    def _compare_prompt_templates(self) -> None:
        paths = filedialog.askopenfilenames(
            title="템플릿 버전·비교 대상 선택(1개 또는 2개)",
            initialdir=str(TEMPLATE_DIR),
            filetypes=[("자연어 템플릿", "*.json"), ("모든 파일", "*.*")],
        )
        if not paths:
            return
        try:
            if len(paths) == 1:
                versions = list_prompt_template_versions(Path(paths[0]))
                lines = [f"{Path(paths[0]).name}의 로컬 버전 목록", ""]
                for item in versions:
                    marker = "현재" if item.get("is_current") else "보관"
                    lines.append(
                        f"v{item.get('revision')} · {marker} · 정책 {item.get('policy_profile')} · "
                        f"변수 {item.get('placeholder_count')}개 · fingerprint {str(item.get('fingerprint', ''))[:12]}"
                    )
                messagebox.showinfo("템플릿 버전 목록", "\n".join(lines))
                return
            if len(paths) != 2:
                messagebox.showwarning("템플릿 선택", "템플릿은 한 번에 1개 또는 2개만 선택하세요.")
                return
            comparison = compare_prompt_templates(Path(paths[0]), Path(paths[1]))
            changed = comparison.get("changed_fields", [])
            changed_text = ", ".join(changed) if changed else "변경 없음"
            left = comparison.get("left", {})
            right = comparison.get("right", {})
            messagebox.showinfo(
                "템플릿 비교 결과",
                f"왼쪽: {Path(paths[0]).name} v{left.get('revision')}\n"
                f"오른쪽: {Path(paths[1]).name} v{right.get('revision')}\n\n"
                f"변경 필드: {changed_text}\n"
                "템플릿 본문은 진단 이력에 기록하지 않습니다.",
            )
        except Exception as exc:
            messagebox.showerror("템플릿 버전·비교 실패", str(exc))

    def _load_prompt_template(self) -> None:
        path = filedialog.askopenfilename(
            title="자연어 템플릿 불러오기",
            initialdir=str(TEMPLATE_DIR),
            filetypes=[("자연어 템플릿", "*.json"), ("모든 파일", "*.*")],
        )
        if not path:
            return
        try:
            data = load_prompt_template(Path(path))
            values: Dict[str, Any] = {}
            for placeholder in data.get("placeholders", []):
                value = simpledialog.askstring("템플릿 변수", f"{placeholder} 값을 입력하세요.", parent=self)
                if value is None:
                    return
                values[placeholder] = value
            goal = render_prompt_template(data["goal_template"], values)
            self.goal_text.delete("1.0", "end")
            self.goal_text.insert("1.0", goal)
            profile = str(data.get("policy_profile", DEFAULT_POLICY_PROFILE))
            if profile in POLICY_PROFILES:
                self.policy_profile_var.set(profile)
            roots = data.get("document_roots", [])
            if isinstance(roots, list):
                self.document_roots_var.set(";".join(str(item) for item in roots))
            self._set_status(f"자연어 템플릿 불러오기 완료 · {Path(path).name}")
        except Exception as exc:
            messagebox.showerror("자연어 템플릿 불러오기 실패", str(exc))

    def _make_ai_plan(self) -> None:
        if self.ai_running:
            messagebox.showwarning("AI 실행 중", "현재 AI 계획을 먼저 중지하거나 완료하세요.")
            return
        goal = self.goal_text.get("1.0", "end").strip()
        if not goal:
            messagebox.showinfo("업무 목표 필요", "AI에게 시킬 업무 목표를 입력하세요.")
            return
        try:
            settings = {
                "endpoint": validate_local_endpoint(self.endpoint_var.get().strip()),
                "model": self.model_var.get().strip(),
                "timeout": validate_timeout(self.timeout_var.get().strip()),
                "vision": bool(self.vision_var.get()),
                "policy_profile": self._current_policy_profile(),
                "document_roots": [str(path) for path in normalize_document_roots(self.document_roots_var.get())],
            }
            if not settings["model"]:
                raise ValueError("모델 이름을 입력하세요.")
        except ValueError as exc:
            messagebox.showerror("설정값 오류", str(exc))
            return
        if not self._ai_job_lock.acquire(blocking=False):
            messagebox.showwarning("AI 실행 중", "현재 AI 계획 작업이 이미 진행 중입니다.")
            return
        self.last_ai_run_id = secrets.token_hex(8)
        append_execution_history(
            "ai_plan_started", self.workflow, run_id=self.last_ai_run_id,
            plan_steps=0, policy_profile=self._current_policy_profile(),
            dry_run=bool(self.dry_run_enabled),
        )
        self.ai_running = True
        self.execute_plan_button.configure(state="disabled")
        self._set_status("화면 관찰 및 로컬 AI 계획 생성 중…")
        threading.Thread(target=self._agent_worker, args=(goal, settings), daemon=True).start()

    def _agent_worker(self, goal: str, settings: Dict[str, Any]) -> None:
        try:
            observation = capture_observation()
            observation["policy_profile"] = str(settings.get("policy_profile", DEFAULT_POLICY_PROFILE))
            observation["document_context"] = build_document_context(settings.get("document_roots", []), goal)
            client = LocalAIClient(
                endpoint=str(settings["endpoint"]),
                model=str(settings["model"]),
                timeout=int(settings["timeout"]),
                vision=bool(settings["vision"]),
            )
            plan = client.make_plan(goal, observation)
            self.after(0, lambda: self._show_ai_result(observation, plan))
        except Exception as exc:
            report_path = self._handle_exception("AI 계획 생성", exc, {"goal_length": len(goal), "policy_profile": settings.get("policy_profile", DEFAULT_POLICY_PROFILE)})
            self.after(0, lambda: self._agent_failed(exc, report_path))
        finally:
            self.ai_running = False
            self._ai_job_lock.release()

    def _agent_failed(self, error: BaseException, report_path: Optional[Path]) -> None:
        self._set_status("AI 계획 생성 실패")
        location = f"\n\n오류 보고서: {report_path}" if report_path else ""
        messagebox.showerror(
            "로컬 AI 연결 실패",
            f"{type(error).__name__}: {str(error)[:1200]}{location}\n\nOllama 또는 LM Studio가 실행 중인지, 설정의 주소와 모델 이름이 맞는지 확인하세요.",
        )

    @staticmethod
    def _pretty_json(data: Any) -> str:
        return json.dumps(data, ensure_ascii=False, indent=2)

    def _set_text(self, widget: tk.Text, text: str) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", text)
        widget.configure(state="disabled")

    def _show_ai_result(self, observation: Dict[str, Any], plan: Dict[str, Any]) -> None:
        self.last_observation = observation
        self.last_plan = plan
        obs_display = {
            "현재 창": observation.get("active_window", ""),
            "애플리케이션 맥락": observation.get("application_context", {}),
            "읽기 전용 어댑터 검증": observation.get("adapter_validation", {}),
            "안전 프로필": observation.get("policy_profile", DEFAULT_POLICY_PROFILE),
            "문서 검색 맥락": observation.get("document_context", {}),
            "화면 크기": observation.get("screen_size", []),
            "캡처 시각": observation.get("captured_at", ""),
            "OCR": observation.get("ocr_text", ""),
            "접근성 컨트롤 수": len(observation.get("ui_controls", [])),
            "접근성 컨트롤": observation.get("ui_controls", [])[:30],
            "UI 요소 수": len(observation.get("elements", [])),
            "UI 요소": observation.get("elements", [])[:40],
            "화면 해시": observation.get("frame_hash", ""),
            "이미지": observation.get("image_path", ""),
        }
        self._set_text(self.observation_text, self._pretty_json(obs_display))
        review = review_plan(plan, self._current_policy_profile())
        plan_display = {"plan": plan, "preflight_review": review}
        self._set_text(self.plan_text, self._pretty_json(plan_display))
        valid, reason = validate_ai_steps(plan, tuple(observation.get("screen_size", [100000, 100000])), observation, self._current_policy_profile())
        if valid and review["ok"] and plan.get("steps"):
            self.execute_plan_button.configure(state="normal")
            self._set_status(
                f"AI 계획 준비 완료 · {review['step_count']}단계 · "
                f"대기 {review['total_wait_seconds']:.1f}/{review['max_total_wait_seconds']:.0f}초 · "
                f"위험도 {plan.get('risk', 'unknown')}"
            )
        else:
            self.execute_plan_button.configure(state="disabled")
            details = " / ".join(review["warnings"][:2]) or reason
            self._set_status(f"AI 계획 검토 필요 · {details}")

    def _execute_ai_plan(self) -> None:
        if not self.last_plan:
            return
        if self.ai_running or not self._ai_job_lock.acquire(blocking=False):
            messagebox.showwarning("AI 실행 중", "다른 AI 작업이 진행 중입니다.")
            return
        audit = verify_execution_history()
        if not audit["valid"]:
            self._ai_job_lock.release()
            messagebox.showerror("감사 무결성 오류", "실행 감사 이력의 무결성 검증에 실패했습니다. 원인 확인 전 AI 계획 실행을 중지합니다.")
            return
        valid, reason = validate_ai_steps(
            self.last_plan,
            tuple(self.last_observation.get("screen_size", [100000, 100000])) if self.last_observation else None,
            self.last_observation,
            self._current_policy_profile(),
        )
        if not valid:
            self._ai_job_lock.release()
            messagebox.showerror("실행할 수 없는 계획", reason)
            return
        steps = self.last_plan.get("steps", [])
        summary = self.last_plan.get("summary", "요약 없음")
        risk = self.last_plan.get("risk", "unknown")
        review = review_plan(self.last_plan, self._current_policy_profile())
        if not review["ok"]:
            self._ai_job_lock.release()
            messagebox.showerror("AI 계획 정책 위반", "\n".join(review["warnings"]) or review["policy_message"])
            return
        action_summary = ", ".join(f"{name} {count}회" for name, count in review["action_counts"].items()) or "없음"
        risk_summary = ", ".join(f"{name} {count}회" for name, count in review["risk_counts"].items()) or "없음"
        preview = "\n".join(f"{i}. {s.get('action')} · {s.get('reason', '')}" for i, s in enumerate(steps, 1))
        confirmation = (
            f"요약: {summary}\n위험도: {risk}\n"
            f"단계: {review['step_count']}개 / 최대 {review['max_steps']}개\n"
            f"동작: {action_summary}\n위험 분류: {risk_summary}\n"
            f"총 대기: {review['total_wait_seconds']:.1f}초 / 최대 {review['max_total_wait_seconds']:.0f}초\n"
            f"결과 확인 문구: {review['expected_check_count']}개\n\n{preview}\n\n실행하시겠습니까?"
        )
        if not messagebox.askyesno("AI 계획 실행 최종 확인", confirmation):
            self._ai_job_lock.release()
            return
        append_execution_history(
            "ai_plan_approved",
            self.workflow,
            risk=str(risk),
            plan_steps=len(steps),
            dry_run=bool(self.dry_run_enabled),
        )
        self.execute_plan_button.configure(state="disabled")
        self.stop_ai_button.configure(state="normal")
        self.ai_stop_event.clear()
        self.ai_running = True
        threading.Thread(target=self._run_ai_steps, args=(steps,), daemon=True).start()

    @staticmethod
    def _paste_text(text: str) -> None:
        if pyautogui is None:
            raise RuntimeError("pyautogui가 설치되어 있지 않습니다.")
        if pyperclip is not None:
            pyperclip.copy(text)
            pyautogui.hotkey("ctrl", "v")
        else:
            pyautogui.write(text, interval=0.01)

    @staticmethod
    def _element_center(element: Dict[str, Any]) -> tuple[int, int]:
        bbox = element.get("bbox") or element.get("rectangle") or []
        if len(bbox) != 4:
            raise RetryableAutomationError("UI 요소 영역이 없습니다.")
        left, top, right, bottom = [int(value) for value in bbox]
        if right <= left or bottom <= top:
            raise RetryableAutomationError("UI 요소 영역이 유효하지 않습니다.")
        return int((left + right) / 2), int((top + bottom) / 2)

    def _verify_ai_result(self, expected_texts: list[str]) -> None:
        expected = [text.strip().lower() for text in expected_texts if text.strip()]
        if not expected or self.dry_run_enabled:
            return
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            if self.ai_stop_event.is_set():
                raise UserInterventionRequired("사용자가 AI 실행을 중지했습니다.")
            observation = capture_observation()
            current_text = " ".join([
                str(observation.get("active_window", "")),
                str(observation.get("ocr_text", "")),
                " ".join(str(item.get("name", "")) for item in observation.get("elements", [])),
            ]).lower()
            if all(item in current_text for item in expected):
                return
            time.sleep(0.4)
        raise RetryableAutomationError(f"실행 후 기대 문구를 확인하지 못했습니다: {expected_texts}")

    def _execute_ai_step_once(self, step: Dict[str, Any], observation: Dict[str, Any]) -> None:
        action = str(step.get("action", "none"))
        if action == "none":
            raise RetryableAutomationError("AI가 실행할 수 있는 동작을 결정하지 못했습니다.")
        if self.dry_run_enabled:
            append_log(f"DRY_RUN 단계 검증: {redact_sensitive(step)}")
            return
        if pyautogui is None:
            raise RuntimeError("pyautogui가 설치되어 있지 않습니다.")
        elements = {str(item.get("id")): item for item in observation.get("elements", [])}
        element = elements.get(str(step.get("element_id"))) if step.get("element_id") else None
        if action in {"click", "double_click", "type"}:
            if element is None:
                raise RetryableAutomationError(f"현재 화면에 element_id가 없습니다: {step.get('element_id')}")
            if not element.get("visible", True) or not element.get("enabled", True):
                raise RetryableAutomationError("UI 요소가 보이지 않거나 비활성 상태입니다.")
            x, y = self._element_center(element)
            pyautogui.moveTo(x, y, duration=0.1)
            if action == "click":
                pyautogui.click()
            elif action == "double_click":
                pyautogui.doubleClick(interval=0.08)
            else:
                pyautogui.click()
                self._paste_text(str(step.get("text", "")))
        elif action == "hotkey":
            keys = [str(key).lower() for key in step.get("keys", [])]
            if not keys:
                raise RetryableAutomationError("hotkey에 키가 없습니다.")
            pyautogui.hotkey(*keys)
        elif action == "scroll":
            pyautogui.scroll(int(step.get("amount", 0)))
        elif action == "wait":
            if self.ai_stop_event.wait(float(step.get("seconds", 0.5))):
                raise UserInterventionRequired("사용자가 AI 실행을 중지했습니다.")

    def _run_ai_steps(self, steps: list[dict[str, Any]]) -> None:
        started_at = time.monotonic()
        stopped = False
        failed_step: Dict[str, Any] = {}
        run_error_type = ""
        outcome = "ai_plan_failed"
        try:
            if pyautogui is None:
                raise RuntimeError("pyautogui가 설치되어 있지 않습니다.")
            pyautogui.PAUSE = 0.08
            pyautogui.FAILSAFE = True
            for index, step in enumerate(steps, 1):
                self.last_ai_index = index
                if self.ai_stop_event.is_set():
                    stopped = True
                    break
                failed_step = step
                last_error: Optional[BaseException] = None
                for attempt in range(1, 4):
                    if self.ai_stop_event.is_set():
                        stopped = True
                        break
                    observation = capture_observation()
                    plan_risk = "high" if step.get("risk") in {"submit", "delete", "unknown"} else ("medium" if step.get("risk") == "write" else "low")
                    valid, reason = validate_ai_steps(
                        {"summary": "single step", "risk": plan_risk, "steps": [step]},
                        tuple(observation.get("screen_size", [100000, 100000])),
                        observation,
                        self._current_policy_profile(),
                    )
                    if not valid:
                        raise RetryableAutomationError(reason)
                    try:
                        self._execute_ai_step_once(step, observation)
                        self._verify_ai_result(step.get("expected_texts", []))
                        last_error = None
                        break
                    except UserInterventionRequired:
                        raise
                    except RetryableAutomationError as exc:
                        last_error = exc
                        if attempt >= 3:
                            raise
                        if step.get("action") in {"type", "hotkey"} or step.get("risk") in {"write", "submit", "delete", "unknown"}:
                            raise UserInterventionRequired("쓰기·제출·삭제 가능 동작은 자동 재시도하지 않습니다.") from exc
                        time.sleep(min(0.6 * (2 ** (attempt - 1)), 4.0))
                if last_error is not None:
                    raise last_error
                if stopped:
                    break
                self._set_status(f"AI 계획 실행 중: {index}/{len(steps)}")
            outcome = "ai_plan_stopped" if stopped else ("ai_plan_dry_run_completed" if self.dry_run_enabled else "ai_plan_completed")
            self._set_status("AI 계획 사용자 중지" if stopped else ("AI 계획 검증 완료(dry-run)" if self.dry_run_enabled else "AI 계획 실행 완료"))
        except Exception as exc:
            run_error_type = type(exc).__name__
            report_path = self._handle_exception("AI 계획 실행", exc, {"failed_step": self.last_ai_index, "step": failed_step})
            self.after(0, lambda: self._show_exception_dialog("AI 계획 실행", exc, report_path))
        finally:
            duration = round(time.monotonic() - started_at, 3)
            run_id = self.last_ai_run_id or secrets.token_hex(8)
            self.last_ai_run_id = run_id
            report_path = write_execution_report(
                run_id, outcome, self.workflow,
                mode="ai_plan", start_step=1, last_step=self.last_ai_index,
                duration_seconds=duration, error_type=run_error_type,
                policy_profile=self._current_policy_profile(),
            )
            append_execution_history(
                outcome, self.workflow, last_step=self.last_ai_index,
                dry_run=bool(self.dry_run_enabled), run_id=run_id,
                duration_seconds=duration, report_path=str(report_path) if report_path else "",
            )
            self.ai_running = False
            self._ai_job_lock.release()
            self.after(0, lambda: self.execute_plan_button.configure(state="normal"))
            self.after(0, self._refresh_history_view)
            self.after(0, lambda: self.stop_ai_button.configure(state="disabled"))

    def _load_config(self) -> None:
        defaults = {"endpoint": "http://127.0.0.1:11434/v1", "model": "gemma4:e2b", "timeout": 120, "vision": False, "capture_on_error": True, "dry_run": True, "schedule_interval": 3600, "policy_profile": DEFAULT_POLICY_PROFILE, "document_roots": []}
        data: Dict[str, Any] = {}
        config_file_invalid = False
        try:
            if CONFIG_PATH.exists():
                if CONFIG_PATH.stat().st_size > MAX_CONFIG_FILE_BYTES:
                    config_file_invalid = True
                else:
                    loaded = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        data = loaded
                    else:
                        config_file_invalid = True
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
            config_file_invalid = True
        self._config_data, reset_fields = validate_runtime_config(data, defaults)
        if config_file_invalid:
            reset_fields.insert(0, "file")
        if reset_fields:
            append_log(f"설정 검증으로 안전 기본값 복원: {', '.join(reset_fields[:10])}", "WARNING")
        self.dry_run_enabled = bool(self._config_data.get("dry_run", True))
        self.capture_on_error_enabled = bool(self._config_data.get("capture_on_error", True))

    def _save_config(self) -> None:
        try:
            endpoint = validate_local_endpoint(self.endpoint_var.get().strip())
            timeout = validate_timeout(self.timeout_var.get().strip())
            model = self.model_var.get().strip()
            if not model or len(model) > 200:
                raise ValueError("모델 이름은 1~200자로 입력하세요.")
            data = {
                "endpoint": endpoint,
                "model": model,
                "timeout": timeout,
                "vision": bool(self.vision_var.get()),
                "capture_on_error": bool(self.capture_on_error_var.get()),
                "dry_run": bool(self.dry_run_var.get()),
                "schedule_interval": validate_schedule_interval(self.schedule_interval_var.get().strip()),
                "policy_profile": self._current_policy_profile(),
                "document_roots": [str(path) for path in normalize_document_roots(self.document_roots_var.get())],
            }
            temp_path = CONFIG_PATH.with_suffix(".tmp")
            temp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            temp_path.replace(CONFIG_PATH)
            self._set_status("로컬 AI 설정 저장 완료")
        except Exception as exc:
            messagebox.showerror("설정 저장 실패", str(exc))

    def _check_ai_connection(self) -> None:
        try:
            client = LocalAIClient(
                endpoint=self.endpoint_var.get().strip(),
                model=self.model_var.get().strip(),
                timeout=validate_timeout(self.timeout_var.get().strip()),
                vision=bool(self.vision_var.get()),
            )
            self.connection_status_var.set("연결 점검 중...")
            threading.Thread(target=self._check_ai_connection_worker, args=(client,), daemon=True, name="ai-health-check").start()
        except Exception as exc:
            self.connection_status_var.set("설정 오류")
            messagebox.showerror("연결 점검 실패", str(exc))

    def _check_ai_connection_worker(self, client: LocalAIClient) -> None:
        try:
            result = client.health_check()
            self.after(0, lambda: self._show_ai_connection_result(client.model, result))
        except Exception as exc:
            self.after(0, lambda: self._show_ai_connection_error(exc))

    def _show_ai_connection_result(self, model: str, result: Dict[str, Any]) -> None:
        models = result.get("models", [])
        self.connection_status_var.set(f"연결됨 · 모델 {len(models)}개")
        model_note = f"\n현재 모델 '{model}'이 목록에 없습니다." if models and model not in models else ""
        messagebox.showinfo("로컬 AI 연결 확인", f"서버 연결 성공\nHTTP 상태: {result.get('status_code')}\n모델 수: {len(models)}{model_note}")

    def _show_ai_connection_error(self, exc: BaseException) -> None:
        self.connection_status_var.set("연결 실패")
        messagebox.showerror("로컬 AI 연결 실패", str(exc))

    def _start_scheduler(self) -> None:
        try:
            interval = validate_schedule_interval(self.schedule_interval_var.get().strip())
            health = evaluate_scheduler_health()
            circuit_override = False
            if health["circuit_open"]:
                approved = messagebox.askyesno(
                    "예약 실행 회로차단",
                    f"최근 예약 실행이 {health['consecutive_failures']}회 연속 실패해 자동 중지되었습니다.\\n"
                    "오류 리포트와 현재 화면을 확인했습니까? 확인된 경우에만 1회 재시작을 승인할 수 있습니다.",
                )
                if not approved:
                    append_execution_history(
                        "scheduler_restart_denied", self.workflow,
                        consecutive_failures=health["consecutive_failures"],
                    )
                    self._set_status("예약 실행 재시작 대기 · 원인 확인 필요")
                    return
                circuit_override = True
            self.scheduler.start(interval)
            self._scheduler_circuit_override = circuit_override
            self.schedule_status_var.set(f"예약 실행 중 · {interval}초 간격")
            append_execution_history(
                "scheduler_restart_approved" if circuit_override else "scheduler_started",
                self.workflow,
                interval_seconds=interval,
                consecutive_failures=health["consecutive_failures"] if circuit_override else 0,
            )
            self._set_status(f"예약 실행 시작 · {interval}초 간격")
        except Exception as exc:
            self._scheduler_circuit_override = False
            messagebox.showerror("예약 실행 시작 실패", str(exc))

    def _stop_scheduler(self) -> None:
        self._scheduler_circuit_override = False
        self.scheduler.stop()
        if hasattr(self, "schedule_status_var"):
            self.schedule_status_var.set("예약 실행 중지")
        append_execution_history("scheduler_stopped", self.workflow)
        self._set_status("예약 실행 중지")

    def _scheduled_run(self) -> None:
        """Queue a scheduled playback check on Tk's main thread."""
        try:
            self.after(0, self._launch_scheduled_playback)
        except tk.TclError:
            return

    def _launch_scheduled_playback(self) -> None:
        if self.recording or self.player.running or not self.workflow.steps:
            append_execution_history("scheduled_run_skipped", self.workflow, reason="busy_or_empty")
            return
        audit = verify_execution_history()
        if not audit["valid"]:
            self._scheduler_circuit_override = False
            self.scheduler.stop()
            append_execution_history("scheduled_run_skipped", self.workflow, reason="audit_integrity_failure")
            self.schedule_status_var.set("예약 실행 자동 중지 · 감사 이력 무결성 오류")
            self._set_status("예약 실행 자동 중지 · 감사 이력 무결성 오류")
            return
        scheduler_health = evaluate_scheduler_health()
        if scheduler_health["circuit_open"]:
            if self._scheduler_circuit_override:
                self._scheduler_circuit_override = False
                append_execution_history(
                    "scheduler_circuit_override", self.workflow,
                    consecutive_failures=scheduler_health["consecutive_failures"],
                )
            else:
                append_execution_history("scheduler_circuit_open", self.workflow, consecutive_failures=scheduler_health["consecutive_failures"])
                self.scheduler.stop()
                self.schedule_status_var.set("예약 실행 자동 중지 · 반복 실패")
                self._set_status("예약 실행 자동 중지 · 반복 실패")
                return
        valid, reason = validate_workflow(self.workflow, get_screen_size())
        if not valid:
            append_execution_history("scheduled_run_skipped", self.workflow, reason="invalid_workflow")
            self._set_status(f"예약 실행 건너뜀 · {reason}")
            return
        if not self._playback_launch_lock.acquire(blocking=False):
            append_execution_history("scheduled_run_skipped", self.workflow, reason="playback_locked")
            return
        self.step_mode_enabled = False
        self._step_gate.clear()
        self.pause_play_button.configure(state="normal", text="재생 일시정지")
        append_execution_history("scheduled_run_approved", self.workflow)
        threading.Thread(target=self._play_workflow_worker, args=(self.workflow, 0, True), daemon=True, name="scheduled-playback").start()

    def _on_close(self) -> None:
        self.scheduler.stop()
        self.recorder.stop()
        self.player.stop()
        self.ai_stop_event.set()
        if self.hotkey_listener is not None:
            try:
                self.hotkey_listener.stop()
            except Exception:
                pass
        try:
            self._refresh_monitor_view(status_override="closing")
        except Exception:
            pass
        self._save_config()
        self.destroy()


if __name__ == "__main__":
    app = AutoWorkAgent()
    app.mainloop()
