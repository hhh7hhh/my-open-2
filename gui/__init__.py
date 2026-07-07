"""
日常小帮手 — GUI 前端

支持 PySide6（优先）或 tkinter 作为后备。
如果两个都不可用，仅提供 CLI 模式。
"""
import os
import sys
import json
import logging
import threading
from typing import Optional

import yaml

from adapters import OkwwAdapter, M7aAdapter, BaseAdapter, TaskDef, TaskResult, TaskStatus
from core import Scheduler, DeepSeekClient, BattleReporter, Guardian

logger = logging.getLogger("gui")

# ============================================================
# 尝试导入 PySide6，失败则回退到 tkinter
# ============================================================
GUI_ENGINE = None  # "pyside6" or "tkinter"

try:
    from PySide6.QtWidgets import (
        QApplication, QMainWindow, QVBoxLayout, QHBoxLayout,
        QWidget, QCheckBox, QTextBrowser, QPushButton,
        QSystemTrayIcon, QMenu, QStackedWidget, QScrollArea,
        QGroupBox, QLabel, QLineEdit, QMessageBox,
    )
    from PySide6.QtCore import Qt, Signal, QObject
    from PySide6.QtGui import QAction, QFont
    GUI_ENGINE = "pyside6"
except ImportError:
    pass

if GUI_ENGINE is None:
    try:
        import tkinter as tk
        from tkinter import ttk, scrolledtext
        GUI_ENGINE = "tkinter"
    except ImportError:
        pass


# ============================================================
# 信号桥 (跨线程通信)
# ============================================================
class _Signals:
    """纯 Python 信号桥，用于 tkinter 模式跨线程通信"""

    def __init__(self):
        self._callbacks: dict[str, list] = {}

    def connect(self, event: str, cb):
        self._callbacks.setdefault(event, []).append(cb)

    def emit(self, event: str, *args):
        for cb in self._callbacks.get(event, []):
            cb(*args)


# ============================================================
# 辅助函数 (模块级，两种引擎共用)
# ============================================================

def _default_config_path() -> str:
    # PyInstaller 打包后 __file__ 指向临时目录，用 sys._MEIPASS 找嵌入资源
    if getattr(sys, 'frozen', False):
        base = sys._MEIPASS
    else:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "config", "tools.yaml")


def _user_data_dir() -> str:
    """返回用户持久化数据目录，exe 模式下也不会丢失"""
    # PyInstaller 打包后 __file__ 可能指向临时目录，
    # 改用 %APPDATA% 确保跨重启持久化
    base = os.environ.get("APPDATA", os.path.expanduser("~"))
    d = os.path.join(base, "日常小帮手")
    os.makedirs(d, exist_ok=True)
    return d


def _load_api_key(ds_cfg: dict) -> str:
    """加载 API Key：优先用户目录 settings.json，回退到 tools.yaml"""
    settings_path = os.path.join(_user_data_dir(), "settings.json")
    try:
        if os.path.exists(settings_path):
            with open(settings_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                key = data.get("api_key", "")
                if key:
                    return key
    except Exception:
        pass
    return ds_cfg.get("api_key", "")


def _save_api_key_to_file(new_key: str):
    """持久化保存 API Key 到用户目录"""
    settings_path = os.path.join(_user_data_dir(), "settings.json")
    data = {}
    try:
        if os.path.exists(settings_path):
            with open(settings_path, "r", encoding="utf-8") as f:
                data = json.load(f)
    except Exception:
        pass
    data["api_key"] = new_key
    with open(settings_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _make_adapter(tool_cfg: dict) -> Optional[BaseAdapter]:
    adapter_name = tool_cfg.get("adapter", "")
    if adapter_name == "okww_adapter":
        return OkwwAdapter()
    elif adapter_name == "m7a_adapter":
        return M7aAdapter()
    return None


# ============================================================
# PySide6 实现
# ============================================================
if GUI_ENGINE == "pyside6":

    class _QtSignals(QObject):
        """Qt Signal 版本的信号桥"""
        task_started = Signal(str)
        task_finished = Signal(object)
        task_log = Signal(str)
        all_finished = Signal(list)
        error = Signal(str)

    # ---- PySide6 样式函数 ----
    def _ps_bold_font() -> QFont:
        f = QFont()
        f.setPointSize(12)
        f.setBold(True)
        return f

    def _ps_btn_style() -> str:
        return """
            QPushButton { background: #e8e8e8; border: 1px solid #ccc;
                          border-radius: 6px; padding: 6px 12px; }
            QPushButton:checked { background: #0078d4; color: white;
                                  border-color: #0078d4; }
            QPushButton:hover { background: #d0d0d0; }
            QPushButton:checked:hover { background: #106ebe; }
        """

    def _ps_primary_btn_style() -> str:
        return """
            QPushButton { background: #0078d4; color: white; border: none;
                          border-radius: 6px; padding: 8px 20px; font-weight: bold; }
            QPushButton:hover { background: #106ebe; }
            QPushButton:disabled { background: #a0a0a0; }
        """

    def _ps_group_style() -> str:
        return """
            QGroupBox { border: 1px solid #ddd; border-radius: 8px;
                        margin-top: 8px; padding-top: 16px; font-weight: bold; }
            QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 6px; }
        """

    class MainWindow(QMainWindow):
        def __init__(self, config_path: str = ""):
            super().__init__()
            self.setWindowTitle("日常小帮手 v1.0")
            self.resize(960, 640)

            if not config_path:
                config_path = _default_config_path()
            with open(config_path, "r", encoding="utf-8") as f:
                config = yaml.safe_load(f)

            self._tools_cfg = config.get("tools", [])
            self._settings = config.get("settings", {})
            self._ds_cfg = config.get("deepseek", {})

            self._adapters: dict[str, BaseAdapter] = {}
            self._task_widgets: dict[str, QCheckBox] = {}

            self._reporter = BattleReporter()
            saved_key = _load_api_key(self._ds_cfg)
            self._deepseek = DeepSeekClient(
                api_key=saved_key,
                base_url=self._ds_cfg.get("base_url", "https://api.deepseek.com"),
                model=self._ds_cfg.get("model", "deepseek-chat"),
            )

            self._signals = _QtSignals()
            self._signals.task_started.connect(self._on_task_started)
            self._signals.task_finished.connect(self._on_task_finished)
            self._signals.task_log.connect(self._on_log)
            self._signals.all_finished.connect(self._on_all_finished)
            self._signals.error.connect(self._on_error)

            self._run_thread: Optional[threading.Thread] = None
            self._running = False

            self._build_ui()
            self._load_adapters()

            # 系统托盘
            self._tray = QSystemTrayIcon(self)
            self._tray.setToolTip("日常小帮手")
            tray_menu = QMenu()
            show_action = QAction("显示主窗口", self)
            show_action.triggered.connect(self.show)
            quit_action = QAction("退出", self)
            quit_action.triggered.connect(self._quit)
            tray_menu.addAction(show_action)
            tray_menu.addAction(quit_action)
            self._tray.setContextMenu(tray_menu)
            self._tray.show()

            logger.info("GUI initialized (PySide6)")

        # ---- UI 构建 ----
        def _build_ui(self):
            central = QWidget()
            self.setCentralWidget(central)
            main_layout = QHBoxLayout(central)
            main_layout.setContentsMargins(8, 8, 8, 8)

            # 左侧导航
            nav_widget = QWidget()
            nav_widget.setFixedWidth(140)
            nav_layout = QVBoxLayout(nav_widget)
            nav_layout.setContentsMargins(4, 4, 4, 4)

            title_label = QLabel("🧰 日常小帮手")
            title_label.setAlignment(Qt.AlignCenter)
            title_font = QFont()
            title_font.setPointSize(11)
            title_font.setBold(True)
            title_label.setFont(title_font)
            nav_layout.addWidget(title_label)
            nav_layout.addSpacing(16)

            self._btn_tasks = QPushButton("📋 任务列表")
            self._btn_tasks.setCheckable(True)
            self._btn_tasks.setChecked(True)
            self._btn_tasks.clicked.connect(lambda: self._stack.setCurrentIndex(0))
            self._btn_report = QPushButton("📊 历史战报")
            self._btn_report.setCheckable(True)
            self._btn_report.clicked.connect(lambda: self._stack.setCurrentIndex(1))

            for btn in [self._btn_tasks, self._btn_report]:
                btn.setMinimumHeight(36)
                btn.setStyleSheet(_ps_btn_style())

            nav_layout.addWidget(self._btn_tasks)
            nav_layout.addWidget(self._btn_report)
            nav_layout.addStretch()

            # 互斥导航
            self._btn_tasks.clicked.connect(
                lambda: (self._btn_tasks.setChecked(True),
                         self._btn_report.setChecked(False))
            )
            self._btn_report.clicked.connect(
                lambda: (self._btn_report.setChecked(True),
                         self._btn_tasks.setChecked(False))
            )
            main_layout.addWidget(nav_widget)

            # 右侧堆叠
            self._stack = QStackedWidget()
            self._task_page = self._create_task_page()
            self._report_page = self._create_report_page()
            self._stack.addWidget(self._task_page)
            self._stack.addWidget(self._report_page)
            main_layout.addWidget(self._stack)

        def _create_task_page(self) -> QWidget:
            page = QWidget()
            layout = QVBoxLayout(page)
            layout.setContentsMargins(8, 8, 8, 8)

            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setStyleSheet(
                "QScrollArea { border: 1px solid #ccc; border-radius: 6px; }"
            )
            self._task_container = QWidget()
            self._task_layout = QVBoxLayout(self._task_container)
            self._task_layout.setAlignment(Qt.AlignTop)
            self._task_layout.setSpacing(8)
            scroll.setWidget(self._task_container)
            layout.addWidget(scroll, 1)

            # 按钮栏
            btn_layout = QHBoxLayout()
            btn_layout.addStretch()
            self._start_btn = QPushButton("▶ 开始执行")
            self._start_btn.setMinimumHeight(36)
            self._start_btn.setStyleSheet(_ps_primary_btn_style())
            self._start_btn.clicked.connect(self._on_start)
            self._stop_btn = QPushButton("⏹ 停止")
            self._stop_btn.setMinimumHeight(36)
            self._stop_btn.setEnabled(False)
            self._stop_btn.clicked.connect(self._on_stop)
            btn_layout.addWidget(self._start_btn)
            btn_layout.addWidget(self._stop_btn)
            layout.addLayout(btn_layout)

            # 日志区
            log_label = QLabel("执行日志:")
            log_label.setFont(_ps_bold_font())
            layout.addWidget(log_label)
            self._log_browser = QTextBrowser()
            self._log_browser.setMaximumHeight(180)
            self._log_browser.setStyleSheet(
                "QTextBrowser { font-family: Consolas, monospace; font-size: 12px; }"
            )
            layout.addWidget(self._log_browser)
            return page

        def _create_report_page(self) -> QWidget:
            page = QWidget()
            layout = QVBoxLayout(page)
            layout.setContentsMargins(8, 8, 8, 8)

            title = QLabel("历史战报")
            title.setFont(_ps_bold_font())
            layout.addWidget(title)

            self._report_browser = QTextBrowser()
            self._report_browser.setStyleSheet(
                "QTextBrowser { font-family: Consolas, monospace; font-size: 12px; }"
            )
            layout.addWidget(self._report_browser, 1)

            btn_layout = QHBoxLayout()
            btn_layout.addStretch()
            refresh_btn = QPushButton("🔄 刷新")
            refresh_btn.setMinimumHeight(36)
            refresh_btn.clicked.connect(self._refresh_reports)
            btn_layout.addWidget(refresh_btn)
            layout.addLayout(btn_layout)
            return page

        # ---- 适配器加载 ----
        def _create_apikey_group(self) -> QGroupBox:
            group = QGroupBox("🤖 DeepSeek AI 配置 (可选)")
            group.setStyleSheet(_ps_group_style())
            gl = QHBoxLayout(group)

            gl.addWidget(QLabel("API Key:"))
            self._apikey_input = QLineEdit()
            self._apikey_input.setEchoMode(QLineEdit.Password)
            self._apikey_input.setPlaceholderText("sk-xxxxxxxxxxxx")
            saved_key = _load_api_key(self._ds_cfg)
            if saved_key:
                self._apikey_input.setText(saved_key)
            gl.addWidget(self._apikey_input, 1)

            self._apikey_save_btn = QPushButton("💾 保存")
            self._apikey_save_btn.setMinimumHeight(30)
            self._apikey_save_btn.clicked.connect(self._save_api_key)
            gl.addWidget(self._apikey_save_btn)

            return group

        def _save_api_key(self):
            new_key = self._apikey_input.text().strip()

            # 持久化到用户目录，exe 模式下重启不丢失
            _save_api_key_to_file(new_key)

            self._ds_cfg["api_key"] = new_key
            self._deepseek = DeepSeekClient(
                api_key=new_key,
                base_url=self._ds_cfg.get("base_url", "https://api.deepseek.com"),
                model=self._ds_cfg.get("model", "deepseek-chat"),
            )

            if new_key:
                QMessageBox.information(self, "保存成功", "API Key 已保存，AI 诊断已启用！")
                self._apikey_save_btn.setText("✅ 已保存")
            else:
                QMessageBox.information(self, "已清空", "API Key 已清空，AI 诊断将不可用。")
                self._apikey_save_btn.setText("💾 保存")

        def _load_adapters(self):
            # 顶层放 API Key 配置
            self._task_layout.addWidget(self._create_apikey_group())

            for tool_cfg in self._tools_cfg:
                adapter = _make_adapter(tool_cfg)
                if adapter is None:
                    continue
                adapter.load_config(tool_cfg)
                self._adapters[tool_cfg["name"]] = adapter

                group = QGroupBox(tool_cfg["name"])
                group.setStyleSheet(_ps_group_style())
                gl = QVBoxLayout(group)

                for task in tool_cfg.get("tasks", []):
                    cb = QCheckBox(f"{task['name']} — {task.get('desc', '')}")
                    cb.setChecked(task.get("enabled", False))
                    cb.setEnabled(tool_cfg.get("enabled", True))
                    key = f"{tool_cfg['name']}::{task['id']}"
                    self._task_widgets[key] = cb
                    gl.addWidget(cb)

                self._task_layout.addWidget(group)

        # ---- 运行控制 ----
        def _on_start(self):
            if self._running:
                return
            selected_tasks = self._collect_selected()
            if not selected_tasks:
                self._signals.task_log.emit("[系统] 请至少选择一个任务")
                return
            self._running = True
            self._start_btn.setEnabled(False)
            self._stop_btn.setEnabled(True)
            self._log_browser.clear()
            self._run_thread = threading.Thread(
                target=self._run_tasks, args=(selected_tasks,), daemon=True,
            )
            self._run_thread.start()

        def _collect_selected(self):
            selected = []
            for tool_cfg in self._tools_cfg:
                name = tool_cfg["name"]
                adapter = self._adapters.get(name)
                if not adapter or not tool_cfg.get("enabled", True):
                    continue
                for task in tool_cfg.get("tasks", []):
                    key = f"{name}::{task['id']}"
                    cb = self._task_widgets.get(key)
                    if cb is None or not cb.isChecked():
                        continue
                    td = TaskDef(
                        task_id=task["id"], task_name=task["name"],
                        args=task.get("args", ""), desc=task.get("desc", ""),
                        eta=task.get("eta", 30),
                    )
                    selected.append((name, td, adapter))
            return selected

        def _run_tasks(self, selected_tasks):
            results = []
            max_restarts = self._settings.get("max_restart_count", 3)
            interval = self._settings.get("task_interval_seconds", 3)
            default_timeout = self._settings.get("default_timeout_minutes", 60) * 60

            guardian = Guardian(
                cpu_dead_threshold=self._settings.get("cpu_dead_threshold", 0.0),
                cpu_dead_duration=self._settings.get("cpu_dead_duration", 10),
                hung_check_interval=self._settings.get("hung_check_interval", 5),
            )
            scheduler = Scheduler(guardian=guardian)

            for tool_name, td, adapter in selected_tasks:
                if not self._running:
                    break
                self._signals.task_started.emit(f"{tool_name} - {td.task_name}")
                cmd = adapter.build_command(td)
                self._signals.task_log.emit(f"[CMD] {' '.join(cmd)}")
                result = scheduler.run(
                    cmd,
                    task_id=f"{tool_name}::{td.task_id}",
                    task_name=f"{tool_name} - {td.task_name}",
                    tool_name=tool_name,
                    timeout=default_timeout,
                    max_restarts=max_restarts,
                    cwd=os.path.dirname(adapter.exe_path),
                    game_exe=adapter.game_exe,
                    needs_window=adapter.needs_window,
                    daemon_launcher=adapter.daemon_launcher,
                )
                result.task_name = td.task_name
                result.tool_name = tool_name
                results.append(result)
                self._signals.task_finished.emit(result)
                if interval > 0:
                    import time
                    time.sleep(interval)

            if results:
                report_path = self._reporter.save(results)
                self._signals.task_log.emit(f"[战报] 已保存至 {report_path}")
            self._signals.all_finished.emit(results)

        def _on_stop(self):
            self._running = False
            self._start_btn.setEnabled(True)
            self._stop_btn.setEnabled(False)
            self._signals.task_log.emit("[系统] 用户请求停止...")

        # ---- 信号回调 ----
        def _on_task_started(self, label: str):
            self._log_browser.append(f"▶ 开始: {label}")

        def _on_task_finished(self, result: TaskResult):
            icon = "✅" if result.status == TaskStatus.SUCCESS else "❌"
            self._log_browser.append(
                f"{icon} 完成: {result.tool_name} - {result.task_name} | "
                f"状态: {result.status.value} | 重启: {result.restart_count}次"
            )
            if result.error_message:
                self._log_browser.append(f"   错误: {result.error_message[:200]}")

        def _on_log(self, line: str):
            self._log_browser.append(line)

        def _on_all_finished(self, results):
            self._running = False
            self._start_btn.setEnabled(True)
            self._stop_btn.setEnabled(False)
            total = len(results)
            success = sum(1 for r in results if r.status == TaskStatus.SUCCESS)
            self._log_browser.append(
                f"\n{'='*50}\n全部完成: {success}/{total} 成功\n{'='*50}"
            )

        def _on_error(self, msg: str):
            self._log_browser.append(f"❗ 错误: {msg}")

        def _refresh_reports(self):
            self._report_browser.clear()
            reports = self._reporter.list_reports()
            if not reports:
                self._report_browser.append("暂无战报记录")
                return
            for report in reports:
                ts = report.get("timestamp", "")
                s = report.get("summary", {})
                self._report_browser.append(
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"[{ts}] 总任务: {s.get('total', 0)} | "
                    f"成功: {s.get('success', 0)} | "
                    f"失败: {s.get('failed', 0)} | "
                    f"成功率: {s.get('success_rate', '0%')}"
                )
                for t in report.get("tasks", []):
                    self._report_browser.append(
                        f"  • {t.get('tool', '')} / {t.get('task_name', '')} : "
                        f"{t.get('status', '')} ({t.get('duration_seconds', 0)}s)"
                    )

        # ---- 窗口事件 ----
        def closeEvent(self, event):
            reply = QMessageBox.question(
                self, "日常小帮手",
                "请选择：\n\n点击「最小化」按钮 → 隐藏到系统托盘继续运行\n点击「退出」按钮 → 停止所有任务并退出程序",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.Yes,
            )
            if reply == QMessageBox.Yes:
                # 最小化到托盘
                event.ignore()
                self.hide()
                self._tray.showMessage(
                    "日常小帮手", "已最小化到系统托盘",
                    QSystemTrayIcon.Information, 2000,
                )
            else:
                # 退出
                self._quit()

        def _quit(self):
            self._running = False
            self._tray.hide()
            QApplication.quit()


# ============================================================
# tkinter 实现
# ============================================================
elif GUI_ENGINE == "tkinter":

    class _FauxStack(tk.Frame):
        """模拟 QStackedWidget 的简单容器"""
        def __init__(self, parent):
            super().__init__(parent)
            self.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True)
            self._frames: list[tk.Frame] = []

        def add(self, frame: tk.Frame):
            self._frames.append(frame)

        def select(self, index: int):
            for f in self._frames:
                f.pack_forget()
            self._frames[index].pack(fill=tk.BOTH, expand=True)

    class MainWindow:
        def __init__(self, config_path: str = ""):
            if not config_path:
                config_path = _default_config_path()
            with open(config_path, "r", encoding="utf-8") as f:
                config = yaml.safe_load(f)

            self._tools_cfg = config.get("tools", [])
            self._settings = config.get("settings", {})
            self._ds_cfg = config.get("deepseek", {})

            self._adapters: dict[str, BaseAdapter] = {}
            self._task_vars: dict[str, tk.BooleanVar] = {}

            self._reporter = BattleReporter()
            saved_key = _load_api_key(self._ds_cfg)
            self._deepseek = DeepSeekClient(
                api_key=saved_key,
                base_url=self._ds_cfg.get("base_url", "https://api.deepseek.com"),
                model=self._ds_cfg.get("model", "deepseek-chat"),
            )

            self._signals = _Signals()
            self._run_thread: Optional[threading.Thread] = None
            self._running = False

            self._root = tk.Tk()
            self._root.title("日常小帮手 v1.0")
            self._root.geometry("960x640")
            self._root.protocol("WM_DELETE_WINDOW", self._on_close)

            self._build_ui()
            self._load_adapters()
            self._wire_signals()

            logger.info("GUI initialized (tkinter)")

        # ---- UI 构建 ----
        def _build_ui(self):
            # 左侧导航
            nav_frame = tk.Frame(self._root, width=140, bg="#f0f0f0")
            nav_frame.pack(side=tk.LEFT, fill=tk.Y)
            nav_frame.pack_propagate(False)

            tk.Label(
                nav_frame, text="🧰 日常小帮手", bg="#f0f0f0",
                font=("Microsoft YaHei", 11, "bold"),
            ).pack(pady=10)

            tk.Button(
                nav_frame, text="📋 任务列表",
                command=lambda: self._stack.select(0), bg="#e8e8e8",
            ).pack(fill=tk.X, padx=6, pady=2)
            tk.Button(
                nav_frame, text="📊 历史战报",
                command=lambda: self._stack.select(1), bg="#e8e8e8",
            ).pack(fill=tk.X, padx=6, pady=2)

            # 右侧堆叠
            self._stack = _FauxStack(self._root)

            # -- 任务页 --
            task_frame = tk.Frame(self._stack)
            self._task_canvas = tk.Canvas(
                task_frame, borderwidth=0, highlightthickness=0,
            )
            scrollbar = ttk.Scrollbar(
                task_frame, orient=tk.VERTICAL,
                command=self._task_canvas.yview,
            )
            self._task_inner = tk.Frame(self._task_canvas)
            self._task_inner.bind(
                "<Configure>",
                lambda e: self._task_canvas.configure(
                    scrollregion=self._task_canvas.bbox("all"),
                ),
            )
            self._task_canvas.create_window(
                (0, 0), window=self._task_inner, anchor="nw",
            )
            self._task_canvas.configure(yscrollcommand=scrollbar.set)
            self._task_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
            scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

            # 按钮栏
            btn_frame = tk.Frame(task_frame)
            btn_frame.pack(side=tk.BOTTOM, fill=tk.X, pady=4)
            self._start_btn = tk.Button(
                btn_frame, text="▶ 开始执行",
                bg="#0078d4", fg="white", command=self._on_start,
            )
            self._start_btn.pack(side=tk.RIGHT, padx=4)
            self._stop_btn = tk.Button(
                btn_frame, text="⏹ 停止",
                state=tk.DISABLED, command=self._on_stop,
            )
            self._stop_btn.pack(side=tk.RIGHT, padx=4)

            tk.Label(
                task_frame, text="执行日志:",
                font=("Microsoft YaHei", 10, "bold"),
            ).pack(side=tk.BOTTOM, anchor=tk.W, padx=4)
            self._log_area = scrolledtext.ScrolledText(
                task_frame, height=8, font=("Consolas", 10), state=tk.DISABLED,
            )
            self._log_area.pack(side=tk.BOTTOM, fill=tk.X, padx=4, pady=4)
            self._stack.add(task_frame)

            # -- 战报页 --
            report_frame = tk.Frame(self._stack)
            tk.Label(
                report_frame, text="历史战报",
                font=("Microsoft YaHei", 12, "bold"),
            ).pack(anchor=tk.W, padx=8, pady=4)
            self._report_area = scrolledtext.ScrolledText(
                report_frame, font=("Consolas", 10), state=tk.DISABLED,
            )
            self._report_area.pack(fill=tk.BOTH, expand=True, padx=8, pady=4)
            tk.Button(
                report_frame, text="🔄 刷新", command=self._refresh_reports,
            ).pack(side=tk.RIGHT, padx=8, pady=4)
            self._stack.add(report_frame)
            self._stack.select(0)

        # ---- API Key 保存 (tkinter) ----
        def _save_api_key_tk(self):
            new_key = self._apikey_entry.get().strip()

            # 持久化到用户目录，exe 模式下重启不丢失
            _save_api_key_to_file(new_key)

            self._ds_cfg["api_key"] = new_key
            self._deepseek = DeepSeekClient(
                api_key=new_key,
                base_url=self._ds_cfg.get("base_url", "https://api.deepseek.com"),
                model=self._ds_cfg.get("model", "deepseek-chat"),
            )

            if new_key:
                self._apikey_save_btn_tk.configure(text="✅ 已保存")
            else:
                self._apikey_save_btn_tk.configure(text="💾 保存")

        # ---- 适配器加载 ----
        def _load_adapters(self):
            # 顶层放 API Key 配置
            apikey_frame = tk.LabelFrame(
                self._task_inner, text="🤖 DeepSeek AI 配置 (可选)",
                font=("Microsoft YaHei", 10, "bold"),
            )
            apikey_frame.pack(fill=tk.X, padx=8, pady=4)

            tk.Label(apikey_frame, text="API Key:").pack(side=tk.LEFT, padx=4)
            self._apikey_entry = tk.Entry(apikey_frame, show="*", width=40)
            self._apikey_entry.pack(side=tk.LEFT, padx=4, fill=tk.X, expand=True)
            saved_key = _load_api_key(self._ds_cfg)
            if saved_key:
                self._apikey_entry.insert(0, saved_key)

            btn_text = "✅ 已保存" if saved_key else "💾 保存"
            self._apikey_save_btn_tk = tk.Button(
                apikey_frame, text=btn_text,
                command=self._save_api_key_tk,
            )
            self._apikey_save_btn_tk.pack(side=tk.RIGHT, padx=4)

            for tool_cfg in self._tools_cfg:
                adapter = _make_adapter(tool_cfg)
                if adapter is None:
                    continue
                adapter.load_config(tool_cfg)
                self._adapters[tool_cfg["name"]] = adapter

                group = tk.LabelFrame(
                    self._task_inner, text=tool_cfg["name"],
                    font=("Microsoft YaHei", 10, "bold"),
                )
                group.pack(fill=tk.X, padx=8, pady=4)

                for task in tool_cfg.get("tasks", []):
                    var = tk.BooleanVar(value=task.get("enabled", False))
                    key = f"{tool_cfg['name']}::{task['id']}"
                    self._task_vars[key] = var
                    state = tk.NORMAL if tool_cfg.get("enabled", True) else tk.DISABLED
                    cb = tk.Checkbutton(
                        group,
                        text=f"{task['name']} — {task.get('desc', '')}",
                        variable=var, state=state,
                    )
                    cb.pack(anchor=tk.W, padx=12, pady=2)

        # ---- 信号连接 ----
        def _wire_signals(self):
            self._signals.connect(
                "task_started", lambda s: self._append_log(f"▶ 开始: {s}")
            )
            self._signals.connect("task_log", self._append_log)
            self._signals.connect("task_finished", self._on_task_finished_tk)
            self._signals.connect("all_finished", self._on_all_finished_tk)
            self._signals.connect(
                "error", lambda s: self._append_log(f"❗ 错误: {s}")
            )

        # ---- 运行控制 ----
        def _on_start(self):
            if self._running:
                return
            selected = self._collect_selected()
            if not selected:
                self._append_log("[系统] 请至少选择一个任务")
                return
            self._running = True
            self._start_btn.configure(state=tk.DISABLED)
            self._stop_btn.configure(state=tk.NORMAL)
            self._log_area.configure(state=tk.NORMAL)
            self._log_area.delete(1.0, tk.END)
            self._log_area.configure(state=tk.DISABLED)
            self._run_thread = threading.Thread(
                target=self._run_tasks, args=(selected,), daemon=True,
            )
            self._run_thread.start()

        def _collect_selected(self):
            selected = []
            for tool_cfg in self._tools_cfg:
                name = tool_cfg["name"]
                adapter = self._adapters.get(name)
                if not adapter or not tool_cfg.get("enabled", True):
                    continue
                for task in tool_cfg.get("tasks", []):
                    key = f"{name}::{task['id']}"
                    var = self._task_vars.get(key)
                    if var and var.get():
                        td = TaskDef(
                            task_id=task["id"], task_name=task["name"],
                            args=task.get("args", ""), desc=task.get("desc", ""),
                            eta=task.get("eta", 30),
                        )
                        selected.append((name, td, adapter))
            return selected

        def _run_tasks(self, selected_tasks):
            results = []
            max_restarts = self._settings.get("max_restart_count", 3)
            interval = self._settings.get("task_interval_seconds", 3)
            default_timeout = self._settings.get("default_timeout_minutes", 60) * 60

            guardian = Guardian(
                cpu_dead_threshold=self._settings.get("cpu_dead_threshold", 0.0),
                cpu_dead_duration=self._settings.get("cpu_dead_duration", 10),
                hung_check_interval=self._settings.get("hung_check_interval", 5),
            )
            scheduler = Scheduler(guardian=guardian)

            for tool_name, td, adapter in selected_tasks:
                if not self._running:
                    break
                self._signals.emit(
                    "task_started", f"{tool_name} - {td.task_name}"
                )
                cmd = adapter.build_command(td)
                self._signals.emit("task_log", f"[CMD] {' '.join(cmd)}")
                result = scheduler.run(
                    cmd,
                    task_id=f"{tool_name}::{td.task_id}",
                    task_name=f"{tool_name} - {td.task_name}",
                    tool_name=tool_name,
                    timeout=default_timeout,
                    max_restarts=max_restarts,
                    cwd=os.path.dirname(adapter.exe_path),
                    game_exe=adapter.game_exe,
                    needs_window=adapter.needs_window,
                    daemon_launcher=adapter.daemon_launcher,
                )
                result.task_name = td.task_name
                result.tool_name = tool_name
                results.append(result)
                self._signals.emit("task_finished", result)
                if interval > 0:
                    import time
                    time.sleep(interval)

            if results:
                report_path = self._reporter.save(results)
                self._signals.emit("task_log", f"[战报] 已保存至 {report_path}")
            self._signals.emit("all_finished", results)

        def _on_stop(self):
            self._running = False
            self._start_btn.configure(state=tk.NORMAL)
            self._stop_btn.configure(state=tk.DISABLED)
            self._append_log("[系统] 用户请求停止...")

        # ---- 日志 ----
        def _append_log(self, text: str):
            self._log_area.configure(state=tk.NORMAL)
            self._log_area.insert(tk.END, text + "\n")
            self._log_area.see(tk.END)
            self._log_area.configure(state=tk.DISABLED)

        # ---- 信号回调 ----
        def _on_task_finished_tk(self, result: TaskResult):
            icon = "✅" if result.status == TaskStatus.SUCCESS else "❌"
            self._append_log(
                f"{icon} 完成: {result.tool_name} - {result.task_name} | "
                f"状态: {result.status.value} | 重启: {result.restart_count}次"
            )
            if result.error_message:
                self._append_log(f"   错误: {result.error_message[:200]}")

        def _on_all_finished_tk(self, results):
            self._running = False
            self._start_btn.configure(state=tk.NORMAL)
            self._stop_btn.configure(state=tk.DISABLED)
            total = len(results)
            success = sum(1 for r in results if r.status == TaskStatus.SUCCESS)
            self._append_log(
                f"\n{'='*50}\n全部完成: {success}/{total} 成功\n{'='*50}"
            )

        def _refresh_reports(self):
            self._report_area.configure(state=tk.NORMAL)
            self._report_area.delete(1.0, tk.END)
            reports = self._reporter.list_reports()
            if not reports:
                self._report_area.insert(tk.END, "暂无战报记录")
            else:
                for report in reports:
                    ts = report.get("timestamp", "")
                    s = report.get("summary", {})
                    self._report_area.insert(tk.END, (
                        f"━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"[{ts}] 总任务: {s.get('total', 0)} | "
                        f"成功: {s.get('success', 0)} | "
                        f"失败: {s.get('failed', 0)} | "
                        f"成功率: {s.get('success_rate', '0%')}\n"
                    ))
                    for t in report.get("tasks", []):
                        self._report_area.insert(tk.END, (
                            f"  • {t.get('tool', '')} / "
                            f"{t.get('task_name', '')} : "
                            f"{t.get('status', '')} "
                            f"({t.get('duration_seconds', 0)}s)\n"
                        ))
            self._report_area.configure(state=tk.DISABLED)

        # ---- 窗口事件 ----
        def _on_close(self):
            self._running = False
            self._root.destroy()

        def show(self):
            self._root.mainloop()


# ============================================================
# 入口
# ============================================================
def run():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    if GUI_ENGINE == "pyside6":
        app = QApplication(sys.argv)
        app.setQuitOnLastWindowClosed(False)
        app.setStyle("Fusion")
        window = MainWindow()
        window.show()
        sys.exit(app.exec())
    elif GUI_ENGINE == "tkinter":
        window = MainWindow()
        window.show()
    else:
        print("错误: 没有可用的 GUI 引擎 (PySide6/tkinter)。请使用 --headless 模式。")
        sys.exit(1)


if __name__ == "__main__":
    run()