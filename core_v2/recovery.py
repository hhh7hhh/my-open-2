"""
Recovery Engine — execute recovery actions based on failure analysis
"""
import logging
import threading
import time
import os
import subprocess
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional, Callable

from .error_patterns import ErrorCategory, ErrorPattern
from .log_analyzer import FailureContext

logger = logging.getLogger("recovery")


class RecoveryAction(Enum):
    WAIT_RETRY = auto()          # 等待后重试
    SKIP_STAGE = auto()          # 跳过当前阶段
    RESTART_GAME_RETRY = auto()  # 重启游戏后重试
    ESCALATE = auto()            # 人工介入
    NONE = auto()                # 无需恢复（成功）


@dataclass
class RecoveryResult:
    action: RecoveryAction
    delay_seconds: float = 0
    message: str = ""
    ai_diagnosis: str = ""
    restart_callback: Optional[Callable[[], bool]] = None


class RecoveryEngine:
    """
    Recovery engine that decides and executes a recovery strategy
    based on failure context, pattern matching, and optional AI diagnosis.
    """

    MAX_RECOVERABLE_RETRIES = 3      # 同类型可恢复错误最多重试次数
    MAX_RESTART_RETRIES = 2          # 最多重启游戏次数
    DEFAULT_WAIT_BEFORE_RETRY = 10   # 重试前默认等待秒数

    def __init__(self, deepseek_client=None):
        self._deepseek = deepseek_client
        self._retry_count: dict[str, int] = {}    # key -> retry count
        self._restart_count = 0
        self._lock = threading.Lock()
        self._game_killer: Optional[Callable[[], None]] = None
        self._game_launcher: Optional[Callable[[], Optional[subprocess.Popen]]] = None

    def set_game_callbacks(
        self,
        killer: Optional[Callable[[], None]] = None,
        launcher: Optional[Callable[[], Optional[subprocess.Popen]]] = None,
    ):
        """Set callbacks for killing and restarting the game process."""
        self._game_killer = killer
        self._game_launcher = launcher

    def decide(self, ctx: FailureContext) -> RecoveryResult:
        """
        Decide recovery strategy based on failure context.
        Returns RecoveryResult with action and parameters.
        """
        category = ctx.category
        error_key = self._make_key(ctx)

        # 1. Unknown — try AI then decide
        if category == ErrorCategory.UNKNOWN:
            ai_diag = self._ask_ai(ctx)
            # AI may suggest a recoverable or critical path
            if ai_diag:
                logger.info(f"AI diagnosis: {ai_diag}")
            if self._count_retries(error_key) < self.MAX_RECOVERABLE_RETRIES:
                return RecoveryResult(
                    action=RecoveryAction.WAIT_RETRY,
                    delay_seconds=self.DEFAULT_WAIT_BEFORE_RETRY,
                    message=f"Unknown error, attempting retry ({self._count_retries(error_key)+1}/{self.MAX_RECOVERABLE_RETRIES})",
                    ai_diagnosis=ai_diag,
                )
            return RecoveryResult(
                action=RecoveryAction.ESCALATE,
                message="Unknown error exceeded max retries, human intervention needed",
                ai_diagnosis=ai_diag,
            )

        # 2. Recoverable — simple wait + retry
        if category == ErrorCategory.RECOVERABLE:
            if self._count_retries(error_key) < self.MAX_RECOVERABLE_RETRIES:
                self._inc_retry(error_key)
                suggested = (
                    ctx.error_pattern.suggested_action
                    if ctx.error_pattern else "等待后重试"
                )
                return RecoveryResult(
                    action=RecoveryAction.WAIT_RETRY,
                    delay_seconds=self.DEFAULT_WAIT_BEFORE_RETRY,
                    message=suggested,
                )
            # Exceeded max retries — try AI before escalating
            ai_diag = self._ask_ai(ctx)
            return RecoveryResult(
                action=RecoveryAction.ESCALATE,
                message=f"Recoverable error retried {self.MAX_RECOVERABLE_RETRIES} times, escalated",
                ai_diagnosis=ai_diag,
            )

        # 3. Needs restart game
        if category == ErrorCategory.NEEDS_RESTART_GAME:
            if self._restart_count < self.MAX_RESTART_RETRIES:
                self._restart_count += 1
                return RecoveryResult(
                    action=RecoveryAction.RESTART_GAME_RETRY,
                    delay_seconds=15,
                    message=f"Restarting game ({self._restart_count}/{self.MAX_RESTART_RETRIES})",
                )
            return RecoveryResult(
                action=RecoveryAction.ESCALATE,
                message=f"Game restarted {self.MAX_RESTART_RETRIES} times, still failing",
            )

        # 4. Critical — immediate escalation
        if category == ErrorCategory.CRITICAL:
            ai_diag = self._ask_ai(ctx)
            return RecoveryResult(
                action=RecoveryAction.ESCALATE,
                message="Critical error, human intervention required",
                ai_diagnosis=ai_diag,
            )

        # Fallback
        return RecoveryResult(action=RecoveryAction.NONE, message="No action needed")

    def execute(self, result: RecoveryResult) -> bool:
        """
        Execute the recovery action.
        Returns True if action was executed successfully.
        """
        if result.action == RecoveryAction.NONE:
            return True

        if result.action == RecoveryAction.ESCALATE:
            logger.error(f"ESCALATE: {result.message}")
            if result.ai_diagnosis:
                logger.info(f"AI says: {result.ai_diagnosis}")
            return False

        if result.action == RecoveryAction.WAIT_RETRY:
            logger.info(f"WAIT_RETRY: {result.message} (delay={result.delay_seconds}s)")
            time.sleep(result.delay_seconds)
            return True

        if result.action == RecoveryAction.SKIP_STAGE:
            logger.info(f"SKIP_STAGE: {result.message}")
            return True

        if result.action == RecoveryAction.RESTART_GAME_RETRY:
            logger.info(f"RESTART_GAME_RETRY: {result.message}")
            if self._game_killer:
                self._game_killer()
            time.sleep(result.delay_seconds)
            if self._game_launcher:
                try:
                    self._game_launcher()
                except Exception as e:
                    logger.error(f"Failed to restart game: {e}")
                    return False
            return True

        return False

    # ── helpers ──

    def _make_key(self, ctx: FailureContext) -> str:
        return f"{ctx.tool_name}:{ctx.task_name}"

    def _count_retries(self, key: str) -> int:
        with self._lock:
            return self._retry_count.get(key, 0)

    def _inc_retry(self, key: str):
        with self._lock:
            self._retry_count[key] = self._retry_count.get(key, 0) + 1

    def reset_retries(self, key: Optional[str] = None):
        """Reset retry counters (call after a task succeeds)."""
        with self._lock:
            if key:
                self._retry_count.pop(key, None)
            else:
                self._retry_count.clear()
                self._restart_count = 0

    def _ask_ai(self, ctx: FailureContext) -> str:
        """Ask DeepSeek for diagnosis if available."""
        if not self._deepseek or not getattr(self._deepseek, "enabled", False):
            return ""
        try:
            return self._deepseek.diagnose(
                tool_name=ctx.tool_name,
                task_name=ctx.task_name,
                error_message=ctx.log_tail,
                exit_code=ctx.exit_code,
            )
        except Exception as e:
            logger.warning(f"DeepSeek diagnosis failed: {e}")
            return ""