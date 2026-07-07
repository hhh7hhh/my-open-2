"""
基础适配器抽象类 — 所有自动化工具适配器的基类
"""
import subprocess
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional
from enum import Enum


class TaskStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    TIMEOUT = "timeout"
    SKIPPED = "skipped"


@dataclass
class TaskResult:
    """单个任务的执行结果"""
    tool_name: str = ""
    task_id: str = ""
    task_name: str = ""
    status: TaskStatus = TaskStatus.PENDING
    start_time: Optional[float] = None
    end_time: Optional[float] = None
    restart_count: int = 0
    error_message: str = ""
    exit_code: Optional[int] = None


@dataclass
class TaskDef:
    """任务定义"""
    task_id: str = ""
    task_name: str = ""
    args: str = ""
    desc: str = ""
    eta: int = 15
    enabled: bool = True
    timeout_minutes: int = 60


class BaseAdapter(ABC):
    """
    自动化工具适配器基类

    每个自动化工具（okww、M7A等）继承此类，实现具体逻辑。
    管家通过此接口统一调度不同工具。
    """

    # 子类必须覆盖的属性
    name: str = "Unknown"
    icon: str = "game"
    game_exe: str = ""
    exe_path: str = ""
    needs_window: bool = False   # True = 工具需要 GUI 窗口（如 Electron 程序），不加 CREATE_NO_WINDOW
    daemon_launcher: bool = False  # True = 启动器是守护进程（永不退出），调度器跳过 proc.wait 直接监控游戏进程

    def __init__(self, config: dict = None):
        """
        :param config: tools.yaml 中该工具的配置字典（可选）
        """
        self._config = config or {}
        self.enabled = True
        self.tasks: list[TaskDef] = []
        if config:
            self.load_config(config)

    def load_config(self, config: dict = None):
        """加载或更新配置"""
        cfg = config or self._config
        if not cfg:
            return
        self._config = cfg
        self.name = cfg.get("name", self.name)
        self.icon = cfg.get("icon", self.icon)
        self.game_exe = cfg.get("game_exe", self.game_exe)
        self.exe_path = cfg.get("exe_path", self.exe_path)
        self.enabled = cfg.get("enabled", True)

        self.tasks = []
        for task_cfg in cfg.get("tasks", []):
            self.tasks.append(TaskDef(
                task_id=task_cfg.get("id", task_cfg.get("task_id", "")),
                task_name=task_cfg.get("name", task_cfg.get("task_name", "")),
                args=task_cfg.get("args", ""),
                desc=task_cfg.get("desc", ""),
                eta=task_cfg.get("eta", 15),
                enabled=task_cfg.get("enabled", True),
                timeout_minutes=task_cfg.get("timeout", 60),
            ))

    def get_enabled_tasks(self) -> list[TaskDef]:
        """获取所有启用的任务"""
        return [t for t in self.tasks if t.enabled]

    @abstractmethod
    def build_command(self, task: TaskDef) -> list[str]:
        """
        构建启动命令

        :param task: 任务定义
        :return: 命令行参数列表，如 ["C:\\tool.exe", "-t", "1", "-e"]
        """
        pass

    def launch(self, task: TaskDef) -> subprocess.Popen:
        """
        启动工具进程

        :param task: 任务定义
        :return: Popen 进程对象
        """
        cmd = self.build_command(task)
        work_dir = os.path.dirname(self.exe_path)

        flags = 0
        if os.name == "nt" and not self.needs_window:
            # GUI-less tool: hide console window
            flags = subprocess.CREATE_NO_WINDOW

        proc = subprocess.Popen(
            cmd,
            cwd=work_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=flags,
        )
        return proc

    def get_icon_path(self) -> str:
        """获取自定义图标路径，不存在则返回空字符串"""
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        icon_map = {
            "wave": os.path.join(base, "assets", "wuthering_waves.png"),
            "train": os.path.join(base, "assets", "honkai_star_rail.png"),
        }
        path = icon_map.get(self.icon, "")
        if path and os.path.exists(path):
            return path
        return ""