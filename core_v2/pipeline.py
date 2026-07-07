"""
One-Dragon Pipeline — v2 核心编排引擎
=======================================
一条龙任务全自动执行管道：
  启动 → 监控 → 失败分析（日志+AI）→ 恢复决策 → 重试/跳过 → 最终报告

与 v1 scheduler 的关键区别：
  - 失败不直接 abort，而是走 recovery 流程
  - 集成 DeepSeek AI 辅助诊断
  - 自动跳过无法恢复的子阶段，确保一条龙完整跑完
  - 记录完整的 pipeline 执行报告
"""
import logging
import time
import subprocess
import os
import threading
from datetime import datetime
from dataclasses import dataclass, field
from typing import Optional, Callable
from enum import Enum, auto

# v1 imports (复用现有模块)
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.guardian import Guardian
from core.deepseek import DeepSeekClient
from adapters.base_adapter import BaseAdapter, TaskDef, TaskStatus, TaskResult

# v2 imports
from .log_analyzer import LogAnalyzer, FailureContext
from .recovery import RecoveryEngine, RecoveryResult, RecoveryAction
from .error_patterns import ErrorCategory

logger = logging.getLogger("pipeline")


class PipelineStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    RECOVERING = "recovering"
    SUCCESS = "success"
    PARTIAL = "partial"      # 部分成功（跳过了一些阶段）
    FAILED = "failed"
    TIMEOUT = "timeout"


@dataclass
class PipelineResult:
    """一条龙管道的最终执行结果"""
    tool_name: str = ""
    task_name: str = ""
    status: PipelineStatus = PipelineStatus.PENDING
    stages_completed: int = 0
    stages_total: int = 0
    stages_skipped: int = 0
    restart_count: int = 0
    retry_count: int = 0
    ai_calls: int = 0
    duration_seconds: float = 0
    final_error: str = ""
    recovery_log: list[str] = field(default_factory=list)


class OneDragonPipeline:
    """
    一条龙管道执行器

    用法:
        pipeline = OneDragonPipeline(adapter, deepseek_client)
        pipeline.set_exe_path(r"D:\\AIButler\\tools\\ok-ww\\ok-ww.exe")
        result = pipeline.run(task_def)
    """

    # 管道配置
    MAX_TOTAL_RETRIES = 3       # 整个管道最多重试次数
    MAX_STAGE_SKIPS = 5         # 最多跳过几个子阶段
    PROCESS_START_TIMEOUT = 30  # 进程启动超时（秒）
    POST_RECOVERY_WAIT = 5      # 恢复后等待游戏稳定（秒）

    def __init__(self, adapter: BaseAdapter, deepseek: Optional[DeepSeekClient] = None):
        self._adapter = adapter
        self._deepseek = deepseek
        self._guardian = Guardian()
        self._log_analyzer = LogAnalyzer()
        self._recovery = RecoveryEngine(deepseek_client=deepseek)
        self._exe_path = ""

        # Runtime state
        self._current_proc: Optional[subprocess.Popen] = None
        self._pipeline_status = PipelineStatus.PENDING
        self._stop_event = threading.Event()

    def set_exe_path(self, exe_path: str):
        self._exe_path = exe_path

    def run(self, task: TaskDef, timeout_minutes: int = 60) -> PipelineResult:
        """
        执行一条龙管道。返回 PipelineResult。

        :param task: 任务定义（通常是 DailyTask / main 等一条龙任务）
        :param timeout_minutes: 超时时间（分钟）
        """
        start_time = time.time()
        result = PipelineResult(
            tool_name=self._adapter.name,
            task_name=task.task_name or task.task_id,
            status=PipelineStatus.RUNNING,
        )

        self._pipeline_status = PipelineStatus.RUNNING
        self._stop_event.clear()
        self._recovery.reset_retries()

        logger.info(f"Pipeline starting: {self._adapter.name} -> {task.task_name} (timeout={timeout_minutes}m)")

        total_retries = 0
        stage_skips = 0

        while total_retries <= self.MAX_TOTAL_RETRIES:
            if self._stop_event.is_set():
                result.status = PipelineStatus.FAILED
                result.final_error = "Pipeline stopped by user"
                break

            # ── Launch ──
            launch_ok = self._launch_task(task)
            if not launch_ok:
                result.final_error = "Failed to launch task"
                result.status = PipelineStatus.FAILED
                break

            # ── Monitor ──
            proc_result = self._monitor(task, timeout_minutes)

            if proc_result.status == TaskStatus.SUCCESS:
                result.status = PipelineStatus.SUCCESS
                result.stages_completed = result.stages_total  # best effort
                logger.info(f"Pipeline SUCCESS: {self._adapter.name} -> {task.task_name}")
                break

            # ── Failure analysis ──
            ctx = self._log_analyzer.analyze(
                tool_name=self._adapter.name,
                task_name=task.task_name,
                exit_code=proc_result.exit_code,
                exe_path=self._exe_path,
            )
            result.stages_completed = ctx.stage_progress[0]
            result.stages_total = ctx.stage_progress[1] if ctx.stage_progress[1] > 0 else result.stages_total

            # Update stage info from proc result if available
            last_stage_info = f"last_stage={ctx.last_stage}" if ctx.last_stage else "unknown"
            logger.warning(
                f"Pipeline FAILED at {last_stage_info}, "
                f"category={ctx.category.name}, "
                f"exit_code={ctx.exit_code}"
            )

            # ── Recovery decision ──
            recovery = self._recovery.decide(ctx)
            result.recovery_log.append(
                f"[{datetime.now().strftime('%H:%M:%S')}] {recovery.action.name}: {recovery.message}"
            )
            if recovery.ai_diagnosis:
                result.ai_calls += 1
                result.recovery_log.append(f"  AI: {recovery.ai_diagnosis}")

            # ── Execute recovery ──
            total_retries += 1
            result.retry_count += 1

            if recovery.action == RecoveryAction.ESCALATE:
                result.status = PipelineStatus.FAILED
                result.final_error = recovery.message
                logger.error(f"Pipeline ESCALATED: {recovery.message}")
                break

            if recovery.action == RecoveryAction.RESTART_GAME_RETRY:
                result.restart_count += 1
                self._recovery.execute(recovery)  # kills + restarts game
                time.sleep(self.POST_RECOVERY_WAIT)
                # Continue loop for retry
                continue

            if recovery.action == RecoveryAction.WAIT_RETRY:
                self._recovery.execute(recovery)  # just sleeps
                if stage_skips >= self.MAX_STAGE_SKIPS:
                    # Too many skips, escalate
                    result.status = PipelineStatus.PARTIAL
                    result.stages_skipped = stage_skips
                    result.final_error = f"Exceeded max stage skips ({self.MAX_STAGE_SKIPS})"
                    break
                stage_skips += 1
                result.stages_skipped = stage_skips
                # Continue loop for retry
                continue

            if recovery.action == RecoveryAction.SKIP_STAGE:
                stage_skips += 1
                result.stages_skipped = stage_skips
                # Continue loop
                continue

            # NONE or unknown
            break

        # ── Wrap up ──
        self._pipeline_status = result.status
        self._guardian.stop()

        result.duration_seconds = time.time() - start_time

        logger.info(
            f"Pipeline finished: status={result.status.value}, "
            f"duration={result.duration_seconds:.1f}s, "
            f"retries={result.retry_count}, skips={result.stages_skipped}"
        )
        return result

    def stop(self):
        """Signal pipeline to stop gracefully."""
        self._stop_event.set()
        self._guardian.stop()
        if self._current_proc:
            try:
                self._current_proc.kill()
            except Exception:
                pass

    # ═══════════════════════════════════════════
    # Internal
    # ═══════════════════════════════════════════

    def _launch_task(self, task: TaskDef) -> bool:
        """Launch the task process and start guardian."""
        try:
            self._current_proc = self._adapter.launch(task)
            logger.info(f"Launched {self._adapter.name} pid={self._current_proc.pid}")

            # Start guardian
            self._guardian.start(self._current_proc)

            # Wait briefly to ensure process started
            time.sleep(2)
            if self._current_proc.poll() is not None:
                logger.error(f"Process exited immediately with code {self._current_proc.returncode}")
                return False

            return True
        except Exception as e:
            logger.error(f"Launch failed: {e}")
            return False

    def _monitor(self, task: TaskDef, timeout_minutes: int) -> TaskResult:
        """Monitor the process with guardian checks. Returns TaskResult."""
        proc = self._current_proc
        if not proc:
            return TaskResult(status=TaskStatus.FAILED, error_message="No process")

        result = TaskResult(
            tool_name=self._adapter.name,
            task_name=task.task_name,
            status=TaskStatus.RUNNING,
            start_time=time.time(),
        )

        timeout_seconds = timeout_minutes * 60
        check_interval = 3  # seconds

        while True:
            if self._stop_event.is_set():
                result.status = TaskStatus.FAILED
                result.error_message = "Stopped by user"
                break

            elapsed = time.time() - (result.start_time or time.time())
            if elapsed > timeout_seconds:
                result.status = TaskStatus.TIMEOUT
                result.error_message = f"Timeout after {timeout_minutes} minutes"
                logger.warning(result.error_message)
                self._kill_proc(proc)
                break

            # Check if process exited
            exit_code = proc.poll()
            if exit_code is not None:
                result.end_time = time.time()
                result.exit_code = exit_code
                if exit_code == 0:
                    result.status = TaskStatus.SUCCESS
                else:
                    result.status = TaskStatus.FAILED
                    stdout, stderr = self._read_outputs(proc)
                    result.error_message = stderr or stdout or f"Exit code: {exit_code}"
                break

            # Guardian checks
            if self._guardian.is_monitoring and self._guardian.monitored_pid:
                pass  # Guardian callbacks handle recovery signals

            time.sleep(check_interval)

        self._guardian.stop()
        return result

    def _kill_proc(self, proc: subprocess.Popen):
        """Kill a process and its children."""
        try:
            proc.kill()
            proc.wait(timeout=5)
        except Exception:
            try:
                import signal
                os.kill(proc.pid, signal.SIGTERM)
            except Exception:
                pass

    @staticmethod
    def _read_outputs(proc: subprocess.Popen) -> tuple[str, str]:
        """Safely read stdout/stderr from a finished process."""
        stdout = ""
        stderr = ""
        try:
            if proc.stdout:
                stdout = proc.stdout.read()
        except Exception:
            pass
        try:
            if proc.stderr:
                stderr = proc.stderr.read()
        except Exception:
            pass
        return stdout, stderr