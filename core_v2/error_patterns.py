"""
Error patterns library — classify failure causes from OK-WW / M7A logs
"""
from dataclasses import dataclass
from enum import Enum, auto
from typing import Optional


class ErrorCategory(Enum):
    RECOVERABLE = auto()
    NEEDS_RESTART_GAME = auto()
    CRITICAL = auto()
    RECOVERY_SUCCEEDED = auto()
    UNKNOWN = auto()


@dataclass
class ErrorPattern:
    keywords: list[str]
    category: ErrorCategory = ErrorCategory.RECOVERABLE
    description: str = ""
    suggested_action: str = ""
    restart_game: bool = False


ERROR_PATTERNS: list[ErrorPattern] = [
    # ── 网络 / 连接类 ──
    ErrorPattern(
        keywords=["NetworkError", "timeout", "ConnectionError", "SSL: DECRYPTION_FAILED"],
        category=ErrorCategory.RECOVERABLE,
        description="网络异常或连接超时",
        suggested_action="等待30秒后重试当前任务",
    ),
    ErrorPattern(
        keywords=["当前网络异常", "network unreachable"],
        category=ErrorCategory.RECOVERABLE,
        description="客户端网络中断",
        suggested_action="检查网络后重试",
    ),
    # ── 启动 / 登录类 ──
    ErrorPattern(
        keywords=["启动游戏失败", "无法注入", "游戏窗口未找到", "窗口创建失败"],
        category=ErrorCategory.NEEDS_RESTART_GAME,
        description="游戏启动或注入脚本失败",
        suggested_action="重启游戏后再试",
        restart_game=True,
    ),
    ErrorPattern(
        keywords=["月卡已领取", "月卡领取失败"],
        category=ErrorCategory.RECOVERABLE,
        description="月卡领取异常",
        suggested_action="跳过月卡阶段继续",
    ),
    # ── 传送 / 导航类 ──
    ErrorPattern(
        keywords=["传送失败", "传送点未解锁", "无法传送"],
        category=ErrorCategory.RECOVERABLE,
        description="传送失败",
        suggested_action="等待角色脱战，重试传送，失败则跳过此阶段",
    ),
    # ── 战斗 / 识别类 ──
    ErrorPattern(
        keywords=["战斗超时", "怪物未刷新", "Boss未出现", "寻敌失败"],
        category=ErrorCategory.RECOVERABLE,
        description="战斗或寻敌异常",
        suggested_action="等待10秒后重试当前阶段",
    ),
    ErrorPattern(
        keywords=["OCR识别失败", "画面黑屏", "卡在加载界面"],
        category=ErrorCategory.RECOVERABLE,
        description="画面识别问题",
        suggested_action="等待5秒后重试，超时则跳过",
    ),
    # ── 体力 / 资源类 ──
    ErrorPattern(
        keywords=["体力不足", "结晶波片不足", "疲劳不足"],
        category=ErrorCategory.RECOVERABLE,
        description="体力/资源不足",
        suggested_action="跳过该消耗体力的子任务",
    ),
    ErrorPattern(
        keywords=["体力已耗尽", "结晶波片已耗尽"],
        category=ErrorCategory.RECOVERABLE,
        description="体力完全耗尽",
        suggested_action="跳过所有体力子任务，仅完成不耗体力的打卡项",
    ),
    # ── 游戏崩溃 ──
    ErrorPattern(
        keywords=["Fatal error", "Unreal Engine Crash", "Access Violation", "崩溃"],
        category=ErrorCategory.NEEDS_RESTART_GAME,
        description="游戏崩溃",
        suggested_action="重启游戏客户端，登录后从断点继续",
        restart_game=True,
    ),
    ErrorPattern(
        keywords=["游戏进程不存在", "已退出游戏"],
        category=ErrorCategory.CRITICAL,
        description="游戏进程意外退出",
        suggested_action="人工检查游戏状态后重试",
    ),
    # ── OK-WW 内部错误 ──
    ErrorPattern(
        keywords=["脚本执行异常", "未知错误", "当前任务执行失败"],
        category=ErrorCategory.RECOVERABLE,
        description="OK-WW 脚本内部执行异常",
        suggested_action="等待10秒后重试最后一个子阶段",
    ),
    ErrorPattern(
        keywords=["Traceback", "Exception:"],
        category=ErrorCategory.CRITICAL,
        description="运行时报错",
        suggested_action="查看 Traceback，人工修复后重试",
    ),
]

KEYWORD_TO_CATEGORY: dict[str, ErrorCategory] = {
    "timeout": ErrorCategory.RECOVERABLE,
    "connection": ErrorCategory.RECOVERABLE,
    "network": ErrorCategory.RECOVERABLE,
    "传送失败": ErrorCategory.RECOVERABLE,
    "传送": ErrorCategory.RECOVERABLE,
    "战斗超时": ErrorCategory.RECOVERABLE,
    "体力不足": ErrorCategory.RECOVERABLE,
    "体力已耗尽": ErrorCategory.RECOVERABLE,
    "ocr": ErrorCategory.RECOVERABLE,
    "识别失败": ErrorCategory.RECOVERABLE,
    "黑屏": ErrorCategory.RECOVERABLE,
    "崩溃": ErrorCategory.NEEDS_RESTART_GAME,
    "crash": ErrorCategory.NEEDS_RESTART_GAME,
    "fatal": ErrorCategory.NEEDS_RESTART_GAME,
    "access violation": ErrorCategory.NEEDS_RESTART_GAME,
    "traceback": ErrorCategory.CRITICAL,
    "exception": ErrorCategory.CRITICAL,
}


def classify_error(log_snippet: str) -> Optional[ErrorPattern]:
    if not log_snippet:
        return None
    lower = log_snippet.lower()
    for pattern in ERROR_PATTERNS:
        for kw in pattern.keywords:
            if kw.lower() in lower:
                return pattern
    return None