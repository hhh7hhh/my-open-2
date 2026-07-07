"""
Log Analyzer — extract error context from OK-WW / M7A log files
"""
import os
import re
from datetime import datetime
from dataclasses import dataclass, field
from typing import Optional

from .error_patterns import ErrorPattern, ErrorCategory, classify_error


@dataclass
class FailureContext:
    tool_name: str
    task_name: str
    exit_code: Optional[int]
    error_pattern: Optional[ErrorPattern]
    category: ErrorCategory
    log_tail: str                  # last N lines of log
    last_stage: str                # last detected sub-stage before failure
    stage_progress: tuple[int, int]  # (current, total) if detectable
    log_path: str = ""


class LogAnalyzer:
    """Analyze log files to determine failure cause and recovery context."""

    # Common OK-WW log file location relative to exe_path
    DEFAULT_LOG_RELATIVE = r"data\apps\ok-ww\working\logs\ok-script.log"

    # Sub-stage patterns in OK-WW logs
    STAGE_PATTERNS = [
        # OK-WW format: [DailyTask] Step X/Y: description
        re.compile(r"\[DailyTask\]\s*Step\s*(\d+)\s*/\s*(\d+)\s*[:：]\s*(.+)", re.IGNORECASE),
        # Generic step indicator
        re.compile(r"Step\s*(\d+)\s*/\s*(\d+)", re.IGNORECASE),
        # 开始执行 / Starting
        re.compile(r"(?:开始执行|Starting)\s*[:：]?\s*(.+)", re.IGNORECASE),
        # 任务进度 / progress
        re.compile(r"(\d+)%\s*(?:完成|complete)", re.IGNORECASE),
    ]

    # Lines to grab before the end of log (for AI context)
    CONTEXT_LINES = 30

    def __init__(self, log_path: Optional[str] = None):
        self._log_path = log_path or ""

    def analyze(
        self,
        tool_name: str,
        task_name: str,
        exit_code: Optional[int],
        log_path: Optional[str] = None,
        exe_path: Optional[str] = None,
    ) -> FailureContext:
        """
        Analyze failure given tool/task info and log content.
        Returns a FailureContext with classification and last detected stage.
        """
        path = log_path or self._log_path or self._find_log(exe_path)
        tail = self._read_tail(path, self.CONTEXT_LINES)

        # Classify
        pattern = classify_error(tail)
        category = pattern.category if pattern else ErrorCategory.UNKNOWN

        # Detect last stage
        last_stage, stage_progress = self._detect_last_stage(tail)

        return FailureContext(
            tool_name=tool_name,
            task_name=task_name,
            exit_code=exit_code,
            error_pattern=pattern,
            category=category,
            log_tail=tail,
            last_stage=last_stage,
            stage_progress=stage_progress,
            log_path=path,
        )

    def _find_log(self, exe_path: Optional[str]) -> str:
        """Auto-detect log path from exe directory."""
        if not exe_path:
            return ""
        base = os.path.dirname(exe_path)
        candidate = os.path.join(base, self.DEFAULT_LOG_RELATIVE)
        if os.path.isfile(candidate):
            return candidate
        return ""

    def _read_tail(self, path: str, lines: int) -> str:
        """Read the last `lines` lines from a file."""
        if not path or not os.path.isfile(path):
            return ""
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                all_lines = f.readlines()
                tail = all_lines[-lines:] if len(all_lines) > lines else all_lines
                return "".join(tail)
        except Exception:
            return ""

    def _detect_last_stage(self, text: str) -> tuple[str, tuple[int, int]]:
        """Extract last sub-stage name and (current, total) from log tail."""
        last_stage = ""
        progress = (0, 0)

        for line in text.splitlines():
            for pat in self.STAGE_PATTERNS:
                m = pat.search(line)
                if m:
                    groups = m.groups()
                    if len(groups) == 3:
                        # Step X/Y: description
                        progress = (int(groups[0]), int(groups[1]))
                        last_stage = groups[2].strip()
                    elif len(groups) == 2:
                        # Step X/Y (no description)
                        progress = (int(groups[0]), int(groups[1]))
                        if not last_stage:
                            last_stage = line.strip()
                    elif len(groups) == 1:
                        # Starting: xxx or percentage
                        candidate = groups[0].strip()
                        if candidate and not candidate.isdigit():
                            last_stage = candidate
        return last_stage, progress