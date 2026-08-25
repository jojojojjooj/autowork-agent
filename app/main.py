from __future__ import annotations

import json
import os
import threading
import time
import tkinter as tk
import traceback
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any, Dict, Optional

from engine import (
    APP_DIR,
    CONFIG_PATH,
    DEBUG_DIR,
    ERROR_DIR,
    LOG_PATH,
    WORKFLOW_DIR,
    InputRecorder,
    LocalAIClient,
    RetryableAutomationError,
    UserInterventionRequired,
    Step,
    Workflow,
    get_screen_size,
    WorkflowPlayer,
    append_log,
    capture_observation,
    ensure_app_dirs,
    write_error_report,
    load_workflow,
    save_workflow,
    validate_ai_steps,
    validate_local_endpoint,
    validate_timeout,
)

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
        self.last_ai_index = 0
        self.dry_run_enabled = True
        self.capture_on_error_enabled = True

        self.recorder = InputRecorder(on_step=self._on_recorded_step, on_error=self._on_recorder_error)
        self.player = WorkflowPlayer(on_step=self._on_play_step, on_status=self._set_status, on_error=self._on_player_error)

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
        ttk.Button(goal_frame, text="화면 관찰 후 계획 생성", command=self._make_ai_plan, style="Primary.TButton").pack(anchor="e", pady=(8, 0))

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
        ttk.Button(card, text="설정 저장", command=self._save_config).grid(row=5, column=1, sticky="w", pady=(10, 0))
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
        ttk.Label(self.debug_tab, textvariable=self.last_action_var, style="Sub.TLabel").pack(anchor="w", pady=(0, 8))
        log_frame = ttk.LabelFrame(self.debug_tab, text="최근 로그", style="Card.TLabelframe")
        log_frame.pack(fill="both", expand=True)
        self.log_text = tk.Text(log_frame, wrap="word", state="disabled", font=("Consolas", 9))
        self.log_text.pack(side="left", fill="both", expand=True)
        log_scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scroll.set)
        log_scroll.pack(side="right", fill="y")
        self._refresh_log_view()

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
        report_path = self._handle_exception("작업 재생", exc, {"failed_step": index, "step": step.__dict__})
        self.after(0, lambda: self._show_exception_dialog("작업 재생", exc, report_path))

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

    def _delete_selected_step(self) -> None:
        if self.recording or self.player.running:
            messagebox.showwarning("작업 실행 중", "기록 또는 재생 중에는 단계를 삭제할 수 없습니다.")
            return
        selection = self.step_tree.selection()
        if not selection:
            messagebox.showinfo("단계 선택 필요", "삭제할 단계를 먼저 선택하세요.")
            return
        item = selection[0]
        try:
            index = int(self.step_tree.item(item, "values")[0]) - 1
            removed = self.workflow.remove_step(index)
        except (IndexError, TypeError, ValueError) as exc:
            messagebox.showerror("단계 삭제 실패", str(exc))
            return
        self._refresh_steps()
        self._set_status(f"단계 삭제 완료 · {removed.type} · 남은 단계 {len(self.workflow.steps)}개")

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
        if hasattr(self, "pause_play_button"):
            self.pause_play_button.configure(state="disabled", text="재생 일시정지")
        self._set_status("재생 중지 요청")

    def _stop_ai_plan(self) -> None:
        self.ai_stop_event.set()
        self._set_status("AI 실행 중지 요청")

    def _on_play_step(self, index: int, step: Step) -> None:
        self.after(0, lambda: self.step_tree.selection_set(self.step_tree.get_children()[index - 1]))

    def _play_workflow(self) -> None:
        if self.recording:
            messagebox.showwarning("기록 중", "기록을 먼저 중지하세요.")
            return
        if not self.workflow.steps:
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
                return
        ok = messagebox.askyesno(
            "재생 확인",
            "저장된 모든 마우스·키보드 동작을 현재 화면에서 실행합니다.\n\n"            "민감한 정보 입력이나 문서 전송이 포함되어 있지 않은지 확인했습니까?",
        )
        if not ok:
            return
        self.pause_play_button.configure(state="normal", text="재생 일시정지")
        threading.Thread(target=self._play_workflow_worker, daemon=True).start()

    def _play_workflow_worker(self) -> None:
        try:
            self.player.play(self.workflow)
        finally:
            self.after(0, lambda: self.pause_play_button.configure(state="disabled", text="재생 일시정지"))

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
            save_workflow(self.workflow, Path(path))
            self.current_path = Path(path)
            self._set_status(f"저장 완료 · {self.current_path.name}")
        except Exception as exc:
            messagebox.showerror("저장 실패", str(exc))

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
            self._set_status(f"불러오기 완료 · {self.current_path.name}")
        except Exception as exc:
            messagebox.showerror("불러오기 실패", str(exc))

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
            }
            if not settings["model"]:
                raise ValueError("모델 이름을 입력하세요.")
        except ValueError as exc:
            messagebox.showerror("설정값 오류", str(exc))
            return
        self.execute_plan_button.configure(state="disabled")
        self._set_status("화면 관찰 및 로컬 AI 계획 생성 중…")
        threading.Thread(target=self._agent_worker, args=(goal, settings), daemon=True).start()

    def _agent_worker(self, goal: str, settings: Dict[str, Any]) -> None:
        try:
            observation = capture_observation()
            client = LocalAIClient(
                endpoint=str(settings["endpoint"]),
                model=str(settings["model"]),
                timeout=int(settings["timeout"]),
                vision=bool(settings["vision"]),
            )
            plan = client.make_plan(goal, observation)
            self.after(0, lambda: self._show_ai_result(observation, plan))
        except Exception as exc:
            report_path = self._handle_exception("AI 계획 생성", exc, {"goal_length": len(goal)})
            self.after(0, lambda: self._agent_failed(exc, report_path))

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
        self._set_text(self.plan_text, self._pretty_json(plan))
        valid, reason = validate_ai_steps(plan, tuple(observation.get("screen_size", [100000, 100000])), observation)
        if valid and plan.get("steps"):
            self.execute_plan_button.configure(state="normal")
            self._set_status(f"AI 계획 준비 완료 · 위험도 {plan.get('risk', 'unknown')}")
        else:
            self.execute_plan_button.configure(state="disabled")
            self._set_status(f"AI 계획 검토 필요 · {reason}")

    def _execute_ai_plan(self) -> None:
        if not self.last_plan:
            return
        valid, reason = validate_ai_steps(
            self.last_plan,
            tuple(self.last_observation.get("screen_size", [100000, 100000])) if self.last_observation else None,
            self.last_observation,
        )
        if not valid:
            messagebox.showerror("실행할 수 없는 계획", reason)
            return
        steps = self.last_plan.get("steps", [])
        summary = self.last_plan.get("summary", "요약 없음")
        risk = self.last_plan.get("risk", "unknown")
        preview = "\n".join(f"{i}. {s.get('action')} · {s.get('reason', '')}" for i, s in enumerate(steps, 1))
        if not messagebox.askyesno("AI 계획 실행 최종 확인", f"요약: {summary}\n위험도: {risk}\n\n{preview}\n\n실행하시겠습니까?"):
            return
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
            append_log(f"DRY_RUN 단계 검증: {step}")
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
        stopped = False
        failed_step: Dict[str, Any] = {}
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
                        time.sleep(min(0.6 * (2 ** (attempt - 1)), 4.0))
                if last_error is not None:
                    raise last_error
                if stopped:
                    break
                self._set_status(f"AI 계획 실행 중: {index}/{len(steps)}")
            self._set_status("AI 계획 사용자 중지" if stopped else ("AI 계획 검증 완료(dry-run)" if self.dry_run_enabled else "AI 계획 실행 완료"))
        except Exception as exc:
            report_path = self._handle_exception("AI 계획 실행", exc, {"failed_step": self.last_ai_index, "step": failed_step})
            self.after(0, lambda: self._show_exception_dialog("AI 계획 실행", exc, report_path))
        finally:
            self.ai_running = False
            self.after(0, lambda: self.execute_plan_button.configure(state="normal"))
            self.after(0, lambda: self.stop_ai_button.configure(state="disabled"))

    def _load_config(self) -> None:
        defaults = {"endpoint": "http://127.0.0.1:11434/v1", "model": "gemma4:e2b", "timeout": "120", "vision": False, "capture_on_error": True}
        data: Dict[str, Any] = {}
        try:
            if CONFIG_PATH.exists():
                data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        self._config_data = {**defaults, **data}
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
            }
            CONFIG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            self._set_status("로컬 AI 설정 저장 완료")
        except Exception as exc:
            messagebox.showerror("설정 저장 실패", str(exc))

    def _on_close(self) -> None:
        self.recorder.stop()
        self.player.stop()
        self.ai_stop_event.set()
        if self.hotkey_listener is not None:
            try:
                self.hotkey_listener.stop()
            except Exception:
                pass
        self._save_config()
        self.destroy()


if __name__ == "__main__":
    app = AutoWorkAgent()
    app.mainloop()
